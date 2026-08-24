import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Optional
from autowork_core.utils.bus import normalize

KEY_MAP = {
    # =========================
    # 文本 / 名称
    # 统一转成 pywinauto 的 title
    # =========================
    "name": "title",
    "text": "title",
    "title": "title",
    "caption": "title",
    "label": "title",

    "name_re": "title_re",
    "text_re": "title_re",
    "title_re": "title_re",
    "caption_re": "title_re",
    "label_re": "title_re",

    # =========================
    # AutomationId
    # 统一转成 auto_id
    # =========================
    "auto_id": "auto_id",
    "automation_id": "auto_id",
    "automationid": "auto_id",
    "id": "auto_id",

    "auto_id_re": "auto_id_re",
    "automation_id_re": "auto_id_re",
    "automationid_re": "auto_id_re",
    "id_re": "auto_id_re",

    # =========================
    # 控件类型
    # =========================
    "control_type": "control_type",
    "type": "control_type",
    "tag": "control_type",
    "role": "control_type",

    # =========================
    # class_name
    # =========================
    "class_name": "class_name",
    "class": "class_name",
    "classname": "class_name",

    "class_name_re": "class_name_re",
    "class_re": "class_name_re",
    "classname_re": "class_name_re",

    # =========================
    # index
    # =========================
    "found_index": "found_index",
    "index": "found_index",

    # =========================
    # 父级
    # =========================
    "parent": "parent",
    "parent_key": "parent",
    "parent_name": "parent",

    # =========================
    # Root 元信息
    # =========================
    "top_level": "top_level",
    "toplevel": "top_level",
    "desktop": "top_level",

    # =========================
    # 定位方式
    # =========================
    "by": "by",
    "strategy": "by",
    "locator_type": "by",

    # =========================
    # 等待
    # =========================
    "timeout": "timeout",
    "wait_timeout": "timeout",

    "interval": "interval",
    "retry_interval": "interval",

    "wait": "wait",
    "wait_state": "wait_state",
    "state": "wait_state",

    # =========================
    # 进程 / 句柄
    # 一般不常用，但可以支持
    # =========================
    "process": "process",
    "process_id": "process",
    "pid": "process",

    "handle": "handle",
    "hwnd": "handle",

    # =========================
    # OCR / 图片定位相关
    # =========================
    "value": "value",
    "file": "file",
    "filename": "file",
    "image": "file",
    "pic": "file",

    "pos": "pos",
    "position": "pos",
    "target_pos": "pos",

    "threshold": "threshold",
    "confidence": "threshold",

    # =========================
    # 坐标相关
    # =========================
    "x": "x",
    "y": "y",
    "coords": "coords",
    "coord": "coords",
    "point": "coords",
    "pos_xy": "coords",

    # =========================
    # 状态过滤
    # descendants 自定义过滤时用
    # =========================
    "visible": "visible",
    "enabled": "enabled",

    # =========================
    # 特殊属性
    # descendants / xpath 自定义过滤时用
    # =========================
    "legacyiaccessible.value": "legacyiaccessible.value",
    "legacy_value": "legacyiaccessible.value",

    "value.value": "value.value",
    "value_value": "value.value",
}

