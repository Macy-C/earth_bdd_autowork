from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from autowork_core.utils.debug_tools.recorder.generation_design import (
    compact_generation_design_contract,
)
from autowork_core.utils.debug_tools.recorder.ai_plan_context import (
    ai_plan_context_identity_is_valid,
)
from autowork_core.utils.debug_tools.recorder.reconciliation_repository import (
    load_generation_brief,
)
from autowork_core.utils.debug_tools.recorder.semantic_reconciler import (
    brief_matches_request,
)


AI_CONTEXT_ENVELOPE_VERSION = "2.1"
READABLE_AI_CONTEXT_ENVELOPE_VERSIONS = {AI_CONTEXT_ENVELOPE_VERSION}
GENERATION_DESIGN_CONTEXT_VERSION = "1.1"
DESIGN_CONTEXT_OMITTED_SECTIONS = frozenset({
    "adjustment",
    "agent_tasks",
    "annotation_snapshot",
    "conflicts",
    "coverage",
    "memory_digest.detail",
    "scenario_intelligence",
    "semantics.packs",
    "semantics.locator_reuse_matches",
    "semantics.step_continuity",
    "semantics.window_causality",
    "window_ownership.window_causality",
})
STEP_DESIGN_CONTEXT_OMITTED_SECTIONS = frozenset({
    "adjustment",
    "agent_tasks",
    "annotation_snapshot",
    "conflicts",
    "coverage",
    "scenario_intelligence",
})


def build_ai_context_envelope(
        *,
        session_dir,
        request,
        state,
        brief_path,
        job_value,
        job_path=None,
        workflow_version="5.0",
        workflow_context=None,
        ai_capabilities=None,
        plan_context=None,
        generation_task_bundle=None,
    ):
    session_dir = Path(session_dir).resolve()
    request = request if isinstance(request, dict) else {}
    state = state if isinstance(state, dict) else {}
    job = job_value if isinstance(job_value, dict) else {}
    brief = load_generation_brief(brief_path)
    if not brief_matches_request(brief, request):
        raise ValueError("AI Context Envelope Brief身份与Request不一致")

    decision = state.get("decision") or {}
    execution = state.get("job_execution") or {}
    if not execution:
        for entry in reversed(list(state.get("retired_jobs") or ())):
            pointer = (entry or {}).get("job") or {}
            if pointer.get("job_id") == job.get("job_id"):
                execution = (entry or {}).get("job_execution") or {}
                break
    request_ref = job.get("request") or {}
    profile = job.get("profile_lease") or {}
    boundary = job.get("execution_boundary") or {}
    design_context_enabled = "job-design-context" in set(
        boundary.get("allowed_queries") or ()
    )
    if not design_context_enabled:
        raise ValueError(
            "Generation Job 缺少 current job-design-context query"
        )
    task_bundle = copy.deepcopy(generation_task_bundle or {})
    brief_projection = (
        _build_envelope_brief_reference(
            brief,
            session_dir=session_dir,
            brief_path=brief_path,
        )
        if task_bundle
        else _build_direct_brief_reference(
            brief,
            session_dir=session_dir,
            brief_path=brief_path,
        )
    )
    envelope = {
        "ai_context_envelope_version": AI_CONTEXT_ENVELOPE_VERSION,
        "workflow_version": str(workflow_version),
        "job": _without_empty({
            "job_id": job.get("job_id"),
            "job_fingerprint": job.get("job_fingerprint"),
            "workload": copy.deepcopy(job.get("workload") or {}),
            "path": _relative_path(session_dir, job_path),
        }),
        "request": _without_empty({
            "request_id": request_ref.get("request_id")
            or request.get("request_id"),
            "request_fingerprint": request_ref.get("request_fingerprint")
            or request.get("request_fingerprint"),
            "revision_seal": request_ref.get("revision_seal")
            or ((request.get("revision_snapshot") or {}).get("seal")),
            "path": request_ref.get("path") or request.get("request_path"),
        }),
        "generation_profile": _without_empty({
            "profile_id": profile.get("profile_id"),
            "profile_fingerprint": profile.get("profile_fingerprint"),
        }),
        "job_execution": _without_empty({
            "phase": execution.get("phase") or "ready",
            "epoch": execution.get("epoch", 0),
            "claim_id": execution.get("claim_id"),
            "attempt_no": execution.get("attempt_no", 0),
        }),
        "allowed_queries": list(boundary.get("allowed_queries") or ()),
        "workflow": workflow_context or _compact_workflow_context(state),
        "decision": _without_empty({
            "status": decision.get("status"),
            "pack": _without_empty({
                "path": (decision.get("pack") or {}).get("path"),
                "pack_id": (decision.get("pack") or {}).get("pack_id"),
                "pack_fingerprint": (
                    (decision.get("pack") or {}).get("pack_fingerprint")
                ),
            }),
            "answers": _without_empty({
                "path": (decision.get("answers") or {}).get("path"),
                "answer_fingerprint": (
                    (decision.get("answers") or {}).get(
                        "answer_fingerprint"
                    )
                ),
            }),
        }),
        "brief": brief_projection,
        "plan_context": plan_context,
        "generation_task_bundle": task_bundle,
        "ai_capabilities": ai_capabilities or {},
        "design_contract": compact_generation_design_contract(),
        "query_policy": {
            "rule": (
                "Expand omitted design detail only through allowed_queries."
            ),
        },
    }
    envelope["envelope_fingerprint"] = ai_context_envelope_fingerprint(
        envelope
    )
    if not ai_context_envelope_identity_is_valid(envelope):
        raise ValueError("AI Context Envelope identity无效")
    return envelope


