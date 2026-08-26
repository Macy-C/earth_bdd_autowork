"""Statically validate resources reachable from selected Behave Steps."""

from __future__ import annotations

import ast
import inspect
import textwrap
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path

import yaml

from autowork_core.common.compile import (
    compile_locators,
    compile_window_locator_package,
)
from autowork_core.page.singleton import BasePage, get_page, get_script_page
from autowork_core.page.window import WindowPage, WindowView
from autowork_core.utils.bus import normalize


class ResourcePreflightError(ValueError):
    """Raised when a selected Step has an invalid static resource dependency."""


@dataclass(frozen=True)
class SelectedStepResourceTarget:
    feature_name: str
    scenario_name: str
    step_text: str
    function: object
    scope_key: str

    @property
    def label(self):
        return (
            f"{self.feature_name} / {self.scenario_name} / "
            f"{self.step_text}"
        )

@dataclass(frozen=True)
class _OwnerBinding:
    owner_class: type | None
    page_class: type | None = None
    script_id: int | None = None


@dataclass(frozen=True)
class _ResourceReference:
    kind: str
    key: str
    source: str
    locator_files: tuple[str, ...] = ()
    data_files: tuple[str, ...] = ()


@dataclass
class _WindowUsage:
    page_class: type
    views: set[type] = field(default_factory=set)
    view_files: list[str] = field(default_factory=list)
    references: list[_ResourceReference] = field(default_factory=list)
    sources: set[str] = field(default_factory=set)


@dataclass
class _FlatUsage:
    locator_files: list[str] = field(default_factory=list)
    data_files: list[str] = field(default_factory=list)
    references: list[_ResourceReference] = field(default_factory=list)
    sources: set[str] = field(default_factory=set)


_UNKNOWN = object()


def preflight_selected_step_resources(
    targets,
    *,
    steps_dir,
    locators_dir=None,
    data_dir=None,
):
    """Validate Locator/Data resources reachable from selected Step functions."""
    targets = tuple(targets)
    if not targets:
        return ()
    analyzer = _ResourceAnalyzer(
        Path(steps_dir).resolve(),
        locators_dir=locators_dir,
        data_dir=data_dir,
    )
    for target in targets:
        try:
            analyzer.analyze_target(target)
        except ResourcePreflightError:
            raise
        except (
                OSError,
                TypeError,
                ValueError,
                KeyError,
                SyntaxError,
                yaml.YAMLError,
        ) as error:
            raise ResourcePreflightError(
                "Resource preflight failed before test startup:\n"
                f"  {target.label}: {type(error).__name__}: {error}"
            ) from error
    analyzer.validate()
    return tuple(analyzer.warnings)