XPATH_KEY_MAP = {
    # 文本类
    "name": "name",
    "text": "name",
    "title": "name",
    "value": "value.value",
    "value_value": "value.value",
    "value.value": "value.value",
    "value.isreadonly": "value.isreadonly",
    "value.is_readonly": "value.isreadonly",
    "value_isreadonly": "value.isreadonly",
    "value_is_readonly": "value.isreadonly",

    # AutomationId
    "auto_id": "automation_id",
    "automation_id": "automation_id",
    "automationid": "automation_id",
    "id": "automation_id",

    # 控件类型
    "control_type": "control_type",
    "type": "control_type",

    # class
    "class_name": "class_name",
    "class": "class_name",
    "classname": "class_name",

    # Inspect / UIA 常用属性
    "framework": "framework_id",
    "framework_id": "framework_id",
    "localized_control_type": "localized_control_type",
    "localized_type": "localized_control_type",
    "help_text": "help_text",
    "helptext": "help_text",
    "accelerator_key": "accelerator_key",
    "access_key": "access_key",
    "item_status": "item_status",
    "item_type": "item_type",
    "process": "process_id",
    "process_id": "process_id",
    "pid": "process_id",
    "handle": "handle",
    "hwnd": "handle",
    "runtime_id": "runtime_id",
    "enabled": "enabled",
    "is_enabled": "enabled",
    "visible": "visible",
    "is_visible": "visible",
    "offscreen": "is_offscreen",
    "is_offscreen": "is_offscreen",
    "focusable": "is_keyboard_focusable",
    "keyboard_focusable": "is_keyboard_focusable",
    "is_keyboard_focusable": "is_keyboard_focusable",
    "focused": "has_keyboard_focus",
    "has_keyboard_focus": "has_keyboard_focus",
    "legacy_value": "legacyiaccessible.value",
    "legacy_name": "legacyiaccessible.name",
    "legacy_role": "legacyiaccessible.role",
    "legacy_state": "legacyiaccessible.state",
    "legacy_description": "legacyiaccessible.description",
    "legacy_help": "legacyiaccessible.help",
    "legacy_default_action": "legacyiaccessible.defaultaction",
    "legacy_keyboard_shortcut": "legacyiaccessible.keyboardshortcut",
    "legacy_child_id": "legacyiaccessible.childid",
}

DICT_BY_ALIASES = {
    "child": "child",
    "window": "child",
    "default": "default",
    "auto": "default",
    "xpath": "xpath",
    "ocr": "ocr",
    "pic": "pic",
    "picture": "pic",
    "image": "pic",
    "pos": "pos",
    "position": "pos",
    "coord": "pos",
    "coords": "pos",
}

VALID_PREFIXES = {"default", "child", "xpath", "ocr", "pic", "pos"}

STRING_CHILD_KEYS = {
    "auto_id", "auto_id_re",
    "title", "title_re",
    "control_type",
    "class_name", "class_name_re",
    "parent",
    "process",
    "handle",
}

CHILD_FILTER_KEYS = frozenset({
    "auto_id",
    "auto_id_re",
    "title",
    "title_re",
    "control_type",
    "class_name",
    "class_name_re",
    "process",
    "handle",
    "depth",
    "framework_id",
    "visible",
    "visible_only",
    "enabled",
    "enabled_only",
})

CHILD_METADATA_KEYS = frozenset({
    "backend",
    "parent",
    "top_level_only",
})

CHILD_CRITERIA_KEYS = CHILD_FILTER_KEYS | CHILD_METADATA_KEYS

def data_map(data):
    tmp_data = dict()
    for key, value in data.items():
        if isinstance(value, dict):
            new_value = dict()
            for inside_key, inside_value in value.items():
                new_key = KEY_MAP.get(inside_key, inside_key)
                new_value[new_key] = inside_value
            tmp_data[key] = new_value
        else:
            tmp_data[key] = value
    return tmp_data


def normalize_child_criteria(criteria):
    mapped = data_map({"locator": dict(criteria or {})})["locator"]
    for alias, native in (("visible", "visible_only"), ("enabled", "enabled_only")):
        if alias in mapped:
            if native in mapped:
                raise ValueError(f"Child locator 不能同时声明 {alias} 和 {native}")
            mapped[native] = mapped.pop(alias)
    if "found_index" in mapped:
        raise ValueError(
            "Child locator 不支持 index/found_index；"
            "需要按序号定位时请使用 XPath [index]"
        )
    unknown = sorted(set(mapped) - CHILD_CRITERIA_KEYS)
    if unknown:
        raise ValueError(f"Child locator 包含不支持的条件: {', '.join(unknown)}")

    return mapped

def _is_str(str_temp, identifiers='"', identifiers2="'"):
    return (str_temp.startswith(identifiers) and str_temp.endswith(identifiers)) or (
            str_temp.startswith(identifiers2) and str_temp.endswith(identifiers2))

