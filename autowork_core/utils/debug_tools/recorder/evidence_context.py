from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path

from autowork_core.utils.debug_tools.recorder.evidence_graph import (
    EVIDENCE_GRAPH_VERSION,
    EVENT_ARTIFACT_KINDS,
    load_evidence_graph,
)
from autowork_core.utils.debug_tools.recorder.models import SCHEMA_VERSION
from autowork_core.utils.debug_tools.recorder.projection_store import (
    ProjectionStore,
)
from autowork_core.utils.debug_tools.recorder.request_repository import (
    request_identity_is_valid,
    request_revision_matches,
    resolve_session_path,
    session_dir_for_request_path,
)
from autowork_core.utils.debug_tools.recorder.writer import write_json_atomic


EVIDENCE_CONTEXT_VERSION = "2.3"
SUPPORTED_EVIDENCE_CONTEXT_VERSIONS = {EVIDENCE_CONTEXT_VERSION}


def build_evidence_context(session_dir, request, *, write=True):
    session_dir = Path(session_dir).resolve()
    request_id = str(request.get("request_id") or "").strip()
    if not request_id:
        raise ValueError("AI request 缺少 request_id")

    source_graphs = []
    items = []
    deferred = []
    minimum_ids = []
    scoped_ids = len(request.get("evidence") or []) > 1
    for evidence in request.get("evidence") or []:
        step = evidence.get("step") or {}
        artifacts = evidence.get("artifacts") or {}
        take_dir = resolve_session_path(session_dir, artifacts.get("take"))
        graph = load_evidence_graph(take_dir)
        graph_path = resolve_session_path(
            session_dir,
            artifacts.get("evidence_graph"),
        )
        source_graphs.append({
            "step_id": step.get("id"),
            "take_id": graph.get("take", {}).get("id"),
            "take_path": take_dir.relative_to(session_dir).as_posix(),
            "path": graph_path.relative_to(session_dir).as_posix(),
            "graph_fingerprint": graph.get("graph_fingerprint"),
            "source_fingerprint": graph.get("source", {}).get(
                "artifact_fingerprint"
            ),
        })
        graph_items, graph_minimum = _graph_items(
            step,
            graph,
            scoped_ids=scoped_ids,
        )
        items.extend(graph_items)
        minimum_ids.extend(graph_minimum)
        deferred.extend(_deferred_artifacts(step, graph))

    selected_ids = [item["evidence_id"] for item in items]
    context = {
        "schema_version": SCHEMA_VERSION,
        "evidence_context_version": EVIDENCE_CONTEXT_VERSION,
        "request_id": request_id,
        "materialized_at": datetime.now().isoformat(timespec="seconds"),
        "source_graphs": source_graphs,
        "items": items,
        "default_selected_evidence_ids": selected_ids,
        "required_consumption_evidence_ids": selected_ids,
        "minimum_decision_evidence_ids": list(dict.fromkeys(minimum_ids)),
        "deferred_artifacts": deferred,
        "retrieval": {
            "command": (
                "python -m autowork_core.utils.debug_tools.recorder."
                "evidence_context show <context-path> --evidence-id <id>"
            ),
            "policy": (
                "Load deferred raw/media evidence when selected evidence is "
                "missing, ambiguous, contradictory, or insufficient for a claim."
            ),
        },
        "coverage": {
            "source_graphs": len(source_graphs),
            "selected_items": len(items),
            "minimum_items": len(set(minimum_ids)),
            "deferred_items": len(deferred),
            "decision_claims_with_provenance": 0,
            "decision_claims_total": 0,
        },
    }
    context["context_fingerprint"] = _evidence_context_fingerprint(
        context
    )
    if write:
        output = (
            session_dir
            / "ai"
            / "evidence-context"
            / f"{request_id}.json"
        )
        write_json_atomic(output, context)
        context["context_path"] = str(output)
    return context


def load_evidence_context(path):
    path = Path(path).resolve()
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("evidence_context_version") not in (
        SUPPORTED_EVIDENCE_CONTEXT_VERSIONS
    ):
        raise ValueError(f"不支持的 evidence context: {path}")
    declared_fingerprint = value.get("context_fingerprint")
    if not declared_fingerprint:
        raise ValueError(f"evidence context fingerprint 缺失: {path}")
    if declared_fingerprint and declared_fingerprint != (
        _evidence_context_fingerprint(value)
    ):
        raise ValueError(f"evidence context fingerprint 无效: {path}")
    value["context_path"] = str(path)
    return value


