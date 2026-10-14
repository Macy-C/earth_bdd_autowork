from dataclasses import dataclass

from autowork_core.common.element_properties import (
    ElementPropertyReadError,
    read_accessible_name,
)
from pywinauto.uia_defines import NoPatternInterfaceError


@dataclass(frozen=True)
class LocatorPropertySpec:
    key: str
    source: str
    reader: str
    arg: object = None
    aliases: tuple[str, ...] = ()


_RAW_UIA_PROPERTY_IDS = {
    "help_text": 30013,
    "localized_control_type": 30004,
    "access_key": 30007,
    "accelerator_key": 30006,
    "item_status": 30026,
    "item_type": 30021,
    "is_offscreen": 30022,
    "is_keyboard_focusable": 30009,
    "has_keyboard_focus": 30008,
    "provider_description": 30107,
}


_LOCATOR_PROPERTY_SPECS = (
    LocatorPropertySpec("name", "element_info.name", "name", aliases=(
        "text", "title", "caption", "label",
    )),
    LocatorPropertySpec("auto_id", "element_info.automation_id", "info_attr", "automation_id", aliases=(
        "automation_id", "automationid", "id",
    )),
    LocatorPropertySpec("control_type", "element_info.control_type", "info_attr", "control_type", aliases=(
        "type", "tag", "role",
    )),
    LocatorPropertySpec("class_name", "element_info.class_name", "info_attr", "class_name", aliases=(
        "class", "classname",
    )),
    LocatorPropertySpec("framework_id", "element_info.framework_id", "info_attr", "framework_id", aliases=(
        "framework",
    )),
    LocatorPropertySpec("process", "element_info.process_id", "info_attr", "process_id", aliases=(
        "process_id", "pid",
    )),
    LocatorPropertySpec("handle", "element_info.handle", "info_attr", "handle", aliases=(
        "hwnd",
    )),
    LocatorPropertySpec("runtime_id", "element_info.runtime_id", "info_attr", "runtime_id"),
    LocatorPropertySpec("enabled", "wrapper.is_enabled", "wrapper_bool", "is_enabled", aliases=(
        "is_enabled", "enabled_only",
    )),
    LocatorPropertySpec("visible", "wrapper.is_visible", "wrapper_bool", "is_visible", aliases=(
        "is_visible", "visible_only",
    )),
    LocatorPropertySpec("value.value", "Value.Value", "value_value", aliases=(
        "value", "value_value",
    )),
    LocatorPropertySpec("value.isreadonly", "Value.IsReadOnly", "value_isreadonly", aliases=(
        "value.is_readonly", "value_isreadonly", "value_is_readonly",
    )),
    LocatorPropertySpec("selection", "Selection.Selection", "selection", aliases=(
        "selection.selection",
    )),
    LocatorPropertySpec("legacyiaccessible.name", "LegacyIAccessible.Name", "legacy"),
    LocatorPropertySpec("legacyiaccessible.value", "LegacyIAccessible.Value", "legacy", aliases=(
        "legacy_value",
    )),
    LocatorPropertySpec("legacyiaccessible.role", "LegacyIAccessible.Role", "legacy", aliases=(
        "legacy_role",
    )),
    LocatorPropertySpec("legacyiaccessible.state", "LegacyIAccessible.State", "legacy", aliases=(
        "legacy_state",
    )),
    LocatorPropertySpec("legacyiaccessible.description", "LegacyIAccessible.Description", "legacy", aliases=(
        "legacy_description",
    )),
    LocatorPropertySpec("legacyiaccessible.help", "LegacyIAccessible.Help", "legacy", aliases=(
        "legacy_help",
    )),
    LocatorPropertySpec("legacyiaccessible.defaultaction", "LegacyIAccessible.DefaultAction", "legacy", aliases=(
        "legacy_default_action",
    )),
    LocatorPropertySpec("legacyiaccessible.keyboardshortcut", "LegacyIAccessible.KeyboardShortcut", "legacy", aliases=(
        "legacy_keyboard_shortcut",
    )),
    LocatorPropertySpec("legacyiaccessible.childid", "LegacyIAccessible.ChildId", "legacy", aliases=(
        "legacy_child_id",
    )),
    LocatorPropertySpec("help_text", "raw_uia.HelpText", "raw_uia", "help_text", aliases=(
        "helptext",
    )),
    LocatorPropertySpec("localized_control_type", "raw_uia.LocalizedControlType", "raw_uia", "localized_control_type", aliases=(
        "localized_type",
    )),
    LocatorPropertySpec("access_key", "raw_uia.AccessKey", "raw_uia", "access_key"),
    LocatorPropertySpec("accelerator_key", "raw_uia.AcceleratorKey", "raw_uia", "accelerator_key"),
    LocatorPropertySpec("item_status", "raw_uia.ItemStatus", "raw_uia", "item_status"),
    LocatorPropertySpec("item_type", "raw_uia.ItemType", "raw_uia", "item_type"),
    LocatorPropertySpec("is_offscreen", "raw_uia.IsOffscreen", "raw_uia", "is_offscreen", aliases=(
        "offscreen",
    )),
    LocatorPropertySpec("is_keyboard_focusable", "raw_uia.IsKeyboardFocusable", "raw_uia", "is_keyboard_focusable", aliases=(
        "focusable", "keyboard_focusable",
    )),
    LocatorPropertySpec("has_keyboard_focus", "raw_uia.HasKeyboardFocus", "raw_uia", "has_keyboard_focus", aliases=(
        "focused",
    )),
    LocatorPropertySpec("provider_description", "raw_uia.ProviderDescription", "raw_uia", "provider_description"),
)


