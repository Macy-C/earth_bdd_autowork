from __future__ import annotations

import ast
import importlib
import inspect
import json
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, get_type_hints

from autowork_core.page import BasePage, WindowPage, WindowView
from autowork_core.utils.debug_tools.recorder.ai_capability_registry import (
    AI_CAPABILITIES,
    base_page_public_api_names,
    validate_base_page_action_classification,
)
from config.paths import Paths


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
) -> PageViewCatalog:
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

    targets = tuple(sorted(
        [*pages, *bound_views],
        key=lambda item: (item.kind != "page", item.label.casefold()),
    ))
    return PageViewCatalog(
        targets=targets,
        import_errors=tuple(import_errors),
        warnings=tuple(warnings),
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
    if target.kind == "view":
        return "\n".join((
            f"page = get_page(context, {target.parent_class_name})",
            f"view = page.{target.view_property}",
            f"view.{call}",
        ))
    return "\n".join((
        f"page = get_page(context, {target.class_name})",
        f"page.{call}",
    ))


def render_call_source_snippet(
        target: PageViewDescriptor,
        call_source: str,
) -> str:
    call = call_source.strip()
    if target.kind == "view":
        return "\n".join((
            f"page = get_page(context, {target.parent_class_name})",
            f"view = page.{target.view_property}",
            call,
        ))
    return "\n".join((
        f"page = get_page(context, {target.class_name})",
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


def unload_modules(package: str):
    for module_name in list(sys.modules):
        if module_name == package or module_name.startswith(package + "."):
            sys.modules.pop(module_name, None)