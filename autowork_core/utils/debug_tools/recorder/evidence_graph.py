from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import yaml

from autowork_core.utils.debug_tools.recorder.canonical_action import (
    build_canonical_action,
)
from autowork_core.utils.debug_tools.recorder.models import SCHEMA_VERSION
from autowork_core.utils.debug_tools.recorder.projection_store import (
    PROJECTION_ROOT_NAME,
    resolve_take_artifact,
)
from autowork_core.utils.debug_tools.recorder.raw_event_journal import (
    requires_capture_integrity,
    validate_capture_integrity,
)
from autowork_core.utils.debug_tools.recorder.writer import write_json_atomic


EVIDENCE_GRAPH_VERSION = "2.6"
EVENT_ARTIFACT_KINDS = frozenset({
    "raw_events",
    "canonical_events",
    "effective_events",
})
_GRAPH_PATH = Path("evidence") / "graph.json"
_EXCLUDED_SOURCE_PARTS = {
    "evidence",
    "extracted_frames",
}
_EXCLUDED_SOURCE_ROOTS = {
    PROJECTION_ROOT_NAME,
    "projections",
}
_EXCLUDED_SOURCE_NAMES = {
    "current-projection.json",
    "timeline-state.json",
    "actions.effective.json",
    "events.effective.jsonl",
    "locator-candidates.effective.yaml",
    "action-media.json",
    "action-contact-sheet.png",
}
_CONTAINER_LIKE_TYPES = {"window", "pane", "group", "custom", "document"}
_VALUE_PROPERTY_NAMES = (
    "Value.Value",
    "LegacyIAccessible.Value",
    "element_info.rich_text",
    "wrapper.window_text",
)


def build_evidence_graph(
        take_dir,
        *,
        write=True,
        projection_dir=None,
        projection_prefix=None,
    ):
    take_dir = Path(take_dir).resolve()
    projection_dir = (
        Path(projection_dir).resolve()
        if projection_dir is not None
        else None
    )
    effective_root = projection_dir or take_dir
    projection_prefix = str(projection_prefix or "").strip("/")
    logical = lambda name: (
        f"{projection_prefix}/{name}" if projection_prefix else name
    )
    take = _read_json(take_dir / "take.json")
    capture_integrity = take.get("capture_integrity") or {}
    if requires_capture_integrity(take_dir, take):
        base_events = _read_jsonl(take_dir / "events.jsonl")
        capture_integrity = validate_capture_integrity(
            take_dir,
            [str(event.get("id") or "") for event in base_events],
        )
        if capture_integrity.get("status") != "complete":
            raise RuntimeError(
                "Take 原始事件完整性校验失败: "
                + "; ".join(capture_integrity.get("errors") or ())
            )
    else:
        capture_integrity = {
            "status": capture_integrity.get("status") or "legacy_unavailable",
        }
    effective_events_path = effective_root / "events.effective.jsonl"
    event_artifact = (
        logical("events.effective.jsonl")
        if effective_events_path.exists()
        else "events.jsonl"
    )
    action_artifact = logical("actions.effective.json")
    locator_artifact = logical("locator-candidates.effective.yaml")
    media_artifact = logical("action-media.json")
    events = _read_jsonl(
        effective_events_path
        if effective_events_path.exists()
        else take_dir / "events.jsonl"
    )
    actions_path = effective_root / "actions.effective.json"
    actions = list((_read_json(actions_path).get("actions") or []))
    if not actions and projection_dir is None:
        actions = _read_actions(take_dir)
    action_media_path = effective_root / "action-media.json"
    action_media = _read_json(action_media_path)
    tree_diff = _read_json(take_dir / "ui" / "tree-diff.json")
    locator_bundle = _read_yaml(
        effective_root / "locator-candidates.effective.yaml",
    )

    projected_artifacts = []
    if projection_dir is not None:
        for name in (
            "timeline-state.json",
            "actions.effective.json",
            "events.effective.jsonl",
            "locator-candidates.effective.yaml",
            "action-media.json",
            "action-contact-sheet.png",
        ):
            path = projection_dir / name
            if path.exists():
                projected_artifacts.append((logical(name), path))
    artifacts = _artifact_manifest(
        take_dir,
        active_supplement_ids=_active_supplement_ids(actions),
        projected_artifacts=projected_artifacts,
    )
    artifact_fingerprint = _artifact_fingerprint(artifacts)
    event_map = {
        str(event.get("id")): event
        for event in events
        if event.get("id")
    }
    media_map = {
        str(entry.get("action_id")): entry
        for entry in action_media.get("actions") or []
        if entry.get("action_id")
    }
    target_map = {
        str(entry.get("event_id")): entry
        for entry in locator_bundle.get("event_targets") or []
        if entry.get("event_id")
    }

    envelopes = [
        _action_envelope(
            action,
            event_map=event_map,
            media=media_map.get(str(action.get("id"))) or {},
            target_map=target_map,
            prior_events=events,
            tree_diff=tree_diff,
            event_artifact=event_artifact,
            action_artifact=action_artifact,
            locator_artifact=locator_artifact,
            media_artifact=media_artifact,
        )
        for action in actions
    ]
    graph = {
        "schema_version": SCHEMA_VERSION,
        "evidence_graph_version": EVIDENCE_GRAPH_VERSION,
        "materialized_at": datetime.now().isoformat(timespec="seconds"),
        "take": {
            "id": take.get("id"),
            "status": take.get("status"),
            "step": take.get("step") or {},
            "path": ".",
        },
        "source": {
            "artifact_fingerprint": artifact_fingerprint,
            "artifact_count": len(artifacts),
            "total_bytes": sum(item["size"] for item in artifacts),
            "artifacts": artifacts,
        },
        "observations": {
            "event_count": len(events),
            "event_ids": list(event_map),
            "tree_delta": {
                "scope": "take",
                "comparable": tree_diff.get("comparable"),
                "summary": tree_diff.get("summary") or {},
                "artifact": "ui/tree-diff.json",
            },
        },
        "capture_integrity": capture_integrity,
        "actions": envelopes,
        "coverage": _coverage(events, envelopes, artifacts),
        "policy": {
            "raw_evidence_immutable": True,
            "business_semantics_inferred_by_ai": True,
            "static_control_type_is_not_target_quality": True,
        },
    }
    graph["graph_fingerprint"] = _stable_hash({
        "source": artifact_fingerprint,
        "actions": envelopes,
        "policy": graph["policy"],
    })
    if write:
        output = (
            projection_dir / _GRAPH_PATH
            if projection_dir is not None
            else take_dir / _GRAPH_PATH
        )
        write_json_atomic(output, graph)
    return graph


