"""本地调试入口，支持普通 Feature 执行和基于 @single 标签的单步调试。

Local debugging entry point for normal feature runs and @single-based
single-step debugging.
"""

import sys
from config.paths import Paths
from autowork_core.runtime.behave_runner import run_behave
from autowork_core.runtime.single_step import (
    find_single_step_plan,
    generated_single_step_feature,
)


def main(feature_path=None, settings_overrides=None, verbose=True, formatter=None):
    feature_path = feature_path or Paths.TEST_FEATURES_DIR
    single_step_plan = find_single_step_plan(feature_path)
    if single_step_plan is not None:
        _print_single_step_plan(single_step_plan)
        with generated_single_step_feature(single_step_plan) as generated_path:
            _print_generated_feature(generated_path)
            return run_behave(
                generated_path,
                step_scope=single_step_plan.step_scope,
                settings_overrides=settings_overrides,
                verbose=verbose,
                formatter=formatter,
            )

    return run_behave(
        feature_path,
        settings_overrides=settings_overrides,
        verbose=verbose,
        formatter=formatter,
    )


def _print_single_step_plan(plan):
    print("Single-step debug")
    print(f"Feature : {plan.feature_name}")
    print(f"Scenario: {plan.scenario_name}")
    print(f"Mode    : {plan.mode}")
    if plan.example_id:
        print(f"Example : {plan.example_id}")
    print(f"Target  : Step {plan.step_index} - {plan.target_keyword} {plan.target_name}")
    print(f"Source  : {plan.source_path}")


def _print_generated_feature(generated_path):
    content = generated_path.read_text(encoding="utf-8")
    print("\n--- Generated single-step feature ---")
    print(content.rstrip())
    print("--- End generated single-step feature ---\n")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else None))


