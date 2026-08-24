"""Behave 执行编排器，负责参数构造、Feature 级 Step scope 和输出目录准备。

Behave execution orchestrator for argument construction, feature-scoped
step loading, and output-directory preparation.
"""

import os
from behave import step_registry as behave_step_registry  # type: ignore
from behave.formatter._registry import make_formatters  # type: ignore
from behave.matchers import use_current_step_matcher_as_default  # type: ignore
from behave.runner import Context, Runner as BehaveRunner  # type: ignore
from behave.runner_util import parse_features  # type: ignore
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from behave.__main__ import main as behave_main  # type: ignore
from loguru import logger
from config.paths import Paths
from config.settings import settings
from autowork_core.runtime.run_state import (
    activated_step_scope,
    active_step_scope,
)
from autowork_core.runtime.step_scope import step_scope_for_feature
from autowork_core.runtime.step_validation import (
    activate_step_registry,
    preflight_feature_steps,
    reset_behave_step_state,
)
from autowork_core.runtime.reporting.run_result_bridge import (
    GENERATION_TRANSACTION_ENV,
)
from autowork_core.utils.bus import del_all_file


RUNNER_CLASS = "autowork_core.runtime.behave_runner:FeatureScopedRunner"
OUTPUT_PREPARATION = ContextVar("autowork_output_preparation", default=None)


class FeatureScopedRunner(BehaveRunner):
    """Run all features in one Behave lifecycle while switching step scopes."""

    def load_step_definitions(self, extra_step_paths=None):
        use_current_step_matcher_as_default()
        reset_behave_step_state()

    def run_with_paths(self):
        self.context = Context(self)
        self.load_hooks()
        self.load_step_definitions()

        feature_locations = [
            filename
            for filename in self.feature_locations()
            if not self.config.exclude(filename)
        ]
        self.features.extend(parse_features(
            feature_locations,
            language=self.config.lang,
        ))
        self._preflight_steps(self.features)

        clear_screenshots = OUTPUT_PREPARATION.get()
        if clear_screenshots is not None:
            _prepare_output_dirs(clear_screenshots=clear_screenshots)

        self.formatters = make_formatters(
            self.config,
            self.config.outputs,
        )
        return self.run_model()

    def run_model(self, features=None):
        selected_features = list(self.features if features is None else features)
        if not hasattr(self, "step_preflight"):
            self._preflight_steps(selected_features)

        original_feature_runs = []
        original_rule_runs = []
        original_scenario_runs = []

        for feature in selected_features:
            original_run = feature.run
            original_feature_runs.append((feature, original_run))

            def run_with_scope(runner, _feature=feature, _run=original_run):
                return self._run_feature_with_scope(_feature, _run, runner)

            feature.run = run_with_scope
            for rule in feature.rules:
                rule_run = rule.run
                original_rule_runs.append((rule, rule_run))

                def run_rule_with_scope(
                        runner,
                        _feature=feature,
                        _rule=rule,
                        _run=rule_run,
                ):
                    return self._run_rule_with_scope(
                        _feature,
                        _rule,
                        _run,
                        runner,
                    )

                rule.run = run_rule_with_scope
            for scenario in feature.walk_scenarios():
                scenario_run = scenario.run
                original_scenario_runs.append((scenario, scenario_run))

                def run_scenario_with_scope(
                        runner,
                        _feature=feature,
                        _scenario=scenario,
                        _run=scenario_run,
                ):
                    return self._run_scenario_with_scope(
                        _feature,
                        _scenario,
                        _run,
                        runner,
                    )

                scenario.run = run_scenario_with_scope

        try:
            return super().run_model(selected_features)
        finally:
            for scenario, original_run in original_scenario_runs:
                scenario.run = original_run
            for rule, original_run in original_rule_runs:
                rule.run = original_run
            for feature, original_run in original_feature_runs:
                feature.run = original_run

    def _preflight_steps(self, features):
        steps_dir = Path(self.base_dir) / self.config.steps_dir
        self.step_preflight = preflight_feature_steps(
            features,
            self.config,
            steps_dir,
            explicit_scope=active_step_scope(),
            application_launch_mode=settings.app_launch_mode,
            resource_locators_dir=Paths.LOCATORS_DIR,
            resource_data_dir=Paths.DATA_DIR,
        )
        for warning in self.step_preflight.resource_warnings:
            logger.warning(f"Resource preflight: {warning}")

    def _run_feature_with_scope(self, feature, feature_run, runner):
        prepared_scope = self.step_preflight.prepared_scope_for(feature)
        activate_step_registry(prepared_scope)
        scope = prepared_scope.scope if prepared_scope is not None else None
        with activated_step_scope(scope):
            runner.step_registry = behave_step_registry.registry
            return feature_run(runner)

    def _run_scenario_with_scope(
            self,
            feature,
            scenario,
            scenario_run,
            runner,
    ):
        feature_scope = self.step_preflight.prepared_scope_for(feature)
        template = (
            scenario.parent
            if type(getattr(scenario, "parent", None)).__name__
            == "ScenarioOutline"
            else scenario
        )
        rule = getattr(template, "parent", None)
        parent_scope = (
            self.step_preflight.prepared_scope_for_rule(feature, rule)
            if getattr(rule, "keyword", None) == "Rule"
            else feature_scope
        ) or feature_scope
        prepared_scope = (
            self.step_preflight.prepared_scope_for_scenario(feature, scenario)
            or feature_scope
        )
        activate_step_registry(prepared_scope)
        scope = prepared_scope.scope if prepared_scope is not None else None
        try:
            with activated_step_scope(scope):
                runner.step_registry = behave_step_registry.registry
                return scenario_run(runner)
        finally:
            activate_step_registry(parent_scope)
            runner.step_registry = behave_step_registry.registry

    def _run_rule_with_scope(self, feature, rule, rule_run, runner):
        feature_scope = self.step_preflight.prepared_scope_for(feature)
        prepared_scope = (
            self.step_preflight.prepared_scope_for_rule(feature, rule)
            or feature_scope
        )
        activate_step_registry(prepared_scope)
        scope = prepared_scope.scope if prepared_scope is not None else None
        try:
            with activated_step_scope(scope):
                runner.step_registry = behave_step_registry.registry
                return rule_run(runner)
        finally:
            activate_step_registry(feature_scope)
            runner.step_registry = behave_step_registry.registry


