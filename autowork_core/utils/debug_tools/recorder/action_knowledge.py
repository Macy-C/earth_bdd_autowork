from __future__ import annotations

from autowork_core.utils.debug_tools.recorder.ai_capability_registry import (
    capability_by_name,
    operations_for_recorded_action,
    plan_operation_names,
)
from autowork_core.utils.debug_tools.recorder.value_authority import (
    qualify_value_sources,
)


ACTION_KNOWLEDGE_VERSION = "1.2"
KNOWN_STANDARD_CONTROL_TYPES = frozenset({
    "Button",
    "CheckBox",
    "ComboBox",
    "Edit",
    "List",
    "ListItem",
    "DataGrid",
    "DataItem",
    "RadioButton",
    "Slider",
    "Tab",
    "TabItem",
    "Tree",
    "TreeItem",
})


def query_action_knowledge(
        brief,
        *,
        step_id=None,
        action_id=None,
        operation_names=(),
        list_only=False,
    ):
    operations = _operation_names(operation_names)
    if step_id and not action_id:
        raise ValueError("step_id必须与action_id一起使用")
    action = (
        _brief_action(brief, action_id, step_id=step_id)
        if action_id
        else None
    )
    if list_only:
        operations = sorted(plan_operation_names())
    elif not operations:
        raise ValueError(
            "Action knowledge要求AI先提供--operation候选，或使用--list"
        )
    return {
        "action_knowledge_version": ACTION_KNOWLEDGE_VERSION,
        "request_id": (brief or {}).get("request_id"),
        "action": _action_facts(action) if action is not None else None,
        "operations": [
            _operation_card(name, action, brief)
            for name in operations
        ],
        "policy": {
            "ai_supplies_operation_candidates": True,
            "operations_preserve_query_order": not list_only,
            "automatic_operation_ranking": False,
            "semantic_hypotheses_exposed": False,
            "ai_may_choose_another_registered_operation": True,
            "only_proven_incompatibility_is_rejected": True,
            "unknown_requires_ai_investigation_or_runtime_validation": True,
            "static_compatibility_is_not_runtime_proof": True,
        },
    }


def operation_compatibility(operation_name, action):
    capability = capability_by_name(operation_name)
    if capability is None or not capability.plan_enabled:
        return _compatibility(
            "incompatible",
            "Operation is not available to AI Plans.",
            "capability_registry",
        )
    if action is None:
        return _compatibility(
            "unknown",
            "No target Action was supplied.",
            "missing_action_facts",
        )
    target = action.get("target") or {}
    control_type = str(target.get("control_type") or "")
    required = set(capability.required_control_types)
    if required:
        if not control_type:
            return _compatibility(
                "unknown",
                "The frozen target has no control_type.",
                "missing_control_type",
            )
        if control_type not in required:
            if control_type in KNOWN_STANDARD_CONTROL_TYPES:
                return _compatibility(
                    "incompatible",
                    (
                        f"Runtime API requires {sorted(required)}, "
                        f"but the frozen standard target is {control_type}."
                    ),
                    "frozen_standard_control_type",
                )
            return _compatibility(
                "unknown",
                (
                    f"Runtime API normally requires {sorted(required)}, but "
                    f"the {control_type} target has no frozen Pattern proof."
                ),
                "unproven_custom_control_adapter",
            )
        return _compatibility(
            "compatible",
            (
                f"Frozen target control_type {control_type} is in the "
                "runtime API target domain."
            ),
            "frozen_standard_control_type",
        )
    action_type = str(action.get("type") or "")
    if operation_name in operations_for_recorded_action(action_type):
        return _compatibility(
            "compatible",
            f"Frozen mechanical Action type {action_type} is supported.",
            "frozen_recorded_action_type",
        )
    return _compatibility(
        "unknown",
        (
            "The current evidence neither proves compatibility nor proves "
            "incompatibility."
        ),
        "insufficient_frozen_facts",
    )


def _compatibility(status, reason, basis):
    return {
        "status": status,
        "reason": reason,
        "basis": basis,
        "information_class": "static_capability_assessment",
        "runtime_proof": False,
    }


