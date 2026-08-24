from loguru import logger
from pywinauto.base_wrapper import BaseWrapper
from pywinauto.controls.hwndwrapper import HwndWrapper
from pywinauto.controls.uiawrapper import UIAWrapper
from pywinauto.mouse import click as mouse_click, double_click as mouse_double_click, right_click as mouse_right_click
from autowork_core.common.element_finder import get_element
from autowork_core.common.log_helper import log_call
from autowork_core.common.target import ResolvedTarget
from autowork_core.common.runtime_diagnostics import (
    RuntimeDiagnostic,
    remember_runtime_diagnostic,
    runtime_diagnostic_from_exception,
)


def _remember_probe_error(error, locator, stage):
    diagnostic = runtime_diagnostic_from_exception(error)
    if diagnostic is None:
        diagnostic = RuntimeDiagnostic(
            code="PROBE_BACKEND_ERROR",
            category="backend_error",
            stage=stage,
            summary="自动化后端探测失败",
            locator_name=str(locator),
        ).with_cause(error)
    remember_runtime_diagnostic(diagnostic)


def _exists(context, locator, timeout=3,entry_point=None):
    entry_point = log_call(entry_point,locator=locator,timeout=timeout)

    try:
        el = get_element(context, locator, visual_timeout=timeout, wait_type=None, required=False,entry_point=entry_point)
        return _target_exists(el, timeout=timeout)
    except Exception as error:
        _remember_probe_error(error, locator, "locate_exists")
        return False

def _is_visible(context, locator, timeout=3,entry_point=None):
    entry_point = log_call(entry_point,locator=locator,timeout=timeout)

    try:
        el = get_element(context, locator, visual_timeout=timeout, wait_type=None, required=False,entry_point=entry_point)
        el = _target_to_wrapper(el, timeout=timeout)

        if el is None:
            return False

        if _is_coords_target(el):
            return True

        if isinstance(el, BaseWrapper):
            try:
                return el.is_visible()
            except Exception as error:
                _remember_probe_error(error, locator, "wait_visible")
                return False

        return False
    except Exception as error:
        _remember_probe_error(error, locator, "wait_visible")
        return False

def _is_enabled(context, locator, timeout=3,entry_point=None):
    entry_point = log_call(entry_point,locator=locator,timeout=timeout)

    try:
        el = get_element(context, locator, visual_timeout=timeout, wait_type=None, required=False,entry_point=entry_point)
        el = _target_to_wrapper(el, timeout=timeout)

        if el is None:
            return False

        if _is_coords_target(el):
            return True

        if isinstance(el, BaseWrapper):
            try:
                return el.is_enabled()
            except Exception as error:
                _remember_probe_error(error, locator, "wait_enabled")
                return False

        return False
    except Exception as error:
        _remember_probe_error(error, locator, "wait_enabled")
        return False

def _is_exposed(context, locator, timeout=3, entry_point=None):
    entry_point = log_call(entry_point, locator=locator, timeout=timeout)

    try:
        element = get_element(
            context,
            locator,
            visual_timeout=timeout,
            wait_type=None,
            required=False,
            entry_point=entry_point,
        )
        element = _target_to_wrapper(element, timeout=timeout)
        if not isinstance(element, BaseWrapper):
            return False
        if not element.is_visible() or not element.is_enabled():
            return False
        return _wrapper_is_exposed(element)
    except Exception as error:
        _remember_probe_error(error, locator, "wait_exposed")
        logger.debug(
            "wait_exposed probe failed | locator={} | error={}: {}",
            locator,
            type(error).__name__,
            error,
        )
        return False

def _wrapper_is_exposed(target, ancestor_limit=32):
    rect = target.rectangle()
    if rect.right <= rect.left or rect.bottom <= rect.top:
        return False

    point = rect.mid_point()
    current = _wrapper_from_point(target, point.x, point.y)
    for _ in range(ancestor_limit):
        if _same_wrapper_identity(target, current):
            return True
        try:
            current = current.parent()
        except Exception:
            return False
        if current is None:
            return False
    return False

def _same_wrapper_identity(left, right):
    left_info = getattr(left, "element_info", None)
    right_info = getattr(right, "element_info", None)
    left_runtime = tuple(getattr(left_info, "runtime_id", None) or ())
    right_runtime = tuple(getattr(right_info, "runtime_id", None) or ())
    if left_runtime and right_runtime:
        if left_runtime != right_runtime:
            return False
        return _same_process_if_known(left_info, right_info)

    left_handle = getattr(left, "handle", None) or getattr(left_info, "handle", None)
    right_handle = getattr(right, "handle", None) or getattr(right_info, "handle", None)
    if not left_handle or not right_handle:
        return False
    return int(left_handle) == int(right_handle) and (
        _same_process_if_known(left_info, right_info)
    )

def _same_process_if_known(left_info, right_info):
    left_process = getattr(left_info, "process_id", None)
    right_process = getattr(right_info, "process_id", None)
    return (
        left_process is None
        or right_process is None
        or int(left_process) == int(right_process)
    )

def _wrapper_from_point(target, x, y):
    if isinstance(target, UIAWrapper):
        return _uia_wrapper_from_point(x, y)
    if isinstance(target, HwndWrapper):
        return _win32_wrapper_from_point(x, y)
    raise TypeError(
        "wait_exposed only supports UIAWrapper or HwndWrapper, "
        f"got {type(target).__name__}"
    )

