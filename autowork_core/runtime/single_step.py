"""解析 @single 调试标签，选择目标 Step，并生成供 Behave 执行的临时 Feature。

Parses @single debug tags, selects the target step, and generates a
temporary feature for execution by Behave.
"""

from __future__ import annotations

import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from behave.model import ScenarioOutline
from behave.parser import Parser

from autowork_core.runtime.step_scope import (
    collect_feature_files,
    resolved_step_scope_for_scenario,
)
from autowork_core.runtime.tag_manager import (
    TAG_MANAGER,
    TagConfigurationError,
    TagOwner,
    normalize_tag,
)
from config.paths import Paths
STEP_KEYWORDS = {
    "given": "Given",
    "when": "When",
    "then": "Then",
}


class SingleStepConfigError(ValueError):
    pass


@dataclass(frozen=True)
class SingleStepPlan:
    source_path: Path
    feature_name: str
    scenario_name: str
    mode: str
    step_index: int
    example_index: int | None
    row_index: int | None
    target_keyword: str
    target_name: str
    feature_tags: tuple[str, ...]
    scenario_tags: tuple[str, ...]
    steps: tuple[object, ...]
    step_scope: dict

    @property
    def example_id(self):
        if self.example_index is None:
            return None
        return f"{self.example_index}.{self.row_index}"


def find_single_step_plan(feature_path, *, steps_dir=None):
    steps_dir = Path(steps_dir or (Paths.BDD_DIR / "steps")).resolve()
    plans = []
    for source_path in collect_feature_files(feature_path):
        plan = _plan_for_feature(source_path, steps_dir=steps_dir)
        if plan is not None:
            plans.append(plan)

    if len(plans) > 1:
        locations = ", ".join(str(plan.source_path) for plan in plans)
        raise SingleStepConfigError(
            f"Only one @single tag is allowed per run. Found {len(plans)} in: {locations}"
        )
    return plans[0] if plans else None


def render_single_step_feature(plan):
    lines = []
    if plan.feature_tags:
        lines.append(" ".join(f"@{tag}" for tag in plan.feature_tags))
    lines.append(f"Feature: Single-step debug - {_single_line(plan.feature_name)}")
    lines.append("")
    if plan.scenario_tags:
        lines.append("  " + " ".join(f"@{tag}" for tag in plan.scenario_tags))

    suffix = f"mode={plan.mode}, step={plan.step_index}"
    if plan.example_id:
        suffix += f", example={plan.example_id}"
    lines.append(f"  Scenario: {_single_line(plan.scenario_name)} [{suffix}]")
    for step in plan.steps:
        lines.extend(_render_step(step))
    lines.append("")
    return "\n".join(lines)


@contextmanager
def generated_single_step_feature(plan):
    content = render_single_step_feature(plan)
    with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{plan.source_path.stem}.single-step.",
            suffix=".feature",
            dir=plan.source_path.parent,
            delete=False,
    ) as stream:
        stream.write(content)
        generated_path = Path(stream.name)
    try:
        yield generated_path
    finally:
        generated_path.unlink(missing_ok=True)


