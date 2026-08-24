import re
import time
from autowork_core.actions.action_helper import _do_click, _first_or_self, _is_coords_target, _is_spec
from autowork_core.common.compile import CompiledLocator, compile_locator
from autowork_core.common.element_finder import get_element, get_elements
from autowork_core.common.log_helper import log_call
from autowork_core.common.locator import _bind_runtime_root_if_needed, _first_region_target
from autowork_core.common.ocr_engine import (
    match_ocr_candidates,
    normalize_ocr_criteria,
    save_ocr_debug_image as _save_ocr_debug_image,
    scan_ocr,
    wait_ocr_text_absent as _wait_ocr_text_absent,
    wait_ocr_text_present as _wait_ocr_text_present,
)
from autowork_core.common.pic_engine import (
    get_pic_region as _get_pic_region,
    save_pic_debug_image as _save_pic_debug_image,
    wait_pic_absent as _wait_pic_absent,
    wait_pic_present as _wait_pic_present,
)
from autowork_core.common.wait_coordinator import poll_boolean
from autowork_core.utils.bus import normalize
from autowork_core.utils.visual_marker import mark_visual_target
from config.settings import settings


def _to_int(value, default=0):
    try:
        return int(float(value))
    except Exception:
        return default


def _to_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


_RELATIVE_DIRECTION_MAP = {
    "right": "right",
    "右": "right",
    "右侧": "right",
    "left": "left",
    "左": "left",
    "左侧": "left",
    "down": "down",
    "below": "down",
    "下": "down",
    "下方": "down",
    "up": "up",
    "above": "up",
    "上": "up",
    "上方": "up",
    "center": "center",
    "middle": "center",
    "中间": "center",
}


def _relative_options(direction="right", offset=20):
    direction_text = str(direction or "right").strip().lower()
    direction = _RELATIVE_DIRECTION_MAP.get(direction_text, direction_text)
    return direction, _to_int(offset, 20)


def _region_root_name(region):
    if region is None:
        return None
    if isinstance(region, CompiledLocator):
        return region.name
    if isinstance(region, str):
        return region
    return None


def _effective_region(context, compiled, region):
    if region is not None:
        return region
    region_name = getattr(compiled, "region_name", None)
    if not region_name:
        return None
    region_locator = context.autowork_feature.locators.get(region_name)
    if region_locator is None:
        raise KeyError(f"region 引用了不存在的 locator: {region_name}")
    return region_locator


def _visual_anchor_raw(compiled, prefix):
    data = dict(compiled.criteria) if isinstance(compiled.criteria, dict) else {}
    data["by"] = prefix

    if prefix == "ocr" and "value" not in data:
        data["value"] = compiled.criteria
    elif prefix == "pic" and "file" not in data:
        data["file"] = compiled.criteria

    return data


def _compile_visual_anchor(context, criteria, prefix, region=None):
    if isinstance(criteria, CompiledLocator):
        compiled = criteria
    elif isinstance(criteria, dict) and ("root" in criteria or "by" in criteria):
        compiled = compile_locator(criteria)
    else:
        value_key = "value" if prefix == "ocr" else "file"
        compiled = compile_locator({"by": prefix, value_key: criteria})

    if compiled.prefix != prefix:
        raise ValueError(f"相对定位锚点只支持 {prefix} locator: {compiled}")

    if region is not None:
        root_name = _region_root_name(region)
        if not root_name:
            raise ValueError("相对定位 region 需要传命名 locator")
        data = _visual_anchor_raw(compiled, prefix)
        data["region"] = root_name
        compiled = compile_locator(data)

    return _bind_runtime_root_if_needed(context, compiled)


def _compile_visual_locator(context, criteria, prefix, region=None):
    if isinstance(criteria, CompiledLocator):
        compiled = criteria
    elif isinstance(criteria, dict) and ("root" in criteria or "by" in criteria):
        compiled = compile_locator(criteria)
    else:
        return None

    if compiled.prefix != prefix:
        raise ValueError(f"视觉动作只支持 {prefix} locator: {compiled}")

    if region is not None:
        root_name = _region_root_name(region)
        if not root_name:
            return None
        data = _visual_anchor_raw(compiled, prefix)
        data["region"] = root_name
        compiled = compile_locator(data)

    return _bind_runtime_root_if_needed(context, compiled)


def _candidate_from_target(target):
    if _is_coords_target(target) and len(target) >= 3 and isinstance(target[2], dict):
        return target[2]
    return None


