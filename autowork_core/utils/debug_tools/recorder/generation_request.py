from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from autowork_core.utils.debug_tools.recorder.annotations import (
    RecordingAnnotationRepository,
    build_annotation_snapshot,
)
from autowork_core.utils.debug_tools.recorder.bundle_validator import validate_ai_bundle
from autowork_core.utils.debug_tools.recorder.artifact_repair import (
    repair_derived_artifacts,
)
from autowork_core.utils.debug_tools.recorder.evidence_recovery import (
    write_request_recovery_report,
)
from autowork_core.utils.debug_tools.recorder.evidence_context import (
    EVIDENCE_CONTEXT_VERSION,
    build_evidence_context,
)
from autowork_core.utils.debug_tools.recorder.evidence_graph import (
    load_evidence_graph,
)
from autowork_core.utils.debug_tools.recorder.execution_profile import (
    execution_profile_fingerprint,
    normalize_execution_profile,
)
from autowork_core.utils.debug_tools.recorder.generation_contract import (
    ensure_generation_contract,
)
from autowork_core.utils.debug_tools.recorder.catalog import load_recording_catalog
from autowork_core.utils.debug_tools.recorder.models import SCHEMA_VERSION
from autowork_core.utils.debug_tools.recorder.request_repository import (
    artifact_hashes,
    evidence_fingerprint,
    generation_request_id,
    index_generation_request,
    request_identity_is_valid,
    request_matches_current_evidence,
    request_scenario_scope,
    request_scope_key,
    request_revision_snapshot,
    resolve_session_path,
)
from autowork_core.utils.debug_tools.recorder.project_memory import (
    build_request_memory_context,
    write_request_memory_context,
)
from autowork_core.utils.debug_tools.recorder.scope_binding import (
    bind_recording_step_scope,
)
from autowork_core.utils.debug_tools.recorder.writer import write_json_atomic


