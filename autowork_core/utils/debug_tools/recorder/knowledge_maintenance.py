from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path

from autowork_core.utils.debug_tools.recorder.knowledge_audit import (
    inspect_knowledge_store,
)
from autowork_core.utils.debug_tools.recorder.knowledge_store import (
    ai_root_for_recording_root,
    capability_store_lock,
    knowledge_root_for_recording_root,
)
from autowork_core.utils.debug_tools.recorder.models import SCHEMA_VERSION
from autowork_core.utils.debug_tools.recorder.writer import write_json_atomic


KNOWLEDGE_MAINTENANCE_VERSION = "1.0"
CAPABILITY_ID = re.compile(r"^capability-[a-z0-9][a-z0-9-]{2,79}$")
QUARANTINE_ID = re.compile(
    r"^knowledge-quarantine-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$"
)
def plan_capability_quarantine(recording_root, *, capability_ids):
    root = _safe_knowledge_root(recording_root)
    audit = inspect_knowledge_store(recording_root)
    selected = []
    for capability_id in _capability_ids(capability_ids):
        path = _capability_path(root, capability_id)
        if not path.is_file():
            raise FileNotFoundError(f"Capability 不存在: {capability_id}")
        findings = [
            item
            for item in audit.get("findings") or []
            if item.get("record_id") == capability_id
        ]
        invalid = [
            item for item in findings if item.get("severity") == "invalid"
        ]
        if not invalid:
            raise ValueError(
                f"只能隔离 Audit 已判定 invalid 的 Capability: {capability_id}"
            )
        selected.append({
            "capability_id": capability_id,
            "path": path.relative_to(root).as_posix(),
            "sha256": _sha256(path),
            "finding_codes": sorted({
                str(item.get("code")) for item in findings if item.get("code")
            }),
        })
    plan = {
        "knowledge_maintenance_version": KNOWLEDGE_MAINTENANCE_VERSION,
        "action": "quarantine_capabilities",
        "creates_quarantine": False,
        "requires_user_confirmation": True,
        "capabilities": selected,
    }
    plan["plan_fingerprint"] = _fingerprint(plan)
    return plan


def quarantine_capabilities(
        recording_root,
        *,
        capability_ids,
        plan_fingerprint,
        reason,
        user_confirmed=False,
        now=None,
):
    if user_confirmed is not True:
        raise PermissionError("隔离 Knowledge 需要当前任务中的用户明确确认")
    plan_fingerprint = str(plan_fingerprint or "").strip()
    if not plan_fingerprint:
        raise ValueError("隔离 Knowledge 需要已审阅的 plan_fingerprint")
    root = _safe_knowledge_root(recording_root)
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("quarantine reason 不能为空")
    with capability_store_lock(recording_root):
        plan = plan_capability_quarantine(
            recording_root,
            capability_ids=capability_ids,
        )
        if plan["plan_fingerprint"] != plan_fingerprint:
            raise ValueError(
                "Knowledge 隔离计划已变化，请重新审阅 plan-quarantine 输出"
            )
        current = _as_utc(now)
        quarantine_id = (
            f"knowledge-quarantine-{_utc_id(current)}"
            f"-{uuid.uuid4().hex[:8]}"
        )
        directory = _safe_directory(
            root,
            Path("quarantine") / quarantine_id,
            create=True,
            exist_ok=False,
        )
        capability_dir = _safe_directory(
            root,
            Path("quarantine") / quarantine_id / "capabilities",
            create=True,
            exist_ok=True,
        )
        catalog = _capability_directory(root) / "catalog.json"
        catalog_before = catalog.read_bytes() if catalog.is_file() else None
        receipt_path = directory / "receipt.json"
        receipt = {
            "knowledge_maintenance_version": KNOWLEDGE_MAINTENANCE_VERSION,
            "quarantine_id": quarantine_id,
            "status": "prepared",
            "created_at": _utc_timestamp(current),
            "reason": reason,
            "catalog_before_exists": catalog_before is not None,
            "catalog_before_sha256": (
                hashlib.sha256(catalog_before).hexdigest()
                if catalog_before is not None
                else None
            ),
            "capabilities": [
                {
                    **item,
                    "quarantine_path": (
                        capability_dir / Path(item["path"]).name
                    ).relative_to(root).as_posix(),
                }
                for item in plan["capabilities"]
            ],
        }
        write_json_atomic(receipt_path, receipt)
        if catalog_before is not None:
            _write_bytes_atomic(
                directory / "catalog-before.json",
                catalog_before,
            )
        moved = []
        try:
            for item in receipt["capabilities"]:
                source = _contained(root, root / item["path"])
                destination = _contained(
                    root,
                    root / item["quarantine_path"],
                )
                os.replace(source, destination)
                moved.append((source, destination))
                if _sha256(destination) != item["sha256"]:
                    raise ValueError(
                        f"隔离后 Capability hash 不一致: {item['capability_id']}"
                    )
            _write_capability_catalog(root)
            catalog_after = root / "capabilities" / "catalog.json"
            receipt["catalog_after_sha256"] = _sha256(catalog_after)
            receipt.update({
                "status": "completed",
                "completed_at": _utc_timestamp(_as_utc(None)),
            })
            write_json_atomic(receipt_path, receipt)
            post_audit = inspect_knowledge_store(recording_root)
            capability_errors = _active_capability_errors(post_audit, root)
            if capability_errors:
                raise ValueError(
                    "隔离后 active Capability store 仍无效: "
                    f"{capability_errors}"
                )
            receipt["post_audit_status"] = post_audit.get("status")
            write_json_atomic(receipt_path, receipt)
            return {
                **receipt,
                "receipt_path": receipt_path.relative_to(root).as_posix(),
                "post_audit": post_audit,
            }
        except Exception:
            for source, destination in reversed(moved):
                if destination.exists() and not source.exists():
                    source.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(destination, source)
            _restore_catalog(catalog, catalog_before)
            receipt["status"] = "rolled_back"
            write_json_atomic(receipt_path, receipt)
            raise