def _plan_for_feature(source_path, *, steps_dir):
    source_path = Path(source_path).resolve()
    text = source_path.read_text(encoding="utf-8-sig")
    feature = Parser().parse(text, filename=str(source_path))
    candidates = []

    feature_decision = _resolve_tags(
        feature.tags,
        TagOwner.FEATURE,
        feature.name,
        feature.effective_tags,
    )
    for rule in feature.rules:
        _resolve_tags(rule.tags, TagOwner.RULE, rule.name)
    for scenario in _iter_scenarios(feature):
        owner = (
            TagOwner.SCENARIO_OUTLINE
            if isinstance(scenario, ScenarioOutline)
            else TagOwner.SCENARIO
        )
        decision = _resolve_tags(
            scenario.tags,
            owner,
            scenario.name,
            scenario.effective_tags,
        )
        for examples in getattr(scenario, "examples", ()):
            _resolve_tags(examples.tags, TagOwner.EXAMPLES, examples.name)
        if decision.directive is not None:
            candidates.append((scenario, decision, owner))

    if len(candidates) > 1:
        names = ", ".join(repr(scenario.name) for scenario, _, _ in candidates)
        raise SingleStepConfigError(
            f"Feature {source_path} contains multiple @single targets: {names}"
        )
    if not candidates:
        return None

    scenario, scenario_decision, scenario_owner = candidates[0]
    directive = scenario_decision.directive
    mode = directive.mode
    step_index = directive.step_index
    example_index = directive.example_index
    row_index = directive.row_index
    selected_scenario = _select_scenario(
        scenario,
        example_index=example_index,
        row_index=row_index,
    )
    selected_decision = _resolve_tags(
        selected_scenario.tags,
        scenario_owner,
        selected_scenario.name,
        selected_scenario.effective_tags,
    )
    try:
        step_scope = resolved_step_scope_for_scenario(
            feature,
            selected_scenario,
            steps_dir,
        ).runtime_scope()
    except (FileNotFoundError, TagConfigurationError, ValueError) as exc:
        raise SingleStepConfigError(str(exc)) from exc
    feature_tag_keys = {normalize_tag(tag) for tag in feature_decision.passthrough_tags}
    scenario_tags = tuple(
        tag for tag in selected_decision.passthrough_tags
        if normalize_tag(tag) not in feature_tag_keys
    )
    scenario_steps = list(selected_scenario.steps)
    if step_index > len(scenario_steps):
        raise SingleStepConfigError(
            f"@{directive.raw_tag} selects step {step_index}, but scenario {scenario.name!r} "
            f"contains only {len(scenario_steps)} steps"
        )

    target_step = scenario_steps[step_index - 1]
    background_steps = list(getattr(selected_scenario, "background_steps", None) or [])
    if mode == "previous":
        selected_steps = background_steps + scenario_steps[:step_index]
    elif mode == "background":
        selected_steps = background_steps + [target_step]
    else:
        selected_steps = [target_step]

    return SingleStepPlan(
        source_path=source_path,
        feature_name=feature.name,
        scenario_name=selected_scenario.name,
        mode=mode,
        step_index=step_index,
        example_index=example_index,
        row_index=row_index,
        target_keyword=_step_keyword(target_step),
        target_name=target_step.name,
        feature_tags=feature_decision.passthrough_tags,
        scenario_tags=scenario_tags,
        steps=tuple(selected_steps),
        step_scope=step_scope,
    )


def _select_scenario(scenario, *, example_index, row_index):
    is_outline = isinstance(scenario, ScenarioOutline)
    if not is_outline:
        if example_index is not None:
            raise SingleStepConfigError(
                f"Scenario {scenario.name!r} is not an outline and cannot use example=..."
            )
        return scenario

    if example_index is None:
        raise SingleStepConfigError(
            f"Scenario Outline {scenario.name!r} requires example=<examples-index>.<row-index>"
        )

    example_id = f"{example_index}.{row_index}"
    for expanded_scenario in scenario.scenarios:
        row = getattr(expanded_scenario, "_row", None)
        if row is not None and str(getattr(row, "id", "")) == example_id:
            return expanded_scenario

    row_counts = [len(example.table.rows) if example.table else 0 for example in scenario.examples]
    raise SingleStepConfigError(
        f"Scenario Outline {scenario.name!r} has no example={example_id}. "
        f"Examples row counts: {row_counts}"
    )


def _render_step(step):
    lines = [f"    {_step_keyword(step)} {step.name}"]
    if step.text is not None:
        lines.append('      """')
        lines.extend(f"      {line}" for line in str(step.text).splitlines())
        lines.append('      """')
    if step.table is not None:
        lines.append("      " + _render_table_row(step.table.headings))
        for row in step.table.rows:
            lines.append("      " + _render_table_row(row.cells))
    return lines


def _render_table_row(cells):
    return "| " + " | ".join(_escape_table_cell(cell) for cell in cells) + " |"


def _escape_table_cell(value):
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", "\\n")


def _step_keyword(step):
    return STEP_KEYWORDS.get(str(step.step_type).lower(), "Given")


def _resolve_tags(tags, owner, owner_name, effective_tags=None):
    try:
        return TAG_MANAGER.resolve_single_step(
            tags,
            owner,
            owner_name,
            effective_tags=effective_tags,
        )
    except TagConfigurationError as exc:
        raise SingleStepConfigError(str(exc)) from exc


def _single_line(value):
    return " ".join(str(value).splitlines()).strip()


def _iter_scenarios(feature):
    yield from feature.scenarios
    for rule in feature.rules:
        yield from rule.scenarios