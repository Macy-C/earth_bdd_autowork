from __future__ import annotations

import argparse
import ast
import builtins
import io
import json
import os
import re
import socket
import subprocess
import sys
import time
import tokenize
from collections import defaultdict
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

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


class _SelectedResourceUnavailable(RuntimeError):
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
    parser.add_argument(
        "--scenario",
        help="Check one Scenario in --feature-path without importing Steps.",
    )
    parser.add_argument(
        "--example-id",
        help="Select one expanded Outline example by its existing row ID.",
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Return a selected-Scenario report with per-check timings.",
    )
    parser.add_argument(
        "--resources",
        action="store_true",
        help="Preflight resources through guarded loading of trusted local declarations.",
    )
    parser.add_argument(
        "--prepare-generation",
        action="store_true",
        help=(
            "Output a static JSON context bundle for ordinary scenario generation "
            "without importing step/page modules or reading existing target step bodies."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    steps_dir = resolve_project_path(args.steps_dir)
    base_dir = Path.cwd()

    if args.example_id and not args.scenario:
        parser.error("--example-id requires --scenario")
    if args.json_output and not args.scenario:
        parser.error("--json requires --scenario and --feature-path")
    if args.resources and not args.scenario:
        parser.error("--resources requires --scenario and --feature-path")
    if args.prepare_generation and (not args.scenario or not args.feature_path):
        parser.error("--prepare-generation requires --scenario and --feature-path")
    if args.prepare_generation:
        bundle = prepare_generation_context(
            steps_dir,
            resolve_project_path(args.feature_path),
            base_dir,
            scenario_name=args.scenario,
            example_id=args.example_id,
        )
        if args.json_output:
            print(json.dumps(bundle, ensure_ascii=False, separators=(",", ":")))
        else:
            print(format_generation_context(bundle))
        return bundle["exit_code"]
    if args.scenario:
        if not args.feature_path:
            parser.error("--scenario requires --feature-path")
        if args.no_undefined:
            parser.error("Selected-Scenario checks cannot disable undefined checks")
        report = check_selected_scenario(
            steps_dir,
            resolve_project_path(args.feature_path),
            base_dir,
            scenario_name=args.scenario,
            example_id=args.example_id,
            cross_type=args.cross_type,
            resources=args.resources,
        )
        if args.json_output:
            print(json.dumps(report, ensure_ascii=False))
        else:
            for name, result in report["checks"].items():
                print(f"{name}: {result['status']}")
                if result.get("details"):
                    print(result["details"])
                for warning in result.get("warnings", ()):
                    print(f"WARNING: {warning}")
            print(f"duration_seconds: {report['duration_seconds']:.6f}")
        return report["exit_code"]

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


def check_selected_scenario(
        steps_dir,
        feature_path,
        base_dir,
        *,
        scenario_name,
        example_id=None,
        cross_type=False,
        resources=False,
):
    started = time.perf_counter()
    report = {
        "selection": {
            "feature": display_path(Path(feature_path), Path(base_dir)),
            "scenario": scenario_name,
            "example_id": example_id,
            "scope_files": [],
        },
        "checks": {
            "syntax": {"status": "not_run", "duration_seconds": 0},
            "bindings": {"status": "not_run", "duration_seconds": 0},
            "resources": {
                "status": "not_run" if resources else "not_requested",
                "duration_seconds": 0,
            },
        },
        "status": "failed",
        "exit_code": 2,
        "limitations": [
            "Static checks do not execute Steps or validate product behavior.",
            "Resource loading supports trusted local declarations, not arbitrary Python.",
            "Named-reference preflight does not validate every Step-local locator or UI match.",
        ],
    }
    stage_started = started
    stage = "syntax"
    try:
        steps_dir = Path(steps_dir).resolve()
        feature_path = Path(feature_path).resolve()
        if not steps_dir.is_dir() or not feature_path.is_file():
            raise ValueError("Selected check requires a Step directory and one Feature file")
        feature = parse_file(str(feature_path))
        required = {"name": scenario_name, "example_id": example_id}
        scenarios = [
            scenario
            for scenario in feature.walk_scenarios()
            if _scenario_matches_requirement(scenario, required)
        ]
        if len(scenarios) != 1:
            raise ValueError(
                "Select exactly one Scenario/Example; check its name and --example-id"
            )
        scope = resolved_step_scope_for_scenario(feature, scenarios[0], steps_dir)
        report["selection"]["scope_files"] = list(scope.files)
        for file_value in scope.files:
            file_path = (steps_dir / file_value).resolve()
            compile(read_python_source(file_path), str(file_path), "exec")
        report["checks"][stage] = {
            "status": "passed",
            "duration_seconds": time.perf_counter() - stage_started,
        }

        stage = "bindings"
        stage_started = time.perf_counter()
        details = io.StringIO()
        with redirect_stdout(details), redirect_stderr(details):
            result = check_features(
                steps_dir,
                feature_path,
                Path(base_dir),
                cross_type=cross_type,
                check_undefined=True,
                required_scenario=required,
            )
        report["checks"][stage] = {
            "status": "passed" if result == 0 else "failed",
            "details": details.getvalue().strip(),
            "duration_seconds": time.perf_counter() - stage_started,
        }
        report["status"] = "passed" if result == 0 else "failed"
        report["exit_code"] = result
        if result == 0 and resources:
            stage = "resources"
            stage_started = time.perf_counter()
            details = io.StringIO()
            with redirect_stdout(details), redirect_stderr(details):
                warnings = _check_selected_resources(
                    feature, scenarios[0], scope, steps_dir
                )
            report["checks"][stage] = {
                "status": "passed",
                "warnings": list(warnings),
                "details": details.getvalue().strip(),
                "duration_seconds": time.perf_counter() - stage_started,
            }
    except _SelectedResourceUnavailable as error:
        report["checks"][stage] = {
            "status": "unverified",
            "details": str(error),
            "duration_seconds": time.perf_counter() - stage_started,
        }
        report["status"] = "partial"
        report["exit_code"] = 2
    except ResourcePreflightError as error:
        report["checks"][stage] = {
            "status": "failed",
            "details": str(error),
            "duration_seconds": time.perf_counter() - stage_started,
        }
        report["status"] = "failed"
        report["exit_code"] = 1
    except Exception as error:
        report["checks"][stage] = {
            "status": "failed",
            "details": f"{type(error).__name__}: {error}",
            "duration_seconds": time.perf_counter() - stage_started,
        }
    finally:
        report["duration_seconds"] = time.perf_counter() - started
    return report


def prepare_generation_context(
        steps_dir,
        feature_path,
        base_dir,
        *,
        scenario_name,
        example_id=None,
):
    started = time.perf_counter()
    base_dir = Path(base_dir).resolve()
    bundle = {
        "selection": {
            "feature": display_path(Path(feature_path), base_dir),
            "scenario": scenario_name,
            "example_id": example_id,
        },
        "status": "failed",
        "exit_code": 2,
    }
    try:
        steps_dir = Path(steps_dir).resolve()
        feature_path = Path(feature_path).resolve()
        if not steps_dir.is_dir() or not feature_path.is_file():
            raise ValueError(
                "Generation context preparation requires a Step directory and one Feature file"
            )
        project_root = (
            steps_dir.parent.parent
            if steps_dir.parent.name.casefold() == "bdd"
            else steps_dir.parent
        )
        bdd_dir = steps_dir.parent
        feature = parse_file(str(feature_path))
        required = {"name": scenario_name, "example_id": example_id}
        scenarios = [
            item
            for item in feature.walk_scenarios()
            if _scenario_matches_requirement(item, required)
        ]
        outline_template = None
        if not scenarios and example_id is None:
            name_matches = [
                item
                for item in feature.walk_scenarios()
                if _scenario_name_matches(item, scenario_name)
            ]
            parent_templates = {
                id(getattr(item, "parent", item)): getattr(item, "parent", item)
                for item in name_matches
            }
            if name_matches and len(parent_templates) == 1:
                candidate_template = next(iter(parent_templates.values()))
                if isinstance(candidate_template, ScenarioOutline):
                    outline_template = candidate_template
                    scenarios = name_matches
        if not scenarios or (outline_template is None and len(scenarios) != 1):
            raise ValueError(
                "Select exactly one Scenario/Example; check its name and --example-id"
            )
        scenario = scenarios[0]
        scope = resolved_step_scope_for_scenario(
            feature,
            scenario,
            steps_dir,
            require_files=False,
        )
        target_step_path = (steps_dir / scope.entry_file).resolve()
        companion_md_path = target_step_path.with_suffix(".md")
        target_step_bytes = (
            target_step_path.stat().st_size
            if target_step_path.is_file()
            else 0
        )
        companion_md_bytes = (
            companion_md_path.stat().st_size
            if companion_md_path.is_file()
            else 0
        )
        companion_status = _read_companion_frontmatter_status(companion_md_path)
        missing_parent_scope_files = [
            display_path((steps_dir / file_value).resolve(), base_dir).replace("\\", "/")
            for file_value in scope.files
            if file_value != scope.entry_file and not (steps_dir / file_value).is_file()
        ]
        validation_example_ids = [
            str(getattr(getattr(item, "_row", None), "id", "") or "")
            for item in scenarios
            if getattr(getattr(item, "_row", None), "id", None)
        ]
        bundle["selection"].update({
            "feature_name": str(feature.name),
            "is_scenario_outline": outline_template is not None or bool(validation_example_ids),
            "validation_example_ids": validation_example_ids,
            "scope_files": list(scope.files),
            "entry_file": scope.entry_file,
            "scope_origin": scope.origin,
            "file_statuses": dict(scope.file_statuses),
            "missing_parent_scope_files": missing_parent_scope_files,
            "target_step_file": display_path(target_step_path, base_dir).replace("\\", "/"),
            "target_step_exists": target_step_path.is_file(),
            "target_step_bytes": target_step_bytes,
            "companion_md_file": display_path(companion_md_path, base_dir).replace("\\", "/"),
            "companion_md_exists": companion_md_path.is_file(),
            "companion_md_bytes": companion_md_bytes,
            "companion_status": companion_status,
            "generation_mode": (
                "resume"
                if companion_status in {"needs_confirmation", "blocked"} and companion_md_bytes > 0
                else "initial"
            ),
        })
        if outline_template is not None:
            bundle["examples"] = [
                {
                    "name": str(ex.name or ""),
                    "headings": [str(h) for h in ex.table.headings],
                    "rows": [
                        {
                            "example_id": f"{ex_idx}.{row_idx}",
                            "cells": [str(cell) for cell in row.cells],
                        }
                        for row_idx, row in enumerate(ex.table.rows, start=1)
                    ],
                }
                for ex_idx, ex in enumerate(getattr(outline_template, "examples", ()), start=1)
                if getattr(ex, "table", None) is not None
            ]

        shared_files = [
            (steps_dir / file_value).resolve()
            for file_value in scope.files
            if file_value != scope.entry_file and (steps_dir / file_value).is_file()
        ]
        shared_layers = []
        for shared_path in shared_files:
            definitions, _ = collect_step_definitions(steps_dir, files=[shared_path])
            shared_layers.append(definitions)
        shared_definitions = _compose_layered_definitions(shared_layers)

        raw_steps = (
            [*(getattr(scenario, "background_steps", None) or ()), *outline_template.steps]
            if outline_template is not None
            else list(scenario.all_steps)
        )
        expanded_steps = list(scenario.all_steps)
        scenario_steps = []
        for index, step in enumerate(raw_steps, start=1):
            expanded_step = expanded_steps[index - 1] if index - 1 < len(expanded_steps) else step
            feature_step = FeatureStep(
                step_type=str(expanded_step.step_type),
                keyword=str(step.keyword).strip(),
                text=str(expanded_step.name),
                file_path=feature_path,
                line=int(step.location.line),
            )
            matched_shared = [
                definition
                for definition in shared_definitions
                if step_definition_matches(definition, feature_step)
            ]
            step_table = getattr(step, "table", None)
            table_payload = (
                {
                    "headings": [str(heading) for heading in step_table.headings],
                    "rows": [
                        [str(cell) for cell in row.cells]
                        for row in step_table.rows
                    ],
                }
                if step_table is not None
                else None
            )
            step_item = {
                "index": index,
                "step_type": feature_step.step_type,
                "keyword": feature_step.keyword,
                "text": str(step.name),
                "line": feature_step.line,
                "bound_in_shared_scope": (
                    {
                        "file": display_path(matched_shared[-1].file_path, base_dir).replace("\\", "/"),
                        "line": matched_shared[-1].line,
                        "function": matched_shared[-1].function_name,
                    }
                    if matched_shared
                    else None
                ),
            }
            if table_payload is not None:
                step_item["table"] = table_payload
            if getattr(step, "text", None):
                step_item["text_block"] = str(step.text)
            scenario_steps.append(step_item)
        bundle["scenario_steps"] = scenario_steps

        candidate_step_files = list(shared_files)
        if target_step_path.parent.is_dir() and target_step_path.parent != steps_dir:
            for sibling_path in sorted(target_step_path.parent.glob("*.py")):
                resolved_sibling = sibling_path.resolve()
                if (
                    resolved_sibling == target_step_path
                    or resolved_sibling.name == "__init__.py"
                    or resolved_sibling in candidate_step_files
                ):
                    continue
                candidate_step_files.append(resolved_sibling)

        sibling_steps = []
        referenced_page_names: set[str] = set()
        referenced_view_attrs: dict[str, set[str]] = defaultdict(set)
        for step_file_path in candidate_step_files[:6]:
            definitions, _ = collect_step_definitions(steps_dir, files=[step_file_path])
            source_text = read_python_source(step_file_path)
            lines = source_text.splitlines()
            try:
                step_tree = ast.parse(source_text, filename=str(step_file_path))
            except SyntaxError:
                step_tree = None
            if step_tree is not None:
                pages_used, views_used = _extract_step_ast_usage(step_tree)
                referenced_page_names.update(pages_used)
                for page_name, view_names in views_used.items():
                    referenced_view_attrs[page_name].update(view_names)
            sibling_steps.append({
                "file": display_path(step_file_path, base_dir).replace("\\", "/"),
                "empty": not bool(source_text.strip()),
                "lines": len(lines),
                "step_definitions": [
                    {
                        "step_type": item.step_type,
                        "pattern": item.pattern,
                        "function": item.function_name,
                        "line": item.line,
                    }
                    for item in definitions
                ],
            })
        bundle["sibling_steps"] = sibling_steps

        scenario_text_parts = [
            str(feature.name),
            *(str(d) for d in getattr(feature, "description", ()) or ()),
            *(str(r.name) for r in getattr(feature, "rules", ()) or ()),
            str(scenario_name),
            *(item["text"] for item in scenario_steps),
        ]
        for item in scenario_steps:
            if "table" in item:
                scenario_text_parts.extend(item["table"].get("headings", ()))
                for row in item["table"].get("rows", ()):
                    scenario_text_parts.extend(row)
        scenario_tokens = _tokenize_domain_terms(" ".join(scenario_text_parts))

        referenced_pages, other_pages = _build_page_and_locator_catalog(
            bdd_dir=bdd_dir,
            project_root=project_root,
            base_dir=base_dir,
            steps_dir=steps_dir,
            referenced_page_names=referenced_page_names,
            referenced_view_attrs=referenced_view_attrs,
            scenario_tokens=scenario_tokens,
        )
        bundle["knowledge_bases"] = _read_project_knowledge_bases(bdd_dir / "config.yaml")
        bundle["referenced_pages"] = referenced_pages
        bundle["other_pages"] = other_pages
        bundle["status"] = "ok"
        bundle["exit_code"] = 0
    except Exception as error:
        bundle["error"] = f"{type(error).__name__}: {error}"
        bundle["status"] = "failed"
        bundle["exit_code"] = 2
    finally:
        bundle["duration_seconds"] = time.perf_counter() - started
    return bundle


def _read_project_knowledge_bases(config_path: Path) -> dict[str, str]:
    if not config_path.is_file():
        return {}
    try:
        import yaml

        raw = yaml.safe_load(config_path.read_text(encoding="utf-8-sig")) or {}
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    weknora = raw.get("WEKNORA_SETTING") or raw.get("weknora_setting") or {}
    if not isinstance(weknora, dict):
        return {}
    kb_map = weknora.get("KNOWLEDGE_BASES") or weknora.get("knowledge_bases") or {}
    if not isinstance(kb_map, dict):
        return {}
    result: dict[str, str] = {}
    for role in ("ENGINEERING_CASES", "FRAMEWORK_DOCS", "PRODUCT_DOCS", "STATIC_UI_DOCS"):
        val = str(kb_map.get(role) or kb_map.get(role.lower()) or "").strip()
        if val:
            result[role] = val
    return result


def _read_companion_frontmatter_status(md_path: Path) -> str | None:
    if not md_path.is_file():
        return None
    try:
        lines = md_path.read_text(encoding="utf-8-sig").splitlines()[:25]
    except OSError:
        return None
    if not lines:
        return None
    if lines[0].strip() == "---":
        for line in lines[1:]:
            stripped = line.strip()
            if stripped == "---":
                break
            if stripped.lower().startswith("status:"):
                return stripped.split(":", 1)[1].strip().strip("`\"'") or None
        return None
    status_pattern = re.compile(
        r"^(?:[-*]\s+)?(?:\*\*)?status(?:\*\*)?\s*:\s*`?([A-Za-z0-9_-]+)`?",
        re.IGNORECASE,
    )
    for line in lines:
        match = status_pattern.match(line.strip())
        if match:
            return match.group(1).strip() or None
    return None


def _extract_step_ast_usage(tree: ast.AST) -> tuple[set[str], dict[str, set[str]]]:
    known_page_imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and "page_obj" in node.module:
            for alias in node.names:
                known_page_imports.add(alias.asname or alias.name)
    used_pages: set[str] = set()
    views_by_page: dict[str, set[str]] = defaultdict(set)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        var_to_page: dict[str, str] = {}
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Name)
                and sub.func.id == "get_page"
                and len(sub.args) >= 2
                and isinstance(sub.args[1], ast.Name)
            ):
                used_pages.add(sub.args[1].id)
            if isinstance(sub, ast.Assign) and len(sub.targets) == 1 and isinstance(sub.targets[0], ast.Name):
                target_name = sub.targets[0].id
                val = sub.value
                if (
                    isinstance(val, ast.Call)
                    and isinstance(val.func, ast.Name)
                    and val.func.id == "get_page"
                    and len(val.args) >= 2
                    and isinstance(val.args[1], ast.Name)
                ):
                    var_to_page[target_name] = val.args[1].id
                    used_pages.add(val.args[1].id)
                elif (
                    isinstance(val, ast.Attribute)
                    and isinstance(val.value, ast.Call)
                    and isinstance(val.value.func, ast.Name)
                    and val.value.func.id == "get_page"
                    and len(val.value.args) >= 2
                    and isinstance(val.value.args[1], ast.Name)
                ):
                    page_cls = val.value.args[1].id
                    used_pages.add(page_cls)
                    views_by_page[page_cls].add(val.attr)
        for sub in ast.walk(node):
            if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name):
                page_cls = var_to_page.get(sub.value.id)
                if page_cls:
                    views_by_page[page_cls].add(sub.attr)
    return (used_pages or known_page_imports), views_by_page


