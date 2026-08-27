from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path

from config.paths import Paths
from autowork_core.utils.debug_tools.recorder.knowledge_store import (
    ensure_knowledge_store,
    knowledge_root_for_recording_root,
)
from autowork_core.utils.debug_tools.recorder.memory_digest import (
    build_relevant_memory_revision,
    is_relevant_memory_revision,
)
from autowork_core.utils.debug_tools.recorder.models import SCHEMA_VERSION
from autowork_core.utils.debug_tools.recorder.writer import write_json_atomic


MEMORY_EVENT_VERSION = "1.0"
MEMORY_CONTEXT_VERSION = "1.0"
FEEDBACK_STATUSES = {"accepted", "revised", "rejected"}
AUTHORITIES = {
    "user_confirmed",
    "code_verified",
    "generation_result",
    "historical_case",
    "ai_inferred",
}

_MEMORY_LOCKS = {}
_MEMORY_LOCKS_GUARD = threading.Lock()


def append_memory_event(
        output_root,
        *,
        kind,
        authority,
        status,
        claim,
        scope=None,
        source=None,
        payload=None,
        supersedes=None,
        dedupe_key=None,
):
    if authority not in AUTHORITIES:
        raise ValueError(f"不支持的记忆来源等级: {authority}")
    output_root = Path(output_root).resolve()
    knowledge_root = ensure_knowledge_store(output_root)
    memory_dir = knowledge_root / "project-memory"
    event_path = memory_dir / "events.jsonl"
    event = {
        "schema_version": SCHEMA_VERSION,
        "memory_event_version": MEMORY_EVENT_VERSION,
        "memory_id": f"memory-{uuid.uuid4().hex}",
        "created_at": datetime.now().isoformat(timespec="milliseconds"),
        "kind": str(kind),
        "authority": authority,
        "status": str(status),
        "claim": str(claim or "").strip(),
        "scope": dict(scope or {}),
        "source": dict(source or {}),
        "payload": dict(payload or {}),
        "supersedes": list(supersedes or ()),
        "dedupe_key": str(dedupe_key) if dedupe_key else None,
    }
    memory_dir.mkdir(parents=True, exist_ok=True)
    with _memory_lock(knowledge_root):
        if dedupe_key:
            existing, _warnings = load_memory_events(output_root)
            match = next(
                (
                    item
                    for item in reversed(existing)
                    if item.get("dedupe_key") == str(dedupe_key)
                ),
                None,
            )
            if match is not None:
                return match
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        with event_path.open("a", encoding="utf-8") as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
    return event


def load_memory_events(output_root):
    output_root = Path(output_root).resolve()
    knowledge_root = knowledge_root_for_recording_root(output_root)
    path = knowledge_root / "project-memory" / "events.jsonl"
    if not path.exists():
        return [], []
    events = []
    warnings = []
    with _memory_lock(knowledge_root):
        lines = path.read_text(encoding="utf-8").splitlines()
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except Exception as error:
            warnings.append(
                f"记忆日志第 {line_number} 行无法解析: {type(error).__name__}: {error}"
            )
            continue
        if isinstance(value, dict) and value.get("memory_id"):
            events.append(value)
        else:
            warnings.append(f"记忆日志第 {line_number} 行不是有效事件")
    return events, warnings