def rebuild_capability_catalog(
        recording_root,
        *,
        user_confirmed=False,
):
    if user_confirmed is not True:
        raise PermissionError("重建 Capability catalog 需要用户明确确认")
    root = _safe_knowledge_root(recording_root)
    with capability_store_lock(recording_root):
        path = _capability_directory(root) / "catalog.json"
        before = path.read_bytes() if path.is_file() else None
        try:
            _write_capability_catalog(root)
            report = inspect_knowledge_store(recording_root)
            errors = _active_capability_errors(report, root)
            if errors:
                raise ValueError(
                    f"Capability catalog 重建后仍无效: {errors}"
                )
            return {
                "knowledge_maintenance_version": KNOWLEDGE_MAINTENANCE_VERSION,
                "status": "completed",
                "catalog_path": path.relative_to(root).as_posix(),
                "audit_status": report.get("status"),
            }
        except Exception:
            _restore_catalog(path, before)
            raise


def restore_quarantine(
        recording_root,
        *,
        quarantine_id,
        user_confirmed=False,
):
    if user_confirmed is not True:
        raise PermissionError("恢复 Knowledge 隔离项需要用户明确确认")
    if QUARANTINE_ID.fullmatch(str(quarantine_id or "")) is None:
        raise ValueError(f"无效 quarantine_id: {quarantine_id}")
    root = _safe_knowledge_root(recording_root)
    directory = _safe_directory(
        root,
        Path("quarantine") / quarantine_id,
        create=False,
    )
    receipt_path = directory / "receipt.json"
    with capability_store_lock(recording_root):
        receipt = _read_object(receipt_path)
        entries = _validate_quarantine_receipt(
            root,
            directory,
            receipt,
            expected_status="completed",
        )
        catalog = _capability_directory(root) / "catalog.json"
        expected_catalog_hash = receipt.get("catalog_after_sha256")
        catalog_matches = (
            bool(expected_catalog_hash)
            and catalog.is_file()
            and _sha256(catalog) == expected_catalog_hash
        )
        if not catalog_matches:
            raise ValueError(
                "隔离后 Capability catalog 已变化，拒绝覆盖后续知识更新"
            )
        moves = []
        for item in entries:
            source = item["quarantined"]
            destination = item["active"]
            if destination.exists():
                raise FileExistsError(f"Capability 已存在: {destination.name}")
            if not source.is_file() or _sha256(source) != item["sha256"]:
                raise ValueError(f"隔离 Capability hash 无效: {source}")
            moves.append((source, destination))
        completed = []
        catalog_current = catalog.read_bytes() if catalog.is_file() else None
        try:
            for source, destination in moves:
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, destination)
                completed.append((source, destination))
            catalog_before = directory / "catalog-before.json"
            expected_snapshot_hash = receipt.get("catalog_before_sha256")
            if catalog_before.is_file() and expected_snapshot_hash is not None:
                if _sha256(catalog_before) != expected_snapshot_hash:
                    raise ValueError("原 Capability catalog 快照 SHA-256 无效")
            _restore_catalog(
                catalog,
                catalog_before.read_bytes() if catalog_before.is_file() else None,
            )
            receipt.update({
                "status": "restored",
                "restored_at": _utc_timestamp(_as_utc(None)),
            })
            write_json_atomic(receipt_path, receipt)
        except Exception:
            _restore_catalog(catalog, catalog_current)
            for source, destination in reversed(completed):
                if destination.exists() and not source.exists():
                    source.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(destination, source)
            raise
        return {
            "knowledge_maintenance_version": KNOWLEDGE_MAINTENANCE_VERSION,
            "quarantine_id": quarantine_id,
            "status": "restored",
            "restored_capability_ids": [
                item.get("capability_id")
                for item in receipt.get("capabilities") or []
            ],
            "audit": inspect_knowledge_store(recording_root),
        }


