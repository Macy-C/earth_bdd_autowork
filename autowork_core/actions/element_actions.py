from autowork_core.actions.action_helper import _is_coords_target, _target_to_wrapper
from autowork_core.common.compile import XPATH_KEY_MAP
from autowork_core.common.element_finder import get_element, get_elements
from autowork_core.common.log_helper import log_call


def _text_from_element(context, locator, el, timeout=3, entry_point=None):
    el = _target_to_wrapper(el, timeout=timeout)

    if el is None:
        return ""

    if _is_coords_target(el):
        raise RuntimeError("元组内容不支持获取元素文本")

    # Editable controls expose content through UIA/legacy value properties;
    # window_text() may contain only the accessible label.
    value = _value_pattern_value(el)
    if value is not None:
        return str(value)

    try:
        value = el.get_value()
        if value is not None:
            return str(value)
    except Exception:
        pass

    try:
        info = getattr(el, "element_info", None)
        legacy_props = getattr(info, "legacy_properties", None) or {}
        if "Value" in legacy_props and legacy_props["Value"] is not None:
            return str(legacy_props["Value"])
    except Exception:
        pass

    # 1. window_text()
    try:
        text = el.window_text()
        if text not in (None, ""):
            return str(text)
    except Exception:
        pass

    # 2. texts()
    try:
        texts = el.texts()
        if texts:
            texts = [str(i) for i in texts if i not in (None, "")]
            if texts:
                return "\n".join(texts)
    except Exception:
        pass

    # 3. element_info.name
    try:
        info = getattr(el, "element_info", None)
        name = getattr(info, "name", None)
        if name not in (None, ""):
            return str(name)
    except Exception:
        pass

    return ""


def get_text(context, locator, timeout=3, first_only=True, entry_point=None):
    """
    统一获取元素文本。

    支持：
    - Wrapper
    - WindowSpecification
    - list[Wrapper]
    - 不支持 tuple 坐标型目标（ocr/pic/pos）

    返回：
    - str：取到的文本
    - ""：取不到文本
    """
    entry_point = log_call(entry_point,locator=locator,timeout=timeout,first_only=first_only)

    if not first_only:
        elements = get_elements(
            context,
            locator,
            visual_timeout=timeout,
            wait_type="exists",
            wait_timeout=timeout,
            entry_point=entry_point,
        )
        return [
            _text_from_element(context, locator, element, timeout=timeout, entry_point=entry_point)
            for element in (elements or [])
        ]

    el = get_element(context, locator, visual_timeout=timeout, wait_type="exists", wait_timeout=timeout, required=False,entry_point=entry_point)
    return _text_from_element(context, locator, el, timeout=timeout, entry_point=entry_point)


def get_collection_items(
        context,
        locator,
        timeout=5,
        max_items=200,
        entry_point=None,
    ):
    entry_point = log_call(
        entry_point,
        locator=locator,
        timeout=timeout,
        max_items=max_items,
    )
    if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items < 1:
        raise ValueError("max_items 必须为正整数")
    element = get_element(
        context,
        locator,
        visual_timeout=timeout,
        wait_type="exists",
        wait_timeout=timeout,
        required=True,
        entry_point=entry_point,
    )
    element = _target_to_wrapper(element, timeout=timeout)
    if element is None or _is_coords_target(element):
        raise RuntimeError("集合读取需要结构化 UI 元素")
    children = [
        child
        for child in element.children()
        if str(
            getattr(getattr(child, "element_info", None), "control_type", "")
            or ""
        ).casefold() != "scrollbar"
    ]
    if len(children) > max_items:
        raise RuntimeError(
            f"集合项超过安全上限: count={len(children)}, max_items={max_items}"
        )
    return [
        _text_from_element(
            context,
            locator,
            child,
            timeout=timeout,
            entry_point=entry_point,
        )
        for child in children
    ]

def _attr_from_element(context, locator, el, attr_name, timeout=3, default=None, entry_point=None):
    el = _target_to_wrapper(el, timeout=timeout)

    if el is None:
        return default

    # 坐标型目标有限支持
    if _is_coords_target(el):
        if attr_name in ("coords", "point", "position"):
            return el[0]
        return default

    info = getattr(el, "element_info", None)
    attr_name = _normalize_attr_name(attr_name)

    # ---------- 状态类 ----------
    if attr_name == "enabled":
        try:
            return el.is_enabled()
        except Exception:
            return default

    if attr_name == "visible":
        try:
            return el.is_visible()
        except Exception:
            return default

    # ---------- 文本/值类 ----------
    if attr_name == "name":
        try:
            value = getattr(info, "name", default)
            return value if value is not None else default
        except Exception:
            return default

    if attr_name == "value.value":
        value = _value_pattern_value(el)
        return value if value is not None else default

    if attr_name == "value.isreadonly":
        value = _value_pattern_is_readonly(el)
        return value if value is not None else default

    if attr_name.startswith("legacyiaccessible."):
        value = _legacy_property(el, attr_name)
        return value if value is not None else default

    # ---------- 标识类 ----------
    if attr_name in ("automation_id", "auto_id"):
        try:
            value = getattr(info, "automation_id", default)
            return value if value is not None else default
        except Exception:
            return default

    if attr_name == "class_name":
        try:
            value = getattr(info, "class_name", default)
            return value if value is not None else default
        except Exception:
            return default

    if attr_name == "control_type":
        try:
            value = getattr(info, "control_type", default)
            return value if value is not None else default
        except Exception:
            return default

    if attr_name == "handle":
        try:
            value = getattr(info, "handle", default)
            return value if value is not None else default
        except Exception:
            return default

    return default


def _value_pattern_value(element):
    try:
        return element.iface_value.CurrentValue
    except Exception:
        return None


def _value_pattern_is_readonly(element):
    try:
        return element.iface_value.CurrentIsReadOnly
    except Exception:
        return None


def _legacy_property(element, attr_name):
    legacy_key = attr_name.split(".", 1)[1].lower()

    try:
        legacy_props = element.legacy_properties()
    except Exception:
        legacy_props = {}

    for key, value in legacy_props.items():
        if str(key).lower() == legacy_key:
            return value

    return None


def _normalize_attr_name(attr_name):
    attr_name = str(attr_name).strip().lower()
    return XPATH_KEY_MAP.get(attr_name, attr_name)


def get_attr(context, locator, attr_name, timeout=3, default=None, first_only=True, entry_point=None):
    """
    获取元素属性值。

    常用支持：
    - name
    - value / Value.Value
    - Value.IsReadOnly
    - LegacyIAccessible.*
    - class_name
    - control_type
    - automation_id / auto_id
    - enabled
    - visible
    - handle

    返回：
    - 成功取到 -> 对应值
    - 取不到 -> default
    """
    attr_name = _normalize_attr_name(attr_name)

    entry_point = log_call(entry_point,locator=locator,attr_name=attr_name,timeout=timeout,default=default,first_only=first_only)

    if not first_only:
        elements = get_elements(
            context,
            locator,
            visual_timeout=timeout,
            wait_type="exists",
            wait_timeout=timeout,
            entry_point=entry_point,
        )
        return [
            _attr_from_element(context, locator, element, attr_name, timeout=timeout, default=default, entry_point=entry_point)
            for element in (elements or [])
        ]

    el = get_element(context, locator, visual_timeout=timeout, wait_type="exists", wait_timeout=timeout, required=False,entry_point=entry_point)
    return _attr_from_element(context, locator, el, attr_name, timeout=timeout, default=default, entry_point=entry_point)