def build_request_memory_context(
        session_dir,
        request,
        *,
        limit=16,
    exclude_request_id=None,
    exclude_kinds=(),
):
    session_dir = Path(session_dir).resolve()
    output_root = find_recording_root(session_dir)
    events, warnings = load_memory_events(output_root)
    if exclude_request_id:
        excluded_kinds = set(exclude_kinds)
        events = [
            event
            for event in events
            if not (
                event.get("kind") in excluded_kinds
                and (event.get("source") or {}).get("request_id")
                == exclude_request_id
            )
        ]
    effective_events = _effective_events(events)
    query = _request_scope(request)
    scored = []
    for event in effective_events:
        score, reasons = _relevance(event, query)
        if score > 0:
            scored.append((score, event.get("created_at") or "", reasons, event))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    selected = scored[:max(1, int(limit))]
    buckets = {
        "confirmed_knowledge": [],
        "accepted_outcomes": [],
        "past_corrections": [],
        "provisional_insights": [],
        "generation_history": [],
        "other_relevant_experiences": [],
    }
    for score, _created_at, reasons, event in selected:
        item = {
            **event,
            "relevance": {
                "score": score,
                "reasons": reasons,
            },
        }
        buckets[_memory_bucket(event)].append(item)

    recent_confirmed = [
        event
        for event in reversed(effective_events)
        if event.get("authority") == "user_confirmed"
        and event.get("memory_id") not in {
            item[3].get("memory_id") for item in selected
        }
    ][:3]
    knowledge_root = knowledge_root_for_recording_root(output_root)
    journal_path = knowledge_root / "project-memory" / "events.jsonl"
    context = {
        "schema_version": SCHEMA_VERSION,
        "memory_context_version": MEMORY_CONTEXT_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "request_id": request.get("request_id"),
        "policy": {
            "purpose": "Provide relevant project experience to AI; never decide code ownership.",
            "precedence": [
                "current explicit user instruction",
                "current code and current Recorder evidence",
                "user-confirmed project memory",
                "accepted historical outcomes",
                "historical generation results",
                "provisional AI insights",
            ],
            "rules": [
                "Memory is advisory and must not override current code or evidence.",
                "Explain why each used memory applies to the current target.",
                "Treat rejected outcomes as negative examples, not reusable solutions.",
                "Treat ai_inferred memories as provisional until confirmed by the user.",
                "Surface conflicts instead of silently choosing one memory.",
            ],
        },
        "query": query,
        "journal": {
            "path": str(journal_path),
            "recording_root": str(output_root),
            "event_count": len(events),
            "revision": None,
        },
        "relevant_count": len(selected),
        **buckets,
        "recent_confirmed_context": recent_confirmed,
        "warnings": warnings,
    }
    context["journal"]["revision"] = build_relevant_memory_revision(
        context,
    )
    return context


def write_request_memory_context(
        session_dir,
        request,
        *,
        limit=16,
        context=None,
):
    session_dir = Path(session_dir).resolve()
    context = json.loads(json.dumps(
        context
        if isinstance(context, dict)
        else build_request_memory_context(
            session_dir,
            request,
            limit=limit,
        ),
        ensure_ascii=False,
    ))
    context["request_id"] = request.get("request_id")
    output = (
        session_dir
        / "ai"
        / "memory-context"
        / f"{request.get('request_id') or 'request'}.json"
    )
    write_json_atomic(output, context)
    return output, context


def inspect_request_memory_freshness(
    session_dir,
    request,
    *,
    include_current_results=False,
):
    declared = (request.get("memory_context") or {}).get("revision")
    if not is_relevant_memory_revision(declared):
        return {"status": "compatible", "declared": declared}
    if not (request.get("memory_context") or {}).get("available", False):
        return {
            "status": "unavailable",
            "declared": declared,
            "message": "Request 声明了相关经验 revision，但上下文不可用",
        }
    try:
        context = build_request_memory_context(
            session_dir,
            request,
            exclude_request_id=request.get("request_id"),
            exclude_kinds=(
                ("plan_confirmed",)
                if include_current_results
                else ("plan_confirmed", "transaction_completed")
            ),
        )
    except Exception as error:
        return {
            "status": "unavailable",
            "declared": declared,
            "message": (
                "项目经验新鲜度无法读取: "
                f"{type(error).__name__}: {error}"
            ),
        }
    current = (context.get("journal") or {}).get("revision")
    return {
        "status": "matched" if current == declared else "stale",
        "declared": declared,
        "current": current,
        "warnings": context.get("warnings") or [],
    }


def request_memory_matches_current(
        session_dir,
        request,
        *,
        include_current_results=False,
):
    return inspect_request_memory_freshness(
        session_dir,
        request,
        include_current_results=include_current_results,
    )["status"] in {"compatible", "matched"}


def memory_revision_for_session(session_dir):
    output_root = find_recording_root(session_dir)
    return _file_hash(
        ensure_knowledge_store(output_root)
        / "project-memory"
        / "events.jsonl"
    )