def _evidence_context_fingerprint(value):
    return _stable_hash({
        "request_id": value.get("request_id"),
        "source_graphs": value.get("source_graphs") or [],
        "items": value.get("items") or [],
        "minimum_decision_evidence_ids": value.get(
            "minimum_decision_evidence_ids"
        ) or [],
        "required_consumption_evidence_ids": value.get(
            "required_consumption_evidence_ids"
        ) or [],
        "deferred_artifacts": value.get("deferred_artifacts") or [],
    })


def evidence_item_ids(context):
    return {
        str(item.get("evidence_id"))
        for item in context.get("items") or []
        if item.get("evidence_id")
    } | {
        str(item.get("evidence_id"))
        for item in context.get("deferred_artifacts") or []
        if item.get("evidence_id")
    }


def get_evidence_item(context, evidence_id):
    evidence_id = str(evidence_id)
    for group in ("items", "deferred_artifacts"):
        for item in context.get(group) or []:
            if item.get("evidence_id") == evidence_id:
                return item
    raise KeyError(f"evidence id 不存在: {evidence_id}")


def query_request_evidence(
        request_path,
        *,
        evidence_id=None,
        step_id=None,
        action_id=None,
        list_only=False,
):
    selectors = [
        value
        for value in (evidence_id, step_id, action_id)
        if value not in (None, "")
    ]
    if len(selectors) > 1:
        raise ValueError(
            "evidence_id、step_id 和 action_id 只能选择一个"
        )
    request_path = Path(request_path).resolve()
    request = json.loads(request_path.read_text(encoding="utf-8"))
    if not request_identity_is_valid(request):
        raise ValueError("RequestV3 身份或完整性无效")
    session_dir = session_dir_for_request_path(request_path, request)
    declared = request.get("evidence_context") or {}
    context_path = resolve_session_path(session_dir, declared.get("path"))
    context = load_evidence_context(context_path)
    expected_fingerprint = str(
        declared.get("context_fingerprint") or ""
    )
    if (
        expected_fingerprint
        and context.get("context_fingerprint") != expected_fingerprint
    ):
        raise ValueError("Evidence Context 指纹与 RequestV3 不一致")

    values = [
        *list(context.get("items") or ()),
        *list(context.get("deferred_artifacts") or ()),
    ]
    if evidence_id:
        values = [get_evidence_item(context, evidence_id)]
    elif step_id:
        values = [
            item
            for item in values
            if str(item.get("step_id") or "") == str(step_id)
        ]
    elif action_id:
        values = [
            item
            for item in values
            if _evidence_action_id(item) == str(action_id)
        ]
    if list_only:
        values = [
            {
                key: item.get(key)
                for key in (
                    "evidence_id",
                    "kind",
                    "step_id",
                    "selection",
                    "required_for_decision",
                    "path",
                    "reason",
                    "size",
                )
                if item.get(key) is not None
            }
            for item in values
        ]
    return {
        "evidence_query_version": "1.0",
        "request_id": request.get("request_id"),
        "request_path": str(request_path),
        "context_version": context.get("evidence_context_version"),
        "context_fingerprint": context.get("context_fingerprint"),
        "query": {
            "evidence_id": evidence_id,
            "step_id": step_id,
            "action_id": action_id,
            "list_only": bool(list_only),
        },
        "items": values,
        "item_count": len(values),
    }


def _evidence_action_id(item):
    if str(item.get("kind") or "") not in {
        "action",
        "target",
        "text_change",
        "action_media",
    }:
        return None
    evidence_id = str(item.get("evidence_id") or "")
    return evidence_id.rsplit(":", 1)[-1] if ":" in evidence_id else None