def load_evidence_graph(take_dir, *, rebuild_if_stale=True):
    take_dir = Path(take_dir).resolve()
    from autowork_core.utils.debug_tools.recorder.projection_store import (
        PROJECTION_VERSION,
        ProjectionStore,
    )

    projection_store = ProjectionStore(take_dir)
    projection = projection_store.current()
    if projection is None:
        pointer = _read_json(projection_store.pointer_path)
        if pointer.get("projection_version") != PROJECTION_VERSION:
            raise ValueError(
                "当前 Recorder 只接受 Projection 5.7；旧 Run 需要使用"
                "旧版本或独立离线迁移工具"
            )
        from autowork_core.utils.debug_tools.recorder.timeline import (
            TimelineStore,
        )

        TimelineStore(take_dir).materialize()
        projection = projection_store.current()
        if projection is None:
            raise RuntimeError("Evidence Graph current projection 修复失败")
    path = projection.path("evidence_graph")
    graph = _read_json(path)
    if not graph or graph.get("evidence_graph_version") != EVIDENCE_GRAPH_VERSION:
        from autowork_core.utils.debug_tools.recorder.timeline import (
            TimelineStore,
        )

        TimelineStore(take_dir).materialize()
        repaired = projection_store.current()
        if repaired is None:
            raise RuntimeError("Evidence Graph 投影修复后 pointer 不存在")
        graph = _read_json(repaired.path("evidence_graph"))
        if graph.get("evidence_graph_version") == EVIDENCE_GRAPH_VERSION:
            return graph
        raise RuntimeError("Evidence Graph 投影修复失败")
    return graph