def search_memory_events(output_root, query, *, limit=20):
    output_root = Path(output_root).resolve()
    events, warnings = load_memory_events(output_root)
    effective_events = _effective_events(events)
    query_tokens = _tokens(query)
    matches = []
    for event in effective_events:
        event_tokens = _tokens({
            "claim": event.get("claim"),
            "scope": event.get("scope"),
            "payload": event.get("payload"),
        })
        common = event_tokens & query_tokens
        if not common:
            continue
        authority_bonus = {
            "user_confirmed": 30,
            "code_verified": 25,
            "generation_result": 15,
            "historical_case": 10,
            "ai_inferred": 0,
        }.get(event.get("authority"), 0)
        score = min(100, len(common) * 8 + authority_bonus)
        matches.append((score, event.get("created_at") or "", common, event))
    matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return {
        "query": str(query),
        "matches": [
            {
                **event,
                "relevance": {
                    "score": score,
                    "shared_terms": sorted(common)[:12],
                },
            }
            for score, _created_at, common, event in matches[:max(1, int(limit))]
        ],
        "warnings": warnings,
    }


def record_transaction_completed(report_path, report):
    report_path = Path(report_path).resolve()
    session_dir = Path(report["session_dir"]).resolve()
    output_root = find_recording_root(session_dir)
    recording_reference = _recording_reference(
        output_root,
        session_dir,
        report_path,
    )
    unresolved_issues = list(report.get("unresolved_issues") or ())
    event = append_memory_event(
        output_root,
        kind=(
            "transaction_generated_with_issues"
            if unresolved_issues
            else "transaction_completed"
        ),
        authority="generation_result",
        status=report.get("status") or "unknown",
        claim=report.get("summary") or (
            f"Transaction {report.get('transaction_id')} finished with "
            f"status {report.get('status')}."
        ),
        scope=_target_scope(report.get("target") or {}),
        source={
            "request_id": report.get("request_id"),
            "transaction_id": report.get("transaction_id"),
            **recording_reference,
        },
        payload={
            "changed_files": report.get("changed_files") or [],
            "validations": report.get("validations") or {},
            "required_validations": report.get("required_validations") or [],
            "implementation_snapshot": report.get("implementation_snapshot") or [],
            "decision_trace": report.get("decision_trace") or {},
            "unresolved_issues": unresolved_issues,
        },
        dedupe_key=f"transaction:{report.get('transaction_id')}",
    )
    insight_events = []
    for insight in (report.get("decision_trace") or {}).get("insights") or []:
        insight = str(insight or "").strip()
        if not insight:
            continue
        insight_events.append(append_memory_event(
            output_root,
            kind="ai_insight",
            authority="ai_inferred",
            status="provisional",
            claim=insight,
            scope=_target_scope(report.get("target") or {}),
            source={
                "request_id": report.get("request_id"),
                "transaction_id": report.get("transaction_id"),
                **recording_reference,
            },
            payload={
                "memories_used": (
                    report.get("decision_trace") or {}
                ).get("memories_used") or [],
                "memories_rejected": (
                    report.get("decision_trace") or {}
                ).get("memories_rejected") or [],
            },
            dedupe_key=(
                f"transaction-insight:{report.get('transaction_id')}:"
                f"{hashlib.sha256(insight.encode('utf-8')).hexdigest()}"
            ),
        ))
    return [event, *insight_events]


