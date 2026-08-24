import cv2
import numpy as np
from PIL import Image, ImageTk
from pywinauto import Desktop
from pywinauto.findwindows import find_elements


COMMON_INSPECT_PROPERTIES = [
    "element_info.control_type",
    "element_info.name",
    "element_info.automation_id",
    "element_info.class_name",
    "element_info.framework_id",
    "element_info.localized_control_type",
    "element_info.process_id",
    "element_info.handle",
    "element_info.runtime_id",
    "element_info.is_enabled",
    "element_info.is_offscreen",
    "element_info.is_keyboard_focusable",
    "element_info.has_keyboard_focus",
    "element_info.help_text",
    "element_info.access_key",
    "element_info.accelerator_key",
    "element_info.item_status",
    "element_info.item_type",
    "element_info.legacy_properties",
    "LegacyIAccessible.Name",
    "LegacyIAccessible.Value",
    "LegacyIAccessible.Role",
    "LegacyIAccessible.State",
    "LegacyIAccessible.Description",
    "LegacyIAccessible.Help",
    "LegacyIAccessible.DefaultAction",
    "LegacyIAccessible.KeyboardShortcut",
    "LegacyIAccessible.ChildId",
    "Value.Value",
    "Value.IsReadOnly",
    "wrapper.window_text",
    "wrapper.get_value",
    "wrapper.friendly_class_name",
    "wrapper.rectangle",
    "wrapper.is_visible",
    "wrapper.is_enabled",
]


MSAA_ROLE_NAMES = {
    0x01: "title bar",
    0x02: "menu bar",
    0x03: "scroll bar",
    0x09: "window",
    0x0A: "client",
    0x0B: "menu popup",
    0x0C: "menu item",
    0x0D: "tool tip",
    0x0E: "application",
    0x0F: "document",
    0x10: "pane",
    0x12: "dialog",
    0x14: "grouping",
    0x15: "separator",
    0x16: "tool bar",
    0x17: "status bar",
    0x18: "table",
    0x19: "column header",
    0x1A: "row header",
    0x1B: "column",
    0x1C: "row",
    0x1D: "cell",
    0x1E: "link",
    0x21: "list",
    0x22: "list item",
    0x23: "outline",
    0x24: "outline item",
    0x25: "page tab",
    0x26: "property page",
    0x28: "graphic",
    0x29: "static text",
    0x2A: "text",
    0x2B: "push button",
    0x2C: "check button",
    0x2D: "radio button",
    0x2E: "combo box",
    0x2F: "drop list",
    0x30: "progress bar",
    0x33: "slider",
    0x34: "spin button",
    0x3C: "page tab list",
    0x3E: "split button",
    0x40: "outline button",
}


MSAA_STATE_FLAGS = (
    (0x00000001, "unavailable"),
    (0x00000002, "selected"),
    (0x00000004, "focused"),
    (0x00000008, "pressed"),
    (0x00000010, "checked"),
    (0x00000020, "mixed"),
    (0x00000040, "read only"),
    (0x00000080, "hot tracked"),
    (0x00000100, "default"),
    (0x00000200, "expanded"),
    (0x00000400, "collapsed"),
    (0x00000800, "busy"),
    (0x00001000, "floating"),
    (0x00002000, "marqueed"),
    (0x00004000, "animated"),
    (0x00008000, "invisible"),
    (0x00010000, "offscreen"),
    (0x00020000, "sizeable"),
    (0x00040000, "moveable"),
    (0x00080000, "self voicing"),
    (0x00100000, "focusable"),
    (0x00200000, "selectable"),
    (0x00400000, "linked"),
    (0x00800000, "traversed"),
    (0x01000000, "multi selectable"),
    (0x02000000, "extended selectable"),
    (0x04000000, "alert low"),
    (0x08000000, "alert medium"),
    (0x10000000, "alert high"),
    (0x20000000, "protected"),
    (0x40000000, "has popup"),
)


def to_wrapper(element):
    if hasattr(element, "wrapper_object"):
        return element.wrapper_object()
    return element


def try_to_wrapper(element):
    try:
        return to_wrapper(element)
    except Exception:
        return element