class _ResourceAnalyzer:
    def __init__(self, steps_dir, *, locators_dir=None, data_dir=None):
        self.steps_dir = steps_dir
        self.bdd_dir = steps_dir.parent
        self.locators_dir = Path(
            locators_dir or (self.bdd_dir / "locators")
        ).resolve()
        self.data_dir = Path(
            data_dir or (self.bdd_dir / "data")
        ).resolve()
        self.window_usages: dict[type, _WindowUsage] = {}
        self.flat_usages: dict[object, _FlatUsage] = {}
        self.warnings = []
        self._warning_set = set()
        self._visited_functions = set()
        self._script_bindings = {}
        self._active_script_bindings = {}
        self._current_target = None

    def analyze_target(self, target):
        self._current_target = target
        self._analyze_function(target.function)

    def validate(self):
        errors = []
        public_data = {}
        public_path = self.data_dir / "public_data.yaml"
        if self.bdd_dir.name.casefold() == "bdd" or public_path.exists():
            try:
                public_data = _load_mapping(
                    public_path,
                    "public data",
                    normalized_keys=True,
                )
            except (OSError, TypeError, ValueError, yaml.YAMLError) as error:
                errors.append(str(error))

        for usage in self.window_usages.values():
            try:
                self._validate_window_usage(usage, public_data)
            except (OSError, TypeError, ValueError, KeyError, yaml.YAMLError) as error:
                errors.append(f"{_usage_source(usage)}: {error}")
        for usage in self.flat_usages.values():
            try:
                self._validate_flat_usage(usage, public_data)
            except (OSError, TypeError, ValueError, KeyError, yaml.YAMLError) as error:
                errors.append(f"{_usage_source(usage)}: {error}")

        if errors:
            raise ResourcePreflightError(
                "Resource preflight failed before test startup:\n"
                + "\n".join(f"  {error}" for error in errors)
            )

    def _validate_window_usage(self, usage, public_data):
        page_class = usage.page_class
        root_file = getattr(page_class, "root_locator_file", None)
        root_name = getattr(page_class, "root_locator", None)
        if not root_file or not root_name:
            raise TypeError(
                f"{page_class.__name__} 必须声明 root_locator_file 和 "
                "root_locator"
            )
        root_path = _resource_path(
            self.locators_dir,
            root_file,
            "window locator",
        )
        root_data = _load_mapping(
            root_path,
            "window locator",
            normalized_keys=True,
        )

        view_files = list(_resource_values(
            getattr(page_class, "view_locator_files", None),
            None,
        ))
        view_files.extend(usage.view_files)
        owned_views = []
        for view_class in usage.views:
            view_file = getattr(view_class, "locator_file", None)
            if view_file:
                if getattr(view_class, "root_locator", None):
                    owned_views.append((view_class, str(view_file)))
                else:
                    view_files.append(str(view_file))
        view_files = list(dict.fromkeys(view_files))
        view_data = [
            _load_mapping(
                _resource_path(
                    self.locators_dir,
                    file_name,
                    "view locator",
                ),
                "view locator",
                normalized_keys=True,
            )
            for file_name in view_files
        ]
        package = compile_window_locator_package(
            root_data,
            view_data,
            package_name=str(root_file),
        )
        if package.root_name != normalize(str(root_name)):
            raise ValueError(
                f"{page_class.__name__} root_locator 不匹配: "
                f"declared={root_name}, actual={package.root_name}"
            )
        locator_scope = dict(package.locators)
        for view_class, file_name in owned_views:
            owned_data = _load_mapping(
                _resource_path(
                    self.locators_dir,
                    file_name,
                    "view locator",
                ),
                "view locator",
                normalized_keys=True,
            )
            owned = compile_window_locator_package(
                owned_data,
                package_name=file_name,
            )
            expected_root = normalize(str(view_class.root_locator))
            if owned.root_name != expected_root:
                raise ValueError(
                    f"{view_class.__name__} root_locator 不匹配: "
                    f"declared={expected_root}, actual={owned.root_name}"
                )
            duplicates = sorted(set(locator_scope) & set(owned.locators))
            if duplicates:
                raise ValueError(
                    f"{view_class.__name__} locator名称冲突: {duplicates}"
                )
            locator_scope.update(owned.locators)

        data_scope = dict(public_data)
        for data_file in _resource_values(
                getattr(page_class, "data_files", None),
                getattr(page_class, "data_file", None),
        ):
            data_scope.update(_load_mapping(
                _resource_path(self.data_dir, data_file, "data"),
                "data",
                normalized_keys=True,
            ))
        self._validate_references(
            usage.references,
            set(locator_scope),
            {normalize(str(key)) for key in data_scope},
        )

    def _validate_flat_usage(self, usage, public_data):
        locator_scope, data_scope = self._flat_resource_scope(
            usage.locator_files,
            usage.data_files,
            public_data,
        )
        self._validate_references(
            [
                reference
                for reference in usage.references
                if not reference.locator_files
                and not reference.data_files
            ],
            set(locator_scope),
            {normalize(str(key)) for key in data_scope},
        )
        scope_cache = {}
        for reference in usage.references:
            if not reference.locator_files and not reference.data_files:
                continue
            scope_key = (
                reference.locator_files,
                reference.data_files,
            )
            scopes = scope_cache.get(scope_key)
            if scopes is None:
                scopes = self._flat_resource_scope(
                    reference.locator_files,
                    reference.data_files,
                    public_data,
                )
                scope_cache[scope_key] = scopes
            reference_locators, reference_data = scopes
            self._validate_references(
                [reference],
                set(reference_locators),
                {normalize(str(key)) for key in reference_data},
            )

    def _flat_resource_scope(
            self,
            locator_files,
            data_files,
            public_data,
    ):
        locator_scope = {}
        for locator_file in locator_files:
            value = _load_mapping(
                _resource_path(
                    self.locators_dir,
                    locator_file,
                    "locator",
                ),
                "locator",
                normalized_keys=True,
            )
            compiled = compile_locators(
                value,
                external_locators=locator_scope,
            )
            locator_scope.update(compiled)
        data_scope = dict(public_data)
        for data_file in data_files:
            data_scope.update(_load_mapping(
                _resource_path(self.data_dir, data_file, "data"),
                "data",
                normalized_keys=True,
            ))
        return locator_scope, data_scope

    @staticmethod
    def _validate_references(references, locator_keys, data_keys):
        for reference in references:
            key = normalize(reference.key)
            if reference.kind == "locator" and key not in locator_keys:
                raise KeyError(
                    f"locator key 不存在: {reference.key} "
                    f"(引用: {reference.source})"
                )
            if reference.kind == "data" and key not in data_keys:
                raise KeyError(
                    f"data key 不存在: {reference.key} "
                    f"(引用: {reference.source})"
                )
            if reference.kind == "visual":
                in_locator = key in locator_keys
                in_data = key in data_keys
                if in_locator and in_data:
                    raise KeyError(
                        "严格引用同时命中 locator 和 data: "
                        f"{reference.source}，请改用 $loc:{reference.key} "
                        f"或 $data:{reference.key}"
                    )
                if not in_locator and not in_data:
                    raise KeyError(
                        f"严格引用不存在: {reference.key} "
                        f"(引用: {reference.source})"
                    )

    def _analyze_function(self, function, *, self_binding=None, values=None):
        values = dict(values or {})
        visit_key = (
            self._current_target.scope_key,
            function,
            self_binding,
            tuple(sorted(
                (key, repr(value))
                for key, value in values.items()
                if value is not _UNKNOWN
            )),
        )
        if visit_key in self._visited_functions:
            return
        self._visited_functions.add(visit_key)
        try:
            node = _function_node(function)
        except (OSError, TypeError, ValueError, SyntaxError) as error:
            self._warn(
                f"{self._current_target.label}: cannot statically inspect "
                f"{getattr(function, '__qualname__', function)}: "
                f"{type(error).__name__}: {error}"
            )
            return
        walker = _FunctionResourceWalker(
            self,
            function,
            node,
            self_binding=self_binding,
            values=values,
        )
        walker.visit_statements(node.body)

    def add_page(self, page_class):
        if not inspect.isclass(page_class) or not issubclass(
                page_class,
                BasePage,
        ):
            self._warn(
                f"{self._current_target.label}: get_page target cannot be "
                f"statically classified: {page_class!r}"
            )
            return None
        source = self._current_target.label
        if issubclass(page_class, WindowPage):
            usage = self.window_usages.setdefault(
                page_class,
                _WindowUsage(page_class=page_class),
            )
            usage.sources.add(source)
            return _OwnerBinding(page_class, page_class)
        if issubclass(page_class, WindowView):
            self._warn(
                f"{source}: WindowView {page_class.__name__} must be obtained "
                "from its WindowPage"
            )
            return None
        usage = self.flat_usages.setdefault(page_class, _FlatUsage())
        usage.sources.add(source)
        usage.locator_files = list(dict.fromkeys(
            usage.locator_files
            + list(_resource_values(
                getattr(page_class, "locator_files", None),
                getattr(page_class, "locator_file", None),
            ))
        ))
        usage.data_files = list(dict.fromkeys(
            usage.data_files
            + list(_resource_values(
                getattr(page_class, "data_files", None),
                getattr(page_class, "data_file", None),
            ))
        ))
        return _OwnerBinding(page_class)

    def add_view(self, page_binding, view_class):
        if (
                page_binding is None
                or page_binding.page_class is None
                or not inspect.isclass(view_class)
                or not issubclass(view_class, WindowView)
        ):
            self._warn(
                f"{self._current_target.label}: get_view target cannot be "
                f"statically classified: {view_class!r}"
            )
            return None
        usage = self.window_usages[page_binding.page_class]
        usage.views.add(view_class)
        usage.sources.add(self._current_target.label)
        active_locator = getattr(view_class, "active_locator", None)
        if isinstance(active_locator, str):
            reference = _strict_reference(
                active_locator,
                "locator",
                f"{view_class.__name__}.active_locator",
            )
            if reference:
                usage.references.append(reference)
        return _OwnerBinding(view_class, page_binding.page_class)

    def add_script(
            self,
            call_key,
            locator_files,
            data_files,
            *,
            refresh=False,
    ):
        binding = self._script_bindings.get(call_key)
        if binding is not None:
            return binding
        scenario_key = self._current_target.scope_key
        binding = self._active_script_bindings.get(scenario_key)
        if refresh or binding is None:
            script_id = len(self.flat_usages) + 1
            binding = _OwnerBinding(None, script_id=script_id)
            self._active_script_bindings[scenario_key] = binding
            self.flat_usages[("script", script_id)] = _FlatUsage()
        self._script_bindings[call_key] = binding
        usage = self.flat_usages[("script", binding.script_id)]
        usage.sources.add(self._current_target.label)
        usage.locator_files = list(dict.fromkeys(
            usage.locator_files + list(locator_files)
        ))
        usage.data_files = list(dict.fromkeys(
            usage.data_files + list(data_files)
        ))
        return binding

    def add_reference(self, binding, value, role, source):
        reference = _strict_reference(value, role, source)
        if reference is None:
            return
        if binding.page_class is not None:
            usage = self.window_usages[binding.page_class]
        elif binding.script_id is not None:
            usage = self.flat_usages[("script", binding.script_id)]
            reference = replace(
                reference,
                locator_files=tuple(usage.locator_files),
                data_files=tuple(usage.data_files),
            )
        else:
            usage = self.flat_usages[binding.owner_class]
        usage.references.append(reference)

    def analyze_method_call(self, binding, method_name, call, walker):
        if method_name == "get_view":
            return
        owner_class = binding.owner_class
        if owner_class is None:
            roles = _base_page_resource_roles().get(method_name, {})
            self._record_call_references(
                binding,
                getattr(BasePage, method_name, None),
                roles,
                call,
                walker,
            )
            return
        try:
            descriptor = inspect.getattr_static(owner_class, method_name)
        except AttributeError:
            return
        function = _descriptor_function(descriptor)
        if function is None:
            return
        if self._is_project_function(function):
            call_values = _call_values(function, call, walker)
            self._analyze_function(
                function,
                self_binding=binding,
                values=call_values,
            )
            return
        roles = _base_page_resource_roles().get(method_name, {})
        self._record_call_references(
            binding,
            function,
            roles,
            call,
            walker,
        )

    def analyze_project_helper_call(self, function, call, walker):
        if not inspect.isfunction(function) or not self._is_project_function(
                function,
        ):
            return
        self._analyze_function(
            function,
            values=_call_values(function, call, walker),
        )

    def _is_project_function(self, function):
        try:
            source_path = Path(inspect.getsourcefile(function) or "").resolve()
            source_path.relative_to(self.bdd_dir.resolve())
        except (OSError, TypeError, ValueError):
            return False
        return True

    def _record_call_references(
            self,
            binding,
            function,
            roles,
            call,
            walker,
    ):
        if function is None or not roles:
            return
        values = _call_values(function, call, walker)
        for parameter, role in roles.items():
            value = values.get(parameter, _UNKNOWN)
            if value is _UNKNOWN:
                continue
            self.add_reference(
                binding,
                value,
                role,
                f"{binding.owner_class.__name__ if binding.owner_class else 'ScriptPage'}."
                f"{getattr(function, '__name__', '<call>')}({parameter})",
            )

    def _warn(self, message):
        if message not in self._warning_set:
            self._warning_set.add(message)
            self.warnings.append(message)


