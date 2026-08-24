from __future__ import annotations


REPAIR_GUIDANCE = {
    "capture_error": {
        "title": "录制采集失败",
        "repair": "rerecord",
        "steps": (
            "查看下方具体异常和对应窗口。",
            "若窗口本应关闭，请在业务问题中确认这是预期结果。",
            "否则点击“补录当前 Step”，保持目标窗口有效后按 F10 保存。",
        ),
    },
    "tree_not_comparable": {
        "title": "前后控件树无法比较",
        "repair": "rerecord",
        "steps": (
            "检查 before/after 是否属于同一个主窗口。",
            "若关闭窗口是预期结果，应重录为窗口关闭生命周期。",
            "否则重录并在保存前保持主窗口存在。",
        ),
    },
    "no_recorded_actions": {
        "title": "没有可生成的动作",
        "repair": "rerecord",
        "steps": (
            "确认录制期间确实操作了业务窗口，且没有一直处于 F7 暂停。",
            "断言类 Step 可把鼠标放在目标控件上按 F9。",
            "重新录制后按 F10 保存。",
        ),
    },
    "orphan_mouse_boundary": {
        "title": "录制从半次点击开始",
        "repair": "timeline",
        "steps": (
            "打开录制内容并定位该操作。",
            "忽略不完整的点击；若它属于业务操作，则重新录制完整点击。",
        ),
    },
    "external_process_action": {
        "title": "动作来自范围外进程",
        "repair": "timeline",
        "steps": (
            "核对该操作是否属于目标业务窗口。",
            "无关操作直接选择“忽略错误动作”。",
            "确属业务窗口时使用 auto 模式重录，或在 strict 模式提前加入该窗口。",
        ),
    },
    "shell_transport_action": {
        "title": "录入了任务栏/系统切换动作",
        "repair": "timeline",
        "steps": (
            "打开录制内容并定位该操作。",
            "若只是切换窗口且不需要生成代码，直接忽略该动作。",
        ),
    },
    "weak_step_semantics": {
        "title": "Step 的业务含义不够明确",
        "repair": "reconcile",
        "steps": (
            "交给 Copilot 核对现有录制和代码。",
            "只有业务含义无法确定时，Copilot 才会向你确认。",
        ),
    },
    "weak_target_quality": {
        "title": "动作只命中容器或弱目标",
        "repair": "reconcile",
        "steps": (
            "查看对应事件截图并确认目标是否正确。",
            "目标错误时重录；目标正确时交给 Copilot 选择安全定位方式。",
        ),
    },
    "fallback_ocr": {
        "title": "动作依赖 OCR 定位",
        "repair": "reconcile",
        "steps": (
            "查看事件截图和 OCR Region。",
            "优先由 Copilot 使用稳定控件定位；确实只能识别图像时再由你授权。",
        ),
    },
    "fallback_pos": {
        "title": "动作依赖坐标定位",
        "repair": "reconcile",
        "steps": (
            "确认四值坐标来源分辨率正确。",
            "优先由 Copilot寻找稳定定位；确实只能使用坐标时再由你确认风险。",
        ),
    },
    "positional_locator_unstable": {
        "title": "动作依赖位置 XPath",
        "repair": "reconcile",
        "steps": (
            "检查命名 UI 树和现有代码是否已有稳定业务定位。",
            "没有稳定定位时补录或修复 locator；不能直接发布录制索引。",
        ),
    },
    "provisional_window": {
        "title": "发现未预选的跨进程窗口",
        "repair": "reconcile",
        "steps": (
            "核对窗口标题、PID 和首次出现时间。",
            "无法自动判断时，Copilot 会询问它属于业务流程还是无关窗口。",
            "若无关，在录制内容中忽略该窗口的操作。",
        ),
    },
    "window_closed_during_take": {
        "title": "窗口在 Step 中关闭",
        "repair": "reconcile",
        "steps": (
            "核对窗口标题和最后出现时间。",
            "无法自动判断时，Copilot 会询问是预期关闭、流程切换还是意外关闭。",
            "只有意外关闭需要重录。",
        ),
    },
    "pause_state_changed": {
        "title": "F7 暂停期间窗口状态变化",
        "repair": "reconcile",
        "steps": (
            "查看 pause tree diff 和截图。",
            "无法自动判断时，Copilot 会询问它是准备操作、业务动作还是无关变化。",
        ),
    },
    "unsupported_drag": {
        "title": "拖拽动作需要确认",
        "repair": "reconcile",
        "steps": ("确认拖拽起点、终点和业务含义，通常需要 Page Object 方法。",),
    },
    "drag_parameters_unavailable": {
        "title": "拖拽位移证据不完整",
        "repair": "recapture",
        "steps": ("重新录制拖拽，确保完整捕获鼠标按下、移动和释放。",),
    },
    "unsupported_middle_click": {
        "title": "中键动作需要确认",
        "repair": "reconcile",
        "steps": ("确认中键是否是业务动作，并决定是否实现专用 Page Object 方法。",),
    },
    "unsupported_scroll": {
        "title": "滚动动作需要确认",
        "repair": "reconcile",
        "steps": ("确认滚动方向、步数和停止条件。",),
    },
    "dynamic_root_variants": {
        "title": "动态标题拆分出多个 Root",
        "repair": "timeline",
        "steps": ("检查 Root 草稿，使用稳定 class_name/auto_id/title_re 合并定位。",),
    },
}