def _normalize_by(by):
    by = normalize(str(by))
    return DICT_BY_ALIASES.get(by, by)

def _pop_first(data, *keys, default=None):
    for key in keys:
        if key in data:
            return data.pop(key)
    return default

def _format_coords(value):
    if isinstance(value, (list, tuple)):
        return ",".join(str(item) for item in value)
    return str(value)


def parse_pos_coordinates(value):
    text = _format_coords(value)
    parts = [part.strip() for part in text.split(",")]
    if len(parts) != 4:
        raise ValueError(
            "pos locator 必须包含 x,y,source_width,source_height 四个值: "
            f"{value}"
        )
    try:
        x, y, source_width, source_height = (int(part) for part in parts)
    except ValueError as exc:
        raise ValueError(f"pos locator 四个值必须都是整数: {value}") from exc
    if source_width <= 0 or source_height <= 0:
        raise ValueError(f"pos locator 来源桌面尺寸必须大于 0: {value}")
    return x, y, source_width, source_height


def normalize_pos_criteria(value):
    return ",".join(str(item) for item in parse_pos_coordinates(value))

def _infer_dict_prefix(data):
    if "xpath" in data:
        return "xpath"
    if "ocr" in data:
        return "ocr"
    if "coords" in data:
        return "pos"
    if "file" in data or "image" in data or "pic" in data:
        return "pic"
    return "child"

def _parse_dict_locator(locator):
    data = dict(locator)
    root = data.pop("root", None)
    top_level = _as_bool(data.pop("top_level", False))
    prefix = _normalize_by(data.pop("by")) if "by" in data else _infer_dict_prefix(data)

    if prefix == "child":
        return "child", normalize_child_criteria(data), root, top_level

    if prefix == "default":
        criteria = _pop_first(data, "value", "title", "auto_id")
        if criteria is None:
            raise ValueError(f"default locator 缺少 value/title/auto_id: {locator}")
        return "default", str(criteria), root, top_level

    if prefix == "xpath":
        criteria = _pop_first(data, "value", "xpath")
        if criteria is None:
            raise ValueError(f"xpath locator 缺少 value/xpath: {locator}")
        return "xpath", str(criteria), root, top_level

    if prefix == "ocr":
        criteria = _pop_first(data, "value", "ocr", "title", "text", "name")
        if criteria is None:
            raise ValueError(f"ocr locator 缺少 value/ocr/text: {locator}")
        data["value"] = str(criteria)
        return "ocr", data, root, top_level

    if prefix == "pic":
        criteria = _pop_first(data, "file", "value", "image", "pic")
        if criteria is None:
            raise ValueError(f"pic locator 缺少 file/value: {locator}")
        data["file"] = str(criteria)
        return "pic", data, root, top_level

    if prefix == "pos":
        criteria = _pop_first(data, "coords", "pos", "value")
        if criteria is None:
            raise ValueError(f"pos locator 缺少 coords/pos/value: {locator}")
        return "pos", normalize_pos_criteria(criteria), root, top_level

    raise ValueError(f"不支持的 locator by 类型: {prefix}, locator={locator}")

def parse_locator(locator):

    if isinstance(locator, dict):
        get_prefix, get_criteria, get_root, get_top_level = _parse_dict_locator(locator)
    else:
        locator = locator[1:len(locator) - 1] if _is_str(locator) else locator
        get_prefix, get_criteria, get_root = _parse_locator(locator)
        get_top_level = False
    return get_prefix, get_criteria, get_root, get_top_level


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on", "是")


def _parse_locator(locator):
    locator_root, locator = _split_root_locator(locator)

    if re.match(r"\(*//", locator):
        return "xpath", locator, locator_root

    index = _get_locator_separator_index(locator)
    if index != -1:
        raw_prefix = locator[:index].strip()
        criteria = locator[index + 1:].strip()

        prefix = _normalize_by(raw_prefix)
        if prefix in VALID_PREFIXES:
            if prefix == "pos":
                criteria = normalize_pos_criteria(criteria)
            return prefix, criteria, locator_root

        child_key = _normalize_string_child_key(raw_prefix)
        if child_key:
            return "child", normalize_child_criteria({child_key: criteria}), locator_root

        raise ValueError(f"未知 locator 类型或属性: {raw_prefix}, raw={locator}")

    return "default", locator.strip(), locator_root