def record_transaction_feedback(report_path, status, note=""):
    status = str(status).strip().lower()
    if status not in FEEDBACK_STATUSES:
        raise ValueError(f"不支持的生成反馈: {status}")
    report_path = Path(report_path).resolve()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("status") not in {"completed", "completed_no_changes"}:
        raise ValueError(
            f"只有已完成的生成报告可以反馈: status={report.get('status')}"
        )
    session_dir = Path(report["session_dir"]).resolve()
    _validate_transaction_report_path(report_path, session_dir, report)
    project_root = Path(
        report.get("project_root") or Paths.BASE_DIR
    ).resolve()
    output_root = find_recording_root(session_dir)
    previous_events, _warnings = load_memory_events(output_root)
    previous_feedback = [
        event
        for event in previous_events
        if event.get("kind") == "transaction_feedback"
        and (
            (event.get("source") or {}).get("transaction_id")
            == report.get("transaction_id")
        )
    ]
    current_snapshot = snapshot_files(
        report.get("changed_files") or [],
        project_root=project_root,
    )
    before = {
        item.get("path"): item.get("sha256")
        for item in report.get("implementation_snapshot") or []
    }
    modified = [
        item.get("path")
        for item in current_snapshot
        if before.get(item.get("path")) != item.get("sha256")
    ]
    capability_candidates = []
    capability_source_error = None
    capability_session_id = None
    runtime_verification = "not_run"
    if status == "accepted" and not modified:
        try:
            from autowork_core.utils.debug_tools.recorder.capability import (
                validate_accepted_transaction_capability_source,
            )

            _validated_session, validated_request, validated_plan, _runtime = (
                validate_accepted_transaction_capability_source(
                    report_path,
                    report,
                    project_root=project_root,
                )
            )
            runtime_verification = str(_runtime or "not_run")
            if runtime_verification in {"passed", "oracle_verified"}:
                capability_candidates = _feedback_capability_candidates(
                    validated_request,
                    validated_plan,
                )
            capability_session_id = str(
                (validated_request.get("session") or {}).get("id") or ""
            ) or None
        except Exception as error:
            capability_source_error = error
    feedback_tier = _accepted_feedback_tier(
        status,
        modified=modified,
        capability_source_error=capability_source_error,
        runtime_verification=runtime_verification,
    )
    default_claims = {
        "accepted": "User accepted the generated implementation.",
        "revised": "User revised the generated implementation.",
        "rejected": "User rejected the generated implementation.",
    }
    event = append_memory_event(
        output_root,
        kind="transaction_feedback",
        authority="user_confirmed",
        status=status,
        claim=str(note or "").strip() or default_claims[status],
        scope=_target_scope(report.get("target") or {}),
        source={
            "request_id": report.get("request_id"),
            "transaction_id": report.get("transaction_id"),
            "session_id": capability_session_id,
            **_recording_reference(
                output_root,
                session_dir,
                report_path,
            ),
        },
        payload={
            "transaction_status": report.get("status"),
            "changed_files": report.get("changed_files") or [],
            "current_snapshot": current_snapshot,
            "modified_since_generation": modified,
            "accepted_feedback_tier": feedback_tier,
            "runtime_verification": runtime_verification,
            "capability_source_validated": (
                status == "accepted"
                and not modified
                and capability_source_error is None
                and runtime_verification in {"passed", "oracle_verified"}
            ),
            "capability_candidates": capability_candidates,
        },
        supersedes=(
            [previous_feedback[-1]["memory_id"]]
            if previous_feedback
            else []
        ),
    )
    result = dict(event)
    result["capability_paths"] = []
    result["capability_warnings"] = []
    if status == "accepted":
        if modified:
            result["capability_warnings"].append(
                "生成文件在 transaction 后已修改，未发布 Capability"
            )
        elif capability_source_error is not None:
            result["capability_warnings"].append(
                "Capability 未发布: "
                f"{type(capability_source_error).__name__}: "
                f"{capability_source_error}"
            )
        elif feedback_tier == "accepted_static_only":
            result["capability_warnings"].append(
                "accepted_static_only 已记录为建议性反馈；缺少runtime/oracle验证，未发布 Capability 候选"
            )
        else:
            try:
                from autowork_core.utils.debug_tools.recorder.capability import (
                    publish_accepted_transaction_capabilities,
                )

                eligible_step_ids = _eligible_capability_steps(
                    previous_events,
                    capability_candidates,
                    transaction_id=report.get("transaction_id"),
                    request_id=report.get("request_id"),
                    session_id=event["source"].get("session_id"),
                    session_path=event["source"].get("session_path"),
                )
                if not eligible_step_ids:
                    result["capability_warnings"].append(
                        "首次认可已记录为复用候选；等待第二个独立事务确认同一契约"
                    )
                    return result

                result["capability_paths"] = [
                    str(path)
                    for path in publish_accepted_transaction_capabilities(
                        report_path,
                        report,
                        project_root=project_root,
                        eligible_step_ids=eligible_step_ids,
                    )
                ]
            except Exception as error:
                result["capability_warnings"].append(
                    "Capability 未发布: "
                    f"{type(error).__name__}: {error}"
                )
    return result