def compact_ai_context_envelope_contract():
    value = {
        "ai_context_envelope_version": AI_CONTEXT_ENVELOPE_VERSION,
        "required_top_level": [
            "ai_context_envelope_version",
            "workflow_version",
            "job",
            "request",
            "generation_profile",
            "job_execution",
            "allowed_queries",
            "workflow",
            "decision",
            "brief",
            "plan_context",
            "generation_task_bundle",
            "ai_capabilities",
            "design_contract",
            "query_policy",
            "envelope_fingerprint",
        ],
        "required_job_fields": [
            "job_id",
            "job_fingerprint",
            "workload",
        ],
        "required_request_fields": [
            "request_id",
            "request_fingerprint",
            "revision_seal",
        ],
        "required_profile_fields": ["profile_id", "profile_fingerprint"],
        "brief_transport": {
            "version": GENERATION_DESIGN_CONTEXT_VERSION,
            "rule": (
                "The Envelope carries a content-addressed Brief reference and "
                "target summary. Scenario Design context carries per-Step "
                "Action counts while Action facts expand by Step. Direct Jobs "
                "carry system-baseline seed status; paged Jobs carry a "
                "bound GenerationTaskBundle index."
            ),
        },
        "backend_identity": [
            "GenerationJobV1",
            "WorkflowState",
            "GenerationContract",
            "admission_receipt",
        ],
        "omission_rule": (
            "Omitted evidence and artifacts must be expanded only through "
            "allowed_queries."
        ),
    }
    value["contract_fingerprint"] = _fingerprint(value)
    return value


def _build_envelope_brief_reference(brief, *, session_dir, brief_path):
    brief = dict(brief or {})
    target = dict(brief.get("target") or {})
    feature = dict(target.get("feature") or {})
    scenario = dict(target.get("scenario") or {})
    steps = [
        dict(item)
        for item in target.get("steps") or ()
        if isinstance(item, dict)
    ]
    value = {
        "generation_design_context_version": GENERATION_DESIGN_CONTEXT_VERSION,
        "request_id": brief.get("request_id"),
        "brief_fingerprint": brief.get("brief_fingerprint"),
        "target": {
            "feature": _copy_fields(
                feature,
                ("id", "name", "source_relpath"),
            ),
            "scenario": _copy_fields(
                scenario,
                ("id", "name", "kind", "example_id"),
            ),
            "steps": [
                _copy_fields(
                    step,
                    ("id", "keyword", "semantic_type", "text"),
                )
                for step in steps
            ],
        },
        "design_context_transport": {
            "version": GENERATION_DESIGN_CONTEXT_VERSION,
            "scope": "bundle_index",
            "requested_step_id": None,
            "omitted_sections": sorted(DESIGN_CONTEXT_OMITTED_SECTIONS),
            "full_brief": {
                "path": _relative_path(session_dir, brief_path),
                "brief_fingerprint": brief.get("brief_fingerprint"),
                "expand": "job-design-context",
            },
            "actions": {
                "source": "generation_task_bundle",
                "query": "job-task-bundle --fragment-id <fragment-id>",
            },
        },
    }
    value = _without_empty(value)
    value["design_context_fingerprint"] = _design_context_fingerprint(value)
    return value