def compare_request_takes(request_path, *, step_id, take_ids=()):
    request_path = Path(request_path).resolve()
    request = json.loads(request_path.read_text(encoding="utf-8"))
    if not request_identity_is_valid(request):
        raise ValueError("RequestV3 身份或完整性无效")
    session_dir = session_dir_for_request_path(request_path, request)
    target_step_ids = {
        str(step.get("id"))
        for step in (request.get("target") or {}).get("steps") or ()
        if step.get("id")
    }
    if str(step_id) not in target_step_ids:
        raise ValueError(f"Step 不属于当前 Request 目标 Step: {step_id}")
    revision_matches, _current_revision = request_revision_matches(
        session_dir,
        request,
        request.get("revision_snapshot") or {},
    )
    if not revision_matches:
        raise ValueError("Request revision 已变化，请先物化最新 RequestV3")
    manifest_path = session_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = next(
        (
            item
            for item in manifest.get("steps") or ()
            if str((item.get("plan") or {}).get("id") or "")
            == str(step_id)
        ),
        None,
    )
    if entry is None:
        raise KeyError(f"录制任务中不存在 Step: {step_id}")
    requested = {str(item) for item in take_ids or () if item}
    candidates = [
        take
        for take in entry.get("takes") or ()
        if take.get("status") == "completed"
        and (not requested or str(take.get("id")) in requested)
    ]
    found = {str(take.get("id")) for take in candidates}
    missing = sorted(requested - found)
    if missing:
        raise KeyError(f"Take 不存在或未完成: {missing}")
    if len(candidates) < 2:
        raise ValueError("Take 比较至少需要两个 completed Take")

    summaries = [
        _take_comparison_summary(session_dir, take)
        for take in candidates
    ]
    return {
        "take_comparison_version": "1.0",
        "request_id": request.get("request_id"),
        "step_id": str(step_id),
        "selected_take_id": entry.get("selected_take"),
        "take_count": len(summaries),
        "takes": summaries,
        "differences": _take_action_differences(summaries),
        "selection_policy": (
            "Comparison is factual only; AI or the user chooses which Take "
            "best represents the business intent."
        ),
    }


def _take_comparison_summary(session_dir, take):
    take_dir = resolve_session_path(session_dir, take.get("path"))
    projection = ProjectionStore(take_dir).current()
    if projection is None:
        raise ValueError(
            f"Take 缺少有效 Projection 5.7: {take.get('id')}"
        )
    graph_path = projection.path("evidence_graph")
    if graph_path is None:
        raise ValueError(f"Take Projection 缺少 Evidence Graph: {take.get('id')}")
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    if graph.get("evidence_graph_version") != EVIDENCE_GRAPH_VERSION:
        raise ValueError(
            f"Take Evidence Graph身份无效: {take.get('id')}"
        )
    declared_fingerprint = str(take.get("graph_fingerprint") or "")
    if (
        declared_fingerprint
        and graph.get("graph_fingerprint") != declared_fingerprint
    ):
        raise ValueError(
            f"Take Evidence Graph 指纹不匹配: {take.get('id')}"
        )
    return {
        "take_id": str(take.get("id") or ""),
        "timeline_revision": take.get("timeline_revision"),
        "graph_fingerprint": graph.get("graph_fingerprint"),
        "coverage": graph.get("coverage") or {},
        "actions": [
            _comparison_action(action)
            for action in graph.get("actions") or ()
        ],
    }


def _comparison_action(action):
    target = action.get("target") or {}
    text_change = action.get("text_change") or {}
    media = action.get("media") or {}
    return {
        "ordinal": action.get("ordinal"),
        "type": action.get("type"),
        "role": action.get("role"),
        "target_fingerprint": target.get("target_fingerprint"),
        "locator_name": target.get("locator_name"),
        "control_type": target.get("control_type"),
        "value_status": text_change.get("status"),
        "before_value": text_change.get("before_value"),
        "after_value": text_change.get("after_value"),
        "value_delta": text_change.get("value_delta"),
        "closure_status": (action.get("closure") or {}).get("status"),
        "visual_stability": (media.get("stability") or {}).get("status"),
        "visual_outcome": (media.get("outcome") or {}).get("result"),
    }