def _find_visual_locator(context, criteria, prefix, timeout=None, region=None, required=False, entry_point=None):
    compiled = _compile_visual_locator(context, criteria, prefix, region=region)
    if compiled is None:
        return False, None
    target = get_element(
        context,
        compiled,
        visual_timeout=15 if timeout is None else timeout,
        wait_type=None,
        required=required,
        entry_point=entry_point,
    )
    return True, target


def _wait_visual_locator_absent(context, criteria, prefix, timeout=None, interval=None, region=None, entry_point=None):
    compiled = _compile_visual_locator(context, criteria, prefix, region=region)
    if compiled is None:
        return None

    timeout_value = _to_float(timeout, getattr(settings, f"{prefix}_timeout", 15))
    interval_value = _to_float(interval, getattr(settings, f"{prefix}_interval", 0.5))

    def probe():
        return get_element(
            context,
            compiled,
            visual_timeout=0,
            wait_type=None,
            required=False,
            entry_point=entry_point,
        ) is None

    return poll_boolean(
        probe,
        timeout=timeout_value,
        interval=interval_value,
        monotonic=time.monotonic,
        sleep=time.sleep,
    )


def _relative_from_target(target, source, direction, offset):
    target = _first_or_self(target)
    candidate = None

    if _is_coords_target(target):
        point = target[0]
        candidate = target[2] if len(target) >= 3 and isinstance(target[2], dict) else None
        if candidate and candidate.get("abs_bounds"):
            left, top, right, bottom = candidate["abs_bounds"]
        else:
            x, y = point
            left = right = x
            top = bottom = y
        center_x, center_y = candidate.get("center", point) if candidate else point
    else:
        if _is_spec(target):
            target = target.wrapper_object()
        rect = target.rectangle()
        left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom
        center_x = (left + right) / 2
        center_y = (top + bottom) / 2

    if direction == "right":
        coords = (int(right + offset), int(center_y))
    elif direction == "left":
        coords = (int(left - offset), int(center_y))
    elif direction == "down":
        coords = (int(center_x), int(bottom + offset))
    elif direction == "up":
        coords = (int(center_x), int(top - offset))
    elif direction == "center":
        coords = (int(center_x), int(center_y))
    else:
        raise ValueError(f"不支持的相对方向: {direction}")

    return coords, source, candidate


def _get_relative_position(context, criteria, prefix, source, direction="right", offset=20,
                           timeout=None, region=None, entry_point=None):
    anchor = _compile_visual_anchor(context, criteria, prefix, region=region)
    target = get_element(
        context,
        anchor,
        visual_timeout=15 if timeout is None else timeout,
        wait_type=None,
        required=True,
        entry_point=entry_point,
    )
    direction, offset = _relative_options(direction, offset)
    result = _relative_from_target(target, source, direction, offset)
    if settings.draw_outline:
        mark_visual_target(result)
    return result


def _resolve_monitor(context, region=None, timeout=5, entry_point=None):
    timeout = 5 if timeout is None else timeout
    if region is None:
        return None

    if isinstance(region, dict) and {"left", "top", "width", "height"}.issubset(region):
        return {
            "left": _to_int(region.get("left"), 0),
            "top": _to_int(region.get("top"), 0),
            "width": _to_int(region.get("width"), 0),
            "height": _to_int(region.get("height"), 0),
        }

    if isinstance(region, (tuple, list)) and len(region) == 4:
        left, top, width, height = region
        return {
            "left": _to_int(left),
            "top": _to_int(top),
            "width": _to_int(width),
            "height": _to_int(height),
        }

    element = get_elements(
        context,
        region,
        visual_timeout=timeout,
        wait_type=None,
        entry_point=entry_point,
    )
    element = _first_region_target(element, region)

    if _is_coords_target(element):
        x, y = element[0]
        return {"left": int(x) - 30, "top": int(y) - 15, "width": 60, "height": 30}

    if _is_spec(element):
        element = element.wrapper_object()

    rect = element.rectangle()
    return {
        "left": rect.left,
        "top": rect.top,
        "width": rect.right - rect.left,
        "height": rect.bottom - rect.top,
    }


def _split_inline_pic_target(criteria):
    if not isinstance(criteria, str) or "|" not in criteria:
        return criteria, None

    root_text, pic_text = criteria.split("|", 1)
    root_text = root_text.strip()
    pic_text = pic_text.strip()
    if not root_text or not pic_text:
        return criteria, None

    if "=" in root_text:
        separator = "="
    elif ":" in root_text:
        separator = ":"
    else:
        return criteria, None

    root_key, root_value = root_text.split(separator, 1)
    if normalize(root_key) not in ("root", "region"):
        return criteria, None

    pic_separator_index = -1
    for separator in ("=", ":"):
        index = pic_text.find(separator)
        if index != -1 and (pic_separator_index == -1 or index < pic_separator_index):
            pic_separator_index = index

    if pic_separator_index != -1:
        pic_key = normalize(pic_text[:pic_separator_index])
        if pic_key in ("pic", "picture", "image", "file", "value"):
            pic_text = pic_text[pic_separator_index + 1:].strip()

    return pic_text, root_value.strip()