_LOCATOR_PROPERTY_BY_KEY = {}
for spec in _LOCATOR_PROPERTY_SPECS:
    _LOCATOR_PROPERTY_BY_KEY[spec.key] = spec
    for alias in spec.aliases:
        _LOCATOR_PROPERTY_BY_KEY[alias] = spec


ORDERED_LOCATOR_PROPERTY_KEYS = tuple(spec.key for spec in _LOCATOR_PROPERTY_SPECS)


def normalize_locator_property_key(key):
    normalized = str(key or "").strip().lower()
    spec = _LOCATOR_PROPERTY_BY_KEY.get(normalized)
    return spec.key if spec is not None else normalized


def get_locator_property_source(key):
    spec = _LOCATOR_PROPERTY_BY_KEY.get(normalize_locator_property_key(key))
    return spec.source if spec is not None else ""


def get_locator_property(element, key):
    canonical = normalize_locator_property_key(key)
    spec = _LOCATOR_PROPERTY_BY_KEY.get(canonical)
    if spec is None:
        return None

    if spec.reader == "name":
        return read_accessible_name(element, element_info=_element_info(element))
    if spec.reader == "info_attr":
        return getattr(_element_info(element), str(spec.arg), None)
    if spec.reader == "wrapper_bool":
        return _call_wrapper_bool(element, str(spec.arg), _element_info(element))
    if spec.reader == "value_value":
        return _get_value(element)
    if spec.reader == "value_isreadonly":
        return _get_value_is_readonly(element)
    if spec.reader == "selection":
        return _get_selection(element)
    if spec.reader == "legacy":
        return _get_legacy_attr(element, spec.key)
    if spec.reader == "raw_uia":
        return _get_raw_uia_property(element, str(spec.arg))
    return None


def iter_locator_properties(element):
    for spec in _LOCATOR_PROPERTY_SPECS:
        try:
            value = get_locator_property(element, spec.key)
        except Exception:
            continue
        if not _is_absent_property_value(value):
            yield spec.key, value, spec.source


def _is_absent_property_value(value):
    return value is None or value == ""


def _element_info(element):
    return getattr(element, "element_info", element)


def _call_wrapper_bool(element, method_name, info):
    method = getattr(element, method_name, None)
    if method is None:
        fallback_name = method_name[3:] if method_name.startswith("is_") else method_name
        return getattr(info, fallback_name, None)
    try:
        return bool(method()) if callable(method) else bool(method)
    except Exception as error:
        raise ElementPropertyReadError(method_name) from error


def _get_value(element):
    try:
        return element.iface_value.CurrentValue
    except NoPatternInterfaceError:
        return None
    except Exception as error:
        raise ElementPropertyReadError("Value.Value") from error


def _get_value_is_readonly(element):
    try:
        return element.iface_value.CurrentIsReadOnly
    except NoPatternInterfaceError:
        return None
    except Exception as error:
        raise ElementPropertyReadError("Value.IsReadOnly") from error


def _get_selection(element):
    from pywinauto.controls.uiawrapper import UIAWrapper
    from pywinauto.uia_element_info import UIAElementInfo

    try:
        getter = getattr(element, "get_selection", None)
        if not callable(getter):
            info = _element_info(element)
            if not isinstance(info, UIAElementInfo):
                return None
            getter = UIAWrapper(info).get_selection
        return [read_accessible_name(item) for item in getter()]
    except NoPatternInterfaceError:
        return None
    except Exception as error:
        raise ElementPropertyReadError("Selection.Selection") from error


def _get_legacy_attr(element, key):
    legacy_key = key.split(".", 1)[1].lower()
    try:
        legacy_props = element.legacy_properties()
    except NoPatternInterfaceError:
        return None
    except Exception as error:
        raise ElementPropertyReadError(key) from error

    for current_key, value in legacy_props.items():
        if str(current_key).lower() == legacy_key:
            return value

    return None


def _get_raw_uia_property(element, key):
    property_id = _RAW_UIA_PROPERTY_IDS.get(key)
    if property_id is None:
        return None

    raw = getattr(_element_info(element), "element", None)
    if raw is None:
        return None

    try:
        return raw.GetCurrentPropertyValue(property_id)
    except Exception:
        return None
