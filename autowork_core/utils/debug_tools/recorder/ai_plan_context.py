from __future__ import annotations

import hashlib
import json


AI_PLAN_CONTEXT_VERSION = "1.1"
AI_PLAN_CONTEXT_SECTIONS = (
    "intent",
    "proof",
    "annotation",
    "decision",
    "full",
)


def build_ai_plan_context(plan_artifact, *, last_result=None):
    artifact = plan_artifact if isinstance(plan_artifact, dict) else {}
    plan = artifact.get("plan") or {}
    steps = {
        str(step_id): _step_summary(step)
        for step_id, step in (plan.get("steps") or {}).items()
        if isinstance(step, dict)
    }
    step_defaults = _hoist_step_defaults(steps)
    context = _without_empty({
        "plan_context_version": AI_PLAN_CONTEXT_VERSION,
        "request_id": artifact.get("request_id"),
        "plan_id": artifact.get("plan_id"),
        "plan_fingerprint": artifact.get("plan_fingerprint"),
        "status": artifact.get("status"),
        "plan_origin": (artifact.get("source") or {}).get("plan_origin"),
        "summary": plan.get("summary"),
        "scenario_model": _scenario_model_summary(
            plan.get("scenario_model")
        ),
        "window_owners": {
            str(owner_id): _window_owner_summary(owner)
            for owner_id, owner in (plan.get("window_owners") or {}).items()
            if isinstance(owner, dict)
        },
        "step_defaults": step_defaults,
        "steps": steps,
        "last_result": _last_result_summary(last_result),
        "expand": {
            "sections": list(AI_PLAN_CONTEXT_SECTIONS),
            "step_query": True,
        },
    })
    fingerprint = ai_plan_context_fingerprint(context)
    context["plan_context_id"] = "plan-context-" + fingerprint[:16]
    context["plan_context_fingerprint"] = fingerprint
    return context


def query_ai_plan_context(
        plan_artifact,
        *,
        section=None,
        step_id=None,
        last_result=None,
    ):
    artifact = plan_artifact if isinstance(plan_artifact, dict) else {}
    compact = build_ai_plan_context(
        artifact,
        last_result=last_result,
    )
    if section is not None and step_id is not None:
        raise ValueError("Plan query 只能选择 section 或 Step 之一")
    if section is None and step_id is None:
        return compact
    if section is not None and section not in AI_PLAN_CONTEXT_SECTIONS:
        raise ValueError(f"未知 Plan context section: {section}")
    plan = artifact.get("plan") or {}
    if step_id is not None:
        step_id = str(step_id)
        step = (plan.get("steps") or {}).get(step_id)
        if not isinstance(step, dict):
            raise KeyError(f"Plan 中不存在 Step: {step_id}")
        owner_ids = {
            str(operation.get("window_owner"))
            for operation in step.get("operations") or ()
            if isinstance(operation, dict) and operation.get("window_owner")
        }
        value = {
            "step_id": step_id,
            "step": json.loads(json.dumps(step, ensure_ascii=False)),
            "window_owners": {
                owner_id: json.loads(json.dumps(
                    (plan.get("window_owners") or {}).get(owner_id) or {},
                    ensure_ascii=False,
                ))
                for owner_id in sorted(owner_ids)
            },
        }
        return _query_response(compact, "step", value)
    value = {
        "intent": artifact.get("intent") or {},
        "proof": _proof_projection(plan),
        "annotation": plan.get("annotation_trace") or {},
        "decision": {
            "decision_trace": plan.get("decision_trace") or [],
            "ambiguity_resolutions": (
                plan.get("ambiguity_resolutions") or []
            ),
        },
        "full": _verified_full_plan(artifact),
    }[section]
    return _query_response(compact, section, value)


