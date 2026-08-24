from time import monotonic, sleep

from pywinauto.uia_defines import NoPatternInterfaceError

from autowork_core.actions.action_helper import (
    _is_coords_target,
    _target_to_wrapper,
)
from autowork_core.common.element_finder import get_element
from autowork_core.common.log_helper import log_call
from autowork_core.common.wait_coordinator import poll_value


def set_checked(
        context,
        locator,
        checked=True,
        timeout=5,
        interval=0.2,
        visual_timeout=10,
        entry_point=None,
    ):
    checked = _boolean_value(checked, "复选框状态")
    entry_point = log_call(
        entry_point,
        locator=locator,
        checked=checked,
        timeout=timeout,
        interval=interval,
        visual_timeout=visual_timeout,
    )
    desired = 1 if checked else 0
    element = _wait_for_control(
        context,
        locator,
        {"CheckBox"},
        timeout,
        interval,
        visual_timeout,
        entry_point,
    )
    state = _toggle_state(element, locator)
    if state == 2:
        raise ValueError(f"复选框处于不确定状态，不能安全设置: {locator}")
    if state != desired:
        try:
            element.toggle()
        except (NoPatternInterfaceError, AttributeError) as error:
            raise TypeError(
                f"控件不支持 Toggle Pattern: {locator}"
            ) from error
    return _wait_for_state(
        context,
        locator,
        {"CheckBox"},
        lambda current: _toggle_state(current, locator) == desired,
        f"checked={checked}",
        timeout,
        interval,
        visual_timeout,
        entry_point,
    )


def select_radio(
        context,
        locator,
        timeout=5,
        interval=0.2,
        visual_timeout=10,
        entry_point=None,
    ):
    entry_point = log_call(
        entry_point,
        locator=locator,
        timeout=timeout,
        interval=interval,
        visual_timeout=visual_timeout,
    )
    element = _wait_for_control(
        context,
        locator,
        {"RadioButton"},
        timeout,
        interval,
        visual_timeout,
        entry_point,
    )
    if not _is_selected(element, locator):
        _select(element, locator)
    return _wait_for_state(
        context,
        locator,
        {"RadioButton"},
        lambda current: _is_selected(current, locator),
        "selected",
        timeout,
        interval,
        visual_timeout,
        entry_point,
    )


def select_tab(
        context,
        locator,
        timeout=5,
        interval=0.2,
        visual_timeout=10,
        entry_point=None,
    ):
    entry_point = log_call(
        entry_point,
        locator=locator,
        timeout=timeout,
        interval=interval,
        visual_timeout=visual_timeout,
    )
    element = _wait_for_control(
        context,
        locator,
        {"TabItem"},
        timeout,
        interval,
        visual_timeout,
        entry_point,
    )
    if not _is_selected(element, locator):
        _select(element, locator)
    return _wait_for_state(
        context,
        locator,
        {"TabItem"},
        lambda current: _is_selected(current, locator),
        "selected",
        timeout,
        interval,
        visual_timeout,
        entry_point,
    )


def select_list_item(
        context,
        locator,
        timeout=5,
        interval=0.2,
        visual_timeout=10,
        entry_point=None,
    ):
    entry_point = log_call(
        entry_point,
        locator=locator,
        timeout=timeout,
        interval=interval,
        visual_timeout=visual_timeout,
    )
    element = _wait_for_control(
        context,
        locator,
        {"ListItem", "DataItem"},
        timeout,
        interval,
        visual_timeout,
        entry_point,
    )
    if not _is_selected(element, locator):
        _select(element, locator)
    return _wait_for_state(
        context,
        locator,
        {"ListItem", "DataItem"},
        lambda current: _is_selected(current, locator),
        "selected",
        timeout,
        interval,
        visual_timeout,
        entry_point,
    )