def get_element_rect(element):
    element = to_wrapper(element)
    return element.rectangle()


def safe_get_element_rect(element):
    element = try_to_wrapper(element)
    for getter in (
        lambda: element.rectangle(),
        lambda: getattr(element, "rectangle", None),
        lambda: getattr(_element_info(element), "rectangle", None),
    ):
        try:
            rect = getter()
            if callable(rect):
                rect = rect()
            if rect is not None:
                return rect
        except Exception:
            continue
    return None


def _element_info(element):
    element = try_to_wrapper(element)
    info = getattr(element, "element_info", None)
    return info if info is not None else element


def _safe_info_attr(element, name, default=""):
    info = _element_info(element)
    try:
        value = getattr(info, name, default)
        return default if value is None else value
    except Exception:
        return default


def _safe_value_value(element):
    element = try_to_wrapper(element)
    try:
        return element.iface_value.CurrentValue
    except Exception:
        return None


def _safe_value_is_readonly(element):
    element = try_to_wrapper(element)
    try:
        return element.iface_value.CurrentIsReadOnly
    except Exception:
        return None


def _to_int(value):
    if isinstance(value, bool):
        return None

    try:
        return int(value)
    except Exception:
        return None


def _format_legacy_property(key, value):
    key_name = str(key).lower()
    number = _to_int(value)

    if key_name == "role" and number is not None:
        label = MSAA_ROLE_NAMES.get(number)
        return f"{label} (0x{number:X})" if label else f"0x{number:X}"

    if key_name == "state" and number is not None:
        labels = [label for flag, label in MSAA_STATE_FLAGS if number & flag]
        prefix = ",".join(labels) if labels else "none"
        return f"{prefix} (0x{number:X})"

    return value


def _add_legacy_properties(props, element, info):
    try:
        legacy_props = try_to_wrapper(element).legacy_properties()
    except Exception:
        legacy_props = {}

    for key, value in legacy_props.items():
        if value is not None:
            key_text = str(key)
            props[f"LegacyIAccessible.{key_text}"] = safe_to_string(
                _format_legacy_property(key_text, value)
            )


def _rect_text(rect):
    if rect is None:
        return ""
    try:
        return f"{rect.left},{rect.top},{rect.right},{rect.bottom}"
    except Exception:
        return ""


def safe_to_string(value, max_len=300):
    try:
        text = str(value)
    except Exception:
        text = "<unreadable>"

    text = text.replace("\n", "\\n").replace("\r", "\\r")

    if len(text) > max_len:
        text = text[:max_len] + "..."

    return text


def make_xpath_literal(value):
    text = str(value)

    if "'" not in text:
        return f"'{text}'"

    if '"' not in text:
        return f'"{text}"'

    return None


def _quote_free_chunks(value):
    chunks = []
    buf = []

    for ch in str(value):
        if ch in ("'", '"'):
            if buf:
                chunks.append("".join(buf))
                buf = []
            continue

        buf.append(ch)

    if buf:
        chunks.append("".join(buf))

    return [chunk.strip() for chunk in chunks if chunk.strip()]


def make_xpath_predicate(attr, value):
    literal = make_xpath_literal(value)
    if literal is not None:
        return f"@{attr}={literal}"

    chunks = _quote_free_chunks(value)
    if not chunks:
        return None

    predicates = [
        f"contains(@{attr},{make_xpath_literal(chunk)})"
        for chunk in chunks[:5]
    ]
    return " and ".join(predicates)


def iter_ordered_properties(props):
    yielded = set()

    for name in COMMON_INSPECT_PROPERTIES:
        if name in props:
            yielded.add(name)
            yield name, props[name]

    for name in sorted(props):
        if name not in yielded:
            yield name, props[name]


def make_element_key(element):
    info = _element_info(element)
    rect = safe_get_element_rect(element)

    handle = getattr(info, "handle", None)
    if handle:
        return f"handle:{handle}"

    runtime_id = getattr(info, "runtime_id", None)
    if runtime_id:
        return f"runtime:{runtime_id}"

    return (
        f"{getattr(info, 'control_type', '')}|"
        f"{getattr(info, 'name', '')}|"
        f"{getattr(info, 'automation_id', '')}|"
        f"{_rect_text(rect)}"
    )


