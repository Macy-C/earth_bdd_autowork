from __future__ import annotations

import argparse
import ast
import re
import sys
import tokenize
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import behave
from behave import step_registry as behave_step_registry
from behave.exception import ConfigError
from behave.matchers import (
    get_step_matcher_factory,
    use_default_step_matcher,
)
from behave.model import Scenario, ScenarioOutline
from behave.parser import parse_file
from behave.step_registry import AmbiguousStep

from autowork_core.runtime.resource_preflight import (
    ResourcePreflightError,
    SelectedStepResourceTarget,
    preflight_selected_step_resources,
)
from autowork_core.runtime.run_state import activated_step_scope
from autowork_core.runtime.application_lifecycle import (
    ApplicationLifecycleConfigurationError,
    state_has_application_lifecycle_callbacks,
)
from autowork_core.runtime.step_loader import (
    StepLoadingState,
    load_step_modules,
    reset_step_loading_state,
    restore_step_loading_state,
    snapshot_step_loading_state,
)
from autowork_core.runtime.step_scope import (
    collect_feature_files,
    resolved_step_scope_for_scenario,
    rule_scope_key,
    scenario_scope_key,
    step_scope_for_feature,
)
from autowork_core.runtime.tag_manager import (
    TAG_MANAGER,
    TagConfigurationError,
    TagOwner,
    scenario_tag_owner,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
STEP_DECORATORS = {"given", "when", "then", "step"}
LIFECYCLE_DECORATORS = {
    "before_app_start",
    "after_app_start",
    "after_app_stop",
}


@dataclass(frozen=True)
class StepDefinition:
    step_type: str
    pattern: str
    matcher_name: str
    file_path: Path
    line: int
    function_name: str


@dataclass(frozen=True)
class FeatureStep:
    step_type: str
    keyword: str
    text: str
    file_path: Path
    line: int


@dataclass(frozen=True)
class PreparedStepScope:
    scope: dict
    steps: dict[str, tuple]
    loading_state: StepLoadingState


@dataclass(frozen=True)
class StepPreflightResult:
    feature_count: int
    scenario_count: int
    step_count: int
    prepared_scopes: dict[str, PreparedStepScope] = field(
        repr=False,
        compare=False,
    )
    scenario_scopes: dict[str, PreparedStepScope] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )
    rule_scopes: dict[str, PreparedStepScope] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )
    resource_warnings: tuple[str, ...] = field(
        default_factory=tuple,
        repr=False,
        compare=False,
    )

    def prepared_scope_for(self, feature):
        return self.prepared_scopes.get(str(_feature_path(feature)))

    def prepared_scope_for_scenario(self, feature, scenario):
        return self.scenario_scopes.get(scenario_scope_key(feature, scenario))

    def prepared_scope_for_rule(self, feature, rule):
        return self.rule_scopes.get(rule_scope_key(feature, rule))


class StepPreflightError(ConfigError):
    pass


def reset_behave_step_state():
    get_step_matcher_factory().reset()
    use_default_step_matcher()
    behave_step_registry.registry.clear()
    behave_step_registry.setup_step_decorators(
        behave.__dict__,
        registry=behave_step_registry.registry,
    )
    reset_step_loading_state()


def activate_step_registry(prepared_scope: PreparedStepScope | None):
    reset_behave_step_state()
    if prepared_scope is None:
        return
    behave_step_registry.registry.steps = {
        step_type: list(matchers)
        for step_type, matchers in prepared_scope.steps.items()
    }
    restore_step_loading_state(prepared_scope.loading_state)


def _prepare_step_scope_files(steps_dir, files):
    files = tuple(dict.fromkeys(str(value) for value in files))
    layer_steps = []
    layer_states = []
    for layer_index, file_value in enumerate(files):
        reset_behave_step_state()
        with activated_step_scope({"files": [file_value]}):
            load_step_modules(steps_dir)
        state = snapshot_step_loading_state()
        if layer_index and _has_lifecycle_callbacks(state):
            raise ApplicationLifecycleConfigurationError(
                "Application lifecycle callbacks are allowed only in the "
                f"Feature Step scope: {file_value}"
            )
        layer_steps.append({
            step_type: tuple(matchers)
            for step_type, matchers
            in behave_step_registry.registry.steps.items()
        })
        layer_states.append(state)

    steps = _compose_layered_steps(layer_steps)
    loading_state = StepLoadingState(
        loaded_files=tuple(sorted({
            file_value
            for state in layer_states
            for file_value in state.loaded_files
        })),
        modules={
            module_name: module
            for state in layer_states
            for module_name, module in state.modules.items()
        },
        application_lifecycle=layer_states[0].application_lifecycle,
    )
    return PreparedStepScope(
        scope={"files": list(files)},
        steps=steps,
        loading_state=loading_state,
    )