def select_tree_item(
        context,
        locator,
        timeout=5,
        interval=0.2,
        visual_timeout=10,
        entry_point=None,
    ):
    entry_point = log_call(
        entry_point,
        locator=locator,
        timeout=timeout,
        interval=interval,
        visual_timeout=visual_timeout,
    )
    element = _wait_for_control(
        context,
        locator,
        {"TreeItem"},
        timeout,
        interval,
        visual_timeout,
        entry_point,
    )
    try:
        element.ensure_visible()
    except (NoPatternInterfaceError, AttributeError):
        pass
    if not _is_selected(element, locator):
        _select(element, locator)
    return _wait_for_state(
        context,
        locator,
        {"TreeItem"},
        lambda current: _is_selected(current, locator),
        "selected",
        timeout,
        interval,
        visual_timeout,
        entry_point,
    )


def set_tree_expanded(
        context,
        locator,
        expanded=True,
        timeout=5,
        interval=0.2,
        visual_timeout=10,
        entry_point=None,
    ):
    expanded = _boolean_value(expanded, "树节点展开状态")
    entry_point = log_call(
        entry_point,
        locator=locator,
        expanded=expanded,
        timeout=timeout,
        interval=interval,
        visual_timeout=visual_timeout,
    )
    element = _wait_for_control(
        context,
        locator,
        {"TreeItem"},
        timeout,
        interval,
        visual_timeout,
        entry_point,
    )
    if _is_expanded(element, locator) != expanded:
        try:
            element.expand() if expanded else element.collapse()
        except (NoPatternInterfaceError, AttributeError) as error:
            raise TypeError(
                f"控件不支持 ExpandCollapse Pattern: {locator}"
            ) from error
    return _wait_for_state(
        context,
        locator,
        {"TreeItem"},
        lambda current: _is_expanded(current, locator) == expanded,
        f"expanded={expanded}",
        timeout,
        interval,
        visual_timeout,
        entry_point,
    )


def set_slider_value(
        context,
        locator,
        value,
    expected_minimum=None,
    expected_maximum=None,
        timeout=5,
        interval=0.2,
        visual_timeout=10,
        entry_point=None,
    ):
    desired = _numeric_value(value)
    expected_minimum = _optional_numeric_value(
        expected_minimum,
        "滑块最小值",
    )
    expected_maximum = _optional_numeric_value(
        expected_maximum,
        "滑块最大值",
    )
    entry_point = log_call(
        entry_point,
        locator=locator,
        value=desired,
        expected_minimum=expected_minimum,
        expected_maximum=expected_maximum,
        timeout=timeout,
        interval=interval,
        visual_timeout=visual_timeout,
    )
    element = _wait_for_control(
        context,
        locator,
        {"Slider"},
        timeout,
        interval,
        visual_timeout,
        entry_point,
    )
    _validate_slider_range(
        element,
        locator,
        desired,
        expected_minimum,
        expected_maximum,
    )
    if not _numbers_equal(_slider_value(element, locator), desired):
        try:
            element.set_value(desired)
        except (NoPatternInterfaceError, AttributeError) as error:
            raise TypeError(
                f"控件不支持 RangeValue Pattern: {locator}"
            ) from error
    return _wait_for_state(
        context,
        locator,
        {"Slider"},
        lambda current: _slider_matches(
            current,
            locator,
            desired,
            expected_minimum,
            expected_maximum,
        ),
        f"value={desired}",
        timeout,
        interval,
        visual_timeout,
        entry_point,
    )


def _wait_for_control(
        context,
        locator,
        control_types,
        timeout,
        interval,
        visual_timeout,
        entry_point,
    ):
    return _wait_for_state(
        context,
        locator,
        control_types,
        lambda _current: True,
        "ready",
        timeout,
        interval,
        visual_timeout,
        entry_point,
    )


def _wait_for_state(
        context,
        locator,
        control_types,
        predicate,
        description,
        timeout,
        interval,
        visual_timeout,
        entry_point,
    ):
        return poll_value(
            lambda: _get_control(
                context,
                locator,
                control_types,
                visual_timeout,
                entry_point,
            ),
            predicate,
            timeout=timeout,
            interval=interval,
            timeout_message=(
                f"控件未在 {timeout} 秒内达到状态: "
                f"{description}; locator={locator}"
            ),
            fatal_errors=(TypeError, ValueError),
            clamp_interval=True,
            monotonic=monotonic,
            sleep=sleep,
        )