class _FunctionResourceWalker(ast.NodeVisitor):
    def __init__(
            self,
            analyzer,
            function,
            node,
            *,
            self_binding=None,
            values=None,
    ):
        self.analyzer = analyzer
        self.function = function
        self.node = node
        self.globals = getattr(function, "__globals__", {})
        self.bindings = {}
        self.values = dict(values or {})
        if self_binding is not None:
            parameters = list(inspect.signature(function).parameters)
            if parameters:
                self.bindings[parameters[0]] = self_binding

    def visit_statements(self, statements):
        for statement in statements:
            self.visit(statement)

    def visit_Assign(self, node):
        binding = self.resolve_owner(node.value)
        value = self.resolve_value(node.value)
        for target in node.targets:
            self._assign(target, binding, value)
        self.visit(node.value)

    def visit_AnnAssign(self, node):
        if node.value is None:
            return
        binding = self.resolve_owner(node.value)
        value = self.resolve_value(node.value)
        self._assign(node.target, binding, value)
        self.visit(node.value)

    def visit_Call(self, node):
        if isinstance(node.func, ast.Attribute):
            binding = self.resolve_owner(node.func.value)
            if binding is not None:
                self.analyzer.analyze_method_call(
                    binding,
                    node.func.attr,
                    node,
                    self,
                )
        else:
            binding = self.resolve_owner(node)
            if binding is None:
                self.analyzer.analyze_project_helper_call(
                    self.resolve_object(node.func),
                    node,
                    self,
                )
        self.generic_visit(node)

    def visit_Attribute(self, node):
        self.resolve_owner(node)
        self.generic_visit(node)

    def resolve_owner(self, node):
        if isinstance(node, ast.Name):
            return self.bindings.get(node.id)
        if isinstance(node, ast.Attribute):
            owner = self.resolve_owner(node.value)
            if owner is None or owner.owner_class is None:
                return None
            return self._property_owner(owner, node.attr)
        if not isinstance(node, ast.Call):
            return None
        callable_value = self.resolve_object(node.func)
        if callable_value is get_page:
            if len(node.args) < 2:
                self.analyzer._warn(
                    f"{self.analyzer._current_target.label}: get_page target "
                    "is missing"
                )
                return None
            page_class = self.resolve_object(node.args[1])
            if page_class is _UNKNOWN:
                self.analyzer._warn(
                    f"{self.analyzer._current_target.label}: dynamic get_page "
                    "target cannot be statically validated"
                )
                return None
            return self.analyzer.add_page(page_class)
        if callable_value is get_script_page:
            locator_files = self._resource_call_values(
                node,
                "locator_files",
                "locator_file",
            )
            data_files = self._resource_call_values(
                node,
                "data_files",
                "data_file",
            )
            call_key = (
                self.analyzer._current_target.scope_key,
                self.function,
                getattr(node, "lineno", 0),
                getattr(node, "col_offset", 0),
            )
            refresh = next(
                (
                    self.resolve_value(keyword.value)
                    for keyword in node.keywords
                    if keyword.arg == "refresh"
                ),
                False,
            )
            if refresh is _UNKNOWN:
                self.analyzer._warn(
                    f"{self.analyzer._current_target.label}: dynamic "
                    "get_script_page refresh cannot be statically validated"
                )
                refresh = False
            return self.analyzer.add_script(
                call_key,
                locator_files,
                data_files,
                refresh=bool(refresh),
            )
        if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "get_view"
        ):
            page_binding = self.resolve_owner(node.func.value)
            view_class = (
                self.resolve_object(node.args[0])
                if node.args
                else _UNKNOWN
            )
            return self.analyzer.add_view(page_binding, view_class)
        return None

    def resolve_value(self, node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            values = [self.resolve_value(item) for item in node.elts]
            return values if all(value is not _UNKNOWN for value in values) else _UNKNOWN
        if isinstance(node, ast.Name):
            if node.id in self.values:
                return self.values[node.id]
            value = self.globals.get(node.id, _UNKNOWN)
            if isinstance(value, (str, int, float, bool, list, tuple, dict)):
                return value
        return _UNKNOWN

    def resolve_object(self, node):
        if isinstance(node, ast.Name):
            return self.globals.get(node.id, _UNKNOWN)
        if isinstance(node, ast.Attribute):
            owner = self.resolve_object(node.value)
            if owner is _UNKNOWN:
                return _UNKNOWN
            try:
                return inspect.getattr_static(owner, node.attr)
            except AttributeError:
                return _UNKNOWN
        return _UNKNOWN

    def _property_owner(self, owner, attribute):
        try:
            descriptor = inspect.getattr_static(owner.owner_class, attribute)
        except AttributeError:
            return None
        if not isinstance(descriptor, property) or descriptor.fget is None:
            return None
        try:
            node = _function_node(descriptor.fget)
        except (OSError, TypeError, ValueError, SyntaxError):
            return None
        result = None
        for call in (
                item for item in ast.walk(node)
                if isinstance(item, ast.Call)
                and isinstance(item.func, ast.Attribute)
                and item.func.attr == "get_view"
                and item.args
        ):
            view_class = descriptor.fget.__globals__.get(
                getattr(call.args[0], "id", ""),
                _UNKNOWN,
            )
            binding = self.analyzer.add_view(owner, view_class)
            if result is None:
                result = binding
        return result

    def _resource_call_values(self, call, plural_name, singular_name):
        values = []
        for keyword in call.keywords:
            if keyword.arg not in {plural_name, singular_name}:
                continue
            value = self.resolve_value(keyword.value)
            if value is _UNKNOWN:
                self.analyzer._warn(
                    f"{self.analyzer._current_target.label}: dynamic "
                    f"get_script_page {keyword.arg} cannot be statically "
                    "validated"
                )
                continue
            candidates = value if isinstance(value, (list, tuple)) else [value]
            values.extend(str(item) for item in candidates if item)
        return values

    def _assign(self, target, binding, value):
        if not isinstance(target, ast.Name):
            return
        if binding is not None:
            self.bindings[target.id] = binding
        if value is not _UNKNOWN:
            self.values[target.id] = value


def _function_node(function):
    source = textwrap.dedent(inspect.getsource(function))
    tree = ast.parse(source)
    return next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function.__name__
    )