def _split_root_locator(locator):
    locator = str(locator).strip()
    split_index = _find_root_separator(locator)
    if split_index == -1:
        return None, locator

    root_text = locator[:split_index].strip()
    rest = locator[split_index + 1:].strip()
    index = _get_locator_separator_index(root_text)
    if index == -1:
        return None, locator

    key = normalize(root_text[:index])
    if key != "root":
        return None, locator

    root_name = root_text[index + 1:].strip()
    if not root_name:
        raise ValueError(f"root 名称不能为空: {locator}")
    if not rest:
        raise ValueError(f"root 后缺少 locator: {locator}")

    return root_name, rest

def _find_root_separator(locator):
    quote = None
    bracket_depth = 0

    for index, char in enumerate(locator):
        if char in ("'", '"'):
            if quote is None:
                quote = char
            elif quote == char:
                quote = None
            continue

        if quote:
            continue

        if char in "[({":
            bracket_depth += 1
            continue

        if char in "])}":
            bracket_depth = max(0, bracket_depth - 1)
            continue

        if char != "|" or bracket_depth != 0:
            continue

        root_text = locator[:index].strip()
        sep_index = _get_locator_separator_index(root_text)
        if sep_index == -1:
            continue
        if normalize(root_text[:sep_index]) == "root":
            return index

    return -1

def _normalize_string_child_key(raw_key):
    norm_key = normalize(raw_key)
    mapped_key = KEY_MAP.get(norm_key, norm_key)
    if mapped_key in STRING_CHILD_KEYS:
        return mapped_key
    return None

def _get_locator_separator_index(locator):
    if "=" not in locator:
        return locator.find(":")
    if ":" not in locator:
        return locator.find("=")
    return min(locator.find("="), locator.find(":"))

@dataclass(slots=True)
class CompiledLocator:
    name: Optional[str]
    prefix: str
    criteria: Any

    # 原始 root 名称
    root_name: Optional[str] = None

    # OCR/PIC 截图范围引用的普通命名 locator
    region_name: Optional[str] = None

    # 显式声明该 locator 作为 root 时从桌面顶层查找
    top_level: bool = False

    # Window locator packages keep runtime references local instead of
    # publishing page resources into the Feature-wide registry.
    root_locator: Any = None
    region_locator: Any = None
    root_cache_name: Optional[str] = None

    raw: Any = None
    needs_root: bool = False
    needs_monitor: bool = False
    returns_position: bool = False


@dataclass(slots=True)
class CompiledWindowLocatorPackage:
    root_name: str
    locators: dict[str, CompiledLocator]

def compile_locator(raw, name=None):
    parse_raw = raw
    region_name = None
    if isinstance(raw, dict):
        parse_raw = dict(raw)
        region_name = parse_raw.pop("region", None)

    prefix, criteria, root_name, top_level = parse_locator(parse_raw)
    root_name = normalize(root_name) if root_name else root_name
    if prefix in ("ocr", "pic") and root_name:
        raise ValueError("OCR/PIC locator 不支持 root，请使用 region")
    if region_name is not None:
        region_name = str(region_name).strip()
        if not region_name:
            raise ValueError(f"locator[{name or '<anonymous>'}] region 名称不能为空")
        if prefix not in ("ocr", "pic"):
            raise ValueError(f"只有 OCR/PIC locator 支持 region: prefix={prefix}")
        region_name = normalize(region_name)
    return CompiledLocator(
        name=name,
        prefix=prefix,
        criteria=criteria,
        root_name=root_name,
        region_name=region_name,
        top_level=top_level,
        raw=raw,
        needs_root=bool(root_name),
        needs_monitor=prefix in ("ocr", "pic"),
        returns_position=prefix in ("ocr", "pic", "pos"),
    )

