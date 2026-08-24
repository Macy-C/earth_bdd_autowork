from __future__ import annotations

import json
import re


FEATURE_LITERAL_REFERENCES = frozenset({"step_text", "text_block"})
SEMANTIC_LITERAL_OPERATIONS = frozenset({
    "set_checked",
    "set_tree_expanded",
})


def resolve_feature_literal(brief, step_id, reference):
    reference = str(reference or "").strip()
    if reference not in FEATURE_LITERAL_REFERENCES:
        raise ValueError(f"未知feature literal reference: {reference}")

    values = []
    target_step = next((
        item
        for item in (brief.get("target") or {}).get("steps") or ()
        if str(item.get("id") or "") == str(step_id)
    ), {})
    if reference == "text_block" and target_step.get("text_block") is not None:
        values.append(target_step.get("text_block"))
    for action in brief.get("actions") or ():
        if str(action.get("step_id") or "") != str(step_id):
            continue
        for constraint in (
                (action.get("semantics") or {}).get(
                    "implementation_constraints"
                )
                or ()
        ):
            parameters = (
                constraint.get("parameters")
                if isinstance(constraint, dict)
                else None
            )
            if not isinstance(parameters, dict):
                continue
            if (
                parameters.get("expected_source") == reference
                and "expected" in parameters
            ):
                values.append(parameters.get("expected"))
    for ambiguity in brief.get("ambiguities") or ():
        if str(ambiguity.get("step_id") or "") != str(step_id):
            continue
        for expectation in (
                (ambiguity.get("facts") or {}).get("declared_expectations")
                or ()
        ):
            if not isinstance(expectation, dict):
                continue
            if any((
                expectation.get("authority") != "feature_declared",
                str(expectation.get("source") or "") != reference,
                "value" not in expectation,
            )):
                continue
            values.append(expectation.get("value"))

    unique = []
    for value in values:
        if not any(value == existing for existing in unique):
            unique.append(value)
    if not unique:
        raise ValueError(
            f"Step {step_id}缺少冻结feature literal: {reference}"
        )
    if len(unique) > 1:
        raise ValueError(
            f"Step {step_id} feature literal不唯一: {reference}"
        )
    return unique[0]


def resolve_declared_feature_literal(brief, step_id, reference, value):
    reference = str(reference or "").strip()
    if reference not in FEATURE_LITERAL_REFERENCES:
        raise ValueError(f"未知feature literal reference: {reference}")
    if not isinstance(value, str) or not value:
        raise ValueError(
            f"Step {step_id} feature literal必须是非空string: {reference}"
        )
    target_step = next((
        item
        for item in (brief.get("target") or {}).get("steps") or ()
        if str(item.get("id") or "") == str(step_id)
    ), {})
    declaration = (
        target_step.get("text")
        if reference == "step_text"
        else target_step.get("text_block")
    )
    if declaration is None or value not in str(declaration):
        raise ValueError(
            f"Step {step_id} feature literal不在冻结声明中: "
            f"{reference}={value!r}"
        )
    return value