def _descriptor_function(descriptor):
    if isinstance(descriptor, (staticmethod, classmethod)):
        return descriptor.__func__
    return descriptor if inspect.isfunction(descriptor) else None


def _call_values(function, call, walker):
    try:
        parameters = list(inspect.signature(function).parameters.values())
    except (TypeError, ValueError):
        return {}
    if parameters and parameters[0].name in {"self", "cls"}:
        parameters = parameters[1:]
    values = {}
    for parameter, expression in zip(parameters, call.args):
        values[parameter.name] = walker.resolve_value(expression)
    for keyword in call.keywords:
        if keyword.arg:
            values[keyword.arg] = walker.resolve_value(keyword.value)
    return values


@lru_cache(maxsize=1)
def _base_page_resource_roles():
    source = textwrap.dedent(inspect.getsource(BasePage))
    class_node = next(
        node for node in ast.parse(source).body
        if isinstance(node, ast.ClassDef)
    )
    roles = {}
    role_by_getter = {
        "get_locator": "locator",
        "get_data": "data",
        "get_visual_value": "visual",
    }
    for function in (
            item for item in class_node.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
    ):
        method_roles = {}
        for call in (
                item for item in ast.walk(function)
                if isinstance(item, ast.Call)
                and isinstance(item.func, ast.Attribute)
                and item.func.attr in role_by_getter
                and item.args
                and isinstance(item.args[0], ast.Name)
        ):
            method_roles[call.args[0].id] = role_by_getter[call.func.attr]
        if method_roles:
            roles[function.name] = method_roles
    return roles