def _build_direct_brief_reference(brief, *, session_dir, brief_path):
    brief = dict(brief or {})
    target = dict(brief.get("target") or {})
    steps = [
        item
        for item in target.get("steps") or ()
        if isinstance(item, dict)
    ]
    value = {
        "generation_design_context_version": GENERATION_DESIGN_CONTEXT_VERSION,
        "request_id": brief.get("request_id"),
        "brief_fingerprint": brief.get("brief_fingerprint"),
        "target": {
            "feature": _copy_fields(
                dict(target.get("feature") or {}),
                ("id", "name", "source_relpath"),
            ),
            "scenario": _copy_fields(
                dict(target.get("scenario") or {}),
                ("id", "name", "kind", "example_id"),
            ),
            "step_count": len(steps),
        },
        "design_context_transport": {
            "version": GENERATION_DESIGN_CONTEXT_VERSION,
            "scope": "system_baseline",
            "requested_step_id": None,
            "omitted_sections": sorted(DESIGN_CONTEXT_OMITTED_SECTIONS),
            "full_brief": {
                "path": _relative_path(session_dir, brief_path),
                "brief_fingerprint": brief.get("brief_fingerprint"),
                "expand": "job-design-context",
            },
            "actions": {
                "source": "system_baseline",
            },
        },
    }
    value = _without_empty(value)
    value["design_context_fingerprint"] = _design_context_fingerprint(value)
    return value


def build_envelope_brief_projection(
        brief,
        *,
        session_dir,
        brief_path,
        step_id=None,
        expanded=False,
):
    """Project only the frozen facts needed to submit a GenerationDesign."""
    brief = copy.deepcopy(dict(brief or {}))
    target = dict(brief.get("target") or {})
    requested_step_id = str(step_id or "").strip()
    target_steps = [
        dict(item)
        for item in target.get("steps") or ()
        if isinstance(item, dict)
    ]
    if requested_step_id:
        target_steps = [
            item for item in target_steps
            if str(item.get("id") or "") == requested_step_id
        ]
        if len(target_steps) != 1:
            raise ValueError(
                f"Generation Design Context不存在目标Step: {requested_step_id}"
            )
    expanded_step = bool(requested_step_id and expanded)
    selected_step_ids = {
        str(item.get("id") or "")
        for item in target_steps
        if item.get("id")
    }
    actions = [
        dict(item)
        for item in brief.get("actions") or ()
        if isinstance(item, dict)
        and (
            not requested_step_id
            or str(item.get("step_id") or "") in selected_step_ids
        )
    ]
    root_names = {
        str((item.get("target") or {}).get("root_name") or "")
        for item in actions
        if (item.get("target") or {}).get("root_name")
    }
    ownership = dict(brief.get("window_ownership") or {})
    for candidate in ownership.get("ownership_candidates") or ():
        if not isinstance(candidate, dict):
            continue
        root_name = str(candidate.get("root_name") or "")
        if root_name:
            root_names.add(root_name)
    projected = {
        "generation_design_context_version": GENERATION_DESIGN_CONTEXT_VERSION,
        "schema_version": brief.get("schema_version"),
        "brief_version": brief.get("brief_version"),
        "request_id": brief.get("request_id"),
        "brief_fingerprint": brief.get("brief_fingerprint"),
        "reconciliation_fingerprint": brief.get("reconciliation_fingerprint"),
        "risk": copy.deepcopy(brief.get("risk") or {}),
        "generation": copy.deepcopy(brief.get("generation") or {}),
        "target": (
            _project_expanded_design_target(target, target_steps)
            if expanded_step
            else _project_design_target(target, target_steps)
        ),
        "actions": (
            [copy.deepcopy(item) for item in actions]
            if expanded_step
            else (
                [_project_design_action(item) for item in actions]
                if requested_step_id
                else []
            )
        ),
        "action_summary": (
            _project_design_action_summary(actions, target_steps)
            if not requested_step_id
            else {}
        ),
        "ambiguities": _project_design_ambiguities(
            brief.get("ambiguities") or (),
            selected_step_ids,
            requested_step_id=bool(requested_step_id),
        ),
        "window_ownership": (
            _project_expanded_design_window_ownership(
                ownership,
                root_names,
                selected_step_ids,
            )
            if expanded_step
            else _project_design_window_ownership(
                ownership,
                root_names,
                selected_step_ids,
                requested_step_id=bool(requested_step_id),
            )
        ),
        "semantics": (
            _project_expanded_design_semantics(
                brief.get("semantics") or {},
                target_steps,
            )
            if expanded_step
            else _project_design_semantics(
                brief.get("semantics") or {},
                target_steps,
                requested_step_id=bool(requested_step_id),
            )
        ),
        "memory_digest": (
            _project_expanded_design_memory_digest(
                brief.get("memory_digest") or {},
                selected_step_ids,
            )
            if expanded_step
            else _project_design_memory_digest(
                brief.get("memory_digest") or {},
                selected_step_ids,
                requested_step_id=bool(requested_step_id),
            )
        ),
        "design_context_transport": {
            "version": GENERATION_DESIGN_CONTEXT_VERSION,
            "scope": (
                "step_detail"
                if expanded_step
                else "step"
                if requested_step_id
                else "scenario"
            ),
            "requested_step_id": requested_step_id or None,
            "omitted_sections": sorted(
                STEP_DESIGN_CONTEXT_OMITTED_SECTIONS
                if expanded_step
                else DESIGN_CONTEXT_OMITTED_SECTIONS
            ),
            "full_brief": {
                "path": _relative_path(session_dir, brief_path),
                "brief_fingerprint": brief.get("brief_fingerprint"),
                "expand": "job-design-context",
            },
        },
    }
    projected = _without_empty(projected)
    projected["design_context_fingerprint"] = _design_context_fingerprint(
        projected
    )
    return projected