def _operation_card(name, action, brief):
    capability = capability_by_name(name)
    if capability is None or not capability.plan_enabled:
        raise ValueError(f"未知或不可用于Plan的operation: {name}")
    defaults = _category_knowledge(capability)
    card = {
        "operation": capability.name,
        "capability_facts": {
            "information_class": "capability_fact",
            "runtime_api": f"BasePage.{capability.api_name}",
            "signature": _runtime_signature(capability.api_name),
            "category": capability.category,
            "requires_value_action": capability.requires_value_action,
            "required_control_types": sorted(
                capability.required_control_types
            ),
        },
        "maintainer_guidance": {
            "information_class": "advisory_maintainer_guidance",
            "may_be_incomplete_or_wrong": True,
            "purpose": capability.purpose or defaults["purpose"],
            "use_when": list(
                capability.use_when or defaults["use_when"]
            ),
            "avoid_when": list(
                capability.avoid_when or defaults["avoid_when"]
            ),
            "alternatives": list(capability.alternatives),
        },
        "static_assessment": operation_compatibility(name, action),
    }
    if capability.requires_value_action:
        if action is None:
            card["value_source_qualification"] = {
                "information_class": "value_source_qualification",
                "operation": capability.name,
                "actual_values_exposed": False,
                "status": "target_action_required",
                "sources": [],
            }
        else:
            card["value_source_qualification"] = qualify_value_sources(
                brief,
                action.get("step_id"),
                capability.name,
            )
    else:
        card["value_source_qualification"] = {
            "information_class": "value_source_qualification",
            "operation": capability.name,
            "actual_values_exposed": False,
            "status": "forbidden_for_operation",
            "sources": [],
        }
    return card


def _category_knowledge(capability):
    readable_name = capability.name.replace("_", " ")
    if capability.category == "wait":
        return {
            "purpose": f"Synchronize until {readable_name.removeprefix('wait ')}.",
            "use_when": (
                "A later operation depends on this observable UI state.",
            ),
            "avoid_when": (
                "The wait would replace a business assertion or duplicate an "
                "action's built-in wait.",
            ),
        }
    if capability.category == "assertion":
        return {
            "purpose": f"Verify the business-visible condition: {readable_name}.",
            "use_when": (
                "The Step declares an expected result with a frozen value source.",
            ),
            "avoid_when": (
                "The value is only an unconfirmed runtime observation.",
            ),
        }
    if capability.category in {"visual_ocr", "visual"}:
        return {
            "purpose": f"Use visual evidence to {readable_name}.",
            "use_when": (
                "Structured UIA evidence is unavailable and the visual region "
                "is frozen.",
            ),
            "avoid_when": (
                "A stable structured locator or unbounded full-screen search "
                "would be used instead.",
            ),
        }
    return {
        "purpose": f"Perform the registered interaction: {readable_name}.",
        "use_when": (
            "Target facts and business intent support this interaction.",
        ),
        "avoid_when": (
            "A more specific final-state operation better expresses the result.",
        ),
    }


def _runtime_signature(api_name):
    import inspect

    from autowork_core.page.singleton import BasePage

    method = getattr(BasePage, str(api_name), None)
    return str(inspect.signature(method)) if callable(method) else None


def _action_facts(action):
    target = action.get("target") or {}
    effect = ((action.get("semantics") or {}).get("effect") or {})
    return {
        "information_class": "frozen_action_facts",
        "action_id": action.get("id"),
        "step_id": action.get("step_id"),
        "action_type": action.get("type"),
        "control_type": target.get("control_type"),
        "locator_name": target.get("locator_name"),
        "locator_validation": target.get("locator_validation"),
        "target_fingerprint": target.get("target_fingerprint"),
        "observed_value": (
            ((action.get("canonical_action") or {}).get(
                "observed_after"
            ) or {}).get("text")
            if action.get("canonical_action")
            else action.get("value")
        ),
        "parameters": action.get("parameters") or {},
        "effect": effect,
    }


def _brief_action(brief, action_id, *, step_id=None):
    action_id = str(action_id or "")
    step_id = str(step_id or "")
    matches = [
        action
        for action in (brief or {}).get("actions") or []
        if str(action.get("id") or "") == action_id
        and (
            not step_id
            or str(action.get("step_id") or "") == step_id
        )
    ]
    if not matches:
        identity = f"{step_id}/{action_id}" if step_id else action_id
        raise KeyError(f"Brief中不存在Action: {identity}")
    if len(matches) > 1:
        raise ValueError(
            f"Action ID在多个Step中重复，请提供step_id: {action_id}"
        )
    return matches[0]


def _operation_names(values):
    return list(dict.fromkeys(
        str(value)
        for value in values or ()
        if str(value)
    ))