def _action_envelope(
        action,
        *,
        event_map,
        media,
        target_map,
        prior_events,
        tree_diff,
        event_artifact,
        action_artifact,
        locator_artifact,
        media_artifact,
):
    action = dict(action)
    event_ids = [str(value) for value in action.get("event_ids") or []]
    media_event_ids = [
        str(value)
        for value in action.get("media_event_ids") or event_ids
    ]
    action_events = [event_map[event_id] for event_id in media_event_ids if event_id in event_map]
    primary_event_id = action.get("target_event_id") or (
        event_ids[0] if event_ids else None
    )
    target_evidence = target_map.get(primary_event_id) or {}
    selected_candidate = target_evidence.get("selected_candidate") or {}
    target = action.get("target") or {}
    element = target.get("element") or target_evidence.get("element") or {}
    target_identity = _target_identity(
        target,
        element,
        selected_candidate,
        action_events,
        candidates=target_evidence.get("validated_candidates") or (),
    )
    text_change = (
        _text_change(action, action_events, prior_events, element)
        if action.get("type") == "keyboard"
        else None
    )
    media_evidence = _media_evidence(media)
    parameters = _action_parameters(action, action_events)
    canonical_action = build_canonical_action(
        {**action, "parameters": parameters},
        action_events,
        text_change,
    )
    state_delta = {
        "scope": "take",
        "available": bool(tree_diff),
        "comparable": tree_diff.get("comparable"),
        "summary": tree_diff.get("summary") or {},
        "artifact": "ui/tree-diff.json" if tree_diff else None,
    }
    closure = {
        "raw_events": len(action_events) == len(media_event_ids) and bool(media_event_ids),
        "target_observation": bool(element),
        "validated_locator": target_identity["locator_validation"] == "unique_target_match",
        "before_media": bool(media_evidence.get("before")),
        "after_media": bool(media_evidence.get("after")),
        "state_delta": bool(state_delta["available"] or text_change),
    }
    required = (
        "raw_events",
        "target_observation",
        "validated_locator",
        "before_media",
        "after_media",
    )
    missing = [name for name in required if not closure[name]]
    return {
        "action_id": action.get("id"),
        "source_action_id": action.get("source_action_id"),
        "source": action.get("source") or {"kind": "take"},
        "ordinal": action.get("ordinal"),
        "type": action.get("type"),
        "role": action.get("role", "business"),
        "event_ids": event_ids,
        "media_event_ids": media_event_ids,
        "target_event_id": primary_event_id,
        "commit_event_id": action.get("commit_event_id"),
        "value_binding": action.get("value_binding"),
        "note": action.get("note"),
        "parameters": parameters,
        "canonical_action": canonical_action,
        "time": {
            "start_ms": action.get("start_ms"),
            "end_ms": action.get("end_ms"),
        },
        "target": target_identity,
        "text_change": text_change,
        "media": media_evidence,
        "state_delta": state_delta,
        "closure": {
            **closure,
            "status": "complete" if not missing else "partial",
            "missing": missing,
        },
        "provenance": {
            "action_artifact": action_artifact,
            "event_artifact": event_artifact,
            "locator_artifact": locator_artifact,
            "media_artifact": media_artifact,
        },
    }