def _project_design_target(target, steps):
    target = dict(target or {})
    feature = dict(target.get("feature") or {})
    scenario = dict(target.get("scenario") or {})
    return _without_empty({
        "feature": _copy_fields(
            feature,
            ("id", "name", "description", "line", "tags", "source_relpath"),
        ),
        "scenario": _copy_fields(
            scenario,
            (
                "id",
                "name",
                "kind",
                "logical_template_id",
                "example_id",
                "example_values",
                "tags",
                "generation_scope",
                "step_scope_binding",
            ),
        ),
        "steps": [
            _copy_fields(
                step,
                (
                    "id",
                    "keyword",
                    "semantic_type",
                    "text",
                    "text_block",
                    "table",
                    "step_user_context",
                    "step_user_context_revision",
                    "observation_intents",
                ),
            )
            for step in steps
        ],
    })


def _project_expanded_design_target(target, steps):
    target = dict(target or {})
    return _without_empty({
        "feature": copy.deepcopy(target.get("feature") or {}),
        "scenario": copy.deepcopy(target.get("scenario") or {}),
        "steps": [copy.deepcopy(step) for step in steps],
    })


def _project_design_action(action):
    action = dict(action or {})
    target = dict(action.get("target") or {})
    candidates = []
    for item in target.get("locator_candidates") or ():
        if not isinstance(item, dict) or not item.get("candidate_id"):
            continue
        locator = item.get("locator") or {}
        validation = item.get("validation") or {}
        stability = item.get("stability") or {}
        candidates.append(_without_empty({
            "candidate_id": item.get("candidate_id"),
            "by": locator.get("by"),
            "reason": item.get("reason"),
            "stability": stability.get("status"),
            "validation": _without_empty({
                "status": validation.get("status"),
                "target_matches": validation.get("target_matches"),
            }),
        }))
    projected_target = _copy_fields(
        target,
        (
            "root_name",
            "control_type",
            "name",
            "auto_id",
            "locator_name",
            "locator_strategy",
            "locator_stability",
            "locator_validation",
            "locator_candidate_id",
            "target_fingerprint",
            "interaction_confidence",
            "positional_fallback",
        ),
    )
    if candidates:
        projected_target["locator_candidates"] = candidates
    canonical = dict(action.get("canonical_action") or {})
    projected = _copy_fields(
        action,
        ("id", "step_id", "n", "type", "role", "binding", "note", "parameters"),
    )
    projected["target"] = projected_target
    if canonical:
        projected["canonical_action"] = _copy_fields(
            canonical,
            ("canonical_action_version", "command", "observed_after", "business_expectation"),
        )
    semantics = _project_design_action_semantics(action.get("semantics") or {})
    if semantics:
        projected["semantics"] = semantics
    return _without_empty(projected)


def _project_design_action_summary(actions, steps):
    by_step = {}
    for action in actions:
        step_id = str(action.get("step_id") or "")
        if not step_id:
            continue
        item = by_step.setdefault(step_id, {
            "step_id": step_id,
            "action_count": 0,
            "action_types": set(),
            "root_names": set(),
        })
        item["action_count"] += 1
        action_type = str(action.get("type") or "")
        if action_type:
            item["action_types"].add(action_type)
        root_name = str((action.get("target") or {}).get("root_name") or "")
        if root_name:
            item["root_names"].add(root_name)
    ordered = []
    for step in steps:
        item = by_step.get(str(step.get("id") or ""))
        if item is None:
            continue
        ordered.append({
            "step_id": item["step_id"],
            "action_count": item["action_count"],
            "action_types": sorted(item["action_types"]),
            "root_names": sorted(item["root_names"]),
        })
    return {
        "action_count": sum(item["action_count"] for item in ordered),
        "steps": ordered,
    }