def _extract_ast_strings(node: ast.AST | None) -> list[str]:
    if node is None:
        return []
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return [
            elt.value
            for elt in node.elts
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
        ]
    return []


def _format_method_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    params = [
        arg.arg
        for arg in node.args.args
        if arg.arg not in {"self", "cls"}
    ]
    for kwarg in node.args.kwonlyargs:
        params.append(f"{kwarg.arg}=...")
    return f"{node.name}({', '.join(params)})"


def _load_compact_locator_map(locators_dir: Path, rel_path: str) -> dict:
    import yaml

    path_str = str(rel_path)
    if not path_str.endswith((".yaml", ".yml")):
        path_str += ".yaml"
    full_path = (locators_dir / path_str).resolve()
    if not full_path.is_file():
        return {}
    try:
        raw = yaml.safe_load(full_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    items = raw.get("locators") if isinstance(raw.get("locators"), dict) else raw
    compact = {}
    for key, val in items.items():
        if isinstance(val, dict):
            by_val = str(val.get("by") or "")
            loc_val = str(val.get("value") or "")
            if by_val == "xpath" and loc_val:
                parts = re.split(r"/(?=(?:[^']*'[^']*')*[^']*$)", loc_val)
                parts = [part for part in parts if part]
                if len(parts) >= 2 and parts[-1].startswith("next::"):
                    compact[str(key)] = f"{parts[-2]}/{parts[-1]}"
                else:
                    compact[str(key)] = parts[-1] if parts else loc_val
            elif by_val and loc_val:
                compact[str(key)] = f"{by_val}:{loc_val}"
            else:
                compact[str(key)] = val
    return compact


def format_generation_context(bundle: dict) -> str:
    if bundle.get("status") != "ok":
        return f"GENERATION_CONTEXT_ERROR: {bundle.get('error', 'unknown error')}"
    sel = bundle["selection"]
    ex_info = (
        f"example_id={sel['example_id']} (outline_example_ids={sel.get('validation_example_ids')})"
        if sel.get("is_scenario_outline")
        else f"example_id={sel['example_id']}"
    )
    target_empty_note = (
        ", content='' [0-byte empty file: replace directly without read_file]"
        if sel["target_step_exists"] and sel["target_step_bytes"] == 0
        else ""
    )
    companion_empty_note = (
        ", content='' [0-byte empty file: replace directly without read_file]"
        if sel["companion_md_exists"] and sel["companion_md_bytes"] == 0
        else ""
    )
    lines = [
        f"=== GENERATION CONTEXT ({sel['generation_mode']}) ===",
        f"Feature: {sel['feature_name']} ({sel['feature']})",
        f"Scenario: {sel['scenario']} | {ex_info} | scope={sel['scope_files']}",
        (
            f"Target Step: {sel['target_step_file']} "
            f"(exists={sel['target_step_exists']}, bytes={sel['target_step_bytes']}{target_empty_note})"
        ),
        (
            f"Companion MD: {sel['companion_md_file']} "
            f"(exists={sel['companion_md_exists']}, bytes={sel['companion_md_bytes']}, "
            f"status={sel['companion_status']}{companion_empty_note})"
        ),
    ]
    if sel.get("missing_parent_scope_files"):
        lines.append(
            f"Missing Parent Scope Files (create empty placeholder if needed): "
            f"{sel['missing_parent_scope_files']}"
        )
    if bundle.get("knowledge_bases"):
        kb_items = ", ".join(f"{k}={v}" for k, v in bundle["knowledge_bases"].items() if v)
        if kb_items:
            lines.append(f"Knowledge Bases (Bdd/config.yaml): {kb_items}")
    lines.append("\n[Scenario Steps]")
    for st in bundle["scenario_steps"]:
        shared = (
            f" -> bound:{st['bound_in_shared_scope']['file']}:{st['bound_in_shared_scope']['function']}"
            if st["bound_in_shared_scope"]
            else " -> [TO IMPLEMENT]"
        )
        lines.append(f"  {st['index']}. L{st['line']} [{st['step_type']}] {st['keyword']} {st['text']}{shared}")
        if "table" in st:
            lines.append(f"     Table: {st['table']['headings']} | rows={st['table']['rows']}")
    if bundle.get("examples"):
        lines.append("\n[Scenario Outline Examples]")
        for ex in bundle["examples"]:
            rows_str = ", ".join(f"{r['example_id']}:{r['cells']}" for r in ex["rows"])
            lines.append(f"  Headings: {ex['headings']} | Rows: {rows_str}")
    lines.append("\n[Sibling Steps]")
    for sb in bundle["sibling_steps"]:
        defs = ", ".join(
            f"{d['step_type']}:{d['function']}@L{d['line']}"
            for d in sb["step_definitions"]
        ) or "none"
        lines.append(f"  - {sb['file']} (empty={sb['empty']}, lines={sb['lines']}): {defs}")
    lines.append("\n[Referenced Pages & Locators]")
    total_loc_count = sum(
        len(lmap)
        for rp in bundle["referenced_pages"]
        for vd in rp["expanded_views"].values()
        for lmap in vd["locators"].values()
    )
    compact_large_files = total_loc_count > 45
    for rp in bundle["referenced_pages"]:
        aud = rp["resource_preflight_audit"]
        aud_str = aud["status"] if aud["status"] == "passed" else f"unverified ({aud.get('reason')})"
        lines.append(
            f"  * {rp['class_name']} ({rp['file']}) "
            f"root={rp['root_locator']} ({rp['root_locator_file']}) preflight={aud_str}"
        )
        if rp["public_methods"]:
            lines.append(f"    methods: {', '.join(rp['public_methods'])}")
        if rp["root_locators"]:
            lines.append(f"    root_locators: {', '.join(rp['root_locators'].keys())}")
        for vn, vd in rp["expanded_views"].items():
            lines.append(f"    view .{vn} -> {vd['view_class']} ({vd['file']}):")
            for lf, lmap in vd["locators"].items():
                if compact_large_files and len(lmap) >= 15:
                    pairs = ", ".join(lmap.keys())
                else:
                    pairs = ", ".join(f"{lk}={lv}" for lk, lv in lmap.items())
                lines.append(f"      [{lf}] {pairs}")
        if rp["other_views"]:
            other_v = (
                ", ".join(rp["other_views"].keys())
                if isinstance(rp["other_views"], dict)
                else ", ".join(rp["other_views"])
            )
            lines.append(f"    other_views: {other_v}")
    lines.append("\n[Other Pages]")
    for op in bundle["other_pages"]:
        aud = op["resource_preflight_audit"]
        aud_str = aud["status"] if aud["status"] == "passed" else f"unverified ({aud.get('reason')})"
        v_str = (
            ", ".join(f"{k}({','.join(v)})" for k, v in op["views"].items())
            if isinstance(op["views"], dict)
            else ", ".join(op["views"])
        )
        lines.append(
            f"  - {op['class_name']} ({op['file']}) root={op['root_locator']} "
            f"preflight={aud_str} views=[{v_str}] methods={op['public_methods']}"
        )
    return "\n".join(lines)


_GENERIC_STRUCTURAL_TOKENS = frozenset({
    "the", "and", "for", "with", "from", "that", "this", "when", "then",
    "given", "should", "following", "one", "two", "are", "not", "can",
    "use", "used", "shown", "selected", "configured", "mode", "value",
    "values", "option", "options", "table", "examples", "page", "view",
    "window", "button", "edit", "text", "combobox", "list", "tab", "first",
    "item", "data", "left", "right", "screen", "root", "yaml", "yml", "bdd",
    "locators", "obj",
})


def _tokenize_domain_terms(text: str) -> set[str]:
    words: set[str] = set()
    for part in re.findall(r"[A-Za-z]+|[\u4e00-\u9fff]{2,}", str(text)):
        for sub in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)|[\u4e00-\u9fff]{2,}", part):
            w = sub.lower()
            if len(w) >= 2 and w not in _GENERIC_STRUCTURAL_TOKENS:
                words.add(w)
    return words