def build_generation_request(
        recording_path,
        *,
        feature=None,
        scenario=None,
    steps=None,
        latest=True,
        write=True,
        repair=True,
        initialize_workflow=True,
        execution_profile=None,
):
    recording_path = Path(recording_path).resolve()
    session_dir = _resolve_session_dir(
        recording_path,
        feature=feature,
        scenario=scenario,
        latest=latest,
    )
    repair_report = (
        repair_derived_artifacts(session_dir)
        if repair
        else _current_repair_report(session_dir)
    )
    context = _read_json(session_dir / "ai" / "context.json")
    target_index = _read_json(session_dir / "ai" / "target-index.json")
    readiness = validate_ai_bundle(session_dir)
    if not readiness["bundle_valid"]:
        raise ValueError(f"录制证据包无效: {readiness['errors']}")

    scenario_entry = target_index["scenarios"][0]
    generation_contract = _ensure_generation_contract(session_dir)
    available_steps = scenario_entry.get("steps") or []
    selected_steps = _resolve_steps(available_steps, steps)
    incomplete = [entry for entry in selected_steps if entry.get("status") != "completed"]
    if incomplete:
        names = ", ".join(entry.get("key") or entry.get("id") for entry in incomplete)
        raise ValueError(f"目标 Step 尚未完成录制: {names}")

    context_steps = {
        entry["step"]["id"]: entry
        for entry in context.get("steps", [])
    }
    evidence = []
    for target in selected_steps:
        entry = context_steps.get(target["id"])
        if entry is None:
            raise KeyError(f"context.json 缺少目标 Step: {target['id']}")
        artifacts = entry.get("artifacts") or {}
        take_dir = session_dir / artifacts.get("take", "")
        evidence_graph = load_evidence_graph(take_dir)
        timeline_state_value = artifacts.get("timeline_state")
        timeline_state = (
            _read_optional_json(
                resolve_session_path(session_dir, timeline_state_value)
            )
            if timeline_state_value
            else {}
        )
        evidence_artifacts = {
            key: value
            for key, value in artifacts.items()
            if value is not None
        }
        evidence_entry = {
            "step": entry["step"],
            "scenario_example_values": scenario_entry.get("example_values") or {},
            "selected_take": entry.get("selected_take"),
            "target_windows": entry.get("target_windows") or [],
            "window_evidence": entry.get("window_evidence") or [],
            "window_lifecycle": entry.get("window_lifecycle") or [],
            "timeline_revision": timeline_state.get("timeline_revision"),
            "evidence_graph": {
                "version": evidence_graph.get("evidence_graph_version"),
                "graph_fingerprint": evidence_graph.get("graph_fingerprint"),
                "source_fingerprint": evidence_graph.get("source", {}).get(
                    "artifact_fingerprint"
                ),
                "coverage": evidence_graph.get("coverage") or {},
            },
            "artifacts": evidence_artifacts,
            "artifact_hashes": artifact_hashes(
                session_dir,
                evidence_artifacts,
            ),
        }
        input_recovery = _freeze_confirmed_input_recovery(
            entry["step"],
            timeline_state,
            evidence_graph,
        )
        if input_recovery:
            evidence_entry["input_recovery"] = input_recovery
        evidence.append(evidence_entry)

    selected_step_ids = {entry["id"] for entry in selected_steps}
    selected_targets = []
    annotation_repository = RecordingAnnotationRepository(session_dir)
    annotation_revisions = []
    for entry in selected_steps:
        context_entry = context_steps[entry["id"]]
        step_user_context = context_entry.get("step_user_context")
        annotation_revision = annotation_repository.step_context_revision(
            entry["id"]
        )
        if (
                int(annotation_revision.get("revision") or 0) > 0
                and (
                    not isinstance(step_user_context, dict)
                    or step_user_context.get("annotation_id")
                    != annotation_revision.get("annotation_id")
                    or step_user_context.get("revision")
                    != annotation_revision.get("revision")
                )
        ):
            raise ValueError(
                f"StepUserContext投影不是当前版本: {entry['id']}"
            )
        annotation_revisions.append(annotation_revision)
        selected_target = {
            **entry,
            "table": (context_entry["step"] or {}).get("table"),
            "text_block": (
                context_entry["step"] or {}
            ).get("text_block"),
            "step_user_context_revision": annotation_revision,
        }
        if step_user_context is not None:
            selected_target["step_user_context"] = step_user_context
        selected_target["observation_intents"] = [
            dict(item)
            for item in context_entry.get("observation_intents") or ()
            if isinstance(item, dict)
        ]
        selected_targets.append(selected_target)
    selected_steps = selected_targets
    annotation_snapshot = build_annotation_snapshot(selected_steps)
    scenario_scope = request_scenario_scope(
        session_dir,
        selected_step_ids,
    )
    step_scope_binding = bind_recording_step_scope(session_dir)
    target_feature = {
        **target_index["feature"],
        "source_relpath": step_scope_binding["source_relpath"],
    }
    scenario_target = {
        **{
            key: scenario_entry.get(key)
            for key in (
                "id",
                "key",
                "name",
                "kind",
                "example_id",
                "example_values",
                "tags",
                "specification",
            )
        },
        **scenario_scope,
        "step_scope_binding": step_scope_binding,
    }
    target_reviews = [
        item
        for item in readiness.get("review_required", [])
        if item.get("step_id") is None or item.get("step_id") in selected_step_ids
    ]
    memory_context = None
    memory_error = None
    try:
        memory_context = build_request_memory_context(
            session_dir,
            {"target": {
                "feature": target_feature,
                "scenario": scenario_entry,
                "steps": selected_steps,
            }},
        )
    except Exception as error:
        memory_error = error
    memory_revision = (
        (memory_context.get("journal") or {}).get("revision")
        if memory_context is not None
        else None
    )
    specification_fingerprint = _specification_fingerprint(
        target_feature,
        scenario_target,
        selected_steps,
    )
    annotation_fingerprint = annotation_snapshot["snapshot_fingerprint"]
    execution = normalize_execution_profile(execution_profile)
    execution_fingerprint = execution_profile_fingerprint(execution)
    request_id = generation_request_id(
        target_index["feature"]["id"],
        scenario_entry["id"],
        evidence,
        scenario_scope=scenario_scope,
        evidence_context_version=EVIDENCE_CONTEXT_VERSION,
        reviews=target_reviews,
        memory_revision=memory_revision,
        specification_fingerprint=specification_fingerprint,
        annotation_fingerprint=annotation_fingerprint,
        execution_profile_fingerprint=execution_fingerprint,
    )
    target_readiness = _target_readiness(target_reviews)
    target_capture_generation_candidate = target_readiness[
        "target_capture_generation_candidate"
    ]
    request = {
        "schema_version": SCHEMA_VERSION,
        "request_version": "3.0",
        "request_id": request_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "identity_basis": {
            "request_identity_profile": "business-v1",
            "evidence_context_version": EVIDENCE_CONTEXT_VERSION,
            "specification_fingerprint": specification_fingerprint,
            "annotation_fingerprint": annotation_fingerprint,
            "annotation_snapshot_version": annotation_snapshot[
                "annotation_snapshot_version"
            ],
            "execution_profile_fingerprint": execution_fingerprint,
            "step_scope_fingerprint": step_scope_binding[
                "binding_fingerprint"
            ],
        },
        "annotation_snapshot": annotation_snapshot,
        "execution": execution,
        "session": {
            **target_index["session"],
            "absolute_path": str(session_dir),
        },
        "target": {
            "feature": target_feature,
            "scenario": scenario_target,
            "steps": selected_steps,
        },
        "readiness": {
            "bundle_valid": readiness["bundle_valid"],
            "target_capture_generation_candidate": target_capture_generation_candidate,
            "session_capture_generation_candidate": readiness.get(
                "capture_generation_candidate", False
            ),
            "target_reconciliation_required": bool(target_reviews),
            "target_hard_blocker_count": target_readiness[
                "target_hard_blocker_count"
            ],
            "session_warnings": readiness["warnings"],
            "target_review_required": target_reviews,
        },
        "generation_contract": "ai/generation-contract.json",
        "artifact_repair": {
            "report_path": Path(repair_report["report_path"])
            .relative_to(session_dir)
            .as_posix(),
            "repaired": repair_report["repaired"],
            "unrecoverable": repair_report["unrecoverable"],
            "raw_evidence_modified": False,
        },
        "evidence": evidence,
        "instructions": [
            "Generate only the target Steps in this request.",
            "Obey ai/generation-contract.json.",
            "Read existing Step Definitions, Page Objects, locators, and data before editing.",
            (
                "PIC is default-deny. Use it only when Decision Answers authorize "
                "the action and transaction pic_authorization_audit passes; never "
                "call direct PIC APIs."
            ),
            "Do not guess review-required business meaning or expected values.",
            (
                "Resolve target_review_required through the declared system, "
                "AI, user, or evidence authority before generating code."
            ),
            "Generate validated code only; hard evidence blockers require repair or rerecording.",
            "Validate generated Python, locator YAML, and focused Behave scope.",
        ],
    }
    if write:
        request_dir = session_dir / "ai" / "requests"
        request_path = request_dir / f"{request_id}.json"
        existing_request = _read_optional_json(request_path)
        if existing_request:
            return _reuse_immutable_request(
                session_dir,
                existing_request,
                selected_step_ids,
                memory_revision,
                request_path,
            )
        request["request_path"] = request_path.relative_to(session_dir).as_posix()
        request["request_path_absolute"] = str(request_path)
        write_request_recovery_report(session_dir, request)
        if memory_context is not None:
            memory_path, memory_context = write_request_memory_context(
                session_dir,
                request,
                context=memory_context,
            )
            request["memory_context"] = {
                "available": True,
                "path": memory_path.relative_to(session_dir).as_posix(),
                "revision": (memory_context.get("journal") or {}).get("revision"),
                "event_count": (memory_context.get("journal") or {}).get("event_count", 0),
                "relevant_count": memory_context.get("relevant_count", 0),
                "warnings": memory_context.get("warnings") or [],
            }
        else:
            request["memory_context"] = {
                "path": None,
                "available": False,
                "warnings": [
                    "项目记忆上下文不可用: "
                    f"{type(memory_error).__name__}: {memory_error}"
                ],
            }
        evidence_context = build_evidence_context(
            session_dir,
            request,
            write=True,
        )
        context_path = Path(evidence_context["context_path"])
        request["evidence_context"] = {
            "available": True,
            "version": evidence_context.get("evidence_context_version"),
            "path": context_path.relative_to(session_dir).as_posix(),
            "context_fingerprint": evidence_context.get("context_fingerprint"),
            "minimum_decision_evidence_ids": evidence_context.get(
                "minimum_decision_evidence_ids"
            ) or [],
            "coverage": evidence_context.get("coverage") or {},
        }
        request["request_scope"] = request_scope_key(
            step["id"] for step in selected_steps
        )
        request["evidence_fingerprint"] = evidence_fingerprint(request)
        request["revision_snapshot"] = request_revision_snapshot(
            session_dir,
            request,
        )
        index_generation_request(session_dir, request)
        if initialize_workflow:
            from autowork_core.utils.debug_tools.recorder.workflow_service import (
                inspect_workflow,
            )

            inspect_workflow(request_path, write=True)
    return request