def build_step_diagnostics(readiness, session, step_id):
    step = next(
        (item for item in session.selected_steps if item.id == step_id),
        None,
    )
    state = session.step_states.get(step_id) or {}
    selected_take = state.get("selected_take")
    take = next(
        (
            item
            for item in state.get("takes", [])
            if item.get("id") == selected_take
        ),
        None,
    )
    take_dir = session.session_dir / take["path"] if take else None
    result = []
    for review in readiness.get("review_required") or []:
        if review.get("step_id") not in (None, step_id):
            continue
        code = str(review.get("code") or "unknown")
        guidance = REPAIR_GUIDANCE.get(code, {
            "title": review.get("message") or code,
            "repair": "rerecord",
            "steps": ("打开证据文件确认问题；无法补齐事实时重录该 Step。",),
        })
        location = _evidence_location(review.get("evidence"), take_dir)
        recovery = review.get("recovery") or {}
        repair = guidance["repair"]
        repair_steps = list(guidance["steps"])
        if recovery.get("status") == "ai_recoverable":
            repair = "reconcile"
            inventory = recovery.get("inventory") or {}
            repair_steps = [
                "直接交给 Copilot，它会自动核对现有录制证据。",
                (
                    "AI 将交叉分析 "
                    f"{inventory.get('effective_action_count', 0)} 个有效动作、"
                    f"{inventory.get('raw_event_count', 0)} 个事件、"
                    f"{inventory.get('event_screenshot_count', 0)} 张关键帧、"
                    f"视频={'有' if inventory.get('video') else '无'}。"
                ),
                "只有业务含义无法从证据确定时才需要你确认；无需先重录。",
            ]
        elif recovery.get("hard_blocker"):
            repair = "rerecord"
            repair_steps = [
                "必要的操作和画面证据同时不足，无法可靠恢复。",
                "只补录当前 Step，不需要重录整个 Scenario。",
            ]
        result.append({
            "step_id": step_id,
            "step": (
                f"{step.ordinal}. {step.keyword} {step.text}"
                if step is not None
                else step_id
            ),
            "take_id": selected_take,
            "take_path": str(take_dir) if take_dir else None,
            "code": code,
            "title": guidance["title"],
            "message": review.get("message") or "",
            "blocking": bool(review.get("blocking", True)),
            "repair": repair,
            "location": location,
            "evidence": review.get("evidence"),
            "repair_steps": repair_steps,
            "recovery": recovery,
        })
    return result