def _action_parameters(action, action_events):
    if action.get("type") == "click":
        control_type = str(
            ((action.get("target") or {}).get("element") or {}).get(
                "control_type"
            ) or ""
        ).casefold()
        if control_type not in {"document", "edit", "canvas", "custom"}:
            return {}
        point = action.get("start")
        rectangle = (
            ((action.get("target") or {}).get("element") or {}).get(
                "rectangle"
            )
        )
        if (
            not isinstance(point, (list, tuple))
            or len(point) != 2
            or not isinstance(rectangle, (list, tuple))
            or len(rectangle) != 4
            or not all(
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                for value in (*point, *rectangle)
            )
        ):
            return {}
        offset_x = int(point[0]) - int(rectangle[0])
        offset_y = int(point[1]) - int(rectangle[1])
        if not (
            0 <= offset_x < int(rectangle[2]) - int(rectangle[0])
            and 0 <= offset_y < int(rectangle[3]) - int(rectangle[1])
        ):
            return {}
        return {"offset_x": offset_x, "offset_y": offset_y}
    if action.get("type") == "drag":
        start = action.get("start")
        end = action.get("end")
        if not all((
            isinstance(start, (list, tuple)),
            isinstance(end, (list, tuple)),
            len(start) == 2,
            len(end) == 2,
            all(
                isinstance(item, (int, float)) and not isinstance(item, bool)
                for item in (*start, *end)
            ),
        )):
            return {"delta_x": None, "delta_y": None}
        return {
            "delta_x": int(end[0]) - int(start[0]),
            "delta_y": int(end[1]) - int(start[1]),
        }
    if action.get("type") != "scroll":
        return {}
    deltas = []
    for event in action_events:
        if event.get("event_type") != "mouse_wheel":
            continue
        delta = event.get("wheel_delta")
        if isinstance(delta, bool) or not isinstance(delta, (int, float)):
            return {"direction": None, "steps": None}
        delta = int(delta)
        if delta == 0:
            return {"direction": None, "steps": None}
        deltas.append(delta)
    directions = {"up" if delta > 0 else "down" for delta in deltas}
    if not deltas or len(directions) != 1:
        return {"direction": None, "steps": None}
    return {
        "direction": directions.pop(),
        "steps": sum(max(1, abs(delta) // 120) for delta in deltas),
    }


def _target_identity(target, element, candidate, events, *, candidates=()):
    control_type = str(element.get("control_type") or "")
    validation = candidate.get("validation") or {}
    locator = candidate.get("locator") or {}
    unique_match = (
        validation.get("status") == "unique"
        and validation.get("target_matches") is True
    )
    observed_identity = _observed_target_identity(events)
    same_observed_target = observed_identity["same_observed_target"]
    confidence = (
        "high"
        if unique_match and same_observed_target
        else "medium" if unique_match else "low"
    )
    result = {
        "structural_role": (
            "container_like"
            if control_type.casefold() in _CONTAINER_LIKE_TYPES
            else "leaf_like"
        ),
        "control_type": control_type,
        "element": element,
        "root_name": target.get("root_name"),
        "locator": locator or None,
        "locator_name": candidate.get("name"),
        "locator_strategy": _locator_strategy(candidate),
        "locator_stability": candidate.get("stability") or {},
        "locator_validation": "unique_target_match" if unique_match else validation.get("status") or "unvalidated",
        "locator_candidate_id": candidate.get("candidate_id"),
        "locator_candidates": [
            {
                "candidate_id": item.get("candidate_id"),
                "name": item.get("name"),
                "reason": item.get("reason"),
                "locator": dict(item.get("locator") or {}),
                "validation": dict(item.get("validation") or {}),
                "stability": dict(item.get("stability") or {}),
                "score": item.get("score"),
            }
            for item in candidates
            if isinstance(item, dict) and item.get("candidate_id")
        ],
        **observed_identity,
        "interaction_confidence": confidence,
        "signals": {
            "enabled": element.get("enabled"),
            "visible": element.get("visible"),
            "recorded_quality_label": target.get("quality"),
        },
    }
    result["target_fingerprint"] = _target_fingerprint(result)
    return result


def _target_fingerprint(target):
    target = target or {}
    locator = target.get("locator") or {}
    positional = str(locator.get("by") or "").casefold() == "pos"
    return "target-" + _stable_hash({
        "root_name": target.get("root_name"),
        "locator_name": target.get("locator_name"),
        "locator_strategy": target.get("locator_strategy"),
        "control_type": target.get("control_type"),
        "locator": _stable_target_locator(
            locator,
            preserve_position=positional,
        ),
    })[:24]


def _stable_target_locator(value, *, preserve_position=False):
    excluded = {
        "handle",
        "hwnd",
        "point",
        "process_id",
        "rectangle",
        "runtime_id",
    }
    if not preserve_position:
        excluded.add("coords")
    if isinstance(value, dict):
        return {
            str(key): _stable_target_locator(
                item,
                preserve_position=preserve_position,
            )
            for key, item in value.items()
            if str(key).casefold() not in excluded
        }
    if isinstance(value, (list, tuple)):
        return [
            _stable_target_locator(
                item,
                preserve_position=preserve_position,
            )
            for item in value
        ]
    return value


def _observed_target_identity(events):
    event_bound = [
        event
        for event in events
        if (
            ((event.get("details") or {}).get("target_binding") or {}).get(
                "status"
            ) in {"captured", "forensic_verified"}
            and (event.get("target") or {}).get("element")
        )
    ]
    if event_bound:
        events = event_bound
    observations = [
        (event.get("target") or {}).get("element") or {}
        for event in events
        if (event.get("target") or {}).get("element")
    ]
    runtime_identities = []
    native_identities = []
    runtime_identity_complete = bool(observations)
    native_identity_complete = bool(observations)
    for observed in observations:
        runtime_id = tuple(observed.get("runtime_id") or ())
        if runtime_id:
            runtime_identities.append(runtime_id)
        else:
            runtime_identity_complete = False
        handle = observed.get("handle")
        process_id = observed.get("process_id")
        if handle in (None, "", 0) or process_id in (None, ""):
            native_identity_complete = False
            continue
        try:
            native_identities.append((int(handle), int(process_id)))
        except (TypeError, ValueError):
            native_identity_complete = False
    same_runtime_target = (
        runtime_identity_complete
        and len(runtime_identities) == len(observations)
        and len(set(runtime_identities)) == 1
    )
    same_native_handle_target = (
        native_identity_complete
        and len(native_identities) == len(observations)
        and len(set(native_identities)) == 1
    )
    runtime_identity_present = bool(runtime_identities)
    identity_conflict = any((
        runtime_identity_present and not runtime_identity_complete,
        runtime_identity_complete and not same_runtime_target,
        runtime_identity_complete
        and native_identity_complete
        and not same_native_handle_target,
    ))
    same_observed_target = not identity_conflict and (
        same_runtime_target
        or (not runtime_identity_present and same_native_handle_target)
    )
    return {
        "same_runtime_target": same_runtime_target,
        "same_native_handle_target": same_native_handle_target,
        "same_observed_target": same_observed_target,
        "identity_basis": (
            "conflict"
            if identity_conflict
            else "runtime_id"
            if same_runtime_target
            else "hwnd_process" if same_native_handle_target else "unavailable"
        ),
    }


def _locator_strategy(candidate):
    reason = str((candidate or {}).get("reason") or "")
    return {
        "auto_id + control_type": "stable_auto_id",
        "name + control_type": "direct_name",
        "class_name + control_type": "direct_class",
        "same-row value context": "same_row_context",
        "same-row structural context": "same_row_context",
        "sibling label context": "sibling_label_context",
        "stable ancestor + sibling context": (
            "stable_ancestor_sibling"
        ),
        "stable ancestor + same-row context": (
            "stable_ancestor_same_row"
        ),
        "stable ancestor context": "stable_ancestor",
        "stable ancestor chain": "stable_ancestor_chain",
        "generated XPath; validate uniqueness before use": "direct_xpath",
        "generated zero-based XPath index fallback": "positional_fallback",
        "visible text OCR fallback; verify the region before use": "ocr_fallback",
        "coordinate fallback": "pos_fallback",
    }.get(reason, reason or None)


def _text_change(action, action_events, prior_events, element):
    first_ms = action.get("start_ms")
    target_key = _element_key(element)
    before_observations = []
    if first_ms is not None:
        before_observations = [
            observation
            for event in prior_events
            if event.get("monotonic_ms", 0) < first_ms
            and _element_key(((event.get("target") or {}).get("element") or {})) == target_key
            for observation in [_value_observation(event)]
            if observation is not None
        ]
    observations = [
        observation
        for event in action_events
        for observation in [_value_observation(event)]
        if observation is not None
    ]
    before_value = before_observations[-1]["value"] if before_observations else None
    after_commit = [
        observation
        for observation in observations
        if observation.get("phase") == "after_commit"
    ]
    after_value = (
        after_commit[-1]["value"]
        if after_commit
        else observations[-1]["value"] if observations else None
    )
    delta = _value_delta(before_value, after_value)
    key_downs = [
        event.get("key") or {}
        for event in action_events
        if event.get("event_type") == "key_down"
    ]
    return {
        "status": (
            "observed_boundaries"
            if before_value is not None and after_commit
            else "observed"
            if after_value is not None
            else "keys_only"
        ),
        "before_value": before_value,
        "after_value": after_value,
        "value_delta": delta,
        "key_sequence": [entry.get("name") for entry in key_downs],
        "value_binding": action.get("value_binding"),
        "observations": observations,
        "capture_note": (
            "Value observations are timestamped UIA snapshots; use action boundaries "
            "and latency before attributing intermediate values to individual keys."
        ),
    }


def _value_observation(event):
    target = event.get("target") or {}
    properties = target.get("element_properties") or {}
    for name in _VALUE_PROPERTY_NAMES:
        if name in properties and properties[name] is not None:
            return {
                "event_id": event.get("id"),
                "event_ms": event.get("monotonic_ms"),
                "evidence_ms": (event.get("details") or {}).get("evidence_monotonic_ms"),
                "latency_ms": (event.get("details") or {}).get("evidence_latency_ms"),
                "phase": (event.get("details") or {}).get(
                    "observation_phase"
                ),
                "property": name,
                "value": properties[name],
            }
    return None


def _value_delta(before, after):
    if before is None or after is None:
        return {"kind": "unknown", "value": None}
    before = str(before)
    after = str(after)
    if after.startswith(before):
        return {"kind": "append", "value": after[len(before):]}
    return {"kind": "replace", "value": after}


def _media_evidence(media):
    result = {}
    for name in ("before", "commit", "after_immediate", "after", "context"):
        value = media.get(name)
        if value:
            result[name] = value
    result["stability"] = media.get("stability") or {}
    result["outcome"] = media.get("outcome") or {}
    return result


def _coverage(events, envelopes, artifacts):
    action_count = len(envelopes)
    complete = sum(
        envelope.get("closure", {}).get("status") == "complete"
        for envelope in envelopes
    )
    event_ids = {
        str(event.get("id"))
        for event in events
        if event.get("id")
    }
    linked_ids = {
        event_id
        for envelope in envelopes
        for event_id in envelope.get("media_event_ids") or []
    }
    return {
        "integrity": {
            "status": "complete" if artifacts else "missing",
            "hashed_artifacts": len(artifacts),
            "unhashed_source_artifacts": [],
        },
        "events": {
            "total": len(event_ids),
            "linked_to_actions": len(event_ids & linked_ids),
            "unlinked": sorted(event_ids - linked_ids),
        },
        "actions": {
            "total": action_count,
            "complete_envelopes": complete,
            "partial_envelopes": action_count - complete,
        },
    }


def _artifact_manifest(
        take_dir,
        active_supplement_ids=(),
        projected_artifacts=(),
    ):
    active_supplement_ids = set(active_supplement_ids or ())
    artifacts = []
    for path in sorted(take_dir.rglob("*")):
        if not path.is_file() or path.suffix == ".tmp":
            continue
        relative = path.relative_to(take_dir)
        if relative.parts and relative.parts[0] in _EXCLUDED_SOURCE_ROOTS:
            continue
        if any(part in _EXCLUDED_SOURCE_PARTS for part in relative.parts):
            continue
        if relative.as_posix() in _EXCLUDED_SOURCE_NAMES:
            continue
        if (
            relative.parts
            and relative.parts[0] == "supplements"
            and (
                len(relative.parts) < 2
                or relative.parts[1] not in active_supplement_ids
            )
        ):
            continue
        content_hash, size = _hash_file(path)
        reference_hash = hashlib.sha256(
            f"{relative.as_posix()}|{content_hash}".encode("utf-8")
        ).hexdigest()
        artifacts.append({
            "artifact_id": f"artifact-{reference_hash[:16]}",
            "path": relative.as_posix(),
            "kind": _artifact_kind(relative),
            "sha256": content_hash,
            "size": size,
        })
    for logical_path, path in projected_artifacts:
        content_hash, size = _hash_file(path)
        reference_hash = hashlib.sha256(
            f"{logical_path}|{content_hash}".encode("utf-8")
        ).hexdigest()
        artifacts.append({
            "artifact_id": f"artifact-{reference_hash[:16]}",
            "path": str(logical_path),
            "kind": _artifact_kind(Path(logical_path)),
            "sha256": content_hash,
            "size": size,
        })
    return artifacts


def _hash_file(path):
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _active_supplement_ids(actions):
    return {
        str(source.get("supplement_id"))
        for action in actions or ()
        for source in [action.get("source") or {}]
        if source.get("kind") == "supplement"
        and source.get("supplement_id")
    }


def _artifact_fingerprint(artifacts):
    return _stable_hash([
        {
            "path": item["path"],
            "sha256": item["sha256"],
            "size": item["size"],
        }
        for item in artifacts
    ])


def _artifact_kind(path):
    name = path.name.casefold()
    if name == "raw-events.jsonl":
        return "raw_events"
    if name == "events.jsonl":
        return "canonical_events"
    if name == "events.effective.jsonl":
        return "effective_events"
    if path.suffix.casefold() == ".mp4":
        return "video"
    if path.suffix.casefold() in {".png", ".jpg", ".jpeg"}:
        return "image"
    if "tree" in name:
        return "ui_tree"
    if "locator" in name:
        return "locator_evidence"
    if "action" in name or "timeline" in name:
        return "action_projection"
    return "metadata"


def _read_actions(take_dir):
    path = resolve_take_artifact(
        take_dir,
        "actions_effective",
    )
    if path is None:
        raise ValueError(
            "Take 缺少有效 Projection 5.7 actions_effective；"
            "旧 Run 需要使用旧版本或独立离线迁移工具"
        )
    return list((_read_json(path).get("actions") or []))


def _read_json(path):
    path = Path(path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _read_jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    values = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            values.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return values


def _read_yaml(path):
    path = Path(path)
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}


def _element_key(element):
    runtime_id = tuple(element.get("runtime_id") or ())
    if runtime_id:
        return ("runtime", runtime_id)
    return (
        "properties",
        element.get("handle"),
        element.get("process_id"),
        element.get("control_type"),
        element.get("class_name"),
    )


def _stable_hash(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()