def _target_readiness(reviews):
    reviews = [
        item
        for item in reviews or ()
        if isinstance(item, dict)
    ]
    hard_blockers = [
        item
        for item in reviews
        if (item.get("recovery") or {}).get("hard_blocker")
    ]
    return {
        "target_capture_generation_candidate": not hard_blockers,
        "target_reconciliation_required": bool(reviews),
        "target_hard_blocker_count": len(hard_blockers),
    }


def _freeze_confirmed_input_recovery(step, timeline_state, evidence_graph):
    step = dict(step or {})
    state = dict(timeline_state or {})
    graph_actions = {
        str(action.get("action_id") or ""): dict(action or {})
        for action in (evidence_graph or {}).get("actions") or ()
        if isinstance(action, dict) and action.get("action_id")
    }
    result = []
    for raw_candidate in (
            state.get("confirmed_keyboard_input_exclusions") or ()
    ):
        candidate = dict(raw_candidate or {})
        literal = str(candidate.get("excluded_input_text") or "")
        target_action_id = str(candidate.get("target_action_id") or "")
        target = graph_actions.get(target_action_id) or {}
        target_value = dict(target.get("target") or {})
        reason = None
        if candidate.get("status") != "pending_target_validation":
            reason = str(candidate.get("reason") or "target_unavailable")
        elif not literal or literal not in str(step.get("text") or ""):
            reason = "literal_not_declared"
        elif target.get("type") not in {"click", "focus"}:
            reason = "target_action_unavailable"
        elif target_value.get("locator_validation") != "unique_target_match":
            reason = "target_not_unique"
        elif not target_value.get("locator_candidate_id"):
            reason = "target_candidate_missing"
        elif _recovery_target_identity(target_value) != candidate.get(
                "target_identity"
        ):
            reason = "target_identity_changed"
        frozen = {
            "candidate_id": str(candidate.get("candidate_id") or ""),
            "status": "eligible" if reason is None else "unavailable",
            "reason": reason,
            "step_id": str(step.get("id") or ""),
            "confirmed_edit_id": str(
                candidate.get("confirmed_edit_id") or ""
            ),
            "excluded_action_id": str(
                candidate.get("excluded_action_id") or ""
            ),
            "excluded_event_ids": list(
                candidate.get("excluded_event_ids") or ()
            ),
            "literal": literal or None,
            "value_reference": "step_text" if literal else None,
            "target_action_id": target_action_id or None,
            "target_fingerprint": target_value.get("target_fingerprint"),
            "locator_name": target_value.get("locator_name"),
            "locator_candidate_id": target_value.get("locator_candidate_id"),
        }
        result.append({
            key: value
            for key, value in frozen.items()
            if value not in (None, "", [], {})
        })
    return result