def _domain_tokens_match(scenario_tokens: set[str], target_tokens: set[str]) -> bool:
    if not target_tokens:
        return False
    for st in scenario_tokens:
        for tt in target_tokens:
            if st == tt or (len(st) >= 5 and len(tt) >= 5 and (st.startswith(tt) or tt.startswith(st))):
                return True
    return False


def _build_page_and_locator_catalog(
        *,
        bdd_dir: Path,
        project_root: Path,
        base_dir: Path,
        steps_dir: Path,
        referenced_page_names: set[str],
        referenced_view_attrs: dict[str, set[str]],
        scenario_tokens: set[str] | None = None,
):
    page_obj_dir = bdd_dir / "page_obj"
    locators_dir = bdd_dir / "locators"
    if not page_obj_dir.is_dir():
        return [], []

    view_classes: dict[str, dict] = {}
    page_classes: list[dict] = []

    for py_file in sorted(page_obj_dir.rglob("*.py")):
        if py_file.name == "__init__.py":
            continue
        try:
            tree = ast.parse(read_python_source(py_file), filename=str(py_file))
        except SyntaxError:
            continue
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            bases = [
                base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
                for base in node.bases
            ]
            attrs: dict[str, list[str]] = {}
            properties: dict[str, str] = {}
            methods: list[str] = []
            for item in node.body:
                if isinstance(item, ast.Assign) and len(item.targets) == 1 and isinstance(item.targets[0], ast.Name):
                    attrs[item.targets[0].id] = _extract_ast_strings(item.value)
                elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    decs = [decorator_name(d) for d in item.decorator_list]
                    if "property" in decs:
                        for sub in ast.walk(item):
                            if (
                                isinstance(sub, ast.Call)
                                and isinstance(sub.func, ast.Attribute)
                                and sub.func.attr == "get_view"
                                and sub.args
                                and isinstance(sub.args[0], ast.Name)
                            ):
                                properties[item.name] = sub.args[0].id
                    elif not item.name.startswith("_"):
                        methods.append(_format_method_signature(item))
            rel_file = display_path(py_file, base_dir).replace("\\", "/")
            if "WindowView" in bases:
                loc_files = attrs.get("locator_files", []) + attrs.get("locator_file", [])
                view_classes[node.name] = {
                    "view_class": node.name,
                    "file": rel_file,
                    "locator_files": list(dict.fromkeys(loc_files)),
                    "public_methods": methods,
                }
            elif "WindowPage" in bases or "BasePage" in bases:
                root_loc_files = attrs.get("root_locator_file", [])
                root_locs = attrs.get("root_locator", [])
                loc_files = attrs.get("locator_files", []) + attrs.get("locator_file", [])
                page_classes.append({
                    "class_name": node.name,
                    "file": rel_file,
                    "source_path": py_file.resolve(),
                    "base_class": "WindowPage" if "WindowPage" in bases else "BasePage",
                    "root_locator": root_locs[0] if root_locs else None,
                    "root_locator_file": root_loc_files[0] if root_loc_files else None,
                    "locator_files": list(dict.fromkeys(loc_files)),
                    "public_methods": methods,
                    "view_properties": properties,
                })

    if not referenced_page_names and scenario_tokens:
        for page in page_classes:
            cls_name = page["class_name"]
            page_tokens = _tokenize_domain_terms(
                f"{cls_name} {page.get('root_locator_file') or ''} {' '.join(page.get('locator_files', ()))}"
            )
            matched_views = set()
            for prop_name, view_cls_name in page["view_properties"].items():
                v_info = view_classes.get(view_cls_name, {})
                v_tokens = _tokenize_domain_terms(
                    f"{prop_name} {view_cls_name} {' '.join(v_info.get('locator_files', ()))}"
                )
                if _domain_tokens_match(scenario_tokens, v_tokens):
                    matched_views.add(prop_name)
            if matched_views:
                referenced_page_names.add(cls_name)
                referenced_view_attrs[cls_name].update(matched_views)
            elif not page["view_properties"] and _domain_tokens_match(scenario_tokens, page_tokens):
                referenced_page_names.add(cls_name)

    referenced_pages = []
    other_pages = []
    for page in page_classes:
        cls_name = page["class_name"]
        try:
            _audit_selected_resource_imports(
                [page["source_path"]],
                steps_dir,
                project_root,
            )
            audit_result = {"status": "passed"}
        except _SelectedResourceUnavailable as exc:
            audit_result = {"status": "unverified", "reason": str(exc)}

        if cls_name not in referenced_page_names:
            other_pages.append({
                "class_name": cls_name,
                "file": page["file"],
                "root_locator": page["root_locator"],
                "root_locator_file": page["root_locator_file"],
                "resource_preflight_audit": audit_result,
                "public_methods": page["public_methods"],
                "views": {
                    prop_name: view_classes.get(view_cls_name, {}).get("locator_files", [])
                    for prop_name, view_cls_name in page["view_properties"].items()
                },
            })
            continue

        root_locators = (
            _load_compact_locator_map(locators_dir, page["root_locator_file"])
            if page["root_locator_file"]
            else {}
        )
        for lf in page["locator_files"]:
            root_locators.update(_load_compact_locator_map(locators_dir, lf))

        used_views = referenced_view_attrs.get(cls_name, set())
        expanded_views = {}
        other_views = {}
        for prop_name, view_cls_name in page["view_properties"].items():
            view_info = view_classes.get(view_cls_name, {})
            v_loc_files = view_info.get("locator_files", [])
            if prop_name in used_views or not used_views:
                loc_by_file = {
                    lf: _load_compact_locator_map(locators_dir, lf)
                    for lf in v_loc_files
                }
                expanded_views[prop_name] = {
                    "view_class": view_cls_name,
                    "file": view_info.get("file"),
                    "locator_files": v_loc_files,
                    "public_methods": view_info.get("public_methods", []),
                    "locators": loc_by_file,
                }
            else:
                other_views[prop_name] = v_loc_files

        referenced_pages.append({
            "class_name": cls_name,
            "file": page["file"],
            "base_class": page["base_class"],
            "root_locator": page["root_locator"],
            "root_locator_file": page["root_locator_file"],
            "resource_preflight_audit": audit_result,
            "public_methods": page["public_methods"],
            "root_locators": root_locators,
            "expanded_views": expanded_views,
            "other_views": other_views,
        })

    return referenced_pages, other_pages