def _project_design_action_semantics(value):
    value = dict(value or {})
    effect = _copy_fields(
        value.get("effect") or {},
        (
            "result",
            "changes",
            "after_state",
            "windows_opened",
            "windows_closed",
            "visual_stability",
        ),
    )
    return _without_empty({
        "runtime_value_sources": copy.deepcopy(
            value.get("runtime_value_sources") or {}
        ),
        "effect": effect,
        "implementation_constraints": copy.deepcopy(
            value.get("implementation_constraints") or []
        ),
        "locator_fallback": copy.deepcopy(value.get("locator_fallback")),
    })


def _project_design_ambiguities(values, step_ids, *, requested_step_id):
    result = []
    for item in values:
        if not isinstance(item, dict):
            continue
        step_id = str(item.get("step_id") or "")
        if requested_step_id and step_id and step_id not in step_ids:
            continue
        result.append(_copy_fields(
            item,
            (
                "ambiguity_id",
                "code",
                "routing",
                "step_id",
                "action_ids",
                "event_ids",
                "evidence_ids",
                "allowed_outcomes",
                "source",
                "facts",
            ),
        ))
    return result


def _project_design_window_ownership(
        value,
        root_names,
        step_ids,
        *,
        requested_step_id,
):
    value = dict(value or {})
    windows = []
    for item in value.get("windows") or ():
        if not isinstance(item, dict):
            continue
        root_name = str(item.get("root_name") or "")
        if requested_step_id and root_name not in root_names:
            continue
        owner_match = dict(item.get("owner_match") or {})
        candidates = []
        for candidate in owner_match.get("candidates") or ():
            if not isinstance(candidate, dict):
                continue
            projected_candidate = _copy_fields(
                candidate,
                (
                    "candidate_id",
                    "kind",
                    "page_object",
                    "page_class",
                    "root_locator_file",
                    "root_locator",
                    "criteria",
                    "page_sha256",
                    "locator_sha256",
                ),
            )
            methods = [
                _project_design_reuse_candidate(method)
                for method in candidate.get("method_candidates") or ()
                if isinstance(method, dict)
            ]
            if methods:
                projected_candidate["method_candidates"] = methods
            candidates.append(projected_candidate)
        windows.append(_without_empty({
            **_copy_fields(
                item,
                (
                    "root_name",
                    "root_criteria",
                    "step_ids",
                    "action_ids",
                    "action_types",
                    "locator_names",
                    "control_types",
                    "identity_status",
                ),
            ),
            "owner_match": _without_empty({
                "suggested_strategy": owner_match.get("suggested_strategy"),
                "candidates": candidates,
            }),
        }))
    ownership_candidates = []
    for item in value.get("ownership_candidates") or ():
        if not isinstance(item, dict):
            continue
        candidate_step_id = str(item.get("step_id") or "")
        if requested_step_id and candidate_step_id not in step_ids:
            continue
        ownership_candidates.append(_copy_fields(
            item,
            (
                "candidate_id",
                "kind",
                "root_name",
                "opener_action_id",
                "action_ids",
                "step_id",
            ),
        ))
    return _without_empty({
        "model": value.get("model"),
        "required_for_new_plan": value.get("required_for_new_plan"),
        "windows": windows,
        "cross_window_steps": [
            item
            for item in value.get("cross_window_steps") or ()
            if not requested_step_id or item in step_ids
        ],
        "roots_by_step": {
            key: copy.deepcopy(item)
            for key, item in (value.get("roots_by_step") or {}).items()
            if not requested_step_id or key in step_ids
        },
        "unowned_action_ids": copy.deepcopy(
            value.get("unowned_action_ids") or []
        ),
        "ownership_candidates": ownership_candidates,
    })


def _project_expanded_design_window_ownership(value, root_names, step_ids):
    value = dict(value or {})
    windows = [
        copy.deepcopy(item)
        for item in value.get("windows") or ()
        if isinstance(item, dict)
        and str(item.get("root_name") or "") in root_names
    ]
    ownership_candidates = [
        copy.deepcopy(item)
        for item in value.get("ownership_candidates") or ()
        if isinstance(item, dict)
        and str(item.get("step_id") or "") in step_ids
    ]
    related_action_ids = {
        str(action_id)
        for item in ownership_candidates
        for action_id in [
            item.get("opener_action_id"),
            *(item.get("child_action_ids") or ()),
        ]
        if action_id
    }
    causality = [
        copy.deepcopy(item)
        for item in value.get("window_causality") or ()
        if isinstance(item, dict)
        and (
            str(item.get("opened_by_action_id") or "") in related_action_ids
            or str(item.get("closed_by_action_id") or "") in related_action_ids
        )
    ]
    return _without_empty({
        "model": value.get("model"),
        "required_for_new_plan": value.get("required_for_new_plan"),
        "windows": windows,
        "cross_window_steps": [
            item for item in value.get("cross_window_steps") or ()
            if item in step_ids
        ],
        "roots_by_step": {
            key: copy.deepcopy(item)
            for key, item in (value.get("roots_by_step") or {}).items()
            if key in step_ids
        },
        "unowned_action_ids": copy.deepcopy(
            value.get("unowned_action_ids") or []
        ),
        "window_causality": causality,
        "ownership_candidates": ownership_candidates,
    })


