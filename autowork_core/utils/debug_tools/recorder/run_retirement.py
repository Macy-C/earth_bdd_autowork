from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from autowork_core.utils.debug_tools.recorder.capability import (
    load_capability_catalog,
)
from autowork_core.utils.debug_tools.recorder.catalog import (
    load_recording_catalog,
    remove_recording_catalog_entry,
    restore_recording_catalog_entry,
)
from autowork_core.utils.debug_tools.recorder.knowledge_store import (
    ensure_knowledge_store,
)
from autowork_core.utils.debug_tools.recorder.project_memory import (
    load_memory_events,
)
from autowork_core.utils.debug_tools.recorder.run_lock import active_run_lock
from autowork_core.utils.debug_tools.recorder.writer import write_json_atomic


RETIREMENT_VERSION = "1.0"


class RunRetirementError(RuntimeError):
    pass


class RunKnowledgeRequiredError(RunRetirementError):
    pass


def inspect_run_retirement(session_dir):
    session_dir = Path(session_dir).resolve()
    output_root = _find_recording_root(session_dir)
    manifest_path = session_dir / "manifest.json"
    manifest = _read_json(manifest_path)
    session_id = str(manifest.get("session_id") or "")
    if not session_id:
        raise RunRetirementError("Run manifest 缺少 session_id")
    relative_path = session_dir.relative_to(output_root).as_posix()
    catalog = load_recording_catalog(output_root)
    catalog_entry = next(
        (
            entry
            for entry in catalog.get("sessions") or ()
            if entry.get("session_id") == session_id
        ),
        None,
    )
    if catalog_entry is None or catalog_entry.get("path") != relative_path:
        raise RunRetirementError("Run 与 recording catalog 不一致")

    blockers = []
    status = str(manifest.get("status") or "")
    if status not in {"closed", "finalized"}:
        blockers.append(f"Run 尚未关闭: status={status or 'unknown'}")
    lock_owner = active_run_lock(session_dir)
    if lock_owner:
        blockers.append(f"Run 正被写入: pid={lock_owner.get('pid')}")
    running_transactions = _running_transactions(session_dir)
    if running_transactions:
        blockers.append(
            "仍有 running generation transaction: "
            + ", ".join(running_transactions)
        )

    events, memory_warnings = load_memory_events(output_root)
    memory_ids = [
        event.get("memory_id")
        for event in events
        if _event_belongs_to_session(event, session_id, relative_path)
        and _durable_event(event)
    ]
    capabilities = [
        entry
        for entry in (
            load_capability_catalog(output_root).get("capabilities") or ()
        )
        if (entry.get("source") or {}).get("session_id") == session_id
        and entry.get("status") == "confirmed"
    ]
    return {
        "retirement_version": RETIREMENT_VERSION,
        "eligible": not blockers,
        "session_id": session_id,
        "session_path": relative_path,
        "manifest_status": status,
        "blockers": blockers,
        "knowledge": {
            "durable": bool(memory_ids or capabilities),
            "memory_ids": [value for value in memory_ids if value],
            "capability_ids": [
                entry.get("capability_id")
                for entry in capabilities
                if entry.get("capability_id")
            ],
            "warnings": memory_warnings,
        },
        "manifest_sha256": _sha256(manifest_path),
    }


def retire_recording_session(
        session_dir,
        *,
        require_distilled_knowledge=True,
        reason="",
):
    session_dir = Path(session_dir).resolve()
    inspection = inspect_run_retirement(session_dir)
    if inspection["blockers"]:
        raise RunRetirementError("; ".join(inspection["blockers"]))
    if (
        require_distilled_knowledge
        and not inspection["knowledge"]["durable"]
    ):
        raise RunKnowledgeRequiredError(
            "Run 尚无可脱离录屏保存的确认经验或成功生成结果"
        )

    output_root = _find_recording_root(session_dir)
    knowledge_root = ensure_knowledge_store(output_root)
    retirement_id = "retirement-" + uuid.uuid4().hex
    receipt_path = knowledge_root / "retirements" / f"{retirement_id}.json"
    receipt = {
        **inspection,
        "retirement_id": retirement_id,
        "status": "prepared",
        "prepared_at": datetime.now().isoformat(timespec="seconds"),
        "mode": (
            "distilled"
            if inspection["knowledge"]["durable"]
            else "discarded_without_knowledge"
        ),
        "reason": str(reason or "").strip(),
    }
    write_json_atomic(receipt_path, receipt)

    staging_root = output_root / ".retiring"
    staging_root.mkdir(parents=True, exist_ok=True)
    staged = staging_root / retirement_id
    catalog_entry = None
    try:
        session_dir.rename(staged)
        try:
            catalog_entry = remove_recording_catalog_entry(
                output_root,
                inspection["session_id"],
            )
        except Exception:
            staged.rename(session_dir)
            raise
        try:
            shutil.rmtree(staged)
        except Exception as error:
            receipt.update({
                "status": "cleanup_pending",
                "cleanup_path": str(staged),
                "error": f"{type(error).__name__}: {error}",
            })
            write_json_atomic(receipt_path, receipt)
            return {**receipt, "receipt_path": str(receipt_path)}
    except Exception as error:
        if catalog_entry is not None and session_dir.exists():
            restore_recording_catalog_entry(output_root, catalog_entry)
        receipt.update({
            "status": "failed",
            "error": f"{type(error).__name__}: {error}",
        })
        write_json_atomic(receipt_path, receipt)
        raise RunRetirementError(receipt["error"]) from error
    finally:
        if staging_root.exists() and not any(staging_root.iterdir()):
            staging_root.rmdir()

    _remove_empty_session_parents(session_dir.parent, output_root)
    receipt.update({
        "status": "completed",
        "retired_at": datetime.now().isoformat(timespec="seconds"),
    })
    write_json_atomic(receipt_path, receipt)
    return {**receipt, "receipt_path": str(receipt_path)}


def _running_transactions(session_dir):
    result = []
    root = session_dir / "ai" / "generation-transactions"
    for path in root.glob("transaction-*/report.json"):
        try:
            report = _read_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if report.get("status") == "running":
            result.append(str(report.get("transaction_id") or path.parent.name))
    workflow_root = session_dir / "ai" / "workflow"
    for path in workflow_root.glob("*.json"):
        try:
            state = _read_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if state.get("status") == "running":
            value = str(state.get("request_id") or path.stem)
            if value not in result:
                result.append(value)
    return sorted(result)


def _event_belongs_to_session(event, session_id, session_path):
    source = event.get("source") or {}
    return (
        source.get("session_id") == session_id
        or source.get("session_path") == session_path
    )


def _durable_event(event):
    return (
        event.get("authority") in {"user_confirmed", "generation_result"}
        and event.get("kind") in {
            "plan_confirmed",
            "transaction_completed",
            "transaction_feedback",
        }
    )


def _find_recording_root(session_dir):
    for candidate in session_dir.parents:
        if (candidate / "catalog.json").is_file():
            return candidate
    raise RunRetirementError(
        f"无法从 Run 定位 recording_sessions 根目录: {session_dir}"
    )


def _remove_empty_session_parents(path, output_root):
    path = Path(path)
    output_root = Path(output_root).resolve()
    while path != output_root:
        try:
            path.rmdir()
        except OSError:
            return
        path = path.parent


def _read_json(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 必须是 object: {path}")
    return value


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()