def _compose_layered_steps(layer_steps):
    result = {}
    for steps in layer_steps:
        for step_type, matchers in steps.items():
            definitions = result.setdefault(step_type, {})
            for matcher in matchers:
                definitions[_matcher_identity(matcher)] = matcher
    return {
        step_type: tuple(definitions.values())
        for step_type, definitions in result.items()
    }


def _matcher_identity(matcher):
    return (
        type(matcher).__module__,
        type(matcher).__qualname__,
        str(getattr(matcher, "pattern", "")),
    )


def _matching_step_definitions(prepared_scope, step):
    result = []
    for step_type in (step.step_type, "step"):
        for matcher in prepared_scope.steps.get(step_type, ()):
            match = matcher.match(step.name)
            stored_error = getattr(match, "stored_error", None)
            if stored_error is not None:
                raise StepPreflightError(
                    "Step parameter conversion failed before startup: "
                    f"{step.name}: {type(stored_error).__name__}: "
                    f"{stored_error}"
                )
            if match is not None:
                result.append(matcher)
    return result


def _has_lifecycle_callbacks(state):
    return any(
        registrations
        for registrations in state.application_lifecycle.callbacks.values()
    )


def decorator_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id.lower()
    if isinstance(node, ast.Attribute):
        return node.attr.lower()
    return None


def decorator_pattern(node: ast.Call) -> str | None:
    if not node.args:
        return None

    first_arg = node.args[0]
    if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
        return first_arg.value
    return None


def read_python_source(file_path: Path) -> str:
    with tokenize.open(file_path) as source_file:
        return source_file.read()


def read_text_lines(file_path: Path) -> list[str]:
    try:
        return file_path.read_text(encoding="utf-8-sig").splitlines()
    except UnicodeDecodeError:
        return file_path.read_text().splitlines()


def resolve_project_path(path: Path) -> Path:
    if path.is_absolute():
        return path.resolve()
    cwd_path = path.resolve()
    if cwd_path.exists():
        return cwd_path
    return (PROJECT_ROOT / path).resolve()


def iter_python_files(steps_dir: Path):
    for file_path in sorted(steps_dir.rglob("*.py")):
        if file_path.name == "__init__.py":
            continue
        yield file_path


def iter_scope_python_files(steps_dir: Path, scope):
    seen = set()
    for file_value in scope.get("files") or []:
        file_path = (steps_dir / file_value).resolve()
        if file_path.name == "__init__.py":
            continue
        if str(file_path) not in seen:
            seen.add(str(file_path))
            yield file_path


def collect_step_definitions(steps_dir: Path, files=None):
    definitions: list[StepDefinition] = []
    syntax_errors: list[tuple[Path, SyntaxError]] = []

    scan_files = files if files is not None else iter_python_files(steps_dir)
    for file_path in scan_files:
        try:
            tree = ast.parse(read_python_source(file_path), filename=str(file_path))
        except SyntaxError as exc:
            syntax_errors.append((file_path, exc))
            continue

        matcher_name = "parse"
        for node in tree.body:
            selected_matcher = _selected_matcher_name(node)
            if selected_matcher:
                matcher_name = selected_matcher
                continue
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue

                step_type = decorator_name(decorator.func)
                if step_type not in STEP_DECORATORS:
                    continue

                pattern = decorator_pattern(decorator)
                if pattern is None:
                    continue

                definitions.append(
                    StepDefinition(
                        step_type=step_type,
                        pattern=pattern,
                        matcher_name=matcher_name,
                        file_path=file_path,
                        line=decorator.lineno,
                        function_name=node.name,
                    )
                )

    return definitions, syntax_errors


