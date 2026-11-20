"""Generate temporary Features for rerunning one complete Scenario/Example.

This is separate from @single step debugging: selected scenarios keep their
full Background + Scenario body so the normal Spark report can be merged into a
final report.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import re

from behave.model import ScenarioOutline
from behave.parser import Parser

from autowork_core.runtime.step_scope import (
    collect_feature_files,
    resolved_step_scope_for_scenario,
    step_scope_for_feature,
)
from autowork_core.runtime.tag_manager import TAG_MANAGER, TagOwner
from autowork_core.runtime.temporary_features import generated_temporary_feature
from config.paths import Paths


_EXAMPLE_GROUP_RE = re.compile(r"^([1-9]\d*)(?:\.\*)?$")


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
    rule_name: str | None = None
    scenario_line: int | None = None


@dataclass(frozen=True)
class RuleSubsetPlan:
    source_path: Path
    feature_name: str
    rule_name: str
    rule_line: int | None
    feature_tags: tuple[str, ...]
    feature_background_steps: tuple[object, ...]
    rule: object
    scenario_name: str | None = None
    example_id: str | None = None
    mode: str = "rule_subset"


def find_scenario_subset_plan(
        feature_path,
        *,
        scenario_name,
        example_id=None,
        rule_name=None,
        scenario_line=None,
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
            rule_name=rule_name,
            scenario_line=scenario_line,
            steps_dir=steps_dir,
        ))
    if not matches:
        detail = f" scenario={scenario_name!r}"
        if rule_name:
            detail += f" rule={rule_name!r}"
        if example_id:
            detail += f" example_id={example_id!r}"
        raise ScenarioSubsetConfigError(f"没有找到可复跑的完整场景:{detail}")
    if len(matches) > 1:
        locations = ", ".join(
            f"{plan.source_path}:{plan.scenario_line or '?'}"
            for plan in matches
        )
        raise ScenarioSubsetConfigError(
            f"匹配到多个同名场景，请收窄 feature_path 或指定行号: {locations}"
        )
    return matches[0]


def find_rule_subset_plan(
        feature_path,
        *,
        rule_name,
        rule_line=None,
        steps_dir=None,
):
    if not rule_name:
        return None
    steps_dir = Path(steps_dir or (Paths.BDD_DIR / "steps")).resolve()
    matches = []
    for source_path in collect_feature_files(feature_path):
        matches.extend(_rule_plans_for_feature(
            source_path,
            rule_name=rule_name,
            rule_line=rule_line,
            steps_dir=steps_dir,
        ))
    if not matches:
        detail = f" rule={rule_name!r}"
        if rule_line is not None:
            detail += f" line={rule_line}"
        raise ScenarioSubsetConfigError(f"没有找到可复跑的 Rule:{detail}")
    if len(matches) > 1:
        locations = ", ".join(
            f"{plan.source_path}:{plan.rule_line or '?'}"
            for plan in matches
        )
        raise ScenarioSubsetConfigError(
            f"匹配到多个同名 Rule，请收窄 feature_path 或指定 rule_line: {locations}"
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


def render_rule_subset_feature(plan):
    lines = []
    if plan.feature_tags:
        lines.append(" ".join(f"@{tag}" for tag in plan.feature_tags))
    lines.append(f"Feature: Rerun - {_single_line(plan.feature_name)}")
    lines.append("")
    if plan.feature_background_steps:
        lines.append("  Background:")
        for step in plan.feature_background_steps:
            lines.extend(_render_step(step, indent="    "))
        lines.append("")

    rule = plan.rule
    rule_tags = _raw_tags(getattr(rule, "tags", None))
    if rule_tags:
        lines.append("  " + " ".join(f"@{tag}" for tag in rule_tags))
    lines.append(f"  Rule: {_single_line(plan.rule_name)}")
    lines.append("")

    rule_bg = getattr(rule, "background", None)
    rule_bg_steps = tuple(getattr(rule_bg, "steps", None) or ())
    if rule_bg_steps:
        lines.append("    Background:")
        for step in rule_bg_steps:
            lines.extend(_render_step(step, indent="      "))
        lines.append("")

    for scenario in getattr(rule, "scenarios", ()):
        scen_tags = _raw_tags(getattr(scenario, "tags", None))
        if scen_tags:
            lines.append("    " + " ".join(f"@{tag}" for tag in scen_tags))
        if isinstance(scenario, ScenarioOutline):
            lines.append(f"    Scenario Outline: {_single_line(scenario.name)}")
            for step in scenario.steps:
                lines.extend(_render_step(step, indent="      "))
            lines.append("")
            for examples in scenario.examples:
                ex_tags = _raw_tags(getattr(examples, "tags", None))
                if ex_tags:
                    lines.append("      " + " ".join(f"@{tag}" for tag in ex_tags))
                ex_name = _single_line(getattr(examples, "name", "") or "")
                lines.append(f"      Examples: {ex_name}" if ex_name else "      Examples:")
                if examples.table is not None:
                    lines.append("        " + _render_table_row(examples.table.headings))
                    for row in examples.table.rows:
                        lines.append("        " + _render_table_row(row.cells))
                lines.append("")
        else:
            lines.append(f"    Scenario: {_single_line(scenario.name)}")
            for step in scenario.steps:
                lines.extend(_render_step(step, indent="      "))
            lines.append("")
    return "\n".join(lines)


@contextmanager
def generated_scenario_subset_feature(plan):
    content = render_scenario_subset_feature(plan)
    with generated_temporary_feature(
            plan.source_path,
            "rerun",
            content,
    ) as generated_path:
        yield generated_path


@contextmanager
def generated_rule_subset_feature(plan):
    content = render_rule_subset_feature(plan)
    with generated_temporary_feature(
            plan.source_path,
            "rerun",
            content,
    ) as generated_path:
        yield generated_path


def _plans_for_feature(
        source_path,
        *,
        scenario_name,
        example_id,
        rule_name=None,
        scenario_line=None,
        steps_dir,
):
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
        parent_rule = _scenario_rule(template)
        current_rule_name = parent_rule.name if parent_rule is not None else None
        if rule_name is not None and str(current_rule_name or "") != str(rule_name):
            continue
        if scenario_line is not None and getattr(template, "line", None) != int(scenario_line):
            continue
        if isinstance(template, ScenarioOutline):
            concrete_scenarios = (
                _select_examples(template, example_id=example_id)
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
            rule_name=current_rule_name,
            scenario_line=getattr(template, "line", None),
        ))
    return result


def _rule_plans_for_feature(source_path, *, rule_name, rule_line=None, steps_dir):
    source_path = Path(source_path).resolve()
    feature = Parser().parse(
        source_path.read_text(encoding="utf-8-sig"),
        filename=str(source_path),
    )
    result = []
    for rule in feature.rules:
        if str(rule.name) != str(rule_name):
            continue
        if rule_line is not None and getattr(rule, "line", None) != int(rule_line):
            continue
        concrete_scenarios = []
        for scenario in rule.scenarios:
            if isinstance(scenario, ScenarioOutline):
                concrete_scenarios.extend(scenario.scenarios)
            else:
                concrete_scenarios.append(scenario)
        if not concrete_scenarios:
            raise ScenarioSubsetConfigError(
                f"Rule {rule.name!r} 没有可执行场景"
            )
        for concrete in concrete_scenarios:
            resolved_step_scope_for_scenario(feature, concrete, steps_dir)
        feature_tags = list(_raw_tags(feature.tags))
        feature_decision = TAG_MANAGER.resolve_step_scope(
            feature.tags,
            TagOwner.FEATURE,
            feature.name,
        )
        if not feature_decision.step_file:
            inferred_file = step_scope_for_feature(source_path, steps_dir)["files"][0]
            feature_tags.insert(0, f"step:{inferred_file}")
        feature_bg = getattr(feature, "background", None)
        feature_bg_steps = tuple(getattr(feature_bg, "steps", None) or ())
        result.append(RuleSubsetPlan(
            source_path=source_path,
            feature_name=feature.name,
            rule_name=rule.name,
            rule_line=getattr(rule, "line", None),
            feature_tags=tuple(feature_tags),
            feature_background_steps=feature_bg_steps,
            rule=rule,
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


def _select_examples(outline, *, example_id):
    text = str(example_id).strip()
    group_match = _EXAMPLE_GROUP_RE.fullmatch(text)
    if group_match:
        prefix = f"{group_match.group(1)}."
        matched = [
            expanded_scenario
            for expanded_scenario in outline.scenarios
            if getattr(expanded_scenario, "_row", None) is not None
            and str(getattr(expanded_scenario._row, "id", "")).startswith(prefix)
        ]
        if matched:
            return matched
        row_counts = [len(example.table.rows) if example.table else 0 for example in outline.examples]
        raise ScenarioSubsetConfigError(
            f"Scenario Outline {outline.name!r} has no examples group={example_id}. "
            f"Examples row counts: {row_counts}"
        )
    return [_select_example(outline, example_id=text)]


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


def _scenario_rule(template):
    parent = getattr(template, "parent", None)
    return parent if getattr(parent, "keyword", None) == "Rule" or type(parent).__name__ == "Rule" else None


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


def _render_step(step, indent="    "):
    sub_indent = indent + "  "
    lines = [f"{indent}{_step_keyword(step)} {step.name}"]
    if step.text is not None:
        lines.append(f'{sub_indent}"""')
        lines.extend(f"{sub_indent}{line}" for line in str(step.text).splitlines())
        lines.append(f'{sub_indent}"""')
    if step.table is not None:
        lines.append(sub_indent + _render_table_row(step.table.headings))
        for row in step.table.rows:
            lines.append(sub_indent + _render_table_row(row.cells))
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