def recover_prepared_quarantine(
        recording_root,
        *,
        quarantine_id,
        user_confirmed=False,
):
    if user_confirmed is not True:
        raise PermissionError("恢复中断的 Knowledge 隔离需要用户明确确认")
    if QUARANTINE_ID.fullmatch(str(quarantine_id or "")) is None:
        raise ValueError(f"无效 quarantine_id: {quarantine_id}")
    root = _safe_knowledge_root(recording_root)
    directory = _safe_directory(
        root,
        Path("quarantine") / quarantine_id,
        create=False,
    )
    receipt_path = directory / "receipt.json"
    with capability_store_lock(recording_root):
        receipt = _read_object(receipt_path)
        entries = _validate_quarantine_receipt(
            root,
            directory,
            receipt,
            expected_status="prepared",
        )
        items = receipt["capabilities"]
        moves = []
        for item in entries:
            active = item["active"]
            quarantined = item["quarantined"]
            expected_hash = item["sha256"]
            active_valid = active.is_file() and _sha256(active) == expected_hash
            quarantine_valid = (
                quarantined.is_file()
                and _sha256(quarantined) == expected_hash
            )
            if active_valid and not quarantined.exists():
                continue
            if quarantine_valid and not active.exists():
                moves.append((quarantined, active))
                continue
            raise ValueError(
                "Prepared Quarantine 文件缺失、重复或 SHA-256 不一致: "
                f"{item['capability_id']}"
            )
        catalog = _capability_directory(root) / "catalog.json"
        catalog_current = catalog.read_bytes() if catalog.is_file() else None
        completed = []
        try:
            for source, destination in moves:
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, destination)
                completed.append((source, destination))
            _write_capability_catalog(root)
            report = inspect_knowledge_store(recording_root)
            active_errors = _active_capability_errors(report, root)
            expected_active_errors = {
                code
                for item in items
                for code in item.get("finding_codes") or []
                if code.startswith(("capability_", "knowledge_"))
            }
            unexpected = sorted(set(active_errors) - expected_active_errors)
            if unexpected:
                raise ValueError(
                    "Prepared Quarantine 回滚后出现新的 Capability 错误: "
                    f"{unexpected}"
                )
            receipt.update({
                "status": "rolled_back",
                "recovered_at": _utc_timestamp(_as_utc(None)),
                "recovery": "explicit_prepared_rollback",
            })
            write_json_atomic(receipt_path, receipt)
        except Exception:
            _restore_catalog(catalog, catalog_current)
            for source, destination in reversed(completed):
                if destination.exists() and not source.exists():
                    source.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(destination, source)
            raise
        return {
            "knowledge_maintenance_version": KNOWLEDGE_MAINTENANCE_VERSION,
            "quarantine_id": quarantine_id,
            "status": "rolled_back",
            "restored_capability_ids": [
                item.get("capability_id") for item in items
            ],
            "audit": inspect_knowledge_store(recording_root),
        }


