from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import json
import sys
import textwrap
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType
from typing import Any, get_type_hints

import yaml

from autowork_core.common.compile import (
    CompiledLocator,
    _is_top_level_locator,
    compile_locator,
    data_map,
)
from autowork_core.page import BasePage, WindowPage, WindowView
from autowork_core.utils.bus import normalize
from autowork_core.utils.debug_tools.recorder.ai_capability_registry import (
    AI_CAPABILITIES,
    base_page_public_api_names,
    validate_base_page_action_classification,
)
from config.paths import Paths


UNIVERSAL_PAGE_ID = "page:universal_debug_page"
UNIVERSAL_PAGE_LABEL = "公共调试 Page (全量 Locator / Data)"


@dataclass(frozen=True)
class ActionParameterDescriptor:
    name: str
    kind: str
    required: bool
    default_code: str | None
    annotation: str | None
    reference_kinds: tuple[str, ...] = ()


@dataclass(frozen=True)
class ActionDescriptor:
    name: str
    category: str
    signature: str
    parameters: tuple[ActionParameterDescriptor, ...]


@dataclass(frozen=True)
class PageViewDescriptor:
    id: str
    kind: str
    label: str
    module_name: str
    class_name: str
    class_object: type
    source_path: Path | None
    parent_id: str | None = None
    parent_class_name: str | None = None
    parent_class_object: type | None = None
    view_property: str | None = None


@dataclass(frozen=True)
class PageViewCatalog:
    targets: tuple[PageViewDescriptor, ...]
    import_errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


_FRAMEWORK_SUPPORT_POLICY = "framework_support"


def discover_page_view_catalog(
        *,
        page_obj_dir: Path | None = None,
        package: str = "Bdd.page_obj",
        include_universal: bool | None = None,
) -> PageViewCatalog:
    if include_universal is None:
        include_universal = page_obj_dir is None and package == "Bdd.page_obj"
    page_obj_dir = Path(page_obj_dir or Paths.PAGE_OBJ_DIR).resolve()
    modules, import_errors = _import_page_modules(page_obj_dir, package)
    page_classes: list[tuple[type[WindowPage], Path | None]] = []
    view_classes: list[tuple[type[WindowView], Path | None]] = []
    for module in modules:
        for class_object in _module_classes(module):
            if _is_concrete_subclass(class_object, WindowPage):
                page_classes.append((class_object, _source_path(class_object)))
            elif _is_concrete_subclass(class_object, WindowView):
                view_classes.append((class_object, _source_path(class_object)))

    pages = [
        _page_descriptor(page_class, source_path)
        for page_class, source_path in sorted(
            page_classes,
            key=lambda item: _class_sort_key(item[0]),
        )
    ]
    page_by_class = {
        descriptor.class_object: descriptor
        for descriptor in pages
    }
    discovered_view_classes = {view_class for view_class, _ in view_classes}
    bound_view_classes = set()
    bound_views: list[PageViewDescriptor] = []
    for page_class, _source in page_classes:
        parent = page_by_class.get(page_class)
        if parent is None:
            continue
        for property_name, view_class in _view_properties(page_class):
            if view_class not in discovered_view_classes:
                continue
            bound_view_classes.add(view_class)
            bound_views.append(_view_descriptor(
                parent,
                property_name,
                view_class,
                _source_path(view_class),
            ))

    warnings = [
        "WindowView 未绑定父 Page，已跳过: "
        f"{view_class.__module__}.{view_class.__qualname__}"
        for view_class, _source in sorted(
            view_classes,
            key=lambda item: _class_sort_key(item[0]),
        )
        if view_class not in bound_view_classes
    ]

    discovered_targets = tuple(sorted(
        [*pages, *bound_views],
        key=lambda item: (item.kind != "page", item.label.casefold()),
    ))
    if include_universal:
        targets = (universal_debug_page_descriptor(), *discovered_targets)
    else:
        targets = discovered_targets
    return PageViewCatalog(
        targets=targets,
        import_errors=tuple(import_errors),
        warnings=tuple(warnings),
    )


@dataclass(frozen=True)
class _UniversalLocatorEntry:
    rel_path: str
    raw_key: str
    locator: CompiledLocator | None
    target_labels: tuple[str, ...] = ()
    targets: tuple[PageViewDescriptor, ...] = ()
    error: Exception | None = None