def _uia_wrapper_from_point(x, y):
    from comtypes.gen.UIAutomationClient import tagPOINT
    from pywinauto.uia_defines import IUIA
    from pywinauto.uia_element_info import UIAElementInfo

    raw_element = IUIA().iuia.ElementFromPoint(tagPOINT(int(x), int(y)))
    return UIAWrapper(UIAElementInfo(raw_element))

def _win32_wrapper_from_point(x, y):
    import win32gui

    handle = win32gui.WindowFromPoint((int(x), int(y)))
    return HwndWrapper(handle)

def _resolve_target_and_coords(context, target, wait_type="visible", wait_timeout=5, visual_timeout=10, entry_point=None):
    entry_point = log_call(
        entry_point,
        target=target,
        wait_type=wait_type,
        wait_timeout=wait_timeout,
        visual_timeout=visual_timeout,
    )

    # 1. 不传 target：在当前鼠标位置滚
    if target is None:
        return None, None

    # 2. 直接是普通坐标：(x, y)
    coords = _coords_from_target(target)
    if coords is not None:
        return coords, None

    # 3. 其他一律交给 get_element
    element = get_element(
        context,
        target,
        wait_type=wait_type,
        wait_timeout=wait_timeout,
        visual_timeout=visual_timeout,
        entry_point=entry_point,
    )

    # get_element 返回 OCR/PIC 坐标
    coords = _coords_from_target(element)
    if coords is not None:
        return coords, None

    # get_element 返回普通 element
    element = _target_to_wrapper(element, timeout=0)
    rect = element.rectangle()
    x = (rect.left + rect.right) // 2
    y = (rect.top + rect.bottom) // 2
    return (int(x), int(y)), element


def _resolve_target_coords(context, target, wait_type="visible", wait_timeout=5, visual_timeout=10, entry_point=None):
    coords, _element = _resolve_target_and_coords(
        context,
        target,
        wait_type=wait_type,
        wait_timeout=wait_timeout,
        visual_timeout=visual_timeout,
        entry_point=entry_point,
    )
    return coords

def _first_or_self(el):
    if isinstance(el, list):
        return el[0] if el else None
    return el

def _is_coords_target(el):
    return isinstance(el, tuple) and len(el) >= 1 and isinstance(el[0], tuple)

def _is_plain_coords(el):
    return (
        isinstance(el, (tuple, list))
        and len(el) == 2
        and all(isinstance(i, (int, float)) for i in el)
    )

def _coords_from_target(el):
    if _is_plain_coords(el):
        x, y = el
        return int(x), int(y)
    if _is_coords_target(el):
        x, y = el[0]
        return int(x), int(y)
    return None

def _target_kind(el):
    if el is None:
        return "missing"
    if _is_coords_target(el) or _is_plain_coords(el):
        return "coords"
    if isinstance(el, list):
        return "list"
    if isinstance(el, BaseWrapper):
        return "wrapper"
    if _is_spec(el):
        return "spec"
    return type(el).__name__

def _is_spec(el):
    """
    判断是否为 WindowSpecification（懒对象）
    """
    if el is None:
        return False
    return hasattr(el, "exists") and not isinstance(el, BaseWrapper) and not isinstance(el, tuple) and not isinstance(el, list)

def _target_exists(el, timeout=0):
    if el is None:
        return False
    if _is_coords_target(el):
        return True
    if isinstance(el, list):
        return len(el) > 0
    if isinstance(el, BaseWrapper):
        return True
    if _is_spec(el):
        try:
            return el.exists(timeout=timeout)
        except Exception:
            return False
    return bool(el)

def _target_to_wrapper(el, timeout=0):
    el = _first_or_self(el)
    target = ResolvedTarget.from_legacy(el)
    if target.kind in {"missing", "coords"}:
        return target.value
    if target.kind == "spec":
        try:
            if not target.value.exists(timeout=timeout):
                return None
            return target.value.wrapper_object()
        except Exception:
            return None
    return target.value


def _do_click(el, button="left", double=False, offset=None):
    target = ResolvedTarget.from_legacy(el)
    if target.coords is not None:
        if offset is not None:
            raise ValueError("坐标型目标不能叠加控件内点击偏移")
        if double:
            mouse_double_click(coords=target.coords)
        elif button == "right":
            mouse_right_click(coords=target.coords)
        else:
            mouse_click(coords=target.coords)
        return

    if offset is None:
        target.value.click_input(
            button=button,
            double=double,
            use_log=False,
        )
        return
    offset_x, offset_y = _normalized_click_offset(offset)
    rectangle = target.value.rectangle()
    width = int(rectangle.right) - int(rectangle.left)
    height = int(rectangle.bottom) - int(rectangle.top)
    if not (0 <= offset_x < width and 0 <= offset_y < height):
        raise ValueError(
            "控件内点击偏移超出当前目标区域: "
            f"offset=({offset_x}, {offset_y}), size=({width}, {height})"
        )
    target.value.click_input(
        button=button,
        double=double,
        use_log=False,
        coords=(
            int(rectangle.left) + offset_x,
            int(rectangle.top) + offset_y,
        ),
        absolute=True,
    )


def _normalized_click_offset(offset):
    if (
        not isinstance(offset, (list, tuple))
        or len(offset) != 2
        or any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in offset
        )
    ):
        raise TypeError("控件内点击偏移必须是两个整数")
    return int(offset[0]), int(offset[1])