def _is_top_level_locator(locator):
    if locator.root_name:
        return False
    if locator.top_level:
        return True
    if not isinstance(locator.criteria, dict):
        return False
    return str(locator.criteria.get("control_type", "")).strip().lower() == "window"

def compile_locators(data, external_locators=None):
    """
    批量编译 locator，并校验直接引用：
    - root 只能引用顶层窗口 locator
    - region 只能引用普通 Child/XPath locator
    """
    mapped = data_map(data)

    # 1. 第一轮：先全部编译
    compiled_map = {}
    for name, raw in mapped.items():
        name = normalize(name)
        compiled_map[name] = compile_locator(raw, name=name)

    external_locators = external_locators or {}

    # 2. 第二轮：校验 root/region 直接引用
    for name, locator in compiled_map.items():
        if locator.root_name:
            parent = compiled_map.get(locator.root_name) or external_locators.get(locator.root_name)
            if parent is None:
                raise KeyError(f"locator[{name}] 引用了不存在的 root: {locator.root_name}")
            if not _is_top_level_locator(parent):
                raise ValueError(
                    f"locator[{name}] 的 root 必须是顶层窗口: {locator.root_name}"
                )
        if locator.region_name:
            region = compiled_map.get(locator.region_name) or external_locators.get(locator.region_name)
            if region is None:
                raise KeyError(f"locator[{name}] 引用了不存在的 region: {locator.region_name}")
            if region.prefix not in ("child", "xpath"):
                raise ValueError(
                    f"locator[{name}] 的 region 必须是 Child/XPath locator: "
                    f"{locator.region_name} ({region.prefix})"
                )

    return compiled_map


def compile_window_locator_package(
    root_locators,
    view_locators=(),
    *,
    package_name=None,
):
    """Compile one desktop window root and its same-window view locators."""
    root_map = compile_locators(root_locators)
    root_names = [
        name
        for name, locator in root_map.items()
        if _is_top_level_locator(locator)
    ]
    if len(root_names) != 1:
        raise ValueError(
            "窗口 locator 包必须恰好一个顶层 root: "
            f"actual={sorted(root_names)}"
        )

    root_name = root_names[0]
    compiled = dict(root_map)
    for raw_view in view_locators or ():
        view_map = compile_locators(
            raw_view,
            external_locators=root_map,
        )
        view_roots = sorted(
            name
            for name, locator in view_map.items()
            if _is_top_level_locator(locator)
        )
        if view_roots:
            raise ValueError(
                "窗口子页面不能声明顶层 root: "
                f"{view_roots}"
            )
        duplicates = sorted(set(compiled) & set(view_map))
        if duplicates:
            raise ValueError(f"窗口包包含重复 locator: {duplicates}")
        compiled.update(view_map)

    cache_basis = str(package_name or "").strip() or json.dumps(
        root_locators,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    cache_digest = hashlib.sha256(cache_basis.encode("utf-8")).hexdigest()[:12]
    root_cache_name = f"{root_name}:{cache_digest}"
    root_locator = compiled[root_name]
    root_locator.root_cache_name = root_cache_name
    for name, locator in compiled.items():
        if name == root_name:
            continue
        if locator.prefix in {"default", "child", "xpath"} and not (
            locator.root_name
        ):
            raise ValueError(
                f"窗口包结构 locator[{name}] 缺少窗口 root"
            )
        if locator.prefix in {"ocr", "pic"} and not locator.region_name:
            raise ValueError(
                f"窗口包视觉 locator[{name}] 缺少本地 region"
            )
        if locator.root_name and locator.root_name != root_name:
            raise ValueError(
                f"locator[{name}] 不属于窗口 root {root_name}: "
                f"{locator.root_name}"
            )
        if locator.root_name:
            locator.root_locator = root_locator
            locator.root_cache_name = root_cache_name
        if locator.region_name:
            locator.region_locator = compiled[locator.region_name]

    return CompiledWindowLocatorPackage(
        root_name=root_name,
        locators=compiled,
    )