@dataclass(frozen=True)
class _UniversalDataEntry:
    rel_path: str
    raw_key: str
    value: Any
    target_labels: tuple[str, ...] = ()
    targets: tuple[PageViewDescriptor, ...] = ()


@dataclass(frozen=True)
class _UniversalResourceIndex:
    top_roots: dict[str, CompiledLocator]
    locator_entries: dict[str, tuple[_UniversalLocatorEntry, ...]]
    data_entries: dict[str, tuple[_UniversalDataEntry, ...]]


class UniversalDebugPage(BasePage):
    """调试器专属公共 Page：加载全量 locator/data，唯一 key 直接解析，重名 key 明确报错并提示可选 Page/View。"""

    def __init__(self, context):
        self.ctx = context
        self._index = _build_universal_resource_index()

    def _require_locator(self, key, source=None):
        norm_key = normalize(str(key))
        entries = self._index.locator_entries.get(norm_key, ())
        if len(entries) > 1:
            raise KeyError(
                _format_duplicate_key_error("locator", str(key), source, entries)
            )
        if len(entries) == 1:
            entry = entries[0]
            if entry.error is not None:
                raise entry.error
            if entry.locator is not None:
                return entry.locator
        fallback = self.ctx.autowork_feature.locators[key]
        if fallback is not None:
            return fallback
        raise KeyError(
            f"公共 Page 下 locator key 不存在: {key} (引用: {source or key})"
        )

    def _require_data(self, key, source=None):
        norm_key = normalize(str(key))
        entries = self._index.data_entries.get(norm_key, ())
        if len(entries) > 1:
            raise KeyError(
                _format_duplicate_key_error("data", str(key), source, entries)
            )
        if len(entries) == 1:
            return entries[0].value
        fallback = self.ctx.autowork_feature.data[key]
        if fallback is not None:
            return fallback
        raise KeyError(
            f"公共 Page 下 data key 不存在: {key} (引用: {source or key})"
        )

    def get_locator(self, locator_or_name):
        locator = super().get_locator(locator_or_name)
        if (
            isinstance(locator, CompiledLocator)
            or self._parse_strict_ref(locator_or_name)
            or not isinstance(locator, (dict, str))
        ):
            return locator
        compiled = compile_locator(locator)
        if not (compiled.root_name or compiled.region_name):
            return locator
        root_locator = compiled.root_locator
        root_cache_name = compiled.root_cache_name
        region_locator = compiled.region_locator
        if compiled.root_name:
            root_locator = self._index.top_roots.get(compiled.root_name)
            if root_locator is None:
                root_locator = self._require_locator(
                    compiled.root_name,
                    source=f"root:{compiled.root_name}",
                )
            if not _is_top_level_locator(root_locator):
                raise ValueError(
                    f"locator root 必须是顶层窗口: {compiled.root_name}"
                )
            root_cache_name = root_locator.root_cache_name
        if compiled.region_name:
            region_locator = self._index.top_roots.get(compiled.region_name)
            if region_locator is None:
                region_locator = self._require_locator(
                    compiled.region_name,
                    source=f"region:{compiled.region_name}",
                )
            if region_locator.prefix not in {"child", "xpath"}:
                raise ValueError(
                    f"locator region 必须是 Child/XPath: {compiled.region_name}"
                )
        return replace(
            compiled,
            root_locator=root_locator,
            root_cache_name=root_cache_name,
            region_locator=region_locator,
        )

    def get_visual_value(self, value):
        ref = self._parse_strict_ref(value)
        if not ref or ref["kind"] == "literal":
            return super().get_visual_value(value)
        if ref["kind"] == "loc":
            return self._require_locator(ref["key"], ref.get("source"))
        if ref["kind"] == "data":
            return self._require_data(ref["key"], ref.get("source"))

        norm_key = normalize(str(ref["key"]))
        has_loc = bool(self._index.locator_entries.get(norm_key)) or (
            self.ctx.autowork_feature.locators[ref["key"]] is not None
        )
        has_data = bool(self._index.data_entries.get(norm_key)) or (
            self.ctx.autowork_feature.data[ref["key"]] is not None
        )
        if has_loc and has_data:
            raise KeyError(
                f"严格引用同时命中公共 Page locator 和 data: "
                f"{ref.get('source')}，请改用 $loc:{ref['key']} "
                f"或 $data:{ref['key']}"
            )
        if has_loc:
            return self._require_locator(ref["key"], ref.get("source"))
        if has_data:
            return self._require_data(ref["key"], ref.get("source"))
        raise KeyError(
            f"严格引用不存在: {ref['key']} "
            f"(引用: {ref.get('source')})"
        )