def _get_control(
        context,
        locator,
        control_types,
        visual_timeout,
        entry_point,
    ):
    element = get_element(
        context,
        locator,
        wait_type="enabled",
        wait_timeout=0,
        visual_timeout=visual_timeout,
        entry_point=entry_point,
    )
    element = _target_to_wrapper(element, timeout=0)
    if element is None or _is_coords_target(element):
        raise TypeError(f"语义控件动作需要 UIA 控件: {locator}")
    control_type = str(
        getattr(getattr(element, "element_info", None), "control_type", "")
    )
    if control_type not in control_types:
        raise TypeError(
            f"控件类型不匹配: expected={sorted(control_types)} "
            f"actual={control_type or 'unknown'} locator={locator}"
        )
    if not element.is_visible() or not element.is_enabled():
        raise RuntimeError(f"控件尚未 ready: {locator}")
    return element


def _toggle_state(element, locator):
    try:
        return int(element.get_toggle_state())
    except (NoPatternInterfaceError, AttributeError) as error:
        raise TypeError(f"控件不支持 Toggle Pattern: {locator}") from error


def _is_selected(element, locator):
    try:
        return bool(element.is_selected())
    except (NoPatternInterfaceError, AttributeError) as error:
        raise TypeError(f"控件不支持 SelectionItem Pattern: {locator}") from error


def _select(element, locator):
    try:
        element.select()
    except (NoPatternInterfaceError, AttributeError) as error:
        raise TypeError(f"控件不支持 SelectionItem Pattern: {locator}") from error


def _slider_value(element, locator):
    try:
        return float(element.value())
    except (NoPatternInterfaceError, AttributeError) as error:
        raise TypeError(f"控件不支持 RangeValue Pattern: {locator}") from error


def _is_expanded(element, locator):
    try:
        return bool(element.is_expanded())
    except (NoPatternInterfaceError, AttributeError) as error:
        raise TypeError(
            f"控件不支持 ExpandCollapse Pattern: {locator}"
        ) from error


def _numeric_value(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise TypeError(f"滑块值必须是数字: {value!r}")
    try:
        return float(value)
    except ValueError as error:
        raise ValueError(f"滑块值必须是数字: {value!r}") from error


def _boolean_value(value, description):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    raise TypeError(f"{description}必须是 bool 或 true/false: {value!r}")


def _optional_numeric_value(value, description):
    if value is None:
        return None
    try:
        return _numeric_value(value)
    except (TypeError, ValueError) as error:
        raise type(error)(f"{description}必须是数字: {value!r}") from error


def _validate_slider_range(
        element,
        locator,
        desired,
        expected_minimum,
        expected_maximum,
    ):
    try:
        current_minimum = float(element.min_value())
        current_maximum = float(element.max_value())
    except (NoPatternInterfaceError, AttributeError) as error:
        raise TypeError(f"控件不支持 RangeValue Pattern: {locator}") from error
    if desired < current_minimum or desired > current_maximum:
        raise ValueError(
            f"滑块值超出范围: value={desired} "
            f"range=[{current_minimum}, {current_maximum}]"
        )
    if (
        expected_minimum is not None
        and not _numbers_equal(current_minimum, expected_minimum)
    ):
        raise ValueError(
            f"滑块最小值与录制证据不一致: "
            f"expected={expected_minimum} actual={current_minimum}"
        )
    if (
        expected_maximum is not None
        and not _numbers_equal(current_maximum, expected_maximum)
    ):
        raise ValueError(
            f"滑块最大值与录制证据不一致: "
            f"expected={expected_maximum} actual={current_maximum}"
        )


def _slider_matches(
        element,
        locator,
        desired,
        expected_minimum,
        expected_maximum,
    ):
    _validate_slider_range(
        element,
        locator,
        desired,
        expected_minimum,
        expected_maximum,
    )
    return _numbers_equal(_slider_value(element, locator), desired)


def _numbers_equal(left, right):
    return abs(float(left) - float(right)) <= 1e-6


