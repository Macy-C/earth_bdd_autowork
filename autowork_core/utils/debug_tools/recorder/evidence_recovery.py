from __future__ import annotations

import json
from pathlib import Path

from autowork_core.utils.debug_tools.recorder.projection_store import (
    resolve_take_artifact,
)


RECONCILABLE_REVIEW_CODES = {
    "weak_step_semantics",
    "fallback_ocr",
    "fallback_pos",
    "weak_target_quality",
    "pause_state_changed",
    "provisional_window",
    "window_closed_during_take",
    "unsupported_drag",
    "unsupported_middle_click",
    "unsupported_scroll",
}
TIMELINE_REPAIR_CODES = {
    "orphan_mouse_boundary",
    "external_process_action",
    "shell_transport_action",
    "dynamic_root_variants",
}
EVIDENCE_RECOVERY_CODES = {
    "capture_error",
    "drag_parameters_unavailable",
    "tree_not_comparable",
    "no_recorded_actions",
}


def enrich_review_recovery(session_dir, steps, reviews):
    session_dir = Path(session_dir).resolve()
    step_by_id = {
        (entry.get("step") or {}).get("id"): entry
        for entry in steps or []
    }
    for review in reviews:
        if review.get("recovery"):
            continue
        step = step_by_id.get(review.get("step_id"))
        take_dir = None
        step_plan = {}
        if step:
            step_plan = step.get("step") or {}
            take_path = (step.get("artifacts") or {}).get("take")
            if take_path:
                take_dir = session_dir / take_path
        review["recovery"] = assess_review_recovery(
            take_dir,
            step_plan,
            review,
        )
    return reviews


def enrich_request_recovery(request, session_dir):
    evidence_by_step = {
        (entry.get("step") or {}).get("id"): entry
        for entry in request.get("evidence") or []
    }
    steps = []
    for target in (request.get("target") or {}).get("steps") or []:
        evidence = evidence_by_step.get(target.get("id")) or {}
        steps.append({
            "step": target,
            "artifacts": evidence.get("artifacts") or {},
        })
    reviews = (request.get("readiness") or {}).get("target_review_required") or []
    enrich_review_recovery(session_dir, steps, reviews)
    return request


def assess_review_recovery(take_dir, step_plan, review):
    code = str(review.get("code") or "unknown")
    if code in RECONCILABLE_REVIEW_CODES:
        return {
            "status": "user_confirmable",
            "confidence": "high",
            "strategy": "semantic_reconciliation",
            "hard_blocker": False,
            "reason": "事实证据存在，缺少的是业务判断，可由 Decision Pack 一次确认。",
            "inventory": collect_evidence_inventory(take_dir),
        }
    if code in TIMELINE_REPAIR_CODES:
        return {
            "status": "timeline_repairable",
            "confidence": "high",
            "strategy": "timeline_or_ai_review",
            "hard_blocker": False,
            "reason": "原始事件仍在，可由 AI 建议排除/改角色，或由用户在时间线确认。",
            "inventory": collect_evidence_inventory(take_dir),
        }

    inventory = collect_evidence_inventory(take_dir)
    if code not in EVIDENCE_RECOVERY_CODES:
        return {
            "status": "hard_missing",
            "confidence": "low",
            "strategy": "manual_repair",
            "hard_blocker": True,
            "reason": "该问题尚无受支持的自动恢复策略。",
            "inventory": inventory,
        }

    structured = bool(
        inventory["effective_action_count"]
        or inventory["automatic_action_count"]
        or inventory["raw_event_count"]
    )
    visual = bool(
        inventory["event_screenshot_count"]
        or inventory["contact_sheet"]
        or inventory["video"]
        or inventory["before_screenshot"]
        or inventory["after_screenshot"]
    )
    expected_close = _expected_close_signal(take_dir, inventory)

    if code == "no_recorded_actions":
        recoverable = bool(inventory["raw_event_count"] and visual)
    else:
        recoverable = structured and visual

    if not recoverable:
        return {
            "status": "hard_missing",
            "confidence": "low",
            "strategy": "rerecord_minimal_step",
            "hard_blocker": True,
            "reason": (
                "结构化事件/动作与可视证据不足，AI 无法还原可验证事实。"
                "只需重录当前 Step，不需要重录整个 Scenario。"
            ),
            "inventory": inventory,
        }

    confidence = "high" if inventory["effective_action_count"] and (
        inventory["event_screenshot_count"] or inventory["video"]
    ) else "medium"
    strategy = (
        "confirm_expected_window_close"
        if expected_close
        else "reconstruct_from_structured_and_media_evidence"
    )
    reason = (
        "检测到最后业务动作与窗口关闭一致，且事件、关键帧或视频仍完整；"
        "可由 AI 推断后让用户一次确认。"
        if expected_close
        else "动作/事件与可视媒体仍在，可由 V3 协调并在 Decision Pack 中确认缺失事实。"
    )
    return {
        "status": "ai_recoverable",
        "confidence": confidence,
        "strategy": strategy,
        "hard_blocker": False,
        "reason": reason,
        "inventory": inventory,
        "suggested_resolution": (
            "expected_window_close" if expected_close else "ai_reconstruct"
        ),
    }