def _project_design_semantics(value, steps, *, requested_step_id):
    value = dict(value or {})
    step_texts = {
        str(item.get("text") or "")
        for item in steps
        if item.get("text")
    }
    candidates = []
    for item in value.get("reuse_candidates") or ():
        if not isinstance(item, dict):
            continue
        if (
            requested_step_id
            and item.get("kind") == "step_definition"
            and not step_texts.intersection(
                str(text) for text in item.get("matched_step_texts") or ()
            )
        ):
            continue
        candidates.append(_project_design_reuse_candidate(item))
    return _without_empty({
        "available": value.get("available"),
        "reuse_candidates": candidates,
        "locator_reuse_summary": {
            "verified_count": _locator_reuse_match_count(value, steps),
        },
        "environment_dependencies": copy.deepcopy(
            value.get("environment_dependencies") or []
        ),
        "reuse_index": _copy_fields(
            value.get("reuse_index") or {},
            ("available", "index_fingerprint", "stats", "warnings"),
        ),
    })


def _project_expanded_design_semantics(value, steps):
    value = dict(value or {})
    step_ids = {
        str(item.get("id") or "")
        for item in steps
        if item.get("id")
    }
    step_texts = {
        str(item.get("text") or "")
        for item in steps
        if item.get("text")
    }
    packs = [
        copy.deepcopy(item)
        for item in value.get("packs") or ()
        if isinstance(item, dict)
        and str(item.get("step_id") or "") in step_ids
    ]
    candidates = [
        copy.deepcopy(item)
        for item in value.get("reuse_candidates") or ()
        if isinstance(item, dict)
        and (
            item.get("kind") != "step_definition"
            or step_texts.intersection(
                str(text) for text in item.get("matched_step_texts") or ()
            )
        )
    ]
    continuity = [
        copy.deepcopy(item)
        for item in value.get("step_continuity") or ()
        if isinstance(item, dict)
        and (
            str(item.get("from_step_id") or "") in step_ids
            or str(item.get("to_step_id") or "") in step_ids
        )
    ]
    return _without_empty({
        "available": value.get("available"),
        "packs": packs,
        "window_causality": copy.deepcopy(
            value.get("window_causality") or []
        ),
        "step_continuity": continuity,
        "reuse_candidates": candidates,
        "locator_reuse_matches": _project_locator_reuse_matches(
            value,
            steps,
        ),
        "environment_dependencies": copy.deepcopy(
            value.get("environment_dependencies") or []
        ),
        "reuse_index": copy.deepcopy(value.get("reuse_index") or {}),
    })


def _project_design_reuse_candidate(value):
    value = dict(value or {})
    result = _copy_fields(
        value,
        (
            "candidate_id",
            "kind",
            "path",
            "symbol",
            "signature",
            "key",
            "file_sha256",
            "step_patterns",
            "step_pattern_contracts",
            "matched_step_texts",
            "operations",
            "call_sequence",
            "step_parameters",
            "step_parameter_contracts",
            "references",
            "table_usage_hint",
            "semantic_contract",
        ),
    )
    if "exact_step_pattern" in (value.get("reasons") or []):
        result["exact_step_pattern"] = True
    return result


def _project_locator_reuse_matches(value, steps):
    step_ids = {
        str(item.get("id") or "")
        for item in steps or ()
        if isinstance(item, dict) and item.get("id")
    }
    result = []
    for item in (dict(value or {}).get("locator_reuse_matches") or ()):
        if not isinstance(item, dict):
            continue
        if step_ids and str(item.get("step_id") or "") not in step_ids:
            continue
        result.append(_without_empty(_copy_fields(
            item,
            (
                "match_id",
                "status",
                "step_id",
                "action_id",
                "root_name",
                "evidence_name",
                "owner_candidate_id",
                "locator_file",
                "locator_key",
                "target_fingerprint",
                "snapshot_proof",
            ),
        )))
    return result