def _recovery_target_identity(target):
    target = dict(target or {})
    element = target.get("element") or {}
    root_name = str(target.get("root_name") or "")
    process_id = element.get("process_id")
    handle = element.get("handle")
    runtime_id = list(element.get("runtime_id") or ())
    if root_name and process_id and handle:
        return {
            "root_name": root_name,
            "process_id": int(process_id),
            "handle": int(handle),
        }
    if root_name and runtime_id:
        return {"root_name": root_name, "runtime_id": runtime_id}
    return None


def _specification_fingerprint(feature, scenario, steps):
    import hashlib

    value = {
        "feature": {
            key: feature.get(key)
            for key in (
                "id",
                "name",
                "description",
                "tags",
                "source_relpath",
            )
        },
        "scenario": {
            key: scenario.get(key)
            for key in (
                "id",
                "name",
                "kind",
                "logical_template_id",
                "example_id",
                "example_values",
                "specification",
                "step_scope_binding",
            )
        },
        "steps": [
            {
                key: step.get(key)
                for key in (
                    "id",
                    "keyword",
                    "text",
                    "table",
                    "text_block",
                )
            }
            for step in steps or ()
        ],
    }
    return hashlib.sha256(json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _annotation_fingerprint(revisions):
    import hashlib

    return hashlib.sha256(json.dumps(
        list(revisions or ()),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _resolve_session_dir(recording_path, feature, scenario, latest):
    if (recording_path / "manifest.json").exists():
        manifest = _read_json(recording_path / "manifest.json")
        _ensure_manifest_target(manifest, feature, scenario)
        return recording_path

    catalog = load_recording_catalog(recording_path)
    candidates = catalog.get("sessions") or []
    if feature:
        candidates = _best_matches(
            candidates,
            feature,
            lambda entry: entry.get("feature") or {},
            ("id", "key", "name", "source_relpath"),
        )
    if scenario:
        candidates = _best_matches(
            candidates,
            scenario,
            lambda entry: entry.get("scenario") or {},
            ("id", "key", "name", "example_id"),
        )
    if not candidates:
        raise LookupError("没有找到匹配的录制会话")
    if len(candidates) > 1 and not latest:
        paths = ", ".join(entry.get("path", "") for entry in candidates[:10])
        raise ValueError(f"匹配到多个录制会话，请补充选择条件: {paths}")
    candidates.sort(key=lambda entry: entry.get("updated_at") or "", reverse=True)
    return recording_path / candidates[0]["path"]


def _ensure_manifest_target(manifest, feature, scenario):
    if feature and not _matches(manifest.get("feature") or {}, feature, ("id", "key", "name", "source_relpath")):
        raise LookupError(f"当前会话不匹配 Feature: {feature}")
    if scenario and not _matches(manifest.get("scenario") or {}, scenario, ("id", "key", "name", "example_id")):
        raise LookupError(f"当前会话不匹配 Scenario: {scenario}")


def _resolve_steps(available_steps, selectors):
    if selectors is None:
        selected = [
            entry
            for entry in available_steps
            if entry.get("status") != "skipped"
        ]
        if not selected:
            raise ValueError("当前范围没有可生成 Step")
        return selected
    if isinstance(selectors, str):
        selectors = [selectors]
    else:
        selectors = list(selectors)
    if not selectors:
        raise ValueError("至少选择一个目标 Step")
    selected = []
    for selector in selectors:
        matches = _best_matches(
            available_steps,
            selector,
            lambda entry: entry,
            ("id", "key", "text", "ordinal"),
        )
        if not matches:
            raise LookupError(f"没有找到 Step: {selector}")
        if len(matches) > 1:
            choices = ", ".join(entry.get("key") or entry.get("id") for entry in matches)
            raise ValueError(f"Step 选择不唯一: {selector} -> {choices}")
        if matches[0]["id"] not in {entry["id"] for entry in selected}:
            selected.append(matches[0])
    selected_ids = {entry["id"] for entry in selected}
    return [
        entry
        for entry in available_steps
        if entry["id"] in selected_ids
    ]


def _matches(value, selector, fields):
    return _match_score(value, selector, fields) > 0


def _match_score(value, selector, fields):
    selector_text = str(selector).strip().casefold()
    score = 0
    for field in fields:
        field_value = value.get(field)
        if field_value is None:
            continue
        text = str(field_value).strip().casefold()
        if text == selector_text:
            score = max(score, 2)
        elif selector_text and selector_text in text:
            score = max(score, 1)
    return score


def _best_matches(entries, selector, value_getter, fields):
    scored = [
        (_match_score(value_getter(entry), selector, fields), entry)
        for entry in entries
    ]
    best_score = max((score for score, _ in scored), default=0)
    if best_score == 0:
        return []
    return [entry for score, entry in scored if score == best_score]


def _reuse_immutable_request(
        session_dir,
        existing,
        selected_step_ids,
        memory_revision,
        request_path,
):
    if not request_identity_is_valid(existing, selected_step_ids):
        raise ValueError(f"不可变 RequestV3 身份无效: {request_path}")
    declared_memory_revision = (
        existing.get("memory_context") or {}
    ).get("revision")
    if declared_memory_revision != memory_revision:
        raise ValueError(f"RequestV3 identity collision: {request_path}")
    if not request_matches_current_evidence(
        session_dir,
        existing,
        selected_step_ids,
    ):
        raise ValueError(f"RequestV3 内容或 revision 已被修改: {request_path}")
    return existing


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _read_optional_json(path):
    return _read_json(path) if Path(path).exists() else {}


def _current_repair_report(session_dir):
    report_path = session_dir / "ai" / "repairs" / "latest.json"
    report = _read_optional_json(report_path)
    return {
        "report_path": str(report_path),
        "repaired": report.get("repaired") or [],
        "unrecoverable": report.get("unrecoverable") or [],
        "raw_evidence_modified": False,
    }


def _ensure_generation_contract(session_dir):
    return ensure_generation_contract(session_dir, write=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build a targeted AI generation request")
    parser.add_argument("recording_path", help="Recording root or one run directory")
    parser.add_argument("--feature", default=None, help="Feature id, key, name, or source path")
    parser.add_argument("--scenario", default=None, help="Scenario id, key, name, or Examples id")
    parser.add_argument("--step", action="append", dest="steps", help="Step id, key, ordinal, or unique text")
    parser.add_argument("--no-latest", action="store_true", help="Fail when multiple sessions match")
    parser.add_argument(
        "--execution-mode",
        choices=("attach_existing", "launch", "external_manual"),
    )
    parser.add_argument("--app-path")
    parser.add_argument(
        "--process-track-mode",
        choices=("snapshot", "root", "none"),
    )
    args = parser.parse_args(argv)
    request = build_generation_request(
        args.recording_path,
        feature=args.feature,
        scenario=args.scenario,
        steps=args.steps,
        latest=not args.no_latest,
        write=True,
        execution_profile={
            key: value
            for key, value in {
                "mode": args.execution_mode,
                "app_path": args.app_path,
                "process_track_mode": args.process_track_mode,
            }.items()
            if value is not None
        },
    )
    print(json.dumps(request, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())