def _strict_reference(value, role, source):
    if not isinstance(value, str) or not value.startswith("$"):
        return None
    if value.startswith("$$"):
        return None
    raw = value[1:]
    prefix, separator, key = raw.partition(":")
    normalized_prefix = prefix.strip().casefold()
    if separator and normalized_prefix in {"loc", "locator"}:
        if role == "data":
            raise TypeError(f"data 参数不支持 locator 引用: {value}")
        return _ResourceReference("locator", key.strip(), source)
    if separator and normalized_prefix == "data":
        if role == "locator":
            raise TypeError(f"locator 参数不支持 data 引用: {value}")
        return _ResourceReference("data", key.strip(), source)
    key = raw.strip()
    if not key:
        return None
    return _ResourceReference(role, key, source)


def _resource_values(resource_files, resource_file):
    seen = set()
    for source in (resource_files, resource_file):
        if not source:
            continue
        candidates = source if isinstance(source, (list, tuple)) else (source,)
        for value in candidates:
            if value and str(value) not in seen:
                seen.add(str(value))
                yield str(value)


def _resource_path(root, value, label):
    value = str(value)
    if not value.endswith((".yaml", ".yml")):
        value += ".yaml"
    root = Path(root).resolve()
    path = (root / value).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError(f"{label} 文件越界: {value}") from error
    if not path.is_file():
        raise FileNotFoundError(f"{label} 文件不存在: {path}")
    return path


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError(
                f"YAML 重复 key: {key!r} (line {key_node.start_mark.line + 1})"
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _load_mapping(path, label, *, normalized_keys=False):
    path = Path(path)
    value = yaml.load(
        path.read_text(encoding="utf-8"),
        Loader=_UniqueKeyLoader,
    ) or {}
    if not isinstance(value, dict):
        raise ValueError(f"{label} YAML 必须是 mapping: {path}")
    if normalized_keys:
        seen = {}
        for key in value:
            normalized = normalize(str(key))
            if normalized in seen:
                raise ValueError(
                    f"{label} YAML key 规范化后重复: "
                    f"{seen[normalized]!r}, {key!r} ({path})"
                )
            seen[normalized] = key
    return value


def _usage_source(usage):
    sources = sorted(usage.sources)
    if len(sources) <= 2:
        return "; ".join(sources)
    return "; ".join(sources[:2]) + f"; ... ({len(sources)} Steps)"


__all__ = (
    "ResourcePreflightError",
    "SelectedStepResourceTarget",
    "preflight_selected_step_resources",
)