def _locator_reuse_match_count(value, steps):
    step_ids = {
        str(item.get("id") or "")
        for item in steps or ()
        if isinstance(item, dict) and item.get("id")
    }
    return sum(
        isinstance(item, dict)
        and str(item.get("step_id") or "") in step_ids
        for item in (dict(value or {}).get("locator_reuse_matches") or ())
    )


def _project_design_memory_digest(value, step_ids, *, requested_step_id):
    value = dict(value or {})
    items = []
    for item in value.get("items") or ():
        if not isinstance(item, dict):
            continue
        scope = dict(item.get("scope") or {})
        scope_step_ids = {
            str(step_id) for step_id in scope.get("step_ids") or ()
        }
        if requested_step_id and scope_step_ids and not (
                scope_step_ids & step_ids
        ):
            continue
        items.append(_without_empty({
            "memory_id": item.get("memory_id"),
            "kind": item.get("kind"),
            "authority": item.get("authority"),
            "signal": item.get("signal"),
            "reuse": item.get("reuse"),
            "scope": _copy_fields(scope, ("feature_id", "scenario_id", "step_ids")),
        }))
    return _without_empty({
        "digest_fingerprint": value.get("digest_fingerprint"),
        "relevant_count": value.get("relevant_count"),
        "selected_count": value.get("selected_count"),
        "truncated_count": value.get("truncated_count"),
        "items": items,
    })


def _project_expanded_design_memory_digest(value, step_ids):
    value = dict(value or {})
    items = []
    for item in value.get("items") or ():
        if not isinstance(item, dict):
            continue
        scope = item.get("scope") or {}
        scoped_step_ids = {
            str(step_id) for step_id in scope.get("step_ids") or ()
        }
        if scoped_step_ids and not (scoped_step_ids & step_ids):
            continue
        items.append(copy.deepcopy(item))
    return _without_empty({
        key: copy.deepcopy(value.get(key))
        for key in (
            "memory_digest_version",
            "revision",
            "journal_revision",
            "relevant_count",
            "candidate_count",
            "selected_count",
            "truncated_count",
            "bucket_counts",
            "digest_fingerprint",
        )
        if value.get(key) not in (None, "", [], {})
    } | {"items": items})


def _copy_fields(value, fields):
    value = dict(value or {})
    return {
        field: copy.deepcopy(value.get(field))
        for field in fields
        if value.get(field) not in (None, "", [], {})
    }


def _design_context_fingerprint(value):
    return _fingerprint({
        key: item
        for key, item in dict(value or {}).items()
        if key != "design_context_fingerprint"
    })


def ai_context_envelope_identity_is_valid(value):
    if not isinstance(value, dict):
        return False
    required = set(
        compact_ai_context_envelope_contract()["required_top_level"]
    )
    if set(value) != required:
        return False
    if value.get("ai_context_envelope_version") not in (
            READABLE_AI_CONTEXT_ENVELOPE_VERSIONS
    ):
        return False
    if not isinstance(value.get("workflow_version"), str) or not value.get(
            "workflow_version"
    ):
        return False
    if not isinstance(value.get("allowed_queries"), list) or any(
            not isinstance(item, str) or not item
            for item in value.get("allowed_queries") or ()
    ):
        return False
    for key, fields in (
        ("job", ("job_id", "job_fingerprint")),
        ("request", ("request_id", "request_fingerprint", "revision_seal")),
        ("generation_profile", ("profile_id", "profile_fingerprint")),
    ):
        item = value.get(key)
        if not isinstance(item, dict) or any(
                not isinstance(item.get(field), str) or not item.get(field)
                for field in fields
        ):
            return False
    job_value = value.get("job") or {}
    workload = job_value.get("workload")
    if not isinstance(workload, dict) or any((
            not isinstance(workload.get("action_count"), int),
            not isinstance(workload.get("step_count"), int),
            not isinstance(
                workload.get("static_service_level_target_seconds"),
                int,
            ),
    )):
        return False
    if not isinstance(value.get("brief"), dict):
        return False
    if not _brief_transport_is_valid(value):
        return False
    if not isinstance(value.get("workflow"), dict):
        return False
    if not isinstance(value.get("decision"), dict):
        return False
    if not isinstance(value.get("ai_capabilities"), dict):
        return False
    bundle = value.get("generation_task_bundle")
    uses_bundle = "job-task-bundle" in set(value.get("allowed_queries") or ())
    if not isinstance(bundle, dict):
        return False
    if uses_bundle and any((
        bundle.get("generation_task_bundle_version") != "1.4",
        not bundle.get("bundle_id"),
        not bundle.get("bundle_fingerprint"),
        not isinstance(bundle.get("fragments"), list),
    )):
        return False
    if not uses_bundle and bundle:
        return False
    if value.get("plan_context") is not None and not ai_plan_context_identity_is_valid(
            value.get("plan_context")
    ):
        return False
    if value.get("design_contract") != compact_generation_design_contract():
        return False
    execution = value.get("job_execution")
    if not isinstance(execution, dict) or any((
            execution.get("phase") not in {
                "ready", "design", "implementation", "runtime", "oracle",
                "completed", "failed",
            },
            not isinstance(execution.get("epoch"), int),
            execution.get("epoch", 0) < 0,
            not isinstance(execution.get("attempt_no"), int),
            execution.get("attempt_no", 0) < 0,
    )):
        return False
    return value.get("envelope_fingerprint") == ai_context_envelope_fingerprint(
        value
    )