@contextmanager
def temporary_settings(**overrides):
    original = {key: getattr(settings, key) for key in overrides}
    try:
        settings.update(**overrides)
        yield
    finally:
        settings.update(**original)


@contextmanager
def requested_output_preparation(clear_screenshots):
    token = OUTPUT_PREPARATION.set(bool(clear_screenshots))
    try:
        yield
    finally:
        OUTPUT_PREPARATION.reset(token)


@contextmanager
def requested_generation_transaction(report_path):
    if report_path is None:
        yield
        return
    path = Path(report_path)
    if not path.is_absolute():
        path = (Paths.BASE_DIR / path).resolve()
    else:
        path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(
            f"Generation transaction report 不存在: {path}"
        )
    previous = os.environ.get(GENERATION_TRANSACTION_ENV)
    os.environ[GENERATION_TRANSACTION_ENV] = str(path)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(GENERATION_TRANSACTION_ENV, None)
        else:
            os.environ[GENERATION_TRANSACTION_ENV] = previous


def _as_feature_path(feature_path):
    path = Path(feature_path)
    if not path.is_absolute():
        path = Paths.BASE_DIR / path
    return path


def _prepare_output_dirs(clear_screenshots=True):
    Paths.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    Paths.SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    Paths.RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    if clear_screenshots:
        del_all_file(Paths.SCREENSHOTS_DIR)
        del_all_file(Paths.RECORDINGS_DIR)
        del_all_file(Paths.REPORTS_DIR)
    Paths.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    Paths.SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    Paths.RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)


def build_behave_args(feature_path, tags=None, verbose=True, formatter=None):
    args = [
        "--runner",
        RUNNER_CLASS,
        "--no-capture",
        "--no-capture-stderr",
        "--no-logcapture",
    ]

    if formatter:
        args.extend(["--format", str(formatter)])
    if verbose:
        args.append("--verbose")
    if tags:
        args.extend(["--tags", str(tags)])
    args.append(str(_as_feature_path(feature_path)))
    return args


def run_behave(feature_path, *, settings_overrides=None, tags=None, verbose=True, formatter=None,
               clear_screenshots=True, step_scope_source=None,
               step_scope=None,
               generation_transaction_report=None):
    os.chdir(Paths.BASE_DIR)
    feature_path = _as_feature_path(feature_path)
    scope = step_scope
    if scope is None and step_scope_source is not None:
        source_path = _as_feature_path(step_scope_source)
        scope = step_scope_for_feature(
            source_path,
            Paths.BDD_DIR / "steps",
        )

    with temporary_settings(**(settings_overrides or {})):
        with requested_output_preparation(clear_screenshots), \
                requested_generation_transaction(
                    generation_transaction_report
                ):
            return _run_single_behave(
                feature_path,
                scope=scope,
                tags=tags,
                verbose=verbose,
                formatter=formatter,
            )

def _run_single_behave(feature_path, *, scope=None, tags=None, verbose=True, formatter=None):
    reset_behave_step_state()
    with activated_step_scope(scope):
        return behave_main(build_behave_args(
            feature_path,
            tags=tags,
            verbose=verbose,
            formatter=formatter,
        ))

def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        description="Run BDD Autowork features",
    )
    parser.add_argument(
        "feature_path",
        nargs="?",
        default=str(Paths.FEATURES_DIR),
    )
    parser.add_argument("--tags")
    parser.add_argument("--formatter")
    parser.add_argument("--generation-transaction-report")
    parser.add_argument("--execution-request")
    args = parser.parse_args(argv)
    if bool(args.generation_transaction_report) != bool(
            args.execution_request
    ):
        parser.error(
            "--generation-transaction-report与--execution-request必须同时提供"
        )
    settings_overrides = None
    if args.execution_request:
        from autowork_core.utils.debug_tools.recorder.execution_profile import (
            execution_settings_for_request,
        )

        settings_overrides = execution_settings_for_request(
            args.execution_request,
            args.generation_transaction_report,
        )
    return run_behave(
        args.feature_path,
        settings_overrides=settings_overrides,
        tags=args.tags,
        formatter=args.formatter,
        generation_transaction_report=(
            args.generation_transaction_report
        ),
    )