def make_xpath_suggestion(element):
    info = _element_info(element)

    control_type = getattr(info, "control_type", "") or "*"
    name = getattr(info, "name", "") or ""
    auto_id = getattr(info, "automation_id", "") or ""

    if auto_id:
        predicate = make_xpath_predicate("auto_id", auto_id)
        if predicate:
            return f"//{control_type}[{predicate}]"

    if name:
        predicate = make_xpath_predicate("name", name)
        if predicate:
            return f"//{control_type}[{predicate}]"

    return f"//{control_type}"


def get_tree_values(element):
    rect = safe_get_element_rect(element)

    return {
        "control_type": _safe_info_attr(element, "control_type"),
        "name": _safe_info_attr(element, "name"),
        "auto_id": _safe_info_attr(element, "automation_id"),
        "class_name": _safe_info_attr(element, "class_name"),
        "rect": _rect_text(rect),
    }


def iter_tree_children(element):
    element = try_to_wrapper(element)

    for candidate in _direct_children(element):
        yield candidate


def iter_runtime_descendants(element):
    element = try_to_wrapper(element)
    try:
        descendants = element.descendants()
    except Exception:
        descendants = []

    seen = set()
    for candidate in descendants or []:
        key = make_element_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        yield candidate


def safe_parent(element):
    element = try_to_wrapper(element)
    try:
        parent = element.parent()
        if parent is not None:
            return parent
    except Exception:
        pass

    try:
        info_parent = getattr(_element_info(element), "parent", None)
        if callable(info_parent):
            info_parent = info_parent()
        return info_parent
    except Exception:
        return None


def _direct_children(element):
    seen = set()
    for source in (element, getattr(element, "element_info", None)):
        if source is None:
            continue
        try:
            children = source.children()
        except Exception:
            children = []
        for child in children or []:
            key = _safe_element_key(child)
            if key in seen:
                continue
            seen.add(key)
            yield child


def _safe_element_key(element):
    try:
        return make_element_key(element)
    except Exception:
        return f"object:{id(element)}"


def get_all_element_properties(element, excluded_wrapper_methods=()):
    props = {}
    info = None

    try:
        element = to_wrapper(element)
    except Exception:
        pass

    try:
        info = element.element_info

        for attr in dir(info):
            if attr.startswith("_"):
                continue

            if attr in {
                "children",
                "descendants",
                "parent",
                "iter_children",
                "iter_descendants",
                "set_cache_strategy",
            }:
                continue

            try:
                value = getattr(info, attr)
            except Exception:
                continue

            if callable(value):
                continue

            props[f"element_info.{attr}"] = safe_to_string(value)

    except Exception as e:
        props["element_info.error"] = safe_to_string(e)

    _add_legacy_properties(props, element, info)

    value_value = _safe_value_value(element)
    if value_value is not None:
        props["Value.Value"] = safe_to_string(value_value)

    value_is_readonly = _safe_value_is_readonly(element)
    if value_is_readonly is not None:
        props["Value.IsReadOnly"] = safe_to_string(value_is_readonly)

    control_type = str(getattr(info, "control_type", "") or "")
    semantic_methods = {
        "CheckBox": (("get_toggle_state", "Toggle.ToggleState"),),
        "RadioButton": (("is_selected", "SelectionItem.IsSelected"),),
        "TabItem": (("is_selected", "SelectionItem.IsSelected"),),
        "ListItem": (("is_selected", "SelectionItem.IsSelected"),),
        "DataItem": (("is_selected", "SelectionItem.IsSelected"),),
        "TreeItem": (
            ("is_selected", "SelectionItem.IsSelected"),
            ("is_expanded", "ExpandCollapse.IsExpanded"),
        ),
        "Slider": (
            ("value", "RangeValue.Value"),
            ("min_value", "RangeValue.Minimum"),
            ("max_value", "RangeValue.Maximum"),
        ),
    }
    for method_name, property_name in semantic_methods.get(
        control_type,
        (),
    ):
        try:
            method = getattr(element, method_name, None)
            if callable(method):
                props[property_name] = safe_to_string(method())
        except Exception:
            pass

    wrapper_methods = [
        "window_text",
        "get_value",
        "friendly_class_name",
        "rectangle",
        "is_visible",
        "is_enabled",
        "is_active",
        "is_minimized",
        "is_maximized",
    ]
    excluded_wrapper_methods = set(excluded_wrapper_methods)

    for method_name in wrapper_methods:
        if method_name in excluded_wrapper_methods:
            continue
        try:
            method = getattr(element, method_name, None)

            if method is None:
                continue

            if callable(method):
                value = method()
            else:
                value = method

            props[f"wrapper.{method_name}"] = safe_to_string(value)

        except Exception as e:
            props[f"wrapper.{method_name}"] = f"<error: {safe_to_string(e)}>"

    return props