def _write_capability_catalog(root):
    directory = _capability_directory(root)
    entries = []
    for path in sorted(directory.glob("capability-*.json")):
        if _is_link_or_reparse(path):
            raise ValueError(f"Capability 文件不能是链接或 reparse point: {path.name}")
        value = _read_object(path)
        capability_id = str(value.get("capability_id") or "")
        if path.name != f"{capability_id}.json":
            raise ValueError(f"Capability id 与文件名不一致: {path.name}")
        entries.append({
            "capability_id": capability_id,
            "path": path.relative_to(root).as_posix(),
            "published_at": value.get("published_at"),
            "status": value.get("status"),
            "feature": value.get("feature"),
            "scenario": value.get("scenario"),
            "step": value.get("step"),
            "source": value.get("source"),
        })
    entries.sort(
        key=lambda item: (
            item.get("published_at") or "",
            item.get("capability_id") or "",
        ),
        reverse=True,
    )
    path = directory / "catalog.json"
    write_json_atomic(path, {
        "schema_version": SCHEMA_VERSION,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "capabilities": entries,
        "derived": True,
    })
    return path


def _active_capability_errors(report, root):
    directory = _capability_directory(root)
    result = []
    for item in report.get("findings") or []:
        if item.get("severity") != "invalid":
            continue
        path = Path(str(item.get("path") or ""))
        path = (
            path.resolve()
            if path.is_absolute()
            else (root / path).resolve()
        )
        if path == directory / "catalog.json" or directory in path.parents:
            result.append(str(item.get("code")))
    return sorted(set(result))


def _restore_catalog(path, content):
    path = Path(path)
    if content is None:
        path.unlink(missing_ok=True)
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.restore")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def _write_bytes_atomic(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def _capability_ids(values):
    result = list(dict.fromkeys(str(value or "").strip() for value in values or []))
    if not result:
        raise ValueError("至少指定一个 capability_id")
    invalid = [value for value in result if CAPABILITY_ID.fullmatch(value) is None]
    if invalid:
        raise ValueError(f"无效 capability_id: {invalid}")
    return result


def _capability_path(root, capability_id):
    capability_id = str(capability_id or "")
    if CAPABILITY_ID.fullmatch(capability_id) is None:
        raise ValueError(f"无效 capability_id: {capability_id}")
    return _contained(
        root,
        _capability_directory(root) / f"{capability_id}.json",
    )


def _validate_quarantine_receipt(
        root,
        directory,
        receipt,
        *,
        expected_status,
):
    if any((
        receipt.get("knowledge_maintenance_version")
        != KNOWLEDGE_MAINTENANCE_VERSION,
        receipt.get("quarantine_id") != directory.name,
        receipt.get("status") != expected_status,
    )):
        raise ValueError("Quarantine receipt version、id 或 status 无效")
    items = receipt.get("capabilities")
    if not isinstance(items, list) or not items:
        raise ValueError("Quarantine receipt capabilities 必须是非空 array")
    capability_dir = _safe_directory(
        root,
        Path("quarantine") / directory.name / "capabilities",
        create=False,
    )
    entries = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Quarantine receipt capability entry 无效")
        capability_id = str(item.get("capability_id") or "")
        expected_hash = str(item.get("sha256") or "")
        if (
            CAPABILITY_ID.fullmatch(capability_id) is None
            or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None
            or capability_id in seen
        ):
            raise ValueError("Quarantine receipt capability identity 或 SHA-256 无效")
        seen.add(capability_id)
        expected_active = _capability_path(root, capability_id)
        expected_quarantined = capability_dir / f"{capability_id}.json"
        declared_active = _contained(
            root,
            root / str(item.get("path") or ""),
        )
        declared_quarantined = _contained(
            root,
            root / str(item.get("quarantine_path") or ""),
        )
        if (
            declared_active != expected_active
            or declared_quarantined != expected_quarantined
        ):
            raise ValueError("Quarantine receipt capability path 无效")
        for path in (declared_active, declared_quarantined):
            if (path.exists() or path.is_symlink()) and _is_link_or_reparse(path):
                raise ValueError("Quarantine capability 不能是链接或 reparse point")
        entries.append({
            "capability_id": capability_id,
            "sha256": expected_hash,
            "active": declared_active,
            "quarantined": declared_quarantined,
        })
    snapshot = directory / "catalog-before.json"
    before_exists = receipt.get("catalog_before_exists")
    before_hash = receipt.get("catalog_before_sha256")
    if before_exists is True:
        if not snapshot.is_file() or _is_link_or_reparse(snapshot):
            raise ValueError("Quarantine 原 catalog 快照缺失或为链接")
        if (
            re.fullmatch(r"[0-9a-f]{64}", str(before_hash or "")) is None
            or _sha256(snapshot) != before_hash
        ):
            raise ValueError("Quarantine 原 catalog 快照 SHA-256 无效")
    elif before_exists is False:
        if snapshot.exists() or before_hash not in {None, ""}:
            raise ValueError("Quarantine 原 catalog 快照声明不一致")
    else:
        raise ValueError("Quarantine catalog_before_exists 必须是 boolean")
    return entries