def resolve_implementation_parameters(brief, step_id, action_id, operation):
    candidates = []
    action = _brief_action(brief, step_id, action_id)
    for candidate in (
            (action.get("semantics") or {}).get(
                "implementation_constraints"
            )
            or ()
    ):
        if (
            isinstance(candidate, dict)
            and candidate.get("operation") == operation
            and isinstance(candidate.get("parameters"), dict)
        ):
            candidates.append(candidate)
    for ambiguity in brief.get("ambiguities") or ():
        if any((
            str(ambiguity.get("step_id") or "") != str(step_id),
            str(action_id) not in {
                str(item) for item in ambiguity.get("action_ids") or ()
            },
        )):
            continue
        for candidate in (
                (ambiguity.get("facts") or {}).get("assertion_candidates")
                or ()
        ):
            if (
                isinstance(candidate, dict)
                and candidate.get("operation") == operation
                and isinstance(candidate.get("parameters"), dict)
            ):
                candidates.append(candidate)

    unique = {}
    for candidate in candidates:
        key = json.dumps(
            {
                "target": candidate.get("target"),
                "parameters": candidate["parameters"],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        unique[key] = candidate
    if not unique:
        return None
    if len(unique) > 1:
        raise ValueError(
            f"Step {step_id} 操作 {operation} 的冻结实现参数不唯一"
        )
    candidate = next(iter(unique.values()))
    evidence_target = str(
        (action.get("target") or {}).get("locator_name") or ""
    )
    candidate_target = str(candidate.get("target") or "")
    if candidate_target and evidence_target and candidate_target != evidence_target:
        raise ValueError(
            f"Step {step_id} 操作 {operation} 的冻结参数目标不一致"
        )
    return dict(candidate["parameters"])


def resolve_recorded_action_value(brief, step_id, action_id, operation):
    action = _brief_action(brief, step_id, action_id)
    canonical = action.get("canonical_action") or {}
    command = canonical.get("command") or {}
    operation = str(operation or "")
    if canonical and operation == "send_text_keys":
        from autowork_core.utils.debug_tools.recorder.key_command_adapter import (
            encode_pywinauto_command,
        )

        return encode_pywinauto_command(command)
    if canonical and operation == "input_text":
        return command.get("text")
    if not canonical:
        value = action.get("value")
        if value is None:
            value = (action.get("parameters") or {}).get("value")
        if value is not None:
            return value

    after_state = (
        ((action.get("semantics") or {}).get("effect") or {}).get(
            "after_state"
        )
        or {}
    )
    state_keys = {
        "set_checked": "toggle_state",
        "set_tree_expanded": "expanded",
        "set_slider_value": "range_value",
    }
    state_key = state_keys.get(operation)
    if state_key and after_state.get(state_key) is not None:
        value = after_state[state_key]
        if state_key == "toggle_state" and value in {0, 1}:
            return bool(value)
        return value

    parameters = resolve_implementation_parameters(
        brief,
        step_id,
        action_id,
        operation,
    )
    return (parameters or {}).get("expected")


def qualify_value_sources(brief, step_id, operation):
    step_id = str(step_id or "")
    operation = str(operation or "")
    target_step = next((
        item
        for item in (brief.get("target") or {}).get("steps") or ()
        if str(item.get("id") or "") == step_id
    ), {})
    sources = []
    for action in brief.get("actions") or ():
        action_id = str(action.get("id") or "")
        if str(action.get("step_id") or "") != step_id or not action_id:
            continue
        try:
            value = resolve_recorded_action_value(
                brief,
                step_id,
                action_id,
                operation,
            )
        except ValueError:
            value = None
        if value is not None:
            sources.append({
                "shape": {
                    "kind": "recorded_action",
                    "action_id": action_id,
                },
                "status": "available",
                "basis": "frozen_recorded_action_value",
            })
    for reference in sorted(FEATURE_LITERAL_REFERENCES):
        try:
            resolve_feature_literal(brief, step_id, reference)
        except ValueError:
            declaration = (
                target_step.get("text")
                if reference == "step_text"
                else target_step.get("text_block")
            )
            if declaration:
                status = "requires_ai_literal"
                basis = "literal_must_exist_in_frozen_feature_declaration"
            else:
                status = "unavailable"
                basis = "no_frozen_feature_declaration"
        else:
            status = "available"
            basis = "unique_frozen_feature_literal"
        sources.append({
            "shape": {
                "kind": "feature_literal",
                "reference": reference,
            },
            "status": status,
            "basis": basis,
        })
    scenario = (brief.get("target") or {}).get("scenario") or {}
    declared_examples = declared_example_arguments(brief, step_id)
    for reference, value in sorted(
            (scenario.get("example_values") or {}).items()
    ):
        if value is not None and str(reference) in declared_examples:
            sources.append({
                "shape": {
                    "kind": "examples",
                    "reference": str(reference),
                },
                "status": "available",
                "basis": "frozen_scenario_example",
            })
    for reference in (target_step.get("table") or {}).get("headings") or ():
        sources.append({
            "shape": {
                "kind": "data_table",
                "reference": str(reference),
            },
            "status": "available",
            "basis": "frozen_step_table_column",
        })
    if operation in SEMANTIC_LITERAL_OPERATIONS:
        sources.append({
            "shape": {"kind": "semantic_literal"},
            "status": "requires_ai_literal",
            "basis": "operation_allows_semantic_literal",
        })
    sources.append({
        "shape": {"kind": "runtime"},
        "status": "requires_prior_design_producer",
        "basis": "design_runtime_binding",
    })
    return {
        "information_class": "value_source_qualification",
        "step_id": step_id,
        "operation": operation,
        "actual_values_exposed": False,
        "sources": sources,
    }


def declared_example_arguments(brief, step_id):
    target = (brief or {}).get("target") or {}
    scenario = target.get("scenario") or {}
    example_values = scenario.get("example_values") or {}
    if not example_values:
        return set()
    target_step = next((
        step
        for step in target.get("steps") or ()
        if str(step.get("id") or "") == str(step_id)
    ), {})
    concrete = " ".join(str(target_step.get("text") or "").split())
    specification = scenario.get("specification") or (
        (
            ((brief or {}).get("scenario_intelligence") or {}).get(
                "specification"
            )
            or {}
        ).get("scenario")
        or {}
    ).get("specification") or {}
    template_steps = list(
        (specification.get("template") or {}).get("steps") or ()
    )
    if not template_steps and target_step:
        template_steps = [target_step]
    for template_step in template_steps:
        template = str((template_step or {}).get("text") or "")
        arguments = {
            match.group(1)
            for match in re.finditer(r"<([^>]+)>", template)
            if match.group(1) in example_values
        }
        if not arguments:
            continue
        rendered = re.sub(
            r"<([^>]+)>",
            lambda match: str(
                example_values.get(match.group(1), match.group(0))
            ),
            template,
        )
        if (
            " ".join(rendered.split()) == concrete
            or " ".join(template.split()) == concrete
        ):
            return arguments
    return set()


def _brief_action(brief, step_id, action_id):
    return next((
        item
        for item in brief.get("actions") or ()
        if str(item.get("step_id") or "") == str(step_id)
        and str(item.get("id") or "") == str(action_id)
    ), {})


__all__ = [
    "FEATURE_LITERAL_REFERENCES",
    "SEMANTIC_LITERAL_OPERATIONS",
    "resolve_feature_literal",
    "resolve_declared_feature_literal",
    "resolve_implementation_parameters",
    "resolve_recorded_action_value",
    "qualify_value_sources",
    "declared_example_arguments",
]