def _check_selected_resources(feature, scenario, scope, steps_dir):
    project_root = (
        steps_dir.parent.parent
        if steps_dir.parent.name.casefold() == "bdd"
        else steps_dir.parent
    )
    _audit_selected_resource_imports(scope.files, steps_dir, project_root)
    previous_steps = {
        step_type: list(matchers)
        for step_type, matchers in behave_step_registry.registry.steps.items()
    }
    previous_state = snapshot_step_loading_state()
    previous_path = list(sys.path)
    previous_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "Bdd" or name.startswith("Bdd.")
    }
    existing_modules = set(sys.modules)

    def deny_operation(*_args, **_kwargs):
        raise _SelectedResourceUnavailable(
            "Resource loading attempted an external operation, write, or Page construction"
        )

    def read_only_open(original):
        def guarded_open(file, mode="r", *args, **kwargs):
            if any(flag in str(mode) for flag in "wax+"):
                deny_operation()
            return original(file, mode, *args, **kwargs)
        return guarded_open

    try:
        sys.path.insert(0, str(project_root))
        for name in previous_modules:
            sys.modules.pop(name, None)
        from autowork_core.page.singleton import BasePage

        with ExitStack() as guards:
            for target in (
                "requests.sessions.Session.request",
                "socket.socket.connect",
                "socket.socket.connect_ex",
                "socket.getaddrinfo",
                "subprocess.Popen",
                "os.system",
                "os.open",
                "os.remove",
                "os.unlink",
                "os.rename",
                "os.replace",
                "os.mkdir",
                "os.rmdir",
            ):
                guards.enter_context(patch(target, side_effect=deny_operation))
            if hasattr(os, "startfile"):
                guards.enter_context(patch("os.startfile", side_effect=deny_operation))
            guards.enter_context(patch.object(BasePage, "__init__", deny_operation))
            guards.enter_context(patch.object(sys, "dont_write_bytecode", True))
            guards.enter_context(patch("builtins.open", read_only_open(builtins.open)))
            guards.enter_context(patch("io.open", read_only_open(io.open)))
            prepared = _prepare_step_scope_files(steps_dir, scope.files)
            targets = []
            for step in scenario.all_steps:
                matches = _matching_step_definitions(prepared, step)
                if len(matches) != 1:
                    raise _SelectedResourceUnavailable(
                        f"Loaded binding is not unique: {step.name}"
                    )
                targets.append(SelectedStepResourceTarget(
                    feature_name=str(feature.name),
                    scenario_name=str(scenario.name),
                    step_text=str(step.name),
                    function=matches[0].func,
                    scope_key=scenario_scope_key(feature, scenario),
                ))
            return preflight_selected_step_resources(targets, steps_dir=steps_dir)
    except ResourcePreflightError:
        raise
    except (Exception, SystemExit) as error:
        raise _SelectedResourceUnavailable(
            f"Resource check could not safely complete: {type(error).__name__}: {error}"
        ) from error
    finally:
        reset_behave_step_state()
        behave_step_registry.registry.steps = previous_steps
        restore_step_loading_state(previous_state)
        sys.path[:] = previous_path
        for name in list(sys.modules):
            if name == "Bdd" or name.startswith("Bdd."):
                sys.modules.pop(name, None)
            elif name not in existing_modules:
                module_file = getattr(sys.modules[name], "__file__", None)
                if module_file and Path(module_file).resolve().is_relative_to(project_root):
                    if not name.startswith("autowork_core."):
                        sys.modules.pop(name, None)
        sys.modules.update(previous_modules)