def _take_action_differences(summaries):
    actions_by_take = {
        item["take_id"]: {
            action.get("ordinal"): action
            for action in item.get("actions") or ()
        }
        for item in summaries
    }
    ordinals = sorted({
        ordinal
        for actions in actions_by_take.values()
        for ordinal in actions
        if ordinal is not None
    })
    differences = []
    for ordinal in ordinals:
        values = {
            take_id: actions.get(ordinal)
            for take_id, actions in actions_by_take.items()
        }
        signatures = {
            json.dumps(value, ensure_ascii=False, sort_keys=True)
            for value in values.values()
        }
        if len(signatures) > 1:
            differences.append({
                "ordinal": ordinal,
                "by_take": values,
            })
    return differences


def _graph_items(step, graph, *, scoped_ids=False):
    step_id = str(step.get("id") or "unknown-step")
    items = []
    minimum = []
    tree_delta = graph.get("observations", {}).get("tree_delta") or {}
    tree_id = f"tree-delta:{step_id}"
    if tree_delta.get("artifact"):
        items.append(_item(
            tree_id,
            "state_delta",
            step_id,
            tree_delta,
            required=False,
        ))
    for action in graph.get("actions") or []:
        action_id = str(action.get("action_id") or "unknown-action")
        identity = (
            f"{step_id}:{action_id}"
            if scoped_ids
            else action_id
        )
        action_evidence_id = f"action:{identity}"
        target_evidence_id = f"target:{identity}"
        items.append(_item(
            action_evidence_id,
            "action",
            step_id,
            {
                key: action.get(key)
                for key in (
                    "action_id",
                    "ordinal",
                    "type",
                    "role",
                    "event_ids",
                    "media_event_ids",
                    "commit_event_id",
                    "value_binding",
                    "note",
                    "parameters",
                    "canonical_action",
                    "time",
                    "closure",
                    "provenance",
                )
            },
            required=True,
        ))
        items.append(_item(
            target_evidence_id,
            "target",
            step_id,
            action.get("target") or {},
            required=True,
        ))
        minimum.extend([action_evidence_id, target_evidence_id])
        text_change = action.get("text_change")
        if text_change:
            text_id = f"text-change:{identity}"
            items.append(_item(
                text_id,
                "text_change",
                step_id,
                text_change,
                required=True,
            ))
            minimum.append(text_id)
        media = action.get("media") or {}
        if media:
            items.append(_item(
                f"media:{identity}",
                "action_media",
                step_id,
                media,
                required=False,
            ))
    return items, minimum


def _deferred_artifacts(step, graph):
    step_id = str(step.get("id") or "unknown-step")
    deferred = []
    for artifact in graph.get("source", {}).get("artifacts") or []:
        kind = artifact.get("kind")
        if kind not in EVENT_ARTIFACT_KINDS | {"video", "image", "ui_tree"}:
            continue
        reason = {
            "raw_events": "effective action envelope selected by default",
            "canonical_events": "effective action envelope selected by default",
            "effective_events": "effective action envelope selected by default",
            "video": "structured actions and keyframes selected before full video",
            "image": "action-media keyframe references selected by default",
            "ui_tree": "tree delta selected before full tree snapshots",
        }[kind]
        deferred.append({
            "evidence_id": f"artifact:{artifact['artifact_id']}",
            "kind": kind,
            "step_id": step_id,
            "path": artifact.get("path"),
            "sha256": artifact.get("sha256"),
            "size": artifact.get("size"),
            "selection": "deferred",
            "reason": reason,
        })
    return deferred


def _item(evidence_id, kind, step_id, payload, *, required):
    return {
        "evidence_id": evidence_id,
        "kind": kind,
        "step_id": step_id,
        "selection": "default",
        "required_for_decision": bool(required),
        "payload": payload,
    }


def _stable_hash(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Query Recorder V2 evidence context")
    subparsers = parser.add_subparsers(dest="command", required=True)
    show = subparsers.add_parser("show")
    show.add_argument("context_path")
    show.add_argument("--evidence-id")
    build = subparsers.add_parser("build")
    build.add_argument("request_path")
    args = parser.parse_args(argv)
    if args.command == "show":
        context = load_evidence_context(args.context_path)
        value = (
            get_evidence_item(context, args.evidence_id)
            if args.evidence_id
            else context
        )
    else:
        request_path = Path(args.request_path).resolve()
        request = json.loads(request_path.read_text(encoding="utf-8"))
        session_dir = session_dir_for_request_path(request_path, request)
        value = build_evidence_context(session_dir, request, write=True)
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())