def _safe_knowledge_root(recording_root):
    logical_ai = Path(os.path.abspath(ai_root_for_recording_root(recording_root)))
    logical_root = Path(os.path.abspath(
        knowledge_root_for_recording_root(recording_root)
    ))
    for path in (logical_ai, logical_root):
        if (path.exists() or path.is_symlink()) and _is_link_or_reparse(path):
            raise ValueError(
                f"Knowledge maintenance 拒绝链接或 reparse point: {path.name}"
            )
    root = logical_root.resolve()
    if root.parent != logical_ai.resolve():
        raise ValueError("Knowledge root 必须直接位于 ai 目录")
    return root


def _capability_directory(root):
    return _safe_directory(
        root,
        Path("capabilities"),
        create=True,
        exist_ok=True,
    )


def _safe_directory(root, relative, *, create, exist_ok=True):
    root = Path(root).resolve()
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"Knowledge directory 路径无效: {relative}")
    current = root
    for part in relative.parts:
        current = current / part
        if (current.exists() or current.is_symlink()) and _is_link_or_reparse(
            current
        ):
            raise ValueError(
                f"Knowledge directory 不能是链接或 reparse point: {relative}"
            )
    candidate = _contained(root, root / relative)
    if create:
        candidate.mkdir(parents=True, exist_ok=exist_ok)
    if not candidate.is_dir():
        raise FileNotFoundError(f"Knowledge directory 不存在: {relative}")
    for path in (candidate, *candidate.parents):
        if path == root.parent:
            break
        if path == root:
            continue
        if _is_link_or_reparse(path):
            raise ValueError(
                f"Knowledge directory 不能是链接或 reparse point: {relative}"
            )
    return candidate.resolve()


def _is_link_or_reparse(path):
    path = Path(path)
    if path.is_symlink():
        return True
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _contained(root, path):
    root = Path(root).resolve()
    path = Path(path).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError(f"Knowledge maintenance 路径越界: {path}") from error
    return path


def _read_object(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 顶层必须是 object: {path}")
    return value


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _fingerprint(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _as_utc(value):
    current = value or datetime.now(timezone.utc)
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise ValueError("now 必须包含 timezone")
    return current.astimezone(timezone.utc)


def _utc_id(value):
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _utc_timestamp(value):
    return value.astimezone(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Conservatively maintain Recorder durable knowledge",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan-quarantine")
    plan.add_argument("capability_id", nargs="+")
    quarantine = commands.add_parser("quarantine")
    quarantine.add_argument("capability_id", nargs="+")
    quarantine.add_argument("--plan-fingerprint", required=True)
    quarantine.add_argument("--reason", required=True)
    quarantine.add_argument("--user-confirmed", action="store_true")
    rebuild = commands.add_parser("rebuild-catalog")
    rebuild.add_argument("--user-confirmed", action="store_true")
    restore = commands.add_parser("restore")
    restore.add_argument("quarantine_id")
    restore.add_argument("--user-confirmed", action="store_true")
    recover = commands.add_parser("recover-prepared")
    recover.add_argument("quarantine_id")
    recover.add_argument("--user-confirmed", action="store_true")
    for command in (plan, quarantine, rebuild, restore, recover):
        command.add_argument("--recording-root", default=".")
    args = parser.parse_args(argv)
    if args.command == "plan-quarantine":
        result = plan_capability_quarantine(
            args.recording_root,
            capability_ids=args.capability_id,
        )
    elif args.command == "quarantine":
        result = quarantine_capabilities(
            args.recording_root,
            capability_ids=args.capability_id,
            plan_fingerprint=args.plan_fingerprint,
            reason=args.reason,
            user_confirmed=args.user_confirmed,
        )
    elif args.command == "rebuild-catalog":
        result = rebuild_capability_catalog(
            args.recording_root,
            user_confirmed=args.user_confirmed,
        )
    elif args.command == "restore":
        result = restore_quarantine(
            args.recording_root,
            quarantine_id=args.quarantine_id,
            user_confirmed=args.user_confirmed,
        )
    else:
        result = recover_prepared_quarantine(
            args.recording_root,
            quarantine_id=args.quarantine_id,
            user_confirmed=args.user_confirmed,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())