def _audit_selected_resource_imports(files, steps_dir, project_root):
    pending = [(steps_dir / value).resolve() for value in files]
    visited = set()
    while pending:
        source = pending.pop()
        if source in visited:
            continue
        if not source.is_relative_to(project_root):
            raise _SelectedResourceUnavailable(
                f"Local resource import escapes the selected project: {source}"
            )
        visited.add(source)
        tree = ast.parse(read_python_source(source), filename=str(source))
        _validate_resource_declarations(tree.body, source)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            if isinstance(node, ast.ImportFrom) and node.level:
                raise _SelectedResourceUnavailable(
                    f"Relative local imports require a separately reviewed loader: {source}"
                )
            modules = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            for module in modules:
                if module == "autowork_core" or module.startswith("autowork_core."):
                    continue
                module_path = project_root.joinpath(*module.split("."))
                candidates = [module_path.with_suffix(".py"), module_path / "__init__.py"]
                if isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        child = module_path / alias.name
                        candidates.extend([child.with_suffix(".py"), child / "__init__.py"])
                candidates.extend(
                    parent / "__init__.py"
                    for parent in module_path.parents
                    if parent != project_root and parent.is_relative_to(project_root)
                )
                pending.extend(
                    candidate.resolve()
                    for candidate in candidates
                    if candidate.is_file()
                )