def format_step_diagnostics(diagnostics):
    if not diagnostics:
        return "当前 Step 没有阻塞项。"
    lines = []
    for index, item in enumerate(diagnostics, start=1):
        lines.append(f"{index}. [{item['code']}] {item['title']}")
        lines.append(f"   Step: {item['step']}")
        if item.get("location"):
            lines.append(f"   位置: {item['location']}")
        if item.get("message"):
            lines.append(f"   原因: {item['message']}")
        lines.append(f"   修复类型: {_repair_label(item['repair'])}")
        recovery = item.get("recovery") or {}
        if recovery.get("status") == "ai_recoverable":
            lines.append(
                "   AI 恢复: 可恢复，置信度="
                f"{recovery.get('confidence', 'unknown')}"
            )
            lines.append(f"   恢复依据: {recovery.get('reason', '')}")
        elif recovery.get("hard_blocker"):
            lines.append("   AI 恢复: 核心证据不足，仅需最小补录当前 Step")
        for repair_index, step in enumerate(item.get("repair_steps") or (), start=1):
            lines.append(f"   {repair_index}) {step}")
    return "\n".join(lines)


def format_user_step_diagnostic(diagnostic):
    if not isinstance(diagnostic, dict):
        return "当前Step没有需要你处理的问题。"
    lines = [user_diagnostic_title(diagnostic)]
    if diagnostic.get("step"):
        lines.append(f"Step：{diagnostic['step']}")
    repair = diagnostic.get("repair")
    if repair == "reconcile":
        lines.append("处理：Copilot会自动核对现有录制，你无需处理技术细节。")
    elif repair == "timeline":
        lines.append("处理：打开录制内容，确认并忽略误录操作。")
    elif repair in {"rerecord", "recapture"}:
        lines.append("处理：只补录当前Step，其他录制不会受影响。")
    else:
        lines.append("处理：按主按钮检查当前录制内容。")
    return "\n".join(lines)


def user_diagnostic_title(diagnostic):
    repair = diagnostic.get("repair") if isinstance(diagnostic, dict) else None
    return {
        "reconcile": "有录制事实需要 Copilot 核对",
        "timeline": "录制内容中有操作需要确认",
        "rerecord": "当前 Step 的录制内容不完整",
        "recapture": "当前 Step 的录制内容不完整",
    }.get(repair, "录制内容需要检查")


def user_evidence_location(diagnostic):
    event_ids = diagnostic_event_ids([diagnostic])
    if event_ids:
        return "当前录制版本中的对应操作"
    if diagnostic.get("evidence"):
        return "当前录制版本的录制证据"
    return "当前录制版本"


def diagnostic_event_ids(diagnostics):
    result = []
    for item in diagnostics:
        evidence = item.get("evidence")
        event_id = evidence.get("event_id") if isinstance(evidence, dict) else evidence
        if isinstance(event_id, str) and event_id.startswith("event-"):
            result.append(event_id)
    return list(dict.fromkeys(result))


def _evidence_location(evidence, take_dir):
    take_text = str(take_dir) if take_dir else "当前 Take"
    if evidence is None:
        return take_text
    if isinstance(evidence, str):
        if evidence.startswith("event-"):
            return f"{take_text} / {evidence}"
        if "screenshot" in evidence.casefold() or "tree" in evidence.casefold():
            return f"{take_text} / {evidence}"
        return evidence
    if isinstance(evidence, dict):
        event_id = evidence.get("event_id")
        window = evidence.get("title") or evidence.get("class_name")
        handle = evidence.get("handle")
        pause_id = evidence.get("pause_id")
        parts = [take_text]
        if event_id:
            parts.append(str(event_id))
        if pause_id:
            parts.append(str(pause_id))
        if window:
            parts.append(f"窗口={window}")
        if handle:
            parts.append(f"HWND={handle}")
        if evidence.get("tree_diff"):
            parts.append(str(evidence["tree_diff"]))
        if evidence.get("target_process_ids"):
            parts.append(f"目标PID={evidence['target_process_ids']}")
        if evidence.get("event_process_id"):
            parts.append(f"事件PID={evidence['event_process_id']}")
        return " / ".join(parts)
    if isinstance(evidence, (list, tuple)):
        return f"{take_text} / {', '.join(str(item) for item in evidence)}"
    return f"{take_text} / {evidence}"


def _repair_label(repair):
    return {
        "timeline": "校正时间线",
        "reconcile": "AI 证据协调 / 结构化决策",
        "rerecord": "补录或重录",
    }.get(repair, str(repair))
