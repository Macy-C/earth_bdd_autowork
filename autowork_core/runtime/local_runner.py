"""本地调试入口，支持普通 Feature 执行和基于 @single 标签的单步调试。

Local debugging entry point for normal feature runs and @single-based
single-step debugging.
"""

import sys
from config.paths import Paths
from autowork_core.runtime.behave_runner import run_behave
from autowork_core.runtime.run_state import activated_report_source_map
from autowork_core.runtime.knowledge_capture import activated_knowledge_origin
from autowork_core.runtime.scenario_subset import (
    find_rule_subset_plan,
    find_scenario_subset_plan,
    generated_rule_subset_feature,
    generated_scenario_subset_feature,
)
from autowork_core.runtime.single_step import (
    build_single_step_plan,
    find_single_step_plan,
    generated_single_step_feature,
)
from autowork_core.runtime.temporary_features import cleanup_temporary_features


def main(
        feature_path=None,
        settings_overrides=None,
        verbose=True,
        formatter=None,
        scenario_name=None,
        example_id=None,
        *,
        rule_name=None,
        rule_line=None,
        scenario_line=None,
        single_step_index=None,
        single_step_mode="previous",
        tags=None,
        clear_screenshots=True,
):
    feature_path = feature_path or Paths.TEST_FEATURES_DIR
    cleanup_temporary_features(feature_path)

    if single_step_index is not None:
        explicit_single_plan = build_single_step_plan(
            feature_path,
            scenario_name=scenario_name,
            step_index=single_step_index,
            mode=single_step_mode,
            example_id=example_id,
            rule_name=rule_name,
            scenario_line=scenario_line,
        )
        _print_single_step_plan(explicit_single_plan)
        with generated_single_step_feature(explicit_single_plan) as generated_path:
            _print_generated_feature(generated_path, label="single-step")
            with activated_report_source_map({
                generated_path: explicit_single_plan.source_path,
            }), activated_knowledge_origin(explicit_single_plan):
                return run_behave(
                    generated_path,
                    step_scope=explicit_single_plan.step_scope,
                    settings_overrides=settings_overrides,
                    verbose=verbose,
                    formatter=formatter,
                    clear_screenshots=clear_screenshots,
                )

    scenario_subset_plan = find_scenario_subset_plan(
        feature_path,
        scenario_name=scenario_name,
        example_id=example_id,
        rule_name=rule_name,
        scenario_line=scenario_line,
    )
    if scenario_subset_plan is not None:
        _print_scenario_subset_plan(scenario_subset_plan)
        with generated_scenario_subset_feature(scenario_subset_plan) as generated_path:
            _print_generated_feature(generated_path, label="scenario-subset")
            with activated_report_source_map({
                generated_path: scenario_subset_plan.source_path,
            }), activated_knowledge_origin(scenario_subset_plan):
                return run_behave(
                    generated_path,
                    step_scope=scenario_subset_plan.step_scope,
                    settings_overrides=settings_overrides,
                    verbose=verbose,
                    formatter=formatter,
                    clear_screenshots=clear_screenshots,
                )

    rule_subset_plan = find_rule_subset_plan(
        feature_path,
        rule_name=rule_name,
        rule_line=rule_line,
    )
    if rule_subset_plan is not None:
        _print_rule_subset_plan(rule_subset_plan)
        with generated_rule_subset_feature(rule_subset_plan) as generated_path:
            _print_generated_feature(generated_path, label="rule-subset")
            with activated_report_source_map({
                generated_path: rule_subset_plan.source_path,
            }), activated_knowledge_origin(rule_subset_plan):
                return run_behave(
                    generated_path,
                    settings_overrides=settings_overrides,
                    tags=tags,
                    verbose=verbose,
                    formatter=formatter,
                    clear_screenshots=clear_screenshots,
                )

    single_step_plan = find_single_step_plan(feature_path)
    if single_step_plan is not None:
        _print_single_step_plan(single_step_plan)
        with generated_single_step_feature(single_step_plan) as generated_path, activated_knowledge_origin(single_step_plan):
            _print_generated_feature(generated_path, label="single-step")
            return run_behave(
                generated_path,
                step_scope=single_step_plan.step_scope,
                settings_overrides=settings_overrides,
                verbose=verbose,
                formatter=formatter,
                clear_screenshots=clear_screenshots,
            )

    return run_behave(
        feature_path,
        settings_overrides=settings_overrides,
        tags=tags,
        verbose=verbose,
        formatter=formatter,
        clear_screenshots=clear_screenshots,
    )


def _print_single_step_plan(plan):
    print("Single-step debug")
    print(f"Feature : {plan.feature_name}")
    if getattr(plan, "rule_name", None):
        print(f"Rule    : {plan.rule_name}")
    print(f"Scenario: {plan.scenario_name}")
    print(f"Mode    : {plan.mode}")
    if plan.example_id:
        print(f"Example : {plan.example_id}")
    print(f"Target  : Step {plan.step_index} - {plan.target_keyword} {plan.target_name}")
    print(f"Source  : {plan.source_path}")


def _print_scenario_subset_plan(plan):
    print("Scenario rerun")
    print(f"Feature : {plan.feature_name}")
    if getattr(plan, "rule_name", None):
        print(f"Rule    : {plan.rule_name}")
    print(f"Scenario: {plan.scenario_name}")
    if plan.example_id:
        print(f"Example : {plan.example_id}")
    else:
        print(f"Cases   : {len(plan.cases)}")
    print(f"Source  : {plan.source_path}")


def _print_rule_subset_plan(plan):
    print("Rule rerun")
    print(f"Feature : {plan.feature_name}")
    print(f"Rule    : {plan.rule_name}")
    print(f"Source  : {plan.source_path}")


def _print_generated_feature(generated_path, *, label="single-step"):
    content = generated_path.read_text(encoding="utf-8")
    print(f"\n--- Generated {label} feature ---")
    print(content.rstrip())
    print(f"--- End generated {label} feature ---\n")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else None))


