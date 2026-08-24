from math import isclose, isfinite
from time import monotonic, sleep

from comtypes import COMError
from comtypes.gen.UIAutomationClient import OrientationType_Horizontal
from pywinauto.mouse import scroll
from pywinauto.uia_defines import NoPatternInterfaceError
from autowork_core.actions.action_helper import (
    _is_coords_target,
    _resolve_target_and_coords,
    _target_to_wrapper,
)
from autowork_core.common.element_finder import get_element
from autowork_core.common.log_helper import log_call
from autowork_core.common.wait_coordinator import poll_boolean


def _scrollbar_from_target(element, ancestor_limit=3):
    current = element
    for _ in range(ancestor_limit + 1):
        control_type = str(
            getattr(getattr(current, "element_info", None), "control_type", "")
        ).casefold()
        if control_type == "scrollbar":
            return current
        if control_type != "thumb":
            return None
        try:
            current = current.parent()
        except (AttributeError, RuntimeError):
            return None
        if current is None:
            return None
    return None


def _require_horizontal_scrollbar(element, target):
    scrollbar = _scrollbar_from_target(element)
    if scrollbar is None:
        raise TypeError(
            f"水平滚动需要 ScrollBar 或 Thumb 控件: {target}"
        )
    try:
        orientation = int(
            scrollbar.element_info.element.CurrentOrientation
        )
    except (COMError, AttributeError, RuntimeError, TypeError, ValueError) as error:
        raise TypeError(
            f"无法确认滚动条方向: {target}"
        ) from error
    if orientation != int(OrientationType_Horizontal):
        raise TypeError(f"目标不是水平滚动条: {target}")
    return scrollbar


def _set_horizontal_scrollbar_value(
        scrollbar,
        target,
        direction,
        steps,
        verification_timeout,
    ):
    try:
        range_value = scrollbar.iface_range_value
        is_read_only = bool(range_value.CurrentIsReadOnly)
        current = float(range_value.CurrentValue)
        minimum = float(range_value.CurrentMinimum)
        maximum = float(range_value.CurrentMaximum)
        increment = abs(float(range_value.CurrentSmallChange))
    except (
        COMError,
        NoPatternInterfaceError,
        AttributeError,
        TypeError,
        ValueError,
    ) as error:
        raise TypeError(
            f"水平滚动条不支持 RangeValue Pattern: {target}"
        ) from error
    if is_read_only:
        raise RuntimeError(f"水平滚动条 RangeValue 为只读: {target}")
    if (
        not all(isfinite(value) for value in (current, minimum, maximum, increment))
        or maximum < minimum
        or current < minimum
        or current > maximum
        or increment <= 0
    ):
        raise ValueError(
            f"水平滚动条 RangeValue 无效: locator={target} "
            f"value={current} range=[{minimum}, {maximum}] "
            f"small_change={increment}"
        )
    delta = increment * steps * (1 if direction == "right" else -1)
    desired = min(max(current + delta, minimum), maximum)
    try:
        if desired != current:
            range_value.SetValue(desired)
    except (
        COMError,
        NoPatternInterfaceError,
        AttributeError,
        RuntimeError,
    ) as error:
        raise RuntimeError(
            f"设置水平滚动条 RangeValue 失败: {target}"
        ) from error

    actual = None

    def value_committed():
        nonlocal actual
        try:
            actual = float(range_value.CurrentValue)
        except (
            COMError,
            NoPatternInterfaceError,
            AttributeError,
            TypeError,
            ValueError,
        ) as error:
            raise RuntimeError(
                f"无法回读水平滚动条 RangeValue: {target}"
            ) from error
        return isfinite(actual) and isclose(
            actual,
            desired,
            rel_tol=0.0,
            abs_tol=1e-9,
        )

    if poll_boolean(
            value_committed,
            timeout=verification_timeout,
            interval=0.05,
            monotonic=monotonic,
            sleep=sleep,
    ):
        return desired

    raise RuntimeError(
        f"水平滚动条未达到目标值: locator={target} "
        f"expected={desired} actual={actual}"
    )


def scroll_to(context, target=None, direction='down', steps=1, wait_type="visible", wait_timeout=5, visual_timeout=10, entry_point=None):
    entry_point = log_call(
        entry_point,
        target=target,
        direction=direction,
        steps=steps,
        wait_type=wait_type,
        wait_timeout=wait_timeout,
        visual_timeout=visual_timeout,
    )

    if direction not in {"up", "down", "left", "right"}:
        raise ValueError("direction 必须为 up、down、left 或 right")
    if isinstance(steps, bool) or not isinstance(steps, int) or steps <= 0:
        raise ValueError("steps 必须为正整数")

    coords, element = _resolve_target_and_coords(
        context,
        target,
        wait_type=wait_type,
        wait_timeout=wait_timeout,
        visual_timeout=visual_timeout,
        entry_point=entry_point,
    )
    if direction in {"up", "down"}:
        delta = steps if direction == "up" else -steps
        if coords is None:
            scroll(wheel_dist=delta)
        else:
            scroll(coords=coords, wheel_dist=delta)
    else:
        scrollbar = _require_horizontal_scrollbar(element, target)
        _set_horizontal_scrollbar_value(
            scrollbar,
            target,
            direction,
            steps,
            wait_timeout,
        )

    return coords


def drag_by_offset(
        context,
        target,
        delta_x,
        delta_y,
        wait_type="enabled",
        wait_timeout=5,
        visual_timeout=10,
        entry_point=None,
    ):
    entry_point = log_call(
        entry_point,
        target=target,
        delta_x=delta_x,
        delta_y=delta_y,
        wait_type=wait_type,
        wait_timeout=wait_timeout,
        visual_timeout=visual_timeout,
    )
    for name, value in (("delta_x", delta_x), ("delta_y", delta_y)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} 必须为整数")
    if delta_x == 0 and delta_y == 0:
        raise ValueError("drag 位移不能同时为 0")
    element = get_element(
        context,
        target,
        wait_type=wait_type,
        wait_timeout=wait_timeout,
        visual_timeout=visual_timeout,
        entry_point=entry_point,
    )
    element = _target_to_wrapper(element, timeout=wait_timeout)
    if element is None or _is_coords_target(element):
        raise RuntimeError("drag_by_offset 需要结构化源元素")
    point = element.rectangle().mid_point()
    destination = (int(point.x) + delta_x, int(point.y) + delta_y)
    element.drag_mouse_input(dst=destination)
    return destination