def _brief_transport_is_valid(value):
    brief = value.get("brief") or {}
    transport = brief.get("design_context_transport") or {}
    omitted = transport.get("omitted_sections") or []
    full_brief = transport.get("full_brief") or {}
    uses_bundle = "job-task-bundle" in set(value.get("allowed_queries") or ())
    common_invalid = any((
        not isinstance(transport, dict),
        transport.get("version") != GENERATION_DESIGN_CONTEXT_VERSION,
        not isinstance(omitted, list),
        set(omitted) != DESIGN_CONTEXT_OMITTED_SECTIONS,
        brief.get("generation_design_context_version")
        != GENERATION_DESIGN_CONTEXT_VERSION,
        brief.get("request_id") != (value.get("request") or {}).get(
            "request_id"
        ),
        not brief.get("brief_fingerprint"),
        brief.get("design_context_fingerprint")
        != _design_context_fingerprint(brief),
        full_brief.get("brief_fingerprint")
        != brief.get("brief_fingerprint"),
        not full_brief.get("path"),
        full_brief.get("expand") != "job-design-context",
    ))
    if common_invalid:
        return False
    if uses_bundle:
        return not any((
            transport.get("scope") != "bundle_index",
            (transport.get("actions") or {}).get("source")
            != "generation_task_bundle",
        ))
    return bool(
        transport.get("scope") == "system_baseline"
        and (transport.get("actions") or {}).get("source")
        == "system_baseline"
        and isinstance((brief.get("target") or {}).get("step_count"), int)
    )


def ai_context_envelope_fingerprint(value):
    normalized = {
        key: item
        for key, item in copy.deepcopy(dict(value or {})).items()
        if key != "envelope_fingerprint"
    }
    return _fingerprint(normalized)


def _fingerprint(value):
    return hashlib.sha256(json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _relative_path(session_dir, value):
    if not value:
        return None
    path = Path(value)
    path = path.resolve() if path.is_absolute() else (
        Path(session_dir).resolve() / path
    ).resolve()
    try:
        return path.relative_to(Path(session_dir).resolve()).as_posix()
    except ValueError:
        return None


def _without_empty(value):
    return {
        key: item
        for key, item in value.items()
        if item not in (None, "", [], {})
    }


def _compact_workflow_context(state):
    state = state if isinstance(state, dict) else {}
    decision = state.get("decision") or {}
    pack = decision.get("pack") or {}
    active = state.get("active_transaction") or {}
    result = state.get("last_result") or {}
    return _without_empty({
        "workflow_context_version": "1.0",
        "request_id": state.get("request_id"),
        "status": state.get("status"),
        "next_action": state.get("next_action"),
        "risk": state.get("risk") or {},
        "adjustment": state.get("adjustment") or {},
        "decision": _without_empty({
            "status": decision.get("status"),
            "question_count": pack.get("question_count"),
            "blocking_count": pack.get("blocking_count"),
            "forensic_blocking_count": pack.get(
                "forensic_blocking_count"
            ),
            "resolved_ambiguity_ids": decision.get(
                "resolved_ambiguity_ids"
            ) or [],
        }),
        "ambiguity": state.get("ambiguity") or {},
        "required_forensic_evidence": state.get(
            "required_forensic_evidence"
        ) or [],
        "active_transaction": _without_empty({
            "transaction_id": active.get("transaction_id"),
            "report_path": active.get("report_path") or active.get("path"),
        }),
        "last_result": _without_empty({
            "transaction_id": result.get("transaction_id"),
            "report_path": result.get("report_path"),
            "status": result.get("status"),
        }),
        "errors": state.get("errors") or [],
        "warnings": state.get("warnings") or [],
    })