def collect_evidence_inventory(take_dir):
    result = {
        "take": str(take_dir) if take_dir else None,
        "raw_event_count": 0,
        "automatic_action_count": 0,
        "effective_action_count": 0,
        "event_screenshot_count": 0,
        "before_screenshot": False,
        "after_screenshot": False,
        "contact_sheet": False,
        "video": False,
        "before_tree": False,
        "after_tree": False,
        "tree_diff": False,
        "window_lifecycle_count": 0,
        "last_action": None,
        "sources": [],
    }
    if take_dir is None:
        return result
    take_dir = Path(take_dir)
    if not take_dir.exists():
        return result

    events_path = take_dir / "events.jsonl"
    if events_path.exists():
        result["raw_event_count"] = sum(
            1 for line in events_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
        result["sources"].append("events.jsonl")

    for key, path in (
        ("automatic_action_count", take_dir / "actions.auto.json"),
        (
            "effective_action_count",
            resolve_take_artifact(
                take_dir,
                "actions_effective",
                "actions.effective.json",
            ),
        ),
    ):
        value = _read_json(path)
        actions = value.get("actions") or []
        result[key] = len(actions)
        if actions:
            result["sources"].append(path.relative_to(take_dir).as_posix())
            if key == "effective_action_count":
                result["last_action"] = _action_summary(actions[-1])

    media_path = resolve_take_artifact(
        take_dir,
        "media_index",
        "media-index.json",
    )
    media = _read_json(media_path)
    for event in media.get("events") or []:
        screenshot = event.get("screenshot")
        if screenshot and (take_dir / screenshot).exists():
            result["event_screenshot_count"] += 1
    if media:
        result["sources"].append(media_path.relative_to(take_dir).as_posix())

    file_flags = {
        "before_screenshot": "screenshots/before.png",
        "after_screenshot": "screenshots/after.png",
        "contact_sheet": "contact-sheet.png",
        "video": "step.mp4",
        "before_tree": "ui/before-tree.json",
        "after_tree": "ui/after-tree.json",
        "tree_diff": "ui/tree-diff.json",
    }
    for key, relative in file_flags.items():
        exists = (take_dir / relative).exists()
        result[key] = exists
        if exists:
            result["sources"].append(relative)

    take = _read_json(take_dir / "take.json")
    result["window_lifecycle_count"] = len(take.get("window_lifecycle") or [])
    return result


def is_hard_recovery_blocker(review):
    recovery = review.get("recovery") or {}
    return bool(recovery.get("hard_blocker", True))


def write_request_recovery_report(session_dir, request):
    session_dir = Path(session_dir).resolve()
    enrich_request_recovery(request, session_dir)
    request_id = request.get("request_id") or "request"
    path = session_dir / "ai" / "recovery" / f"{request_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "schema_version": request.get("schema_version"),
        "request_id": request_id,
        "items": (request.get("readiness") or {}).get("target_review_required") or [],
        "instructions": [
            "Use existing structured actions/events first.",
            "Use event screenshots and contact sheet before video.",
            "Extract video frames only where static evidence is insufficient.",
            "Ask the user only for business confirmation that evidence cannot prove.",
            "Request re-recording only for items whose recovery.hard_blocker is true.",
        ],
    }
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    request["recovery_report"] = path.relative_to(session_dir).as_posix()
    return path


def _expected_close_signal(take_dir, inventory):
    last_action = inventory.get("last_action") or {}
    target_text = " ".join(str(value or "") for value in (
        last_action.get("target_name"),
        last_action.get("target_auto_id"),
        last_action.get("target_control_type"),
    )).casefold()
    close_words = ("close", "关闭", "dismiss", "exit", "退出", "cancel", "取消")
    action_says_close = any(word in target_text for word in close_words)
    take = _read_json(Path(take_dir) / "take.json") if take_dir else {}
    error_text = str(take.get("capture_error") or "").casefold()
    window_unavailable = any(value in error_text for value in (
        "invalid window handle",
        "无效的窗口句柄",
        "getwindowrect",
        "window closed",
    )) or any(
        item.get("closed_during_take")
        for item in take.get("window_lifecycle") or []
    )
    return action_says_close and window_unavailable


def _action_summary(action):
    target = action.get("target") or {}
    element = target.get("element") or {}
    return {
        "id": action.get("id"),
        "type": action.get("type"),
        "role": action.get("role"),
        "event_ids": action.get("event_ids") or [],
        "target_name": element.get("name"),
        "target_auto_id": element.get("auto_id"),
        "target_control_type": element.get("control_type"),
    }


def _read_json(path):
    path = Path(path)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}