def _validate_resource_declarations(statements, source):
    allowed_decorators = STEP_DECORATORS | LIFECYCLE_DECORATORS | {
        "property", "staticmethod", "classmethod",
    }
    for statement in statements:
        if isinstance(statement, (ast.Import, ast.ImportFrom, ast.Pass)):
            continue
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant):
            continue
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            value = statement.value
            if isinstance(statement, ast.AnnAssign) and any(
                    isinstance(node, ast.Call)
                    for node in ast.walk(statement.annotation)
            ):
                raise _SelectedResourceUnavailable(
                    f"Executable declaration annotation: {source}:{statement.lineno}"
                )
            targets = (
                statement.targets if isinstance(statement, ast.Assign)
                else [statement.target]
            )
            if not all(isinstance(target, ast.Name) for target in targets):
                raise _SelectedResourceUnavailable(
                    f"Unsupported declaration assignment: {source}:{statement.lineno}"
                )
            if value is None or not any(
                    isinstance(node, (ast.Call, ast.Await, ast.NamedExpr, ast.comprehension))
                    for node in ast.walk(value)
            ):
                continue
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            expressions = list(statement.decorator_list)
            if isinstance(statement, ast.ClassDef):
                if statement.keywords or statement.decorator_list or any(
                    not isinstance(base, ast.Name)
                    or base.id not in {"WindowPage", "WindowView", "BasePage", "object"}
                    for base in statement.bases
                ):
                    raise _SelectedResourceUnavailable(
                        f"Custom class construction needs separate review: {source}:{statement.lineno}"
                    )
                expressions.extend(statement.bases)
                expressions.extend(keyword.value for keyword in statement.keywords)
                _validate_resource_declarations(statement.body, source)
            else:
                if statement.name in {
                    "__init_subclass__", "__class_getitem__", "__getattr__",
                }:
                    raise _SelectedResourceUnavailable(
                        f"Dynamic declaration hook needs separate review: {source}:{statement.lineno}"
                    )
                expressions.extend(statement.args.defaults)
                expressions.extend(value for value in statement.args.kw_defaults if value)
                expressions.extend(
                    argument.annotation
                    for argument in (
                        statement.args.posonlyargs + statement.args.args
                        + statement.args.kwonlyargs
                        + ([statement.args.vararg] if statement.args.vararg else [])
                        + ([statement.args.kwarg] if statement.args.kwarg else [])
                    )
                    if argument.annotation
                )
                if statement.returns:
                    expressions.append(statement.returns)
            for expression in expressions:
                for node in ast.walk(expression):
                    if isinstance(node, ast.Call):
                        if not (
                            expression in statement.decorator_list
                            and node is expression
                            and isinstance(node.func, ast.Name)
                            and node.func.id.lower() in allowed_decorators
                            and all(isinstance(value, ast.Constant) for value in node.args)
                            and all(isinstance(value.value, ast.Constant) for value in node.keywords)
                        ):
                            raise _SelectedResourceUnavailable(
                                f"Unsupported declaration-time call: {source}:{node.lineno}"
                            )
            continue
        raise _SelectedResourceUnavailable(
            f"Unsupported module/class execution: {source}:{statement.lineno}"
        )


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


def _scenario_name_matches(scenario, required_name):
    name = re.sub(
        r"\s+--\s+@\d+\.\d+.*$",
        "",
        str(scenario.name),
    ).strip()
    return bool(name == str(required_name or "").strip())


def _scenario_matches_requirement(scenario, required):
    row = getattr(scenario, "_row", None)
    example_id = str(getattr(row, "id", "") or "") or None
    required_example = required.get("example_id")
    return bool(
        _scenario_name_matches(scenario, required.get("name"))
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