def read_image_bgr(path):
    data = np.fromfile(str(path), dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError(f"图片读取失败: {path}")
    return image


def image_monitor_from_bgr(image, left=0, top=0):
    height, width = image.shape[:2]
    return {
        "left": int(left),
        "top": int(top),
        "width": int(width),
        "height": int(height),
    }


def draw_ocr_preview(image, candidates=None, matches=None):
    preview = image.copy()
    match_ids = {id(item) for item in (matches or [])}

    for index, candidate in enumerate(candidates or []):
        points = np.array(candidate.get("box") or [], dtype=np.int32)
        if len(points) < 4:
            continue

        is_match = id(candidate) in match_ids
        color = (0, 0, 255) if is_match else (0, 180, 0)
        thickness = 3 if is_match else 2
        cv2.polylines(preview, [points], True, color, thickness)

        bounds = candidate.get("bounds")
        if bounds:
            left, top, _, _ = bounds
        else:
            left = int(points[:, 0].min())
            top = int(points[:, 1].min())

        cv2.putText(
            preview,
            str(index),
            (int(left), max(16, int(top) - 4)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            color,
            2,
            cv2.LINE_AA,
        )

    return preview


def draw_pic_preview(image, candidate=None):
    preview = image.copy()
    if not candidate:
        return preview

    bounds = candidate.get("bounds")
    if not bounds:
        return preview

    left, top, right, bottom = [int(value) for value in bounds]
    cv2.rectangle(preview, (left, top), (right, bottom), (0, 0, 255), 2)

    confidence = candidate.get("confidence", 0)
    try:
        confidence_text = f"{float(confidence):.3f}"
    except Exception:
        confidence_text = str(confidence)

    cv2.putText(
        preview,
        confidence_text,
        (left, max(16, top - 5)),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        (0, 0, 255),
        2,
        cv2.LINE_AA,
    )
    return preview


def make_photo_image(image, max_width=760, max_height=420):
    height, width = image.shape[:2]
    scale = min(max_width / max(1, width), max_height / max(1, height), 1.0)

    if scale < 1.0:
        image = cv2.resize(
            image,
            (max(1, int(width * scale)), max(1, int(height * scale))),
            interpolation=cv2.INTER_AREA,
        )

    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    return ImageTk.PhotoImage(Image.fromarray(rgb))


def get_open_windows(backend="uia"):
    result = []
    desktop = Desktop(backend=backend)

    elements = find_elements(
        top_level_only=True,
        visible_only=True,
        enabled_only=False,
        backend=backend,
    )

    for elem in elements:
        try:
            title = (
                getattr(elem, "name", "")
                or getattr(elem, "rich_text", "")
                or ""
            )

            if title:
                title = title
            else:
                title = "[not title]"

            handle = getattr(elem, "handle", None)
            process_id = getattr(elem, "process_id", None)
            class_name = getattr(elem, "class_name", "")

            if not handle:
                continue

            win = desktop.window(handle=handle)
            display = f"{title} | pid={process_id} | hwnd={handle}"

            result.append({
                "display": display,
                "title": title,
                "handle": handle,
                "process_id": process_id,
                "class_name": class_name,
                "window": win,
            })

        except Exception:
            continue

    return result