def universal_debug_page_descriptor() -> PageViewDescriptor:
    return PageViewDescriptor(
        id=UNIVERSAL_PAGE_ID,
        kind="universal",
        label=UNIVERSAL_PAGE_LABEL,
        module_name=UniversalDebugPage.__module__,
        class_name=UniversalDebugPage.__name__,
        class_object=UniversalDebugPage,
        source_path=_source_path(UniversalDebugPage),
    )


def action_descriptors(base_page_cls: type[BasePage] = BasePage):
    validate_base_page_action_classification(base_page_cls)
    capabilities = {
        capability.api_name: capability
        for capability in AI_CAPABILITIES
    }
    descriptors = []
    for name in sorted(base_page_public_api_names(base_page_cls)):
        capability = capabilities.get(name)
        if capability and capability.ai_exclusion == _FRAMEWORK_SUPPORT_POLICY:
            continue
        method = getattr(base_page_cls, name)
        signature = inspect.signature(method)
        reference_kinds = _parameter_reference_kinds(method)
        descriptors.append(ActionDescriptor(
            name=name,
            category=(capability.category if capability else "action"),
            signature=str(signature),
            parameters=tuple(
                _parameter_descriptor(
                    parameter,
                    reference_kinds.get(parameter.name, ()),
                )
                for parameter in signature.parameters.values()
                if parameter.name != "self"
            ),
        ))
    return tuple(descriptors)


def render_action_snippet(
        target: PageViewDescriptor,
        action: ActionDescriptor,
        values: dict[str, str],
) -> str:
    call = _render_action_call(action, values)
    resolved_target = (
        _infer_owner_target_from_strings(values.values())
        if target.kind == "universal"
        else None
    ) or target
    if resolved_target.kind == "view":
        return "\n".join((
            f"page = get_page(context, {resolved_target.parent_class_name})",
            f"view = page.{resolved_target.view_property}",
            f"view.{call}",
        ))
    return "\n".join((
        f"page = get_page(context, {resolved_target.class_name})",
        f"page.{call}",
    ))


def render_call_source_snippet(
        target: PageViewDescriptor,
        call_source: str,
) -> str:
    call = call_source.strip()
    resolved_target = (
        _infer_owner_target_from_call_source(call)
        if target.kind == "universal"
        else None
    ) or target
    if resolved_target.kind == "view":
        if call.startswith("page."):
            call = "view." + call[len("page."):]
        return "\n".join((
            f"page = get_page(context, {resolved_target.parent_class_name})",
            f"view = page.{resolved_target.view_property}",
            call,
        ))
    if call.startswith("view."):
        call = "page." + call[len("view."):]
    return "\n".join((
        f"page = get_page(context, {resolved_target.class_name})",
        call,
    ))


def coerce_action_argument(value: str, parameter: ActionParameterDescriptor):
    return _input_value(value, parameter)