def _feedback_capability_candidates(request, plan):
    value = (plan.get("plan") or {})
    owners = value.get("window_owners") or {}
    target_steps = {
        str(step.get("id") or ""): step
        for step in (request.get("target") or {}).get("steps") or ()
    }
    candidates = []
    for step_id, step in (value.get("steps") or {}).items():
        operations = []
        for operation in (step or {}).get("operations") or ():
            owner_id = str(operation.get("window_owner") or "")
            owner = owners.get(owner_id) or {}
            operations.append({
                "op": str(operation.get("op") or ""),
                "target": str(operation.get("target") or ""),
                "source": str(operation.get("source") or ""),
                "value": operation.get("value"),
                "parameters": operation.get("parameters") or {},
                "window_root": str(owner.get("root_locator") or ""),
                "view_owner": str(operation.get("view_owner") or ""),
                "implementation_location": str(
                    operation.get("implementation_location")
                    or "page_method"
                ),
            })
        if not operations:
            continue
        target_step = target_steps.get(str(step_id)) or {}
        step_pattern = _normalized_step_pattern(
            target_step.get("text")
        )
        signature = {
            "step_pattern": step_pattern,
            "table_usage": (step or {}).get("table_usage"),
            "operations": operations,
        }
        fingerprint = hashlib.sha256(json.dumps(
            signature,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        candidates.append({
            "step_id": str(step_id),
            "fingerprint": fingerprint,
            "step_pattern": step_pattern,
            "operations": operations,
        })
    return candidates


def _normalized_step_pattern(value):
    text = " ".join(str(value or "").casefold().split())
    text = re.sub(r'"[^"\r\n]*"', '"{}"', text)
    text = re.sub(r"'[^'\r\n]*'", "'{}'", text)
    text = re.sub(r"\b\d+(?:\.\d+)?\b", "{}", text)
    return text


def _eligible_capability_steps(
        events,
        candidates,
        *,
        transaction_id,
        request_id,
        session_id,
        session_path,
):
    historical = {}
    for event in _effective_events(events):
        source = event.get("source") or {}
        payload = event.get("payload") or {}
        if (
            event.get("kind") != "transaction_feedback"
            or event.get("status") != "accepted"
            or payload.get("capability_source_validated") is not True
            or not source.get("transaction_id")
            or not source.get("request_id")
            or not source.get("session_id")
            or not source.get("session_path")
            or source.get("transaction_id") == transaction_id
            or source.get("request_id") == request_id
            or source.get("session_id") == session_id
            or source.get("session_path") == session_path
        ):
            continue
        source_id = (
            source.get("transaction_id"),
            source.get("request_id"),
            source.get("session_id"),
            source.get("session_path"),
        )
        for candidate in (
            payload.get("capability_candidates") or ()
        ):
            fingerprint = str(candidate.get("fingerprint") or "")
            if fingerprint:
                historical.setdefault(fingerprint, set()).add(source_id)
    return {
        str(candidate.get("step_id"))
        for candidate in candidates
        if historical.get(str(candidate.get("fingerprint") or ""))
    }


def _accepted_feedback_tier(
        status,
        *,
        modified,
        capability_source_error,
        runtime_verification,
):
    if status != "accepted":
        return None
    if modified or capability_source_error is not None:
        return "accepted_static_only"
    if runtime_verification == "oracle_verified":
        return "accepted_oracle_verified"
    if runtime_verification == "passed":
        return "accepted_runtime_verified"
    return "accepted_static_only"


def _validate_transaction_report_path(report_path, session_dir, report):
    expected_root = (
        Path(session_dir) / "ai" / "generation-transactions"
    ).resolve()
    try:
        relative = Path(report_path).resolve().relative_to(expected_root)
    except ValueError as error:
        raise ValueError(f"生成反馈报告路径越界: {report_path}") from error
    if (
        len(relative.parts) != 2
        or relative.parts[1] != "report.json"
        or relative.parts[0] != report.get("transaction_id")
        or not relative.parts[0].startswith("transaction-")
    ):
        raise ValueError(f"生成反馈报告路径与 transaction id 不一致: {report_path}")


def load_transaction_report(report_path, session_dir):
    expected_root = (
        Path(session_dir) / "ai" / "generation-transactions"
    ).resolve()
    path = Path(report_path).resolve()
    try:
        relative = path.relative_to(expected_root)
    except ValueError:
        return None
    if (
        len(relative.parts) != 2
        or relative.parts[1] != "report.json"
        or not relative.parts[0].startswith("transaction-")
    ):
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(value, dict)
        or value.get("transaction_id") != relative.parts[0]
    ):
        return None
    return path, value


def latest_transaction(
    session_dir,
    *,
    request_id=None,
    step_id=None,
    completed_only=False,
):
    root = Path(session_dir).resolve() / "ai" / "generation-transactions"
    reports = []
    for candidate in root.glob("transaction-*/report.json"):
        loaded = load_transaction_report(candidate, session_dir)
        if loaded is None:
            continue
        path, value = loaded
        if request_id and value.get("request_id") != request_id:
            continue
        if completed_only and value.get("status") not in {
            "completed",
            "completed_no_changes",
        }:
            continue
        if step_id and step_id not in {
            step.get("id")
            for step in (value.get("target") or {}).get("steps") or []
        }:
            continue
        reports.append((value.get("started_at") or "", path, value))
    if not reports:
        return None
    _started_at, path, value = max(reports, key=lambda item: item[0])
    return path, value


def snapshot_files(paths, *, project_root):
    project_root = Path(project_root).resolve()
    result = []
    for value in paths:
        path = Path(value)
        absolute = path.resolve() if path.is_absolute() else (project_root / path).resolve()
        try:
            relative = absolute.relative_to(project_root).as_posix()
        except ValueError:
            relative = str(absolute)
        result.append({
            "path": relative,
            "exists": absolute.is_file(),
            "sha256": _file_hash(absolute),
            "size": absolute.stat().st_size if absolute.is_file() else None,
        })
    return result


def find_recording_root(session_dir):
    session_dir = Path(session_dir).resolve()
    for candidate in (session_dir, *session_dir.parents):
        if (candidate / "catalog.json").exists():
            return candidate
    raise FileNotFoundError(f"无法从会话定位 recording_sessions 根目录: {session_dir}")


def _recording_reference(output_root, session_dir, artifact_path=None):
    output_root = Path(output_root).resolve()
    session_dir = Path(session_dir).resolve()
    reference = {
        "session_path": session_dir.relative_to(output_root).as_posix(),
    }
    if artifact_path is not None:
        artifact_path = Path(artifact_path).resolve()
        reference["artifact_path"] = artifact_path.relative_to(
            session_dir
        ).as_posix()
    return reference


def _request_scope(request):
    return _target_scope(request.get("target") or {})


def _target_scope(target, step_ids=None):
    feature = target.get("feature") or {}
    scenario = target.get("scenario") or {}
    steps = target.get("steps") or []
    selected_ids = set(step_ids or ())
    if selected_ids:
        steps = [step for step in steps if step.get("id") in selected_ids]
    return {
        "feature_id": feature.get("id"),
        "feature_name": feature.get("name"),
        "source_relpath": feature.get("source_relpath"),
        "scenario_id": scenario.get("id"),
        "scenario_name": scenario.get("name"),
        "step_ids": [step.get("id") for step in steps if step.get("id")],
        "step_texts": [step.get("text") for step in steps if step.get("text")],
    }


def _relevance(event, query):
    scope = event.get("scope") or {}
    score = 0
    reasons = []
    common_steps = set(scope.get("step_ids") or ()) & set(query.get("step_ids") or ())
    if common_steps:
        score += 100
        reasons.append(f"same_step_id:{','.join(sorted(common_steps))}")
    if scope.get("feature_id") and scope.get("feature_id") == query.get("feature_id"):
        score += 60
        reasons.append("same_feature_id")
    if (
        scope.get("source_relpath")
        and scope.get("source_relpath") == query.get("source_relpath")
    ):
        score += 50
        reasons.append("same_feature_path")
    common_tokens = _tokens([
        scope.get("feature_name"),
        scope.get("scenario_name"),
        scope.get("step_texts") or [],
        event.get("claim"),
    ]) & _tokens([
        query.get("feature_name"),
        query.get("scenario_name"),
        query.get("step_texts") or [],
    ])
    if common_tokens:
        score += min(40, len(common_tokens) * 5)
        reasons.append("shared_terms:" + ",".join(sorted(common_tokens)[:8]))
    return score, reasons


def _effective_events(events):
    superseded = {
        memory_id
        for event in events
        for memory_id in event.get("supersedes") or []
    }
    return [
        event
        for event in events
        if event.get("memory_id") not in superseded
        and event.get("status") not in {"invalidated", "superseded"}
    ]


def _tokens(value):
    text = _token_text(value).casefold()
    tokens = {
        token
        for token in re.findall(r"[a-z0-9_]+", text)
        if len(token) > 1
    }
    for group in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(group) == 1:
            tokens.add(group)
        else:
            tokens.update(group[index:index + 2] for index in range(len(group) - 1))
    return tokens


def _token_text(value):
    if isinstance(value, dict):
        return " ".join(_token_text(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return " ".join(_token_text(item) for item in value)
    if value is None:
        return ""
    return str(value)


def _memory_bucket(event):
    if event.get("kind") == "transaction_feedback":
        if event.get("status") == "accepted":
            payload = event.get("payload") or {}
            if (
                payload.get("capability_source_validated") is not True
                or payload.get("accepted_feedback_tier")
                not in {
                    "accepted_runtime_verified",
                    "accepted_oracle_verified",
                }
            ):
                return "past_corrections"
            return "accepted_outcomes"
        return "past_corrections"
    if event.get("authority") == "user_confirmed":
        return "confirmed_knowledge"
    if event.get("authority") == "ai_inferred":
        return "provisional_insights"
    if event.get("kind") == "transaction_completed":
        return "generation_history"
    return "other_relevant_experiences"


def _plan_claim(step, step_plan):
    operations = [
        str(operation.get("op") or "")
        for operation in step_plan.get("operations") or []
        if operation.get("op")
    ]
    detail = ", ".join(operations)
    text = f"{step.get('keyword') or ''} {step.get('text') or step.get('id') or ''}".strip()
    return f"User confirmed generation plan for '{text}'" + (
        f": {detail}" if detail else "."
    )


def _file_hash(path):
    path = Path(path)
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _memory_lock(output_root):
    key = str(Path(output_root).resolve()).casefold()
    with _MEMORY_LOCKS_GUARD:
        return _MEMORY_LOCKS.setdefault(key, threading.RLock())


def main(argv=None):
    parser = argparse.ArgumentParser(description="Manage Recorder project experience memory")
    commands = parser.add_subparsers(dest="command", required=True)
    context = commands.add_parser("context")
    context.add_argument("request_path")
    context.add_argument("--limit", type=int, default=16)
    feedback = commands.add_parser("feedback")
    feedback.add_argument("report_path")
    feedback.add_argument("--status", choices=sorted(FEEDBACK_STATUSES), required=True)
    feedback.add_argument("--note", default="")
    history = commands.add_parser("history")
    history.add_argument("recording_root")
    history.add_argument("--limit", type=int, default=20)
    search = commands.add_parser("search")
    search.add_argument("recording_root")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)

    if args.command == "feedback":
        result = record_transaction_feedback(args.report_path, args.status, args.note)
    elif args.command == "search":
        result = search_memory_events(
            args.recording_root,
            args.query,
            limit=args.limit,
        )
    elif args.command == "history":
        events, warnings = load_memory_events(args.recording_root)
        result = {"events": events[-max(1, args.limit):], "warnings": warnings}
    else:
        request_path = Path(args.request_path).resolve()
        request = json.loads(request_path.read_text(encoding="utf-8"))
        session_dir = request_path.parents[2]
        output, memory_context = write_request_memory_context(
            session_dir,
            request,
            limit=args.limit,
        )
        result = {"path": str(output), "context": memory_context}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())