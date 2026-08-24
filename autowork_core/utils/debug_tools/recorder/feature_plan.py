from __future__ import annotations

import hashlib
import re
from pathlib import Path

from behave.model import ScenarioOutline
from behave.parser import Parser

from autowork_core.utils.debug_tools.recorder.models import (
    FeaturePlan,
    ScenarioPlan,
    StepPlan,
)
from autowork_core.utils.debug_tools.recorder.identity import (
    ensure_persistent_feature_id,
    feature_identity,
    persistent_feature_id,
    scenario_identity,
    stable_digest,
    step_identity,
)


def load_feature_plan(source_path, *, ensure_identity=False):
    source_path = Path(source_path)
    if source_path.is_symlink():
        raise ValueError(f"Feature文件不能是符号链接: {source_path}")
    source_path = source_path.resolve()
    source_text = source_path.read_text(encoding="utf-8-sig")
    feature = Parser().parse(source_text, filename=str(source_path))
    feature_identity_data = feature_identity(
        source_path,
        feature.name,
        source_text=source_text,
    )
    if ensure_identity and persistent_feature_id(source_text) is None:
        ensure_persistent_feature_id(
            source_path,
            feature_identity_data["id"],
        )
        source_text = source_path.read_text(encoding="utf-8-sig")
        feature = Parser().parse(source_text, filename=str(source_path))
        feature_identity_data = feature_identity(
            source_path,
            feature.name,
            source_text=source_text,
        )
    logical_templates = _logical_template_ids(
        feature,
        feature_identity_data["id"],
    )
    scenarios = []
    scenario_occurrences = {}
    for scenario in feature.walk_scenarios():
        signature = _scenario_signature(scenario)
        occurrence = scenario_occurrences.get(signature, 0) + 1
        scenario_occurrences[signature] = occurrence
        scenarios.append(
            _scenario_plan(
                scenario,
                feature,
                feature_identity_data["id"],
                occurrence,
                logical_templates[id(scenario)],
            )
        )
    scenarios = tuple(scenarios)
    if not scenarios:
        raise ValueError(f"Feature 没有可录制的 Scenario: {source_path}")
    return FeaturePlan(
        id=feature_identity_data["id"],
        key=feature_identity_data["key"],
        source_path=source_path,
        source_relpath=feature_identity_data["source_relpath"],
        source_hash=hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        name=str(feature.name),
        line=_line(feature),
        tags=_tags(feature.effective_tags),
        scenarios=scenarios,
        description=_description(feature),
    )


def _scenario_plan(
    scenario,
    feature,
    feature_id,
    occurrence,
    logical_template_id,
):
    row = getattr(scenario, "_row", None)
    example_id = str(getattr(row, "id", "")) or None
    example_values = _example_values(row)
    background_steps = list(getattr(scenario, "background_steps", None) or [])
    scenario_steps = list(getattr(scenario, "steps", None) or [])
    outline = row is not None or isinstance(scenario, ScenarioOutline)
    scenario_name = _base_scenario_name(str(scenario.name))
    scenario_identity_data = scenario_identity(
        feature_id,
        scenario_name,
        "scenario_outline" if outline else "scenario",
        example_id,
        occurrence,
    )
    steps = []
    step_occurrences = {}
    for ordinal, step in enumerate(background_steps + scenario_steps, start=1):
        is_background = step in background_steps
        step_signature = (
            str(step.keyword).strip().casefold(),
            str(step.name).strip().casefold(),
            is_background,
        )
        step_occurrence = step_occurrences.get(step_signature, 0) + 1
        step_occurrences[step_signature] = step_occurrence
        step_identity_data = step_identity(
            scenario_identity_data["id"],
            str(step.keyword).strip(),
            str(step.name),
            is_background,
            step_occurrence,
        )
        steps.append(
            StepPlan(
                id=step_identity_data["id"],
                key=step_identity_data["key"],
                ordinal=ordinal,
                keyword=str(step.keyword).strip(),
                text=str(step.name),
                line=_line(step),
                semantic_type=str(
                    getattr(step, "step_type", "") or ""
                ).strip().casefold(),
                is_background=is_background,
                text_block=getattr(step, "text", None),
                table=_step_table(getattr(step, "table", None)),
            )
        )
    if not steps:
        raise ValueError(f"Scenario 没有可录制的 Step: {scenario.name}")
    return ScenarioPlan(
        id=scenario_identity_data["id"],
        key=scenario_identity_data["key"],
        logical_template_id=logical_template_id,
        name=scenario_name,
        line=_line(scenario),
        kind="scenario_outline" if outline else "scenario",
        example_id=example_id,
        example_values=example_values,
        tags=_tags(scenario.effective_tags),
        steps=tuple(steps),
        specification=_scenario_specification(feature, scenario),
    )


