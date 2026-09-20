from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

from autowork_core.utils.debug_tools.recorder.evidence_compilation import (
    build_evidence_compilation_result,
    write_evidence_compilation_result,
)


def build_compilation_from_projection(take_dir, projection_snapshot):
    take_dir = Path(take_dir).resolve()
    classification = _classify_projection_evidence(
        take_dir,
        projection_snapshot,
    )
    return build_evidence_compilation_result(
        take_dir,
        status=classification["status"],
        compiled_artifacts=compiled_projection_artifacts(
            take_dir,
            projection_snapshot,
        ),
        issues=classification["issues"],
        review_required=classification["review_required"],
        hard_missing=classification["hard_missing"],
    )


def publish_compilation_from_projection(take_dir, projection_snapshot):
    take_dir = Path(take_dir).resolve()
    result = build_compilation_from_projection(take_dir, projection_snapshot)
    write_evidence_compilation_result(take_dir, result)
    return result


def publish_pending_compilation(take_dir, *, status="not_started"):
    result = build_evidence_compilation_result(
        take_dir,
        status=status,
    )
    write_evidence_compilation_result(take_dir, result)
    return result


def compile_take_evidence(take_dir):
    from autowork_core.utils.debug_tools.recorder.timeline import TimelineStore

    return TimelineStore(take_dir).materialize()


def _classify_projection_evidence(take_dir, projection_snapshot):
    issues = []
    review_required = []
    hard_missing = []
    try:
        actions = _load_projection_actions(projection_snapshot)
        locator_bundle = _load_projection_locator_bundle(projection_snapshot)
        tree_diff = _load_take_tree_diff(take_dir)
    except (OSError, ValueError, TypeError, json.JSONDecodeError, yaml.YAMLError) as error:
        return {
            "status": "failed",
            "issues": [{
                "code": "projection_read_failed",
                "message": f"{type(error).__name__}: {error}",
            }],
            "review_required": [],
            "hard_missing": [],
        }

    business_actions = [
        action for action in actions
        if str(action.get("role") or "business") != "noise"
    ]
    if not business_actions:
        hard_missing.append({
            "code": "no_recorded_actions",
            "message": "完成 Take 没有可生成的录制动作",
        })
    if tree_diff and tree_diff.get("comparable") is False:
        review_required.append({
            "code": "tree_not_comparable",
            "message": "Step 前后控件树不可比较",
        })

    targets = {
        str(target.get("event_id") or ""): target
        for target in locator_bundle.get("event_targets") or ()
        if isinstance(target, dict) and target.get("event_id")
    }
    for action in business_actions:
        action_id = str(action.get("id") or "")
        target_event_id = _action_target_event_id(action)
        if not target_event_id:
            review_required.append({
                "code": "target_event_missing",
                "action_id": action_id,
                "message": "动作缺少 target_event_id",
            })
            continue
        target = targets.get(target_event_id) or {}
        candidate = target.get("selected_candidate") or {}
        locator = candidate.get("locator") or {}
        validation = candidate.get("validation") or {}
        if not candidate:
            review_required.append({
                "code": "target_candidate_missing",
                "action_id": action_id,
                "event_id": target_event_id,
                "message": "动作缺少可用 target locator 候选",
            })
            continue
        if locator.get("by") in {"ocr", "pos"}:
            review_required.append({
                "code": f"fallback_{locator.get('by')}",
                "action_id": action_id,
                "event_id": target_event_id,
                "message": "动作依赖视觉或坐标 fallback",
            })
        if not (
                validation.get("status") == "unique"
                and validation.get("target_matches") is True
        ):
            review_required.append({
                "code": "weak_target_quality",
                "action_id": action_id,
                "event_id": target_event_id,
                "message": "动作缺少唯一且回指录制目标的结构定位证据",
            })

    issues.extend(hard_missing)
    issues.extend(review_required)
    status = (
        "hard_missing" if hard_missing
        else "forensic_review" if review_required
        else "verified"
    )
    return {
        "status": status,
        "issues": issues,
        "review_required": review_required,
        "hard_missing": hard_missing,
    }


def _load_projection_actions(projection_snapshot):
    path = projection_snapshot.path("actions_effective")
    if path is None or not path.is_file():
        raise ValueError("actions_effective artifact missing")
    value = json.loads(path.read_text(encoding="utf-8"))
    actions = value.get("actions") if isinstance(value, dict) else value
    if not isinstance(actions, list):
        raise ValueError("actions_effective artifact invalid")
    return [dict(item) for item in actions if isinstance(item, dict)]


def _load_projection_locator_bundle(projection_snapshot):
    path = projection_snapshot.path("locator_candidates_effective")
    if path is None or not path.is_file():
        raise ValueError("locator_candidates_effective artifact missing")
    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict):
        raise ValueError("locator_candidates_effective artifact invalid")
    return value


def _load_take_tree_diff(take_dir):
    path = Path(take_dir) / "ui" / "tree-diff.json"
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def _action_target_event_id(action):
    if action.get("target_event_id"):
        return str(action.get("target_event_id"))
    event_ids = action.get("event_ids") or ()
    return str(event_ids[0]) if event_ids else ""


def compiled_projection_artifacts(take_dir, projection_snapshot):
    take_dir = Path(take_dir).resolve()
    result = {}
    for key, _relative in sorted((projection_snapshot.artifacts or {}).items()):
        path = projection_snapshot.path(key)
        if path is None or not path.exists() or not path.is_file():
            continue
        resolved = path.resolve()
        try:
            relpath = resolved.relative_to(take_dir).as_posix()
        except ValueError as error:
            raise ValueError(f"编译产物越界: {path}") from error
        content = resolved.read_bytes()
        result[key] = {
            "path": relpath,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
        }
    return result