def collect_feature_steps(feature_file: Path) -> list[FeatureStep]:
    steps: list[FeatureStep] = []
    last_step_type: str | None = None
    step_line_re = re.compile(r"^\s*(Given|When|Then|And|But|\*)\s+(.+?)\s*$")

    for line_number, raw_line in enumerate(read_text_lines(feature_file), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        match = step_line_re.match(raw_line)
        if not match:
            continue

        keyword = match.group(1)
        text = match.group(2).strip()
        keyword_lower = keyword.lower()
        if keyword_lower in {"given", "when", "then"}:
            step_type = keyword_lower
            last_step_type = step_type
        elif keyword in {"And", "But"} and last_step_type:
            step_type = last_step_type
        else:
            step_type = "step"

        steps.append(
            FeatureStep(
                step_type=step_type,
                keyword=keyword,
                text=text,
                file_path=feature_file,
                line=line_number,
            )
        )

    return steps


def duplicate_key(definition: StepDefinition, cross_type: bool):
    if cross_type or definition.step_type == "step":
        return "*", definition.matcher_name, definition.pattern
    return definition.step_type, definition.matcher_name, definition.pattern


def find_duplicates(definitions: list[StepDefinition], cross_type: bool):
    grouped = defaultdict(list)
    for definition in definitions:
        grouped[duplicate_key(definition, cross_type)].append(definition)
    return {key: items for key, items in grouped.items() if len(items) > 1}


def find_undefined_steps(
        feature_steps: list[FeatureStep],
        definitions: list[StepDefinition],
):
    return [
        feature_step
        for feature_step in feature_steps
        if not any(
            step_definition_matches(definition, feature_step)
            for definition in definitions
        )
    ]


def step_definition_matches(
        definition: StepDefinition,
        feature_step: FeatureStep,
) -> bool:
    if (
        definition.step_type != "step"
        and definition.step_type != feature_step.step_type
    ):
        return False
    matcher_class = get_step_matcher_factory().step_matcher_class_mapping.get(
        definition.matcher_name
    )
    if matcher_class is None:
        return False
    try:
        matcher = matcher_class(
            lambda _context, **_kwargs: None,
            definition.pattern,
            step_type=definition.step_type,
        )
        return matcher.match(feature_step.text) is not None
    except (KeyError, TypeError, ValueError):
        return re.fullmatch(
            step_pattern_to_regex(definition.pattern),
            feature_step.text,
        ) is not None


def step_pattern_to_regex(pattern: str) -> str:
    regex_parts = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "{":
            end_index = pattern.find("}", index + 1)
            if end_index != -1:
                regex_parts.append(".+")
                index = end_index + 1
                continue
        regex_parts.append(re.escape(char))
        index += 1
    return "".join(regex_parts)


def display_path(file_path: Path, base_dir: Path) -> str:
    try:
        return str(file_path.resolve().relative_to(base_dir.resolve()))
    except ValueError:
        return str(file_path)


def print_syntax_errors(
        syntax_errors: list[tuple[Path, SyntaxError]],
        base_dir: Path,
):
    print("Python syntax errors found while scanning step files:")
    for file_path, exc in syntax_errors:
        location = display_path(file_path, base_dir)
        print(f"  {location}:{exc.lineno}:{exc.offset}: {exc.msg}")


def print_duplicates(duplicates, base_dir: Path):
    print("Duplicate Behave step definitions found:")
    for (step_type, matcher_name, pattern), definitions in sorted(
            duplicates.items()
    ):
        label = step_type.upper() if step_type != "*" else "ANY"
        print(f"\n[{label}/{matcher_name}] {pattern}")
        for definition in sorted(
                definitions,
                key=lambda item: (str(item.file_path), item.line),
        ):
            location = display_path(definition.file_path, base_dir)
            print(
                f"  {location}:{definition.line} "
                f"in {definition.function_name}"
            )


def print_feature_duplicates(feature_file: Path, duplicates, base_dir: Path):
    print(f"\nFeature: {display_path(feature_file, base_dir)}")
    print_duplicates(duplicates, base_dir)


def print_undefined_steps(
        feature_file: Path,
        undefined_steps: list[FeatureStep],
        base_dir: Path,
):
    print(f"\nFeature: {display_path(feature_file, base_dir)}")
    print("Undefined feature steps found:")
    for step in undefined_steps:
        print(
            f"  {display_path(step.file_path, base_dir)}:{step.line} "
            f"[{step.step_type.upper()}] {step.keyword} {step.text}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Statically check Behave steps without importing step modules."
    )
    parser.add_argument(
        "steps_dir",
        nargs="?",
        type=Path,
        default=PROJECT_ROOT / "Bdd" / "steps",
        help="Directory containing Behave step files.",
    )
    parser.add_argument(
        "--cross-type",
        action="store_true",
        help="Also report the same step text reused across Given/When/Then.",
    )
    parser.add_argument(
        "--feature-path",
        type=Path,
        help=(
            "Feature file or directory. Checks each inferred/@stepfile scope "
            "for exact duplicates and statically undefined feature steps."
        ),
    )
    parser.add_argument(
        "--no-undefined",
        action="store_true",
        help="Only check exact duplicate definitions.",
    )
    parser.add_argument(
        "--explain-scopes",
        action="store_true",
        help="Show the resolved layered Step files for each Scenario.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    steps_dir = resolve_project_path(args.steps_dir)
    base_dir = Path.cwd()

    if not steps_dir.exists():
        print(f"Steps directory does not exist: {steps_dir}", file=sys.stderr)
        return 2
    if not steps_dir.is_dir():
        print(f"Steps path is not a directory: {steps_dir}", file=sys.stderr)
        return 2

    if args.feature_path:
        return check_features(
            steps_dir,
            resolve_project_path(args.feature_path),
            base_dir,
            cross_type=args.cross_type,
            check_undefined=not args.no_undefined,
            explain_scopes=args.explain_scopes,
        )

    definitions, syntax_errors = collect_step_definitions(steps_dir)
    if syntax_errors:
        print_syntax_errors(syntax_errors, base_dir)
        return 2

    duplicates = find_duplicates(definitions, args.cross_type)
    if duplicates:
        print_duplicates(duplicates, base_dir)
        return 1

    scanned_files = {definition.file_path for definition in definitions}
    print("No exact duplicate Behave step definitions found.")
    print(
        f"Scanned {len(definitions)} step definitions "
        f"in {len(scanned_files)} files."
    )
    return 0


def check_features(
        steps_dir: Path,
        feature_path: Path,
        base_dir: Path,
        *,
        cross_type: bool,
        check_undefined: bool,
    required_step_texts=None,
        required_scenario=None,
    explain_scopes=False,
) -> int:
    feature_files = collect_feature_files(feature_path)
    if not feature_files:
        print(f"No feature files found: {feature_path}", file=sys.stderr)
        return 2

    failed = False
    config_error = False
    total_definitions = 0
    total_feature_steps = 0
    scanned_step_files = set()
    definition_cache = {}
    counted_definitions = {}
    required = (
        {str(item) for item in required_step_texts}
        if required_step_texts is not None
        else None
    )
    for feature_file in feature_files:
        try:
            feature = parse_file(str(feature_file))
        except Exception as exc:
            print(f"\nFeature: {display_path(feature_file, base_dir)}")
            print(f"Feature parse error: {exc}")
            config_error = True
            continue
        matching_scenario_found = False
        for scenario in feature.walk_scenarios():
            if required_scenario and not _scenario_matches_requirement(
                scenario,
                required_scenario,
            ):
                continue
            matching_scenario_found = True
            if required is not None and not any(
                str(step.name) in required
                for step in scenario.all_steps
            ):
                continue
            try:
                scope = resolved_step_scope_for_scenario(
                    feature,
                    scenario,
                    steps_dir,
                )
            except (FileNotFoundError, TagConfigurationError, ValueError) as exc:
                print(f"\nFeature: {display_path(feature_file, base_dir)}")
                print(f"Step scope error: {scenario.name}: {exc}")
                config_error = True
                continue
            if explain_scopes:
                statuses = dict(scope.file_statuses)
                files = " -> ".join(
                    f"{file_value} [{statuses[file_value]}]"
                    for file_value in scope.files
                )
                print(
                    f"Scope: {scenario.name} | origin={scope.origin} | "
                    f"{files}"
                )

            layers = []
            scenario_has_syntax_error = False
            for layer_index, file_value in enumerate(scope.files):
                file_path = (steps_dir / file_value).resolve()
                cached = definition_cache.get(file_path)
                if cached is None:
                    cached = collect_step_definitions(
                        steps_dir,
                        files=[file_path],
                    )
                    definition_cache[file_path] = cached
                definitions, syntax_errors = cached
                counted_definitions[file_path] = definitions
                scanned_step_files.add(file_path)
                if syntax_errors:
                    print(
                        f"\nFeature: {display_path(feature_file, base_dir)}"
                    )
                    print_syntax_errors(syntax_errors, base_dir)
                    failed = True
                    scenario_has_syntax_error = True
                if layer_index and _file_has_lifecycle_callback(file_path):
                    print(f"\nFeature: {display_path(feature_file, base_dir)}")
                    print(
                        "Application lifecycle callbacks are allowed only "
                        f"in the Feature Step scope: {file_value}"
                    )
                    failed = True
                layer_duplicates = find_duplicates(definitions, cross_type)
                if layer_duplicates:
                    print_feature_duplicates(
                        feature_file,
                        layer_duplicates,
                        base_dir,
                    )
                    failed = True
                layers.append(definitions)
            if scenario_has_syntax_error:
                continue

            definitions = _compose_layered_definitions(layers)
            cross_layer_duplicates = find_duplicates(
                definitions,
                cross_type,
            )
            if cross_layer_duplicates:
                print_feature_duplicates(
                    feature_file,
                    cross_layer_duplicates,
                    base_dir,
                )
                failed = True

            feature_steps = [
                FeatureStep(
                    step_type=str(step.step_type),
                    keyword=str(step.keyword).strip(),
                    text=str(step.name),
                    file_path=feature_file,
                    line=int(step.location.line),
                )
                for step in scenario.all_steps
                if required is None or str(step.name) in required
            ]
            total_feature_steps += len(feature_steps)
            if check_undefined:
                undefined_steps = []
                for feature_step in feature_steps:
                    matches = [
                        definition
                        for definition in definitions
                        if step_definition_matches(
                            definition,
                            feature_step,
                        )
                    ]
                    if len(matches) > 1:
                        print(
                            f"\nFeature: "
                            f"{display_path(feature_file, base_dir)}"
                        )
                        print(
                            "Ambiguous Step definition across layered "
                            f"scope: {feature_step.text}"
                        )
                        for definition in matches:
                            print(
                                "  "
                                f"{display_path(definition.file_path, base_dir)}"
                                f":{definition.line} in "
                                f"{definition.function_name}"
                            )
                        failed = True
                    elif not matches:
                        undefined_steps.append(feature_step)
                if undefined_steps:
                    print_undefined_steps(
                        feature_file,
                        undefined_steps,
                        base_dir,
                    )
                    failed = True

        if required_scenario and not matching_scenario_found:
            print(f"\nFeature: {display_path(feature_file, base_dir)}")
            print(
                "Step scope error: target Scenario/Examples row not found: "
                f"{required_scenario.get('name')!r} / "
                f"{required_scenario.get('example_id')!r}"
            )
            config_error = True

    total_definitions = sum(
        len(definitions)
        for definitions in counted_definitions.values()
    )

    if config_error:
        return 2
    if failed:
        return 1

    if check_undefined:
        print("No exact duplicate or statically undefined steps found for feature scopes.")
    else:
        print("No exact duplicate step definitions found for feature scopes.")
    print(
        f"Scanned {len(feature_files)} feature files, "
        f"{total_feature_steps} feature steps, "
        f"{total_definitions} scoped step definitions "
        f"in {len(scanned_step_files)} files."
    )
    return 0


def _compose_layered_definitions(layers):
    definitions = {}
    for layer in layers:
        for definition in layer:
            definitions[(
                definition.step_type,
                definition.matcher_name,
                definition.pattern,
            )] = definition
    return list(definitions.values())


def _scenario_matches_requirement(scenario, required):
    row = getattr(scenario, "_row", None)
    example_id = str(getattr(row, "id", "") or "") or None
    name = re.sub(
        r"\s+--\s+@\d+\.\d+.*$",
        "",
        str(scenario.name),
    ).strip()
    required_name = str(required.get("name") or "").strip()
    required_example = required.get("example_id")
    return bool(
        name == required_name
        and example_id == required_example
    )


def _file_has_lifecycle_callback(file_path):
    try:
        tree = ast.parse(
            read_python_source(file_path),
            filename=str(file_path),
        )
    except SyntaxError:
        return False
    return any(
        (
            decorator_name(decorator.func)
            if isinstance(decorator, ast.Call)
            else decorator_name(decorator)
        ) in LIFECYCLE_DECORATORS
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for decorator in node.decorator_list
    )


def _selected_matcher_name(node):
    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        return None
    call = node.value
    if decorator_name(call.func) != "use_step_matcher" or not call.args:
        return None
    value = call.args[0]
    if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
        return None
    return value.value


def preflight_feature_steps(
        features,
        config,
        steps_dir,
        *,
        explicit_scope=None,
        application_launch_mode=None,
    resource_locators_dir=None,
    resource_data_dir=None,
) -> StepPreflightResult:
    steps_dir = Path(steps_dir).resolve()
    feature_count = 0
    scenario_count = 0
    step_count = 0
    undefined_steps = []
    prepared_scopes = {}
    scenario_scopes = {}
    rule_scopes = {}
    prepared_by_files = {}
    resource_targets = []

    try:
        for feature in features:
            if _framework_skip_reason(feature, TagOwner.FEATURE):
                continue
            scenarios = list(_iter_selected_scenarios(feature, config))
            if not scenarios:
                continue

            feature_file = _feature_path(feature)
            for scenario in scenarios:
                try:
                    if explicit_scope:
                        files = tuple(explicit_scope.get("files") or ())
                    else:
                        resolved = resolved_step_scope_for_scenario(
                            feature,
                            scenario,
                            steps_dir,
                        )
                        files = resolved.files
                    prepared_scope = prepared_by_files.get(files)
                    if prepared_scope is None:
                        prepared_scope = _prepare_step_scope_files(
                            steps_dir,
                            files,
                        )
                        prepared_by_files[files] = prepared_scope
                except AmbiguousStep as exc:
                    raise StepPreflightError(
                        "Ambiguous Step definition in scope for "
                        f"{feature_file} / {scenario.name}: {exc}"
                    ) from exc
                except (
                    ApplicationLifecycleConfigurationError,
                    FileNotFoundError,
                    TagConfigurationError,
                    ValueError,
                ) as exc:
                    raise StepPreflightError(str(exc)) from exc

                _validate_application_lifecycle_configuration(
                    [scenario],
                    prepared_scope.loading_state.application_lifecycle,
                    application_launch_mode,
                )
                scenario_prepared_scope = prepared_scope
                if not explicit_scope:
                    scenario_prepared_scope = PreparedStepScope(
                        scope=resolved.runtime_scope(),
                        steps=prepared_scope.steps,
                        loading_state=prepared_scope.loading_state,
                    )
                scenario_scopes[
                    scenario_scope_key(feature, scenario)
                ] = scenario_prepared_scope
                template = (
                    scenario.parent
                    if isinstance(getattr(scenario, "parent", None), ScenarioOutline)
                    else scenario
                )
                rule = getattr(template, "parent", None)
                if (
                        not explicit_scope
                        and getattr(rule, "keyword", None) == "Rule"
                        and rule_scope_key(feature, rule) not in rule_scopes
                ):
                    rule_files = tuple(
                        declaration.step_file
                        for declaration in resolved.declarations
                        if declaration.owner in {"Feature", "Rule"}
                    )
                    rule_files = tuple(dict.fromkeys(rule_files))
                    rule_prepared = prepared_by_files.get(rule_files)
                    if rule_prepared is None:
                        rule_prepared = _prepare_step_scope_files(
                            steps_dir,
                            rule_files,
                        )
                        prepared_by_files[rule_files] = rule_prepared
                    rule_scope = resolved.runtime_scope(
                        through_owner="Rule"
                    )
                    rule_scopes[rule_scope_key(feature, rule)] = (
                        PreparedStepScope(
                            scope=rule_scope,
                            steps=rule_prepared.steps,
                            loading_state=rule_prepared.loading_state,
                        )
                    )
                if str(feature_file) not in prepared_scopes:
                    base_files = (files[0],)
                    base_scope = prepared_by_files.get(base_files)
                    if base_scope is None:
                        base_scope = _prepare_step_scope_files(
                            steps_dir,
                            base_files,
                        )
                        prepared_by_files[base_files] = base_scope
                    prepared_scopes[str(feature_file)] = base_scope

            feature_count += 1
            scenario_count += len(scenarios)
            for scenario in scenarios:
                prepared_scope = scenario_scopes[
                    scenario_scope_key(feature, scenario)
                ]
                for step in scenario.all_steps:
                    step_count += 1
                    matches = _matching_step_definitions(
                        prepared_scope,
                        step,
                    )
                    if len(matches) > 1:
                        descriptions = ", ".join(
                            matcher.describe(matcher.SCHEMA_WITH_LOCATION)
                            for matcher in matches
                        )
                        raise StepPreflightError(
                            "Ambiguous Step definition across layered scope "
                            f"for {feature_file} / {scenario.name} / "
                            f"{step.name}: {descriptions}"
                        )
                    if not matches:
                        undefined_steps.append(step)
                    else:
                        resource_targets.append(SelectedStepResourceTarget(
                            feature_name=str(feature.name),
                            scenario_name=str(scenario.name),
                            step_text=str(step.name),
                            function=matches[0].func,
                            scope_key=scenario_scope_key(feature, scenario),
                        ))
    finally:
        reset_behave_step_state()

    if undefined_steps:
        details = "\n".join(
            f"  {step.location}: [{step.step_type.upper()}] {step.keyword} {step.name}"
            for step in undefined_steps
        )
        error = StepPreflightError(
            "Undefined Steps found before test startup:\n" + details
        )
        error.undefined_steps = tuple(undefined_steps)
        raise error

    try:
        resource_warnings = preflight_selected_step_resources(
            resource_targets,
            steps_dir=steps_dir,
            locators_dir=resource_locators_dir,
            data_dir=resource_data_dir,
        )
    except ResourcePreflightError as error:
        raise StepPreflightError(str(error)) from error

    return StepPreflightResult(
        feature_count=feature_count,
        scenario_count=scenario_count,
        step_count=step_count,
        prepared_scopes=prepared_scopes,
        scenario_scopes=scenario_scopes,
        rule_scopes=rule_scopes,
        resource_warnings=resource_warnings,
    )


def _feature_path(feature) -> Path:
    path = Path(feature.filename)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def _iter_selected_scenarios(container, config):
    for item in getattr(container, "run_items", ()):
        if isinstance(item, ScenarioOutline):
            for scenario in item.scenarios:
                if (
                    scenario.should_run(config)
                    and not _framework_skip_reason(
                        scenario,
                        scenario_tag_owner(scenario),
                    )
                ):
                    yield scenario
        elif isinstance(item, Scenario):
            if (
                item.should_run(config)
                and not _framework_skip_reason(
                    item,
                    scenario_tag_owner(item),
                )
            ):
                yield item
        else:
            yield from _iter_selected_scenarios(item, config)


def _framework_skip_reason(statement, owner):
    return _runtime_decision(statement, owner).skip_reason


def _runtime_decision(statement, owner):
    try:
        return TAG_MANAGER.resolve_runtime(
            statement.tags,
            owner,
            statement.name,
            effective_tags=statement.effective_tags,
        )
    except TagConfigurationError as exc:
        raise StepPreflightError(str(exc)) from exc


def _validate_application_lifecycle_configuration(
        scenarios,
        lifecycle_state,
        application_launch_mode,
):
    if str(application_launch_mode or "").strip().lower() != "attach":
        return
    for scenario in scenarios:
        decision = _runtime_decision(scenario, scenario_tag_owner(scenario))
        if decision.api_only:
            continue
        if state_has_application_lifecycle_callbacks(lifecycle_state, scenario):
            raise StepPreflightError(
                "Application lifecycle callbacks require "
                "app_launch_mode='auto'; attach mode has no framework-owned "
                f"application start/stop boundary: {scenario.name}"
            )


__all__ = [
    "FeatureStep",
    "PreparedStepScope",
    "StepDefinition",
        "activate_step_registry",
    "StepPreflightError",
    "StepPreflightResult",
    "build_parser",
    "check_features",
    "collect_feature_steps",
    "collect_step_definitions",
    "find_duplicates",
    "find_undefined_steps",
    "main",
    "preflight_feature_steps",
    "reset_behave_step_state",
    "step_definition_matches",
    "step_pattern_to_regex",
]


if __name__ == "__main__":
    raise SystemExit(main())