def ai_plan_context_identity_is_valid(value):
    if not isinstance(value, dict):
        return False
    fingerprint = ai_plan_context_fingerprint(value)
    return all((
        value.get("plan_context_version") == AI_PLAN_CONTEXT_VERSION,
        bool(value.get("plan_id")),
        bool(value.get("plan_fingerprint")),
        value.get("plan_context_fingerprint") == fingerprint,
        value.get("plan_context_id")
        == "plan-context-" + fingerprint[:16],
    ))


def ai_plan_context_fingerprint(value):
    payload = {
        key: item
        for key, item in dict(value or {}).items()
        if key not in {
            "plan_context_id",
            "plan_context_fingerprint",
        }
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _scenario_model_summary(value):
    value = value if isinstance(value, dict) else {}
    return _without_empty({
        "mode": value.get("mode"),
        "summary": value.get("summary"),
        "step_roles": {
            str(item.get("step_id")): item.get("role")
            for item in value.get("steps") or ()
            if isinstance(item, dict) and item.get("step_id")
        },
    })


def _window_owner_summary(value):
    resolution = value.get("resolution") or {}
    return _without_empty({
        "strategy": resolution.get("strategy"),
        "candidate_id": resolution.get("candidate_id"),
        "page_object": value.get("page_object"),
        "root_locator_file": value.get("root_locator_file"),
        "root_locator": value.get("root_locator"),
        "views": {
            str(view_id): _without_empty({
                "view_object": view.get("view_object"),
                "locator_file": view.get("locator_file"),
                "active_locator": view.get("active_locator"),
            })
            for view_id, view in (value.get("views") or {}).items()
            if isinstance(view, dict)
        },
    })


def _step_summary(value):
    behavior = value.get("behavior_resolution") or {}
    table_usage = value.get("table_usage")
    operations = [
        item
        for item in value.get("operations") or ()
        if isinstance(item, dict)
    ]
    defaults = {
        key: _common_operation_value(operations, key)
        for key in (
            "window_owner",
            "view_owner",
            "implementation_location",
        )
    }
    return _without_empty({
        "behavior_owner": value.get("behavior_owner"),
        "behavior_file": value.get("behavior_file"),
        "behavior_resolution": _without_empty({
            "strategy": behavior.get("strategy"),
            "candidate_id": behavior.get("candidate_id"),
        }),
        "page_object": value.get("page_object"),
        "locator_file": value.get("locator_file"),
        "data_file": value.get("data_file"),
        "table_usage": _table_usage_summary(table_usage),
        "action_relationships": [
            dict(item)
            for item in value.get("action_relationships") or ()
            if isinstance(item, dict)
        ],
        "operation_defaults": _without_empty(defaults),
        "operations": [
            _operation_summary(index, operation, defaults=defaults)
            for index, operation in enumerate(
                operations,
                start=1,
            )
        ],
        "ignored_action_count": (
            len(value.get("ignored_action_ids") or ()) or None
        ),
    })


def _hoist_step_defaults(steps):
    keys = (
        "behavior_owner",
        "behavior_file",
        "page_object",
        "locator_file",
    )
    defaults = {}
    for key in keys:
        values = {
            step.get(key)
            for step in steps.values()
        }
        if len(values) != 1:
            continue
        value = next(iter(values))
        if value in (None, ""):
            continue
        defaults[key] = value
        for step in steps.values():
            step.pop(key, None)
    return defaults


def _operation_summary(index, value, *, defaults):
    resolution = value.get("implementation_resolution") or {}
    return _without_empty({
        "order": index,
        "op": value.get("op"),
        "target": value.get("target"),
        "value": value.get("value"),
        "source": value.get("source"),
        "value_provenance": value.get("value_provenance") or {},
        "result_binding": value.get("result_binding"),
        "window_owner": _different_from_default(
            value.get("window_owner"),
            defaults.get("window_owner"),
        ),
        "view_owner": _different_from_default(
            value.get("view_owner"),
            defaults.get("view_owner"),
        ),
        "implementation_location": _different_from_default(
            value.get("implementation_location"),
            defaults.get("implementation_location"),
        ),
        "implementation_method": value.get("implementation_method"),
        "implementation_resolution": _without_empty({
            "strategy": resolution.get("strategy"),
            "candidate_id": resolution.get("candidate_id"),
        }),
        "reason": value.get("reason"),
        "rejected_alternatives": value.get("rejected_alternatives") or [],
        "uncertainty": value.get("uncertainty"),
        "parameters": value.get("parameters") or {},
    })


def _common_operation_value(operations, key):
    values = {
        item.get(key)
        for item in operations
    }
    return next(iter(values)) if len(values) == 1 else None


def _different_from_default(value, default):
    return value if value != default else None


def _table_usage_summary(value):
    if not isinstance(value, dict) or not value:
        return None
    return _without_empty({
        "consumption": value.get("consumption"),
        "shape": value.get("shape"),
        "consumer": value.get("consumer"),
        "ordered": value.get("ordered"),
        "reset_between_rows": value.get("reset_between_rows"),
        "columns": value.get("columns") or {},
        "context_key": value.get("context_key"),
    })


def _last_result_summary(value):
    value = value if isinstance(value, dict) else {}
    return _without_empty({
        "transaction_id": value.get("transaction_id"),
        "status": value.get("status"),
        "failed_checks": value.get("failed_checks") or [],
    })


def _proof_projection(plan):
    return {
        "steps": {
            str(step_id): _without_empty({
                "annotation_ids": step.get("annotation_ids") or [],
                "covered_action_ids": step.get("covered_action_ids") or [],
                "ignored_action_ids": step.get("ignored_action_ids") or [],
                "action_relationships": [
                    dict(item)
                    for item in step.get("action_relationships") or ()
                    if isinstance(item, dict)
                ],
                "operations": [
                    _without_empty({
                        key: operation.get(key)
                        for key in (
                            "op",
                            "action_ids",
                            "target_action_id",
                            "value_action_ids",
                            "value_provenance",
                            "target_fingerprint",
                            "evidence_ids",
                            "effect_ids",
                            "decision_ids",
                        )
                    })
                    for operation in step.get("operations") or ()
                    if isinstance(operation, dict)
                ],
            })
            for step_id, step in (plan.get("steps") or {}).items()
            if isinstance(step, dict)
        },
    }


def _query_response(compact, section, value):
    response = {
        "plan_context_version": AI_PLAN_CONTEXT_VERSION,
        "plan_id": compact.get("plan_id"),
        "plan_fingerprint": compact.get("plan_fingerprint"),
        "plan_context_fingerprint": compact.get(
            "plan_context_fingerprint"
        ),
        "section": section,
        "value": json.loads(json.dumps(value, ensure_ascii=False)),
    }
    encoded = json.dumps(
        response,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    response["query_fingerprint"] = hashlib.sha256(encoded).hexdigest()
    return response


def _verified_full_plan(artifact):
    source = artifact.get("source") or {}
    intent = artifact.get("intent")
    value = {
        "plan_version": artifact.get("plan_version"),
        "plan_id": artifact.get("plan_id"),
        "status": artifact.get("status"),
        "request_id": artifact.get("request_id"),
        "source": _without_empty({
            key: source.get(key)
            for key in (
                "confirmation_source",
                "plan_origin",
                "brief_basis_fingerprint",
                "revision_seal",
                "decision_answer_fingerprint",
                "intent_fingerprint",
            )
        }),
        "plan": artifact.get("plan") or {},
        "plan_fingerprint": artifact.get("plan_fingerprint"),
    }
    if isinstance(intent, dict):
        value["intent"] = _without_empty({
            "intent_version": intent.get("intent_version"),
            "intent_fingerprint": intent.get("intent_fingerprint"),
            "content": intent.get("content"),
        })
    return _without_empty(value)


def _without_empty(value):
    return {
        key: item
        for key, item in value.items()
        if item not in (None, "", [], {})
    }