def _resolve_pic_target(context, criteria, region=None, timeout=5, entry_point=None):
    if isinstance(criteria, CompiledLocator):
        if criteria.prefix != "pic":
            raise ValueError(f"图片动作只支持 pic locator: {criteria}")
        region = _effective_region(context, criteria, region)
        return criteria.criteria, _resolve_monitor(
            context,
            region=region,
            timeout=timeout,
            entry_point=entry_point,
        )

    if isinstance(criteria, dict) and ("root" in criteria or "by" in criteria):
        compiled = compile_locator(criteria)
        if compiled.prefix == "pic":
            region = _effective_region(context, compiled, region)
            return compiled.criteria, _resolve_monitor(
                context,
                region=region,
                timeout=timeout,
                entry_point=entry_point,
            )

    criteria, inline_region = _split_inline_pic_target(criteria)
    region = region or inline_region
    monitor = _resolve_monitor(context, region=region, timeout=timeout, entry_point=entry_point)
    return criteria, monitor


def _resolve_ocr_target(context, criteria, region=None, timeout=5, entry_point=None):
    if isinstance(criteria, CompiledLocator):
        if criteria.prefix != "ocr":
            raise ValueError(f"OCR 动作只支持 ocr locator: {criteria}")
        region = _effective_region(context, criteria, region)
        return criteria.criteria, _resolve_monitor(
            context,
            region=region,
            timeout=timeout,
            entry_point=entry_point,
        )

    if isinstance(criteria, dict) and ("root" in criteria or "by" in criteria):
        compiled = compile_locator(criteria)
        if compiled.prefix == "ocr":
            region = _effective_region(context, compiled, region)
            return compiled.criteria, _resolve_monitor(
                context,
                region=region,
                timeout=timeout,
                entry_point=entry_point,
            )

    monitor = _resolve_monitor(context, region=region, timeout=timeout, entry_point=entry_point)
    return criteria, monitor


def save_ocr_debug_image(context, image=None, monitor=None, candidates=None, target_text=None,
                         reason="ocr_debug", region=None, entry_point=None):
    entry_point = log_call(entry_point, target_text=target_text, reason=reason, region=region)
    monitor = monitor or _resolve_monitor(context, region=region, entry_point=entry_point)
    return _save_ocr_debug_image(
        image=image,
        monitor=monitor,
        candidates=candidates,
        target_text=target_text,
        reason=reason,
    )


def get_ocr_text(context, region=None, timeout=None, joiner=" ", use_cache=False, entry_point=None):
    entry_point = log_call(
        entry_point,
        region=region,
        timeout=timeout,
        joiner=joiner,
        use_cache=use_cache,
    )
    monitor = _resolve_monitor(context, region=region, timeout=timeout, entry_point=entry_point)
    _image, _monitor, candidates = scan_ocr(
        monitor=monitor,
        use_cache=use_cache,
    )
    texts = [
        str(candidate.get("text", "")).strip()
        for candidate in candidates or []
        if str(candidate.get("text", "")).strip()
    ]
    return str(joiner).join(texts)


def extract_ocr_regex(
        context,
        pattern,
        region=None,
        timeout=None,
        flags=re.IGNORECASE,
        required=True,
        joiner=" ",
        use_cache=False,
        entry_point=None,
):
    entry_point = log_call(
        entry_point,
        pattern=pattern,
        region=region,
        timeout=timeout,
        required=required,
        joiner=joiner,
        use_cache=use_cache,
    )
    text = get_ocr_text(
        context,
        region=region,
        timeout=timeout,
        joiner=joiner,
        use_cache=use_cache,
        entry_point=entry_point,
    )

    if hasattr(pattern, "search"):
        match = pattern.search(text)
        pattern_text = getattr(pattern, "pattern", pattern)
    else:
        pattern_text = str(pattern)
        match = re.search(pattern_text, text, flags=flags)

    if not match:
        if required:
            raise AssertionError(f"OCR 正则未匹配: pattern={pattern_text}, text={text}")
        return None

    result = match.groupdict()
    if not result:
        result = {
            str(index): value
            for index, value in enumerate(match.groups(), start=1)
        }

    result["_text"] = text
    result["_match"] = match.group(0)
    return result