def _scenario_specification(feature, scenario):
    template = (
        scenario.parent
        if isinstance(getattr(scenario, "parent", None), ScenarioOutline)
        else scenario
    )
    container = getattr(template, "parent", None)
    rule = container if getattr(container, "keyword", None) == "Rule" else None
    backgrounds = []
    for background in (
        getattr(feature, "background", None),
        getattr(rule, "background", None) if rule is not None else None,
    ):
        if background is None:
            continue
        backgrounds.append({
            "name": str(background.name or ""),
            "description": list(_description(background)),
            "line": _line(background),
            "steps": [
                _specification_step(step)
                for step in getattr(background, "steps", None) or ()
            ],
        })
    return {
        "rule": (
            {
                "name": str(rule.name or ""),
                "description": list(_description(rule)),
                "line": _line(rule),
                "tags": list(_tags(getattr(rule, "effective_tags", ()))),
            }
            if rule is not None
            else None
        ),
        "backgrounds": backgrounds,
        "template": {
            "name": _base_scenario_name(str(template.name)),
            "description": list(_description(template)),
            "line": _line(template),
            "kind": (
                "scenario_outline"
                if isinstance(template, ScenarioOutline)
                else "scenario"
            ),
            "steps": [
                _specification_step(step)
                for step in getattr(template, "steps", None) or ()
            ],
            "examples": [
                _specification_examples(examples)
                for examples in getattr(template, "examples", None) or ()
            ],
        },
    }


def _specification_step(step):
    return {
        "keyword": str(step.keyword).strip(),
        "semantic_type": str(
            getattr(step, "step_type", "") or ""
        ).strip().casefold(),
        "text": str(step.name),
        "line": _line(step),
        "text_block": getattr(step, "text", None),
        "table": _step_table(getattr(step, "table", None)),
    }


def _specification_examples(examples):
    table = getattr(examples, "table", None)
    headings = [str(value) for value in getattr(table, "headings", ())]
    return {
        "name": str(examples.name or ""),
        "line": _line(examples),
        "tags": list(_tags(getattr(examples, "tags", ()))),
        "headings": headings,
        "rows": [
            {
                heading: str(value)
                for heading, value in zip(headings, row.cells)
            }
            for row in getattr(table, "rows", ())
        ],
    }


def _logical_template_ids(feature, feature_id):
    result = {}
    occurrences = {}
    containers = [feature, *(getattr(feature, "rules", None) or [])]
    for container in containers:
        for scenario in getattr(container, "scenarios", None) or []:
            outline = isinstance(scenario, ScenarioOutline)
            kind = "scenario_outline" if outline else "scenario"
            name = _base_scenario_name(str(scenario.name))
            step_signatures = tuple(
                (
                    str(step.keyword).strip().casefold(),
                    str(step.name).strip().casefold(),
                    step in (getattr(scenario, "background_steps", None) or []),
                )
                for step in (
                    list(getattr(scenario, "background_steps", None) or [])
                    + list(getattr(scenario, "steps", None) or [])
                )
            )
            signature = (name.casefold(), kind, step_signatures)
            occurrence = occurrences.get(signature, 0) + 1
            occurrences[signature] = occurrence
            template_id = "scenario-template-" + stable_digest(
                "scenario-template",
                feature_id,
                name,
                kind,
                step_signatures,
                occurrence,
            )
            expanded = (
                list(getattr(scenario, "scenarios", None) or [])
                if outline
                else [scenario]
            )
            for instance in expanded:
                result[id(instance)] = template_id
    return result


def _example_values(row):
    if row is None:
        return {}
    headings = list(getattr(row, "headings", None) or [])
    cells = list(getattr(row, "cells", None) or [])
    return {
        str(heading): str(value)
        for heading, value in zip(headings, cells)
    }


def _step_table(table):
    if table is None:
        return None
    return {
        "headings": [str(value) for value in table.headings],
        "rows": [
            [str(value) for value in row.cells]
            for row in table.rows
        ],
    }


def _base_scenario_name(name):
    return re.sub(r"\s+--\s+@\d+\.\d+.*$", "", name).strip()


def _scenario_signature(scenario):
    row = getattr(scenario, "_row", None)
    example_id = str(getattr(row, "id", "")) or None
    outline = row is not None or isinstance(scenario, ScenarioOutline)
    return (
        _base_scenario_name(str(scenario.name)).casefold(),
        "scenario_outline" if outline else "scenario",
        example_id,
    )


def _tags(tags):
    return tuple(sorted(str(tag) for tag in tags or ()))


def _description(item):
    return tuple(
        str(line).strip()
        for line in getattr(item, "description", None) or ()
        if str(line).strip()
    )


def _line(item):
    return int(getattr(getattr(item, "location", None), "line", 0) or 0)