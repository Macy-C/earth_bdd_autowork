from time import monotonic, sleep

from pywinauto.uia_defines import NoPatternInterfaceError

from autowork_core.actions.action_helper import _is_coords_target, _target_to_wrapper
from autowork_core.common.element_finder import get_element
from autowork_core.common.log_helper import log_call
from autowork_core.common.wait_coordinator import poll_value


def _get_dropdown(context, locator, timeout, visual_timeout, entry_point):
    element = get_element(
        context,
        locator,
        wait_type="ready",
        wait_timeout=timeout,
        visual_timeout=visual_timeout,
        entry_point=entry_point,
    )
    element = _target_to_wrapper(element, timeout=timeout)

    if element is None or _is_coords_target(element):
        raise TypeError(f"下拉框操作需要 UIA 控件: {locator}")

    return element


def _wait_for_dropdown_state(
        context,
        locator,
        predicate,
        description,
        timeout,
        interval,
        visual_timeout,
        entry_point,
):
        return poll_value(
            lambda: _get_dropdown(
                context,
                locator,
                timeout=0,
                visual_timeout=visual_timeout,
                entry_point=entry_point,
            ),
            predicate,
            timeout=timeout,
            interval=interval,
            timeout_message=(
                f"下拉框未在 {timeout} 秒内达到状态: {description}"
            ),
            monotonic=monotonic,
            sleep=sleep,
        )


def _wait_for_dropdown(context, locator, timeout, interval, visual_timeout, entry_point):
    return _wait_for_dropdown_state(
        context,
        locator,
        predicate=lambda current: current.is_visible() and current.is_enabled(),
        description="ready",
        timeout=timeout,
        interval=interval,
        visual_timeout=visual_timeout,
        entry_point=entry_point,
    )


def _selected_text(element):
    for getter in (
            lambda: element.selected_text(),
            lambda: element.iface_value.CurrentValue,
            lambda: element.window_text(),
    ):
        try:
            value = getter()
            if value is not None:
                return str(value)
        except Exception:
            pass
    return None


def expand_dropdown(context, locator, timeout=5, interval=0.2, visual_timeout=10, entry_point=None):
    entry_point = log_call(
        entry_point,
        locator=locator,
        timeout=timeout,
        interval=interval,
        visual_timeout=visual_timeout,
    )
    element = _wait_for_dropdown(context, locator, timeout, interval, visual_timeout, entry_point)

    try:
        if not element.is_expanded():
            element.expand()
    except NoPatternInterfaceError as error:
        raise TypeError(f"控件不支持 ExpandCollapse Pattern: {locator}") from error

    return _wait_for_dropdown_state(
        context,
        locator,
        predicate=lambda current: current.is_expanded(),
        description="expanded",
        timeout=timeout,
        interval=interval,
        visual_timeout=visual_timeout,
        entry_point=entry_point,
    )


def select_dropdown_option(
        context,
        locator,
        option,
        timeout=5,
        interval=0.2,
        visual_timeout=10,
        entry_point=None,
):
    if not isinstance(option, (str, int)) or isinstance(option, bool):
        raise TypeError(f"下拉框选项必须是文本或整数索引: {option!r}")

    entry_point = log_call(
        entry_point,
        locator=locator,
        option=option,
        timeout=timeout,
        interval=interval,
        visual_timeout=visual_timeout,
    )
    element = _wait_for_dropdown(context, locator, timeout, interval, visual_timeout, entry_point)

    try:
        if not element.is_expanded():
            element.expand()
            element = _wait_for_dropdown_state(
                context,
                locator,
                predicate=lambda current: current.is_expanded(),
                description="expanded before selection",
                timeout=timeout,
                interval=interval,
                visual_timeout=visual_timeout,
                entry_point=entry_point,
            )
    except NoPatternInterfaceError:
        # Some controls expose Selection without ExpandCollapse.
        pass

    try:
        element.select(option)
    except NoPatternInterfaceError as error:
        raise TypeError(f"控件不支持下拉选择所需的 UIA Pattern: {locator}") from error

    if isinstance(option, int):
        predicate = lambda current: current.selected_index() == option
        description = f"selected_index={option}"
    else:
        predicate = lambda current: _selected_text(current) == option
        description = f"selected_text={option!r}"

    return _wait_for_dropdown_state(
        context,
        locator,
        predicate=predicate,
        description=description,
        timeout=timeout,
        interval=interval,
        visual_timeout=visual_timeout,
        entry_point=entry_point,
    )