"""Generate temporary Features for rerunning one complete Scenario/Example.

This is separate from @single step debugging: selected scenarios keep their
full Background + Scenario body so the normal Spark report can be merged into a
final report.
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
from config.paths import Paths


class ScenarioSubsetConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ScenarioSubsetCase:
    name: str
    tags: tuple[str, ...]
    steps: tuple[object, ...]


@dataclass(frozen=True)
class ScenarioSubsetPlan:
    source_path: Path
    feature_name: str
    scenario_name: str
    example_id: str | None
    feature_tags: tuple[str, ...]
    background_steps: tuple[object, ...]
    cases: tuple[ScenarioSubsetCase, ...]
    step_scope: dict


def find_scenario_subset_plan(
        feature_path,
        *,
        scenario_name,
        example_id=None,
        steps_dir=None,
):
    if not scenario_name:
        return None
    steps_dir = Path(steps_dir or (Paths.BDD_DIR / "steps")).resolve()
    matches = []
    for source_path in collect_feature_files(feature_path):
        matches.extend(_plans_for_feature(
            source_path,
            scenario_name=scenario_name,
            example_id=example_id,
            steps_dir=steps_dir,
        ))
    if not matches:
        detail = f" scenario={scenario_name!r}"
        if example_id:
            detail += f" example_id={example_id!r}"
        raise ScenarioSubsetConfigError(f"没有找到可复跑的完整场景:{detail}")
    if len(matches) > 1:
        locations = ", ".join(str(plan.source_path) for plan in matches)
        raise ScenarioSubsetConfigError(
            f"匹配到多个同名场景，请收窄 feature_path: {locations}"
        )
    return matches[0]


def render_scenario_subset_feature(plan):
    lines = []
    if plan.feature_tags:
        lines.append(" ".join(f"@{tag}" for tag in plan.feature_tags))
    lines.append(f"Feature: Rerun - {_single_line(plan.feature_name)}")
    lines.append("")
    if plan.background_steps:
        lines.append("  Background:")
        for step in plan.background_steps:
            lines.extend(_render_step(step))
        lines.append("")
    for case in plan.cases:
        if case.tags:
            lines.append("  " + " ".join(f"@{tag}" for tag in case.tags))
        lines.append(f"  Scenario: {_single_line(case.name)}")
        for step in case.steps:
            lines.extend(_render_step(step))
        lines.append("")
    return "\n".join(lines)


@contextmanager
def generated_scenario_subset_feature(plan):
    content = render_scenario_subset_feature(plan)
    with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{plan.source_path.stem}.rerun.",
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


def _plans_for_feature(source_path, *, scenario_name, example_id, steps_dir):
    source_path = Path(source_path).resolve()
    feature = Parser().parse(
        source_path.read_text(encoding="utf-8-sig"),
        filename=str(source_path),
    )
    result = []
    for scenario in _iter_scenarios(feature):
        template = _scenario_template(scenario)
        if str(template.name) != str(scenario_name):
            continue
        if isinstance(template, ScenarioOutline):
            concrete_scenarios = (
                [_select_example(template, example_id=example_id)]
                if example_id
                else list(template.scenarios)
            )
            if not concrete_scenarios:
                raise ScenarioSubsetConfigError(
                    f"Scenario Outline {template.name!r} 没有可执行 Example"
                )
        else:
            if example_id:
                raise ScenarioSubsetConfigError(
                    f"Scenario {template.name!r} 不是 Outline，不能使用 example_id"
                )
            concrete_scenarios = [scenario]
        concrete = concrete_scenarios[0]
        step_scope = resolved_step_scope_for_scenario(
            feature,
            concrete,
            steps_dir,
        ).runtime_scope()
        cases = tuple(
            _subset_case(feature, template, concrete_scenario)
            for concrete_scenario in concrete_scenarios
        )
        result.append(ScenarioSubsetPlan(
            source_path=source_path,
            feature_name=feature.name,
            scenario_name=template.name,
            example_id=example_id,
            feature_tags=_raw_tags(feature.tags),
            background_steps=tuple(getattr(concrete, "background_steps", None) or []),
            cases=cases,
            step_scope=step_scope,
        ))
    return result


def _subset_case(feature, template, concrete):
    if isinstance(template, ScenarioOutline):
        example_id = _example_id(concrete)
        name = f"{template.name} -- @{example_id}"
    else:
        name = template.name
    return ScenarioSubsetCase(
        name=name,
        tags=_runtime_tags(concrete.effective_tags, feature.tags),
        steps=tuple(concrete.steps),
    )


def _example_id(scenario):
    row = getattr(scenario, "_row", None)
    value = str(getattr(row, "id", "") or "")
    if not value:
        raise ScenarioSubsetConfigError(
            f"Scenario Outline example {scenario.name!r} 缺少 example id"
        )
    return value


def _select_example(outline, *, example_id):
    for expanded_scenario in outline.scenarios:
        row = getattr(expanded_scenario, "_row", None)
        if row is not None and str(getattr(row, "id", "")) == str(example_id):
            return expanded_scenario
    row_counts = [len(example.table.rows) if example.table else 0 for example in outline.examples]
    raise ScenarioSubsetConfigError(
        f"Scenario Outline {outline.name!r} has no example={example_id}. "
        f"Examples row counts: {row_counts}"
    )


def _scenario_template(scenario):
    parent = getattr(scenario, "parent", None)
    return parent if isinstance(parent, ScenarioOutline) else scenario


def _iter_scenarios(feature):
    yield from feature.scenarios
    for rule in feature.rules:
        yield from rule.scenarios


def _runtime_tags(effective_tags, feature_tags):
    feature_set = {str(tag) for tag in feature_tags or ()}
    return tuple(
        str(tag)
        for tag in effective_tags or ()
        if str(tag) not in feature_set and not str(tag).startswith(("step:", "stepfile:", "step_file:", "steps:"))
    )


def _raw_tags(tags):
    return tuple(str(tag) for tag in tags or ())


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
    return {"given": "Given", "when": "When", "then": "Then"}.get(
        str(step.step_type).lower(),
        "Given",
    )


def _single_line(value):
    return " ".join(str(value).splitlines()).strip()