def wait_ocr_text_present(context, text, timeout=None, interval=None, region=None, entry_point=None):
    entry_point = log_call(entry_point, text=text, timeout=timeout, interval=interval, region=region)
    is_locator, target = _find_visual_locator(
        context,
        text,
        "ocr",
        timeout=timeout,
        region=region,
        required=False,
        entry_point=entry_point,
    )
    if is_locator:
        return target is not None

    text, monitor = _resolve_ocr_target(context, text, region=region, timeout=timeout, entry_point=entry_point)
    return _wait_ocr_text_present(text, timeout=timeout, interval=interval, monitor=monitor, debug_on_fail=None)


def wait_ocr_text_absent(context, text, timeout=None, interval=None, region=None, entry_point=None):
    entry_point = log_call(entry_point, text=text, timeout=timeout, interval=interval, region=region)
    result = _wait_visual_locator_absent(
        context,
        text,
        "ocr",
        timeout=timeout,
        interval=interval,
        region=region,
        entry_point=entry_point,
    )
    if result is not None:
        return result

    text, monitor = _resolve_ocr_target(context, text, region=region, timeout=timeout, entry_point=entry_point)
    return _wait_ocr_text_absent(text, timeout=timeout, interval=interval, monitor=monitor, debug_on_fail=None)


def assert_ocr_contains(context, text, timeout=None, region=None, msg=None, entry_point=None):
    entry_point = log_call(entry_point, text=text, timeout=timeout, region=region)
    if not wait_ocr_text_present(context, text, timeout=timeout, region=region, entry_point=entry_point):
        raise AssertionError(msg or f"断言失败：屏幕 OCR 文本不包含 -> {text}")
    return True


def assert_ocr_not_contains(context, text, timeout=None, region=None, msg=None, entry_point=None):
    entry_point = log_call(entry_point, text=text, timeout=timeout, region=region)
    result = _wait_visual_locator_absent(
        context,
        text,
        "ocr",
        timeout=timeout,
        region=region,
        entry_point=entry_point,
    )
    if result is not None:
        if not result:
            raise AssertionError(msg or f"断言失败：屏幕 OCR 文本不应包含 -> {text}")
        return True

    criteria, monitor = _resolve_ocr_target(context, text, region=region, timeout=timeout, entry_point=entry_point)
    ocr_options = normalize_ocr_criteria(criteria)
    image, monitor, candidates = scan_ocr(
        monitor=monitor,
        det_unclip_ratio=ocr_options.get("det_unclip_ratio"),
        det_limit_side_len=ocr_options.get("det_limit_side_len"),
        det_limit_type=ocr_options.get("det_limit_type"),
        det_box_thresh=ocr_options.get("det_box_thresh"),
        cache_ttl=ocr_options.get("cache_ttl"),
        use_cache=False,
    )
    matches = match_ocr_candidates(candidates, ocr_options["text"], mode=ocr_options.get("match_mode") or "contains")
    if matches:
        _save_ocr_debug_image(
            image=image,
            monitor=monitor,
            candidates=candidates,
            target_text=ocr_options["text"],
            reason="ocr_assert_unexpected",
        )
        raise AssertionError(msg or f"断言失败：屏幕 OCR 文本不应包含 -> {ocr_options['text']}")
    return True


def get_ocr_relative_position(context, text, direction="right", offset=20, timeout=None, region=None, entry_point=None):
    entry_point = log_call(
        entry_point,
        text=text,
        direction=direction,
        offset=offset,
        timeout=timeout,
        region=region,
    )
    result = _get_relative_position(
        context,
        text,
        "ocr",
        "ocr_relative",
        direction=direction,
        offset=offset,
        timeout=timeout,
        region=region,
        entry_point=entry_point,
    )
    return result[0], result[1]


def click_ocr_relative(context, text, direction="right", offset=20, timeout=None, region=None, entry_point=None):
    entry_point = log_call(
        entry_point,
        text=text,
        direction=direction,
        offset=offset,
        timeout=timeout,
        region=region,
    )
    result = _get_relative_position(
        context,
        text,
        "ocr",
        "ocr_relative",
        direction=direction,
        offset=offset,
        timeout=timeout,
        region=region,
        entry_point=entry_point,
    )
    _do_click(result)
    return result[0], result[1]