def _import_page_modules(page_obj_dir: Path, package: str):
    if not page_obj_dir.is_dir():
        return [], [f"Page object directory does not exist: {page_obj_dir}"]
    modules = []
    errors = []
    for path in sorted(page_obj_dir.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        module_name = _module_name(page_obj_dir, package, path)
        if not module_name:
            continue
        try:
            modules.append(importlib.import_module(module_name))
        except Exception as error:
            errors.append(
                f"{module_name}: {type(error).__name__}: {error}"
            )
    return modules, errors


def _module_name(page_obj_dir: Path, package: str, path: Path):
    relative = path.relative_to(page_obj_dir).with_suffix("")
    parts = relative.parts
    if any(not part.isidentifier() for part in parts):
        return ""
    return ".".join((package, *parts))


def _module_classes(module: ModuleType):
    for _name, class_object in inspect.getmembers(module, inspect.isclass):
        if class_object.__module__ == module.__name__:
            yield class_object


def _is_concrete_subclass(class_object: type, base_class: type):
    try:
        return issubclass(class_object, base_class) and class_object is not base_class
    except TypeError:
        return False


def _page_descriptor(page_class: type[WindowPage], source_path: Path | None):
    class_id = _class_id(page_class)
    return PageViewDescriptor(
        id=f"page:{class_id}",
        kind="page",
        label=f"{page_class.__name__} - {_display_source(source_path)}",
        module_name=page_class.__module__,
        class_name=page_class.__name__,
        class_object=page_class,
        source_path=source_path,
    )


def _view_descriptor(
        parent: PageViewDescriptor,
        property_name: str,
        view_class: type[WindowView],
        source_path: Path | None,
):
    class_id = _class_id(view_class)
    return PageViewDescriptor(
        id=f"view:{parent.id}:{property_name}:{class_id}",
        kind="view",
        label=(
            f"{parent.class_name}.{property_name} -> {view_class.__name__}"
            f" - {_display_source(source_path)}"
        ),
        module_name=view_class.__module__,
        class_name=view_class.__name__,
        class_object=view_class,
        source_path=source_path,
        parent_id=parent.id,
        parent_class_name=parent.class_name,
        parent_class_object=parent.class_object,
        view_property=property_name,
    )


def _view_properties(page_class: type[WindowPage]):
    for name, descriptor in inspect.getmembers(page_class):
        if not isinstance(descriptor, property) or descriptor.fget is None:
            continue
        view_class = _view_class_from_type_hint(descriptor.fget)
        if view_class is None:
            view_class = _view_class_from_get_view_call(descriptor.fget)
        if _is_concrete_subclass(view_class, WindowView):
            yield name, view_class


def _view_class_from_type_hint(function):
    try:
        hints = get_type_hints(function)
    except Exception:
        return None
    value = hints.get("return")
    return value if inspect.isclass(value) else None


def _view_class_from_get_view_call(function):
    try:
        source = textwrap.dedent(inspect.getsource(function))
    except (OSError, TypeError):
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "get_view":
            continue
        if not node.args:
            continue
        resolved = _resolve_ast_class(node.args[0], function.__globals__)
        if inspect.isclass(resolved):
            return resolved
    return None


def _resolve_ast_class(node: ast.AST, globals_map: dict[str, Any]):
    if isinstance(node, ast.Name):
        return globals_map.get(node.id)
    if isinstance(node, ast.Attribute):
        owner = _resolve_ast_class(node.value, globals_map)
        return getattr(owner, node.attr, None) if owner is not None else None
    return None


def _parameter_descriptor(
        parameter: inspect.Parameter,
        reference_kinds: tuple[str, ...] = (),
):
    default = parameter.default
    return ActionParameterDescriptor(
        name=parameter.name,
        kind=parameter.kind.name,
        required=default is inspect.Parameter.empty,
        default_code=(None if default is inspect.Parameter.empty else _value_code(default)),
        annotation=_annotation_text(parameter.annotation),
        reference_kinds=reference_kinds,
    )


def _parameter_reference_kinds(function):
    try:
        source = textwrap.dedent(inspect.getsource(function))
    except (OSError, TypeError):
        return {}
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    references: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        reference_kind = {
            "get_locator": "loc",
            "get_data": "data",
            "get_visual_value": "visual",
        }.get(node.func.attr)
        if reference_kind is None:
            continue
        if not isinstance(node.func.value, ast.Name) or node.func.value.id != "self":
            continue
        if not node.args or not isinstance(node.args[0], ast.Name):
            continue
        references.setdefault(node.args[0].id, set()).add(reference_kind)
    return {
        name: tuple(sorted(values))
        for name, values in references.items()
    }


def _annotation_text(annotation):
    if annotation is inspect.Parameter.empty:
        return None
    if isinstance(annotation, type):
        return annotation.__name__
    return str(annotation)


def _render_action_call(action: ActionDescriptor, values: dict[str, str]):
    arguments = []
    keyword_mode = False
    for parameter in action.parameters:
        raw_value = str(values.get(parameter.name, "")).strip()
        if not raw_value:
            if parameter.required:
                value_code = f"<{parameter.name}>"
            else:
                keyword_mode = True
                continue
        else:
            value_code = _input_value_code(raw_value, parameter)
        if parameter.kind == "KEYWORD_ONLY" or keyword_mode or not parameter.required:
            arguments.append(f"{parameter.name}={value_code}")
        else:
            arguments.append(value_code)
    return f"{action.name}({', '.join(arguments)})"


def _input_value_code(value: str, parameter: ActionParameterDescriptor):
    return _value_code(_input_value(value, parameter))


def _input_value(value: str, parameter: ActionParameterDescriptor):
    if value.startswith("$"):
        return value
    if parameter.default_code in {"True", "False"}:
        lowered = value.casefold()
        if lowered in {"true", "1", "yes", "y", "on"}:
            return True
        if lowered in {"false", "0", "no", "n", "off"}:
            return False
    if parameter.annotation in {"float", "float | None"}:
        try:
            return float(value)
        except ValueError:
            pass
    if parameter.default_code is not None and _looks_numeric(parameter.default_code):
        try:
            return float(value) if "." in value else int(value)
        except ValueError:
            return value
    if _looks_like_literal(value):
        try:
            return ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return value
    return value


def _looks_numeric(value: str):
    try:
        float(value)
        return True
    except ValueError:
        return False


def _looks_like_literal(value: str):
    return (
        value in {"None", "True", "False"}
        or value[:1] in {'"', "'", "[", "{", "("}
    )


def _value_code(value):
    if isinstance(value, str):
        return _string_code(value)
    return repr(value)


def _string_code(value: str):
    return json.dumps(value, ensure_ascii=False)


def _class_id(class_object: type):
    return f"{class_object.__module__}.{class_object.__qualname__}"


def _class_sort_key(class_object: type):
    return (_class_id(class_object).casefold(), _class_id(class_object))


def _source_path(class_object: type):
    try:
        source = inspect.getsourcefile(class_object)
    except TypeError:
        return None
    return Path(source).resolve() if source else None


def _display_source(path: Path | None):
    if path is None:
        return "<unknown>"
    try:
        return path.resolve().relative_to(Paths.BASE_DIR.resolve()).as_posix()
    except ValueError:
        return path.name


def _normalize_yaml_rel_path(file_name: str) -> str:
    text = str(file_name).strip().replace("\\", "/")
    if not text.endswith((".yaml", ".yml")):
        text += ".yaml"
    return text


def _iter_yaml_files(root_dir: Path) -> list[tuple[str, Path]]:
    if not root_dir.is_dir():
        return []
    found: dict[str, Path] = {}
    for pattern in ("*.yaml", "*.yml"):
        for path in root_dir.rglob(pattern):
            if not path.is_file():
                continue
            rel = path.relative_to(root_dir).as_posix()
            found[rel] = path
    return [(rel, found[rel]) for rel in sorted(found)]


def _build_resource_file_target_maps(
        targets: tuple[PageViewDescriptor, ...],
) -> tuple[dict[str, tuple[PageViewDescriptor, ...]], dict[str, tuple[PageViewDescriptor, ...]]]:
    loc_map: dict[str, list[PageViewDescriptor]] = {}
    data_map_by_file: dict[str, list[PageViewDescriptor]] = {}
    for target in targets:
        if target.kind == "universal":
            continue
        cls = target.class_object
        loc_files = list(BasePage._iter_resource_files(
            getattr(cls, "locator_files", None),
            getattr(cls, "locator_file", None),
        ))
        if target.kind == "page":
            loc_files.extend(BasePage._iter_resource_files(
                getattr(cls, "view_locator_files", None),
                getattr(cls, "root_locator_file", None),
            ))
        for file_name in loc_files:
            rel = _normalize_yaml_rel_path(file_name)
            bucket = loc_map.setdefault(rel, [])
            if target not in bucket:
                bucket.append(target)

        data_files = list(BasePage._iter_resource_files(
            getattr(cls, "data_files", None),
            getattr(cls, "data_file", None),
        ))
        for file_name in data_files:
            rel = _normalize_yaml_rel_path(file_name)
            bucket = data_map_by_file.setdefault(rel, [])
            if target not in bucket:
                bucket.append(target)

    return (
        {k: tuple(v) for k, v in loc_map.items()},
        {k: tuple(v) for k, v in data_map_by_file.items()},
    )


def _bind_universal_locator_entry(
        locator: CompiledLocator,
        same_file_locators: dict[str, CompiledLocator],
        top_roots: dict[str, CompiledLocator],
        global_non_region_locators: dict[str, CompiledLocator],
) -> CompiledLocator:
    if _is_top_level_locator(locator):
        return locator
    if locator.prefix in {"default", "child", "xpath"} and not locator.root_name:
        raise ValueError(f"窗口包结构 locator[{locator.name}] 缺少窗口 root")
    if locator.prefix in {"ocr", "pic"} and not locator.region_name:
        raise ValueError(f"窗口包视觉 locator[{locator.name}] 缺少本地 region")

    root_locator = locator.root_locator
    root_cache_name = locator.root_cache_name
    region_locator = locator.region_locator

    if locator.root_name:
        root_locator = (
            top_roots.get(locator.root_name)
            or same_file_locators.get(locator.root_name)
        )
        if root_locator is None:
            raise KeyError(f"窗口包 root 不存在: {locator.root_name}")
        if not _is_top_level_locator(root_locator):
            raise ValueError(f"locator root 必须是顶层窗口: {locator.root_name}")
        root_cache_name = root_locator.root_cache_name

    if locator.region_name:
        region_locator = (
            same_file_locators.get(locator.region_name)
            or top_roots.get(locator.region_name)
            or global_non_region_locators.get(locator.region_name)
        )
        if region_locator is None:
            raise KeyError(f"窗口包 region 不存在: {locator.region_name}")
        if region_locator.prefix not in {"child", "xpath"}:
            raise ValueError(f"locator region 必须是 Child/XPath: {locator.region_name}")

    return replace(
        locator,
        root_locator=root_locator,
        root_cache_name=root_cache_name,
        region_locator=region_locator,
    )


def _build_universal_resource_index(
        targets: tuple[PageViewDescriptor, ...] | None = None,
) -> _UniversalResourceIndex:
    if targets is None:
        try:
            targets = discover_page_view_catalog(include_universal=False).targets
        except Exception:
            targets = ()
    locator_file_targets, data_file_targets = _build_resource_file_target_maps(targets)

    raw_files: list[tuple[str, list[tuple[str, str, CompiledLocator | None, Exception | None]]]] = []
    top_roots_by_key: dict[str, list[CompiledLocator]] = {}

    for rel_path, path in _iter_yaml_files(Path(Paths.LOCATORS_DIR)):
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if not isinstance(loaded, dict):
            continue
        mapped = data_map(loaded)
        items: list[tuple[str, str, CompiledLocator | None, Exception | None]] = []
        for raw_key in loaded:
            norm_key = normalize(str(raw_key))
            try:
                compiled = compile_locator(mapped[raw_key], name=norm_key)
                if _is_top_level_locator(compiled):
                    digest = hashlib.sha256(rel_path.encode("utf-8")).hexdigest()[:12]
                    compiled = replace(compiled, root_cache_name=f"{norm_key}:{digest}")
                    top_roots_by_key.setdefault(norm_key, []).append(compiled)
                items.append((str(raw_key), norm_key, compiled, None))
            except Exception as error:
                items.append((str(raw_key), norm_key, None, error))
        raw_files.append((rel_path, items))

    top_roots = {
        key: candidates[0]
        for key, candidates in top_roots_by_key.items()
        if len(candidates) == 1
    }

    file_compiled: dict[str, dict[str, CompiledLocator]] = {}
    file_bound: dict[str, dict[str, CompiledLocator]] = {}
    non_region_by_key: dict[str, list[CompiledLocator]] = {}

    for rel_path, items in raw_files:
        comp_map: dict[str, CompiledLocator] = {}
        for _raw_key, norm_key, compiled, error in items:
            if compiled is not None and error is None:
                comp_map[norm_key] = compiled
        file_compiled[rel_path] = comp_map
        bound_map: dict[str, CompiledLocator] = {}
        for _raw_key, norm_key, compiled, error in items:
            if compiled is None or error is not None:
                continue
            if _is_top_level_locator(compiled):
                bound_map[norm_key] = compiled
                continue
            if compiled.region_name:
                continue
            try:
                bound = _bind_universal_locator_entry(
                    compiled,
                    comp_map,
                    top_roots,
                    {},
                )
            except Exception:
                continue
            bound_map[norm_key] = bound
            non_region_by_key.setdefault(norm_key, []).append(bound)
        file_bound[rel_path] = bound_map

    unique_non_region = {
        key: candidates[0]
        for key, candidates in non_region_by_key.items()
        if len(candidates) == 1
    }

    locator_entries: dict[str, list[_UniversalLocatorEntry]] = {}
    for rel_path, items in raw_files:
        matched_targets = locator_file_targets.get(rel_path, ())
        target_labels = tuple(t.label for t in matched_targets)
        bound_map = file_bound.get(rel_path, {})
        comp_map = file_compiled.get(rel_path, {})
        for raw_key, norm_key, compiled, error in items:
            bound_locator = None
            entry_error = error
            if compiled is not None and entry_error is None:
                if norm_key in bound_map:
                    bound_locator = bound_map[norm_key]
                else:
                    try:
                        bound_locator = _bind_universal_locator_entry(
                            compiled,
                            {**comp_map, **bound_map},
                            top_roots,
                            unique_non_region,
                        )
                    except Exception as bind_err:
                        entry_error = bind_err
            locator_entries.setdefault(norm_key, []).append(
                _UniversalLocatorEntry(
                    rel_path=rel_path,
                    raw_key=raw_key,
                    locator=bound_locator,
                    target_labels=target_labels,
                    targets=matched_targets,
                    error=entry_error,
                )
            )

    data_entries: dict[str, list[_UniversalDataEntry]] = {}
    for rel_path, path in _iter_yaml_files(Path(Paths.DATA_DIR)):
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if not isinstance(loaded, dict):
            continue
        matched_targets = data_file_targets.get(rel_path, ())
        target_labels = tuple(t.label for t in matched_targets)
        for raw_key, value in loaded.items():
            norm_key = normalize(str(raw_key))
            data_entries.setdefault(norm_key, []).append(
                _UniversalDataEntry(
                    rel_path=rel_path,
                    raw_key=str(raw_key),
                    value=value,
                    target_labels=target_labels,
                    targets=matched_targets,
                )
            )

    return _UniversalResourceIndex(
        top_roots=top_roots,
        locator_entries={k: tuple(v) for k, v in locator_entries.items()},
        data_entries={k: tuple(v) for k, v in data_entries.items()},
    )


def _format_duplicate_key_error(
        kind: str,
        key: str,
        source: str | None,
        entries: tuple[_UniversalLocatorEntry, ...] | tuple[_UniversalDataEntry, ...],
) -> str:
    lines = [
        f"公共 Page 下 {kind} key 存在多处重名定义: {key} (引用: {source or key})，"
        "请在顶部切换至具体 Page/View：",
    ]
    for entry in entries:
        if entry.target_labels:
            targets_text = " / ".join(entry.target_labels)
            lines.append(
                f"  - {entry.rel_path} (键名: {entry.raw_key}) -> 可选 Page/View: {targets_text}"
            )
        else:
            lines.append(f"  - {entry.rel_path} (键名: {entry.raw_key})")
    return "\n".join(lines)


def _infer_owner_target_from_strings(values) -> PageViewDescriptor | None:
    refs: list[str] = []
    for raw in values:
        if not isinstance(raw, str):
            continue
        try:
            ref = BasePage._parse_strict_ref(raw.strip())
        except ValueError:
            continue
        if ref and ref["kind"] in {"loc", "auto"} and ref.get("key"):
            refs.append(ref["key"])
    if not refs:
        return None
    index = _build_universal_resource_index()
    for key in refs:
        entries = index.locator_entries.get(normalize(str(key)), ())
        if len(entries) == 1 and len(entries[0].targets) == 1:
            return entries[0].targets[0]
    return None


def _infer_owner_target_from_call_source(call_source: str) -> PageViewDescriptor | None:
    try:
        tree = ast.parse(call_source.strip(), mode="eval")
    except SyntaxError:
        return None
    strings = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    return _infer_owner_target_from_strings(strings)


def unload_modules(package: str):
    for module_name in list(sys.modules):
        if module_name == package or module_name.startswith(package + "."):
            sys.modules.pop(module_name, None)