def save_pic_debug_image(context, criteria, monitor=None, candidates=None, reason="pic_debug", region=None,
                         entry_point=None):
    entry_point = log_call(entry_point, criteria=criteria, reason=reason, region=region)
    criteria, resolved_monitor = _resolve_pic_target(context, criteria, region=region, entry_point=entry_point)
    monitor = monitor or resolved_monitor
    return _save_pic_debug_image(
        monitor=monitor,
        candidates=candidates,
        criteria=criteria,
        reason=reason,
    )


def wait_pic_present(context, criteria, timeout=None, interval=None, region=None, entry_point=None):
    entry_point = log_call(entry_point, criteria=criteria, timeout=timeout, interval=interval, region=region)
    is_locator, target = _find_visual_locator(
        context,
        criteria,
        "pic",
        timeout=timeout,
        region=region,
        required=False,
        entry_point=entry_point,
    )
    if is_locator:
        return target is not None

    criteria, monitor = _resolve_pic_target(context, criteria, region=region, timeout=timeout, entry_point=entry_point)
    return _wait_pic_present(criteria, timeout=timeout, interval=interval, monitor=monitor, debug_on_fail=None)


def wait_pic_absent(context, criteria, timeout=None, interval=None, region=None, entry_point=None):
    entry_point = log_call(entry_point, criteria=criteria, timeout=timeout, interval=interval, region=region)
    result = _wait_visual_locator_absent(
        context,
        criteria,
        "pic",
        timeout=timeout,
        interval=interval,
        region=region,
        entry_point=entry_point,
    )
    if result is not None:
        return result

    criteria, monitor = _resolve_pic_target(context, criteria, region=region, timeout=timeout, entry_point=entry_point)
    return _wait_pic_absent(criteria, timeout=timeout, interval=interval, monitor=monitor, debug_on_fail=None)


def assert_pic_exists(context, criteria, timeout=None, region=None, msg=None, entry_point=None):
    entry_point = log_call(entry_point, criteria=criteria, timeout=timeout, region=region)
    if not wait_pic_present(context, criteria, timeout=timeout, region=region, entry_point=entry_point):
        raise AssertionError(msg or f"断言失败：图片不存在 -> {criteria}")
    return True


def assert_pic_not_exists(context, criteria, timeout=None, region=None, msg=None, entry_point=None):
    entry_point = log_call(entry_point, criteria=criteria, timeout=timeout, region=region)
    if not wait_pic_absent(context, criteria, timeout=timeout, region=region, entry_point=entry_point):
        raise AssertionError(msg or f"断言失败：图片不应存在 -> {criteria}")
    return True


def get_pic_region(context, criteria, timeout=None, region=None, padding=0, entry_point=None):
    entry_point = log_call(entry_point, criteria=criteria, timeout=timeout, region=region, padding=padding)
    is_locator, target = _find_visual_locator(
        context,
        criteria,
        "pic",
        timeout=timeout,
        region=region,
        required=True,
        entry_point=entry_point,
    )
    if is_locator:
        candidate = _candidate_from_target(target)
        if not candidate or not candidate.get("abs_bounds"):
            raise LookupError(f"图片未找到，无法获取区域: {criteria}")
        left, top, right, bottom = candidate["abs_bounds"]
        padding = _to_int(padding, 0)
        return {
            "left": int(left) - padding,
            "top": int(top) - padding,
            "width": int(right - left) + padding * 2,
            "height": int(bottom - top) + padding * 2,
        }

    criteria, monitor = _resolve_pic_target(context, criteria, region=region, timeout=timeout, entry_point=entry_point)
    return _get_pic_region(criteria, timeout=timeout, monitor=monitor, padding=padding)


def get_pic_relative_position(context, criteria, direction="right", offset=20, timeout=None, region=None,
                              entry_point=None):
    entry_point = log_call(
        entry_point,
        criteria=criteria,
        direction=direction,
        offset=offset,
        timeout=timeout,
        region=region,
    )
    result = _get_relative_position(
        context,
        criteria,
        "pic",
        "pic_relative",
        direction=direction,
        offset=offset,
        timeout=timeout,
        region=region,
        entry_point=entry_point,
    )
    return result[0], result[1]


def click_pic_relative(context, criteria, direction="right", offset=20, timeout=None, region=None,
                       entry_point=None):
    entry_point = log_call(
        entry_point,
        criteria=criteria,
        direction=direction,
        offset=offset,
        timeout=timeout,
        region=region,
    )
    result = _get_relative_position(
        context,
        criteria,
        "pic",
        "pic_relative",
        direction=direction,
        offset=offset,
        timeout=timeout,
        region=region,
        entry_point=entry_point,
    )
    _do_click(result)
    return result[0], result[1]



