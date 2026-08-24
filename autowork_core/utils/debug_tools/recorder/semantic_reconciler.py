from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime

from autowork_core.utils.debug_tools.recorder.annotations import (
    annotation_snapshot_is_valid,
    build_annotation_snapshot,
)
from autowork_core.utils.debug_tools.recorder.code_reuse_index import (
    match_window_owner_candidates,
)
from autowork_core.utils.debug_tools.recorder.evidence_graph import (
    EVENT_ARTIFACT_KINDS,
)
from autowork_core.utils.debug_tools.recorder.models import SCHEMA_VERSION
from autowork_core.utils.debug_tools.recorder.reconciliation_repository import (
    BRIEF_VERSION,
    ReconciliationRepository,
    review_source_id,
)


RECONCILER_VERSION = "4.0"
RISK_MODES = {"fast", "clarify", "forensic", "blocked"}

_HARD_CONFLICT_CODES = {
    "drag_parameters_unavailable",
    "external_process_action",
    "no_recorded_actions",
    "orphan_mouse_boundary",
    "unsupported_drag",
    "unsupported_middle_click",
}
_TARGET_CONFLICT_CODES = {
    "fallback_ocr",
    "fallback_pos",
    "weak_target_quality",
}
_BUSINESS_CHOICE_CODES = {
    "pause_state_changed",
    "provisional_window",
    "window_closed_during_take",
}
def build_generation_brief(
    session_dir,
    request,
    *,
    write=True,
):
    repository = ReconciliationRepository(session_dir)
    inputs = repository.load_inputs(request)
    reconciliation, brief = reconcile_generation(
        request,
        inputs["context"],
        inputs["action_metadata"],
        inputs["memory"],
        semantics=inputs.get("semantics"),
        created_at=datetime.now().isoformat(timespec="seconds"),
    )
    if write:
        persisted = repository.write(
            request["request_id"],
            reconciliation,
            brief,
        )
        brief = dict(persisted["brief"])
        brief.update({
            "brief_path": persisted["brief_path"],
            "reconciliation_path": persisted["reconciliation_path"],
        })
    return brief


def reconcile_generation(
    request,
    context,
    action_metadata,
    memory,
    *,
    semantics=None,
    created_at,
):
    semantics = semantics or {
        "available": False,
        "actions": {},
        "window_causality": [],
        "step_continuity": [],
        "reuse_candidates": [],
    }
    items = {
        str(item.get("evidence_id")): item
        for item in context.get("items") or []
        if item.get("evidence_id")
    }
    evidence = _reconcile_evidence(
        request,
        context,
        items,
        action_metadata,
    )
    reviews = _reconcile_reviews(request, evidence)
    ambiguities = _build_ambiguities(
        request,
        evidence,
        reviews,
        semantic_actions=semantics.get("actions") or {},
    )
    adjustment = _adjustment_plan(
        request,
        evidence,
        reviews,
        ambiguities=ambiguities,
    )
    risk = _route_risk(
        request,
        evidence,
        reviews,
        ambiguities,
    )
    agent_tasks = _build_agent_tasks(request, evidence, ambiguities)
    revision = _revision_seal(request, context, reviews, memory)
    window_ownership = _window_ownership_candidates(
        evidence["actions"],
        semantics,
        request_evidence=request.get("evidence") or [],
    )
    annotation_snapshot = _request_annotation_snapshot(request)
    reconciliation = {
        "schema_version": SCHEMA_VERSION,
        "reconciler_version": RECONCILER_VERSION,
        "request_id": request.get("request_id"),
        "created_at": created_at,
        "risk": risk,
        "revision": revision,
        "annotation_snapshot": annotation_snapshot,
        "target": _target_summary(request),
        "story": evidence["story"],
        "actions": evidence["actions"],
        "reviews": reviews,
        "ambiguities": ambiguities,
        "conflicts": evidence["conflicts"],
        "required_forensic_evidence": evidence[
            "required_forensic_evidence"
        ],
        "adjustment": adjustment,
        "agent_tasks": agent_tasks,
        "memory": memory,
        "semantics": semantics,
        "window_ownership": window_ownership,
        "coverage": evidence["coverage"],
        "generation": {
            "allowed_write_roots": [
                "Bdd/steps",
                "Bdd/page_obj",
                "Bdd/locators",
                "Bdd/data",
            ],
            "forbidden": [
                "PIC without a passed transaction-bound authorization",
                "direct PIC API calls",
                "new set_root calls",
                "new inline locator dictionaries",
                "writes outside Bdd generation roots",
            ],
            "locator_priority": [
                "child",
                "xpath",
                "ocr",
                "pos",
                "authorized_pic_fallback",
            ],
            "pic_policy": "default_deny_action_scoped_authorization",
            "validation": "automatic_on_finish",
        },
    }
    reconciliation["reconciliation_fingerprint"] = _stable_hash({
        key: reconciliation[key]
        for key in (
            "reconciler_version",
            "risk",
            "revision",
            "annotation_snapshot",
            "target",
            "story",
            "actions",
            "reviews",
            "ambiguities",
            "conflicts",
            "required_forensic_evidence",
            "adjustment",
            "agent_tasks",
            "memory",
            "semantics",
            "window_ownership",
            "coverage",
            "generation",
        )
    })
    brief = _compact_brief(reconciliation)
    brief["brief_fingerprint"] = _stable_hash({
        key: brief.get(key)
        for key in (
            "brief_version",
            "reconciliation_fingerprint",
            "risk",
            "revision",
            "annotation_snapshot",
            "target",
            "story",
            "actions",
            "ambiguities",
            "conflicts",
            "required_forensic_evidence",
            "adjustment",
            "agent_tasks",
            "coverage",
            "memory_digest",
            "semantics",
            "window_ownership",
            "generation",
            "scenario_intelligence",
        )
    })
    return reconciliation, brief


def brief_matches_request(brief, request):
    revision = brief.get("revision") or {}
    contract_matches = (
        True
        if (request.get("identity_basis") or {}).get(
            "request_identity_profile"
        ) == "business-v1"
        else all((
            revision.get("contract_hash")
            == (request.get("framework_contract") or {}).get(
                "contract_hash"
            ),
            revision.get("api_signature_hash")
            == (request.get("framework_contract") or {}).get(
                "api_signature_hash"
            ),
        ))
    )
    return bool(
        brief.get("request_id") == request.get("request_id")
        and revision.get("evidence_fingerprint")
        == request.get("evidence_fingerprint")
        and contract_matches
        and revision.get("takes") == _take_revisions(request)
        and _brief_annotation_matches_request(brief, request)
        and _brief_scope_binding_matches_request(brief, request)
    )


def _request_annotation_snapshot(request):
    snapshot = request.get("annotation_snapshot")
    if snapshot is None:
        return build_annotation_snapshot(
            (request.get("target") or {}).get("steps") or []
        )
    if not annotation_snapshot_is_valid(snapshot):
        raise ValueError("Request Annotation snapshot无效")
    return snapshot


def _brief_annotation_matches_request(brief, request):
    if (
            "annotation_snapshot" not in brief
            and "annotation_snapshot" not in request
    ):
        return True
    return brief.get("annotation_snapshot") == _request_annotation_snapshot(
        request
    )


def _brief_scope_binding_matches_request(brief, request):
    request_scenario = (request.get("target") or {}).get("scenario") or {}
    brief_scenario = (brief.get("target") or {}).get("scenario") or {}
    request_binding = request_scenario.get("step_scope_binding") or {}
    brief_binding = brief_scenario.get("step_scope_binding") or {}
    if not request_binding and not brief_binding:
        return True
    return brief_binding == _compact_step_scope_binding(request_binding)


def _reconcile_evidence(
    request,
    context,
    items,
    action_metadata,
):
    actions = []
    conflicts = []
    required_forensic = set()
    classified = set()

    step_order = {
        str(step.get("id")): index
        for index, step in enumerate(
            (request.get("target") or {}).get("steps") or []
        )
        if step.get("id")
    }
    action_items = sorted(
        (
            item
            for item in items.values()
            if item.get("kind") == "action"
        ),
        key=lambda item: (
            step_order.get(
                str(item.get("step_id") or ""),
                len(step_order),
            ),
            (item.get("payload") or {}).get("ordinal") or 0,
            item.get("evidence_id") or "",
        ),
    )
    for item in action_items:
        payload = item.get("payload") or {}
        action_id = str(payload.get("action_id") or "")
        step_id = str(item.get("step_id") or "")
        metadata = (
            action_metadata.get(_action_scope_key(step_id, action_id))
            or action_metadata.get(action_id)
            or {}
        )
        evidence_ids = [item["evidence_id"]]
        evidence_identity = str(item["evidence_id"])[len("action:"):]
        target_id = f"target:{evidence_identity}"
        text_id = f"text-change:{evidence_identity}"
        media_id = f"media:{evidence_identity}"
        target = (items.get(target_id) or {}).get("payload") or {}
        text_change = (items.get(text_id) or {}).get("payload")
        media = (items.get(media_id) or {}).get("payload") or {}
        for evidence_id in (target_id, text_id, media_id):
            if evidence_id in items:
                evidence_ids.append(evidence_id)
        classified.update(evidence_ids)

        closure = payload.get("closure") or {}
        locator = target.get("locator") or {}
        locator_kind = str(locator.get("by") or "child")
        conflict_codes = []
        if closure.get("status") != "complete":
            conflict_codes.append("partial_action_envelope")
        same_observed_target = target.get("same_observed_target")
        if same_observed_target is None:
            same_observed_target = target.get("same_runtime_target")
        if same_observed_target is False:
            conflict_codes.append("target_identity_changed")
        if target.get("locator_validation") != "unique_target_match":
            conflict_codes.append("locator_not_uniquely_validated")
        if locator_kind in {"ocr", "pos"}:
            conflict_codes.append(f"fallback_{locator_kind}")
        if (
            target.get("locator_strategy") == "positional_fallback"
            or target.get("positional_fallback") is True
        ):
            conflict_codes.append("positional_locator_unstable")
        if (
            text_change
            and text_change.get("status") == "keys_only"
        ):
            conflict_codes.append("text_value_unobserved")
        if (
            media.get("stability", {}).get("status")
            == "visual_still_changing"
        ):
            conflict_codes.append("visual_state_unsettled")
        parameters = dict(payload.get("parameters") or {})
        if payload.get("type") == "scroll" and any((
            parameters.get("direction") not in {
                "up", "down", "left", "right",
            },
            isinstance(parameters.get("steps"), bool),
            not isinstance(parameters.get("steps"), int),
            (parameters.get("steps") or 0) <= 0,
        )):
            conflict_codes.append("scroll_parameters_unavailable")
        if payload.get("type") == "drag":
            delta_x = parameters.get("delta_x")
            delta_y = parameters.get("delta_y")
            if (
                any(
                    isinstance(value, bool) or not isinstance(value, int)
                    for value in (delta_x, delta_y)
                )
                or (delta_x == 0 and delta_y == 0)
            ):
                conflict_codes.append("drag_parameters_unavailable")

        action = {
            "action_id": action_id,
            "step_id": step_id,
            "ordinal": payload.get("ordinal"),
            "type": payload.get("type"),
            "role": payload.get("role") or "business",
            "event_ids": list(payload.get("event_ids") or []),
            "media_event_ids": list(
                payload.get("media_event_ids") or []
            ),
            "value_binding": (
                metadata.get("value_binding")
                if "value_binding" in metadata
                else payload.get("value_binding")
            ),
            "note": (
                metadata.get("note")
                if "note" in metadata
                else payload.get("note")
            ),
            "parameters": parameters,
            "canonical_action": dict(
                payload.get("canonical_action") or {}
            ),
            "target": _compact_target(target),
            "text_change": _compact_text_change(text_change),
            "media": _compact_media(media),
            "evidence_ids": evidence_ids,
            "conflicts": conflict_codes,
        }
        if metadata.get("correction"):
            action["correction"] = metadata["correction"]
        actions.append(action)
        for code in conflict_codes:
            conflicts.append({
                "code": code,
                "step_id": step_id,
                "action_id": action_id,
                "evidence_ids": evidence_ids,
            })
        if conflict_codes:
            required_forensic.update(
                _forensic_evidence_ids(context, payload, conflict_codes)
            )

    state_ids = [
        item["evidence_id"]
        for item in items.values()
        if item.get("kind") == "state_delta"
    ]
    classified.update(state_ids)
    required = set(context.get("required_consumption_evidence_ids") or [])
    unclassified = sorted(required - classified)
    if unclassified:
        conflicts.append({
            "code": "unclassified_default_evidence",
            "evidence_ids": unclassified,
        })

    return {
        "story": _story(actions),
        "actions": actions,
        "conflicts": conflicts,
        "required_forensic_evidence": sorted(required_forensic),
        "coverage": {
            "required": len(required),
            "classified": len(required & classified),
            "unclassified": unclassified,
            "ratio": (
                round(len(required & classified) / len(required), 4)
                if required
                else 1.0
            ),
            "minimum_decision_evidence_ids": context.get(
                "minimum_decision_evidence_ids"
            ) or [],
            "classified_evidence_ids": sorted(classified),
        },
    }


def _reconcile_reviews(
    request,
    evidence,
):
    active_event_ids = {
        event_id
        for action in evidence["actions"]
        for event_id in _action_event_ids(action, request)
    }
    result = []
    for review in (
        (request.get("readiness") or {}).get("target_review_required")
        or []
    ):
        code = str(review.get("code") or "")
        recovery = review.get("recovery") or {}
        review_event_ids = _review_event_ids(review)
        if (
            code == "provisional_window"
            and review_event_ids
            and not (review_event_ids & active_event_ids)
        ):
            disposition = "auto_resolved_excluded_timeline"
        elif (
            code == "window_closed_during_take"
            and not recovery.get("hard_blocker")
            and _closed_window_review_is_inactive_secondary(
                review,
                review_event_ids,
                active_event_ids,
            )
        ):
            disposition = "auto_resolved_excluded_timeline"
        elif recovery.get("hard_blocker"):
            disposition = "blocked"
        elif code in _TARGET_CONFLICT_CODES:
            disposition = "forensic"
        elif code in _BUSINESS_CHOICE_CODES:
            disposition = "clarify"
        elif code in _HARD_CONFLICT_CODES:
            disposition = "forensic"
        else:
            disposition = "clarify"
        result.append({
            "source_review_id": review_source_id(review),
            "code": code,
            "step_id": review.get("step_id"),
            "disposition": disposition,
            "message": review.get("message"),
            "event_ids": sorted(review_event_ids),
            "hard_blocker": bool(recovery.get("hard_blocker")),
        })
    return result


def _build_ambiguities(
        request,
        evidence,
        reviews,
        *,
        semantic_actions=None,
    ):
    semantic_actions = semantic_actions or {}
    actions = evidence.get("actions") or []
    raw_reviews = (
        (request.get("readiness") or {}).get("target_review_required")
        or []
    )
    review_states = {
        str(item.get("source_review_id") or ""): item
        for item in reviews or ()
        if item.get("source_review_id")
    }
    result = []
    for review in raw_reviews:
        source_id = review_source_id(review)
        state = review_states.get(source_id) or {}
        if state.get("disposition") == "auto_resolved_excluded_timeline":
            continue
        code = str(review.get("code") or "unknown")
        if code in _TARGET_CONFLICT_CODES:
            continue
        event_ids = _source_review_event_ids(review)
        matched_actions = _actions_matching_events(
            actions,
            event_ids,
            step_id=review.get("step_id"),
        )
        if code == "unsupported_scroll":
            matched_actions = [
                action
                for action in actions
                if str(action.get("step_id") or "")
                == str(review.get("step_id") or "")
                and action.get("type") == "scroll"
                and (action.get("role") or "business") != "noise"
            ]
            event_ids = sorted({
                str(event_id)
                for action in matched_actions
                for event_id in (
                    list(action.get("event_ids") or ())
                    + list(action.get("media_event_ids") or ())
                )
                if event_id
            })
        hard_blocker = bool(
            (review.get("recovery") or {}).get("hard_blocker")
        )
        allowed_outcomes = _review_ambiguity_outcomes(
            code,
            hard_blocker=hard_blocker,
            evidence=review.get("evidence"),
        )
        result.append(_ambiguity_record(
            source={
                "kind": "review",
                "source_review_id": source_id,
            },
            code=code,
            step_id=review.get("step_id"),
            action_ids=[
                item.get("action_id") for item in matched_actions
            ],
            event_ids=event_ids,
            evidence_ids=[
                evidence_id
                for action in matched_actions
                for evidence_id in action.get("evidence_ids") or ()
            ],
            facts={
                "message": review.get("message"),
                "evidence": review.get("evidence"),
                "hard_blocker": hard_blocker,
                "recovery_status": (
                    (review.get("recovery") or {}).get("status")
                ),
            },
            allowed_outcomes=allowed_outcomes,
        ))
    result.extend(_semantic_assertion_ambiguities(
        actions,
        semantic_actions,
    ))
    result.extend(_declared_binding_ambiguities(actions))
    result.extend(_specification_conflict_ambiguities(request))
    result.extend(_step_context_conflict_ambiguities(request))
    user_managed_action_ids = {
        str(action_id)
        for item in result
        if any(
            outcome.get("authority") == "user"
            for outcome in item.get("allowed_outcomes") or ()
        )
        for action_id in item.get("action_ids") or ()
    }
    conflicts_by_action = {}
    unscoped_conflicts = []
    for conflict in evidence.get("conflicts") or ():
        key = (
            str(conflict.get("step_id") or ""),
            str(conflict.get("action_id") or ""),
        )
        if all(key):
            conflicts_by_action.setdefault(key, []).append(conflict)
        else:
            unscoped_conflicts.append(conflict)
    for (step_id, action_id), conflicts in conflicts_by_action.items():
        action = next(
            (
                item
                for item in actions
                if str(item.get("step_id") or "") == step_id
                and str(item.get("action_id") or "") == action_id
            ),
            None,
        )
        codes = sorted({
            str(item.get("code") or "unknown")
            for item in conflicts
        })
        action_semantics = semantic_actions.get(
            _action_scope_key(step_id, action_id)
        ) or {}
        declared_binding = _declared_input_binding(
            action_semantics,
            action,
        )
        outcomes = _conflict_ambiguity_outcomes(
            codes,
            declared_binding=declared_binding,
            action=action,
        )
        if action_id in user_managed_action_ids and outcomes:
            continue
        conflict_evidence = sorted({
            str(evidence_id)
            for item in conflicts
            for evidence_id in item.get("evidence_ids") or ()
            if evidence_id
        })
        source_review_ids = sorted({
            review_source_id(review)
            for review in raw_reviews
            if str(review.get("step_id") or "") == step_id
            and str(review.get("code") or "") in codes
        })
        result.append(_ambiguity_record(
            source={
                "kind": "action_conflicts",
                "conflict_id": _stable_hash({
                    "codes": codes,
                    "step_id": step_id,
                    "action_id": action_id,
                    "evidence_ids": conflict_evidence,
                }),
                "source_review_ids": source_review_ids,
            },
            code="action_implementation",
            step_id=step_id,
            action_ids=[action_id],
            event_ids=(
                list(action.get("event_ids") or ())
                if action is not None
                else []
            ),
            evidence_ids=conflict_evidence,
            facts={
                "conflicts": codes,
                "action_type": (action or {}).get("type"),
                "target": (action or {}).get("target") or {},
                "parameters": (action or {}).get("parameters") or {},
                "declared_input_binding": declared_binding,
            },
            allowed_outcomes=outcomes,
        ))
    for conflict in unscoped_conflicts:
        code = str(conflict.get("code") or "unknown")
        result.append(_ambiguity_record(
            source={
                "kind": "conflict",
                "conflict_id": _stable_hash(conflict),
            },
            code=code,
            step_id=None,
            action_ids=[],
            event_ids=[],
            evidence_ids=conflict.get("evidence_ids") or [],
            facts={},
            allowed_outcomes=[],
        ))
    unique = {
        item["ambiguity_id"]: item
        for item in result
    }
    return [unique[key] for key in sorted(unique)]


def _semantic_assertion_ambiguities(actions, semantic_actions):
    result = []
    for action in actions:
        step_id = str(action.get("step_id") or "")
        action_id = str(action.get("action_id") or "")
        semantic = semantic_actions.get(
            _action_scope_key(step_id, action_id)
        ) or {}
        candidates = semantic.get("assertion_candidates") or []
        unresolved = {
            str(item.get("code") or ""): item
            for item in semantic.get("unresolved_decisions") or ()
            if (
                str(item.get("code") or "").startswith("assertion_")
                or str(item.get("code") or "") in {
                    "observation_intent_missing",
                    "collection_assertion_unsupported",
                    "region_text_assertion_unsupported",
                }
            )
        }
        codes = sorted(unresolved)
        if not codes and not (
            semantic.get("assertion_requires_decision") and candidates
        ):
            continue
        code = codes[0] if codes else "assertion_implementation"
        target = action.get("target") or {}
        executable_target = bool(
            target.get("locator_name")
            and target.get("locator_validation")
            == "unique_target_match"
        )
        outcomes = []
        if code == "observation_intent_missing":
            outcomes.append({
                "outcome": "capture_observation_intent",
                "authority": "evidence",
                "effect": "evidence_required",
            })
        elif code == "assertion_business_expectation_required":
            outcomes.extend([{
                "outcome": "confirm_observed_result_as_expected",
                "authority": "user",
                "effect": "scenario_authority",
            }, {
                "outcome": "reject_observed_result_as_expected",
                "authority": "user",
                "effect": "evidence_required",
            }])
        elif code in {
            "collection_assertion_unsupported",
            "region_text_assertion_unsupported",
        }:
            outcomes = []
        elif code == "assertion_value_unobserved" and executable_target:
            outcomes.append({
                "outcome": "implement_declared_expectation",
                "authority": "ai",
                "effect": "plan_coverage",
            })
        elif code == "assertion_implementation" and candidates:
            outcomes.append({
                "outcome": "select_assertion_implementation",
                "authority": "ai",
                "effect": "plan_coverage",
            })
        if code not in {
            "assertion_implementation",
            "assertion_business_expectation_required",
            "observation_intent_missing",
            "collection_assertion_unsupported",
            "region_text_assertion_unsupported",
        }:
            outcomes.append({
                "outcome": "capture_assertion_evidence",
                "authority": "evidence",
                "effect": "evidence_required",
            })
        unresolved_item = unresolved.get(code) or {}
        result.append(_ambiguity_record(
            source={
                "kind": "semantic_assertion",
                "semantic_code": code,
            },
            code=code,
            step_id=step_id,
            action_ids=[action_id],
            event_ids=action.get("event_ids") or [],
            evidence_ids=(
                action.get("evidence_ids")
                or unresolved_item.get("evidence_ids")
                or []
            ),
            facts={
                "declared_expectations": unresolved_item.get(
                    "declared_expectations"
                ),
                "expected": unresolved_item.get("expected"),
                "observed": unresolved_item.get("observed"),
                "observed_candidates": unresolved_item.get(
                    "observed_candidates"
                ),
                "assertion_candidates": candidates,
                "semantic_facts": semantic.get("facts") or {},
                "target": target,
            },
            allowed_outcomes=outcomes,
        ))
    return result


def _declared_binding_ambiguities(actions):
    result = []
    for action in actions:
        if not _input_source_requires_confirmation(action):
            continue
        step_id = str(action.get("step_id") or "")
        action_id = str(action.get("action_id") or "")
        binding = str(action.get("value_binding") or "").strip()
        result.append(_ambiguity_record(
            source={
                "kind": "declared_input_binding",
                "binding_fingerprint": _stable_hash({
                    "step_id": step_id,
                    "action_id": action_id,
                    "binding": binding,
                }),
            },
            code="input_binding_invalid",
            step_id=step_id,
            action_ids=[action_id],
            event_ids=action.get("event_ids") or [],
            evidence_ids=action.get("evidence_ids") or [],
            facts={
                "declared_binding": binding,
                "authority": "user_confirmed",
            },
            allowed_outcomes=[{
                "outcome": "repair_declared_binding",
                "authority": "evidence",
                "effect": "evidence_required",
            }],
        ))
    return result


def _specification_conflict_ambiguities(request):
    target = request.get("target") or {}
    feature = target.get("feature") or {}
    scenario = target.get("scenario") or {}
    specification = scenario.get("specification") or {}
    rule = specification.get("rule") or {}
    rule_claims = [
        str(rule.get("name") or ""),
        *[str(item) for item in rule.get("description") or ()],
    ]
    feature_claims = [
        str(item) for item in feature.get("description") or ()
    ]
    conflict = None
    for feature_claim in feature_claims:
        for rule_claim in rule_claims:
            conflict = _explicit_claim_conflict(feature_claim, rule_claim)
            if conflict is not None:
                break
        if conflict is not None:
            break
    if conflict is None:
        return []

    target_steps = target.get("steps") or []
    step = next(
        (
            item
            for item in reversed(target_steps)
            if str(item.get("semantic_type") or "").casefold() == "then"
        ),
        target_steps[-1] if target_steps else {},
    )
    step_id = str(step.get("id") or "") or None
    facts = {
        **conflict,
        "message": (
            "Feature requirement and current Rule declare opposite business "
            "outcomes for the same subject and threshold."
        ),
        "feature_reference": (
            f"feature:{feature.get('id')}:description"
            if feature.get("id")
            else "feature:description"
        ),
        "rule_reference": (
            f"scenario:{scenario.get('id')}:rule"
            if scenario.get("id")
            else "scenario:rule"
        ),
    }
    return [_ambiguity_record(
        source={
            "kind": "specification_conflict",
            "conflict_id": _stable_hash(facts),
        },
        code="specification_business_conflict",
        step_id=step_id,
        action_ids=[],
        event_ids=[],
        evidence_ids=[],
        facts=facts,
        allowed_outcomes=[{
            "outcome": "follow_feature_requirement",
            "authority": "user",
            "effect": "scenario_authority",
        }, {
            "outcome": "follow_rule_requirement",
            "authority": "user",
            "effect": "scenario_authority",
        }],
    )]


def _step_context_conflict_ambiguities(request):
    target = request.get("target") or {}
    feature = target.get("feature") or {}
    scenario = target.get("scenario") or {}
    specification = scenario.get("specification") or {}
    rule = specification.get("rule") or {}
    specification_claims = [
        *[
            (str(item), f"feature:{feature.get('id')}:description")
            for item in feature.get("description") or ()
        ],
        *[
            (str(item), f"scenario:{scenario.get('id')}:rule")
            for item in [
                rule.get("name"),
                *(rule.get("description") or ()),
            ]
            if item
        ],
    ]
    result = []
    for step in target.get("steps") or ():
        context = step.get("step_user_context") or {}
        if not context.get("active"):
            continue
        if context.get("annotation_version") == "2.0":
            context_claims = [
                str(context.get("business_context") or ""),
            ]
        else:
            context_claims = [
                str(context.get("purpose") or ""),
                str(context.get("constraints") or ""),
            ]
        conflict = None
        feature_reference = None
        context_claim = None
        for candidate in context_claims:
            if not candidate:
                continue
            for specification_claim, reference in specification_claims:
                conflict = _explicit_claim_conflict(
                    specification_claim,
                    candidate,
                )
                if conflict is not None:
                    feature_reference = reference
                    context_claim = candidate
                    break
            if conflict is not None:
                break
        if conflict is None:
            continue
        facts = {
            **conflict,
            "message": (
                "Feature specification and the Step business context declare "
                "opposite outcomes for the same subject and threshold."
            ),
            "feature_reference": feature_reference,
            "step_context_reference": context.get("annotation_id"),
            "step_context_revision": context.get("revision"),
            "step_context_claim": context_claim,
        }
        result.append(_ambiguity_record(
            source={
                "kind": "step_user_context_conflict",
                "annotation_id": context.get("annotation_id"),
            },
            code="step_context_business_conflict",
            step_id=step.get("id"),
            action_ids=[],
            event_ids=[],
            evidence_ids=[],
            facts=facts,
            allowed_outcomes=[{
                "outcome": "follow_feature_requirement",
                "authority": "user",
                "effect": "scenario_authority",
            }, {
                "outcome": "follow_step_user_context",
                "authority": "user",
                "effect": "scenario_authority",
            }],
        ))
    return result


def _explicit_claim_conflict(feature_claim, rule_claim):
    feature = _claim_signature(feature_claim)
    rule = _claim_signature(rule_claim)
    if (
        feature["polarity"] == 0
        or rule["polarity"] == 0
        or feature["polarity"] == rule["polarity"]
    ):
        return None
    shared_topics = feature["topics"] & rule["topics"]
    shared_conditions = feature["conditions"] & rule["conditions"]
    if len(shared_topics) < 2 or not shared_conditions:
        return None
    return {
        "feature_claim": str(feature_claim).strip(),
        "rule_claim": str(rule_claim).strip(),
        "shared_topics": sorted(shared_topics),
        "shared_thresholds": sorted({
            threshold for threshold, _relation in shared_conditions
        }),
        "shared_conditions": [
            {"threshold": threshold, "relation": relation}
            for threshold, relation in sorted(shared_conditions)
        ],
        "feature_outcome": (
            "available" if feature["polarity"] > 0 else "unavailable"
        ),
        "rule_outcome": (
            "available" if rule["polarity"] > 0 else "unavailable"
        ),
    }


def _claim_signature(value):
    text = " ".join(str(value or "").casefold().split())
    unavailable = bool(re.search(
        r"\b(?:unavailable|cannot\s+be\s+used|can\s+not\s+be\s+used|"
        r"not\s+available|disabled)\b|不可用|不能使用|不显示",
        text,
    ))
    available = bool(re.search(
        r"\b(?:available|can\s+be\s+used|enabled)\b|可用|可以使用",
        text,
    ))
    polarity = -1 if unavailable else 1 if available else 0
    thresholds = {
        re.sub(r"\s+", "", match)
        .replace("milliseconds", "ms")
        .replace("millisecond", "ms")
        .replace("毫秒", "ms")
        for match in re.findall(
            r"\d+(?:\.\d+)?\s*(?:%|ms|milliseconds?|毫秒)",
            text,
        )
    }
    if re.search(
        r"\b(?:below|less\s+than)\b|小于|低于",
        text,
    ):
        relation = "below"
    elif re.search(
        r"\b(?:at\s+least|greater\s+than\s+or\s+equal\s+to|"
        r"not\s+less\s+than)\b|大于等于|不低于|至少",
        text,
    ):
        relation = "at_least"
    else:
        relation = "unspecified"
    stopwords = {
        "the", "a", "an", "is", "are", "when", "for", "or", "and",
        "to", "be", "used", "use", "function", "option", "with", "at",
        "least", "than", "equal", "greater", "cannot", "can", "not",
        "available", "unavailable", "enabled", "disabled", "milliseconds",
        "ms",
    }
    topics = {
        token
        for token in re.findall(r"[a-z][a-z0-9_]+", text)
        if token not in stopwords and not token.isdigit()
    }
    return {
        "polarity": polarity,
        "thresholds": thresholds,
        "conditions": {
            (threshold, relation) for threshold in thresholds
        },
        "topics": topics,
    }


def _ambiguity_record(
        *,
        source,
        code,
        step_id,
        action_ids,
        event_ids,
        evidence_ids,
        facts,
        allowed_outcomes,
    ):
    action_ids = sorted({str(item) for item in action_ids if item})
    event_ids = sorted({str(item) for item in event_ids if item})
    evidence_ids = sorted({str(item) for item in evidence_ids if item})
    identity = {
        "source": source,
        "code": code,
        "step_id": step_id,
        "action_ids": action_ids,
        "event_ids": event_ids,
        "evidence_ids": evidence_ids,
    }
    authorities = {
        str(item.get("authority") or "")
        for item in allowed_outcomes
        if item.get("authority")
    }
    if not allowed_outcomes or authorities == {"evidence"}:
        routing = "evidence_required"
    elif authorities == {"user"}:
        routing = "user_decision_required"
    elif authorities == {"ai"}:
        routing = "ai_plan_required"
    else:
        routing = "mixed"
    return {
        "ambiguity_id": "ambiguity-" + _stable_hash(identity)[:20],
        "code": code,
        "routing": routing,
        "step_id": step_id,
        "action_ids": action_ids,
        "event_ids": event_ids,
        "evidence_ids": evidence_ids,
        "facts": _without_empty(facts),
        "allowed_outcomes": [dict(item) for item in allowed_outcomes],
        "source": dict(source),
    }


def _review_ambiguity_outcomes(code, *, hard_blocker, evidence=None):
    if hard_blocker:
        return []
    if code == "provisional_window":
        return [{
            "outcome": "belongs_to_business_flow",
            "authority": "user",
            "effect": "scenario_authority",
        }, {
            "outcome": "unrelated_window",
            "authority": "user",
            "effect": "ignored_action",
        }]
    if code == "window_closed_during_take":
        return [{
            "outcome": "expected_close",
            "authority": "user",
            "effect": "scenario_authority",
        }, {
            "outcome": "workflow_transition",
            "authority": "user",
            "effect": "scenario_authority",
        }, {
            "outcome": "unexpected_close",
            "authority": "user",
            "effect": "evidence_required",
        }]
    if code == "pause_state_changed":
        return [{
            "outcome": "unrelated_to_step",
            "authority": "user",
            "effect": "acknowledge",
        }, {
            "outcome": "step_precondition",
            "authority": "user",
            "effect": "acknowledge",
        }, {
            "outcome": "belongs_to_step",
            "authority": "user",
            "effect": "evidence_required",
        }]
    if code == "unsupported_scroll":
        return [{
            "outcome": "belongs_to_step",
            "authority": "user",
            "effect": "plan_coverage",
        }, {
            "outcome": "ignore_as_noise",
            "authority": "user",
            "effect": "ignored_action",
        }]
    return []


def _conflict_ambiguity_outcomes(
    codes,
    *,
    declared_binding=None,
    action=None,
):
    codes = set(codes or ())
    if codes & {
        "drag_parameters_unavailable",
        "scroll_parameters_unavailable",
        "unclassified_default_evidence",
    }:
        return []
    outcomes = [{
        "outcome": "reuse_existing_behavior",
        "authority": "ai",
        "effect": "behavior_coverage",
    }]
    if "positional_locator_unstable" in codes:
        return outcomes
    if "text_value_unobserved" in codes and declared_binding:
        outcomes.append({
            "outcome": "implement_with_declared_binding",
            "authority": "ai",
            "effect": "plan_coverage",
        })
    if "visual_state_unsettled" in codes:
        outcomes.append({
            "outcome": "implement_recorded_action",
            "authority": "ai",
            "effect": "plan_coverage",
        })
    if codes & {"fallback_ocr", "fallback_pos"}:
        outcomes.append({
            "outcome": "implement_with_frozen_evidence",
            "authority": "ai",
            "effect": "plan_coverage",
        })
    target = (action or {}).get("target") or {}
    if (
        "target_identity_changed" in codes
        and target.get("locator_validation") == "unique_target_match"
        and target.get("locator_name")
        and target.get("target_fingerprint")
    ):
        outcomes.append({
            "outcome": "implement_with_frozen_evidence",
            "authority": "ai",
            "effect": "plan_coverage",
        })
    return outcomes


def _declared_input_binding(action_semantics, action=None):
    source = str(action_semantics.get("resolved_binding") or "")
    if not source.startswith("examples."):
        return None
    command = (
        ((action or {}).get("canonical_action") or {}).get("command")
        or {}
    )
    facts = action_semantics.get("facts") or {}
    replacement = facts.get("replacement_text_candidate") or {}
    value = command.get("text")
    if value in (None, ""):
        value = replacement.get("value")
    if value in (None, ""):
        return None
    candidate = next(
        (
            item
            for item in action_semantics.get("binding_candidates") or ()
            if str(item.get("source") or "") == source
            and item.get("value") == value
            and float(item.get("confidence") or 0.0) >= 0.9
        ),
        None,
    )
    if candidate is None:
        return None
    return {
        "source": source,
        "value": value,
        "candidate_confidence": candidate.get("confidence"),
        "derivation": {
            "operation": (
                command.get("text_operation")
                or replacement.get("operation")
            ),
            "basis": (
                (command.get("text_derivation") or {}).get("basis")
                or replacement.get("basis")
            ),
            "confidence": (
                (command.get("text_derivation") or {}).get("confidence")
                or replacement.get("confidence")
            ),
        },
        "authorities": {
            "source": "feature_declared",
            "key_sequence": "runtime_observed",
            "binding": "ai_hypothesis",
        },
    }


def _source_review_event_ids(review):
    values = set()
    for value in review.get("event_ids") or ():
        if value:
            values.add(str(value))
    evidence = review.get("evidence")
    if isinstance(evidence, str) and evidence.startswith("event-"):
        values.add(evidence)
    elif isinstance(evidence, dict):
        event_id = evidence.get("event_id")
        if event_id:
            values.add(str(event_id))
        values.update(
            str(item)
            for item in evidence.get("event_ids") or ()
            if item
        )
    return sorted(values)


def _actions_matching_events(actions, event_ids, *, step_id):
    expected = set(event_ids or ())
    if not expected:
        return []
    return [
        action
        for action in actions or ()
        if str(action.get("step_id") or "") == str(step_id or "")
        and expected & (
            set(action.get("event_ids") or ())
            | set(action.get("media_event_ids") or ())
        )
    ]


def _closed_window_review_is_inactive_secondary(
        review,
        review_event_ids,
        active_event_ids,
):
    evidence = review.get("evidence")
    if not isinstance(evidence, dict):
        return False
    if not isinstance(evidence.get("event_ids"), list):
        return False
    if evidence.get("primary") is not False:
        return False
    admission = str(evidence.get("admission") or "").casefold()
    if admission not in {"automatic", "provisional"}:
        return False
    return not bool(review_event_ids & active_event_ids)


def _route_risk(
    request,
    evidence,
    reviews,
    ambiguities,
):
    reasons = []
    mode = "fast"
    evidence_required = [
        item for item in ambiguities
        if item.get("routing") == "evidence_required"
    ]
    user_required = [
        item for item in ambiguities
        if item.get("routing") == "user_decision_required"
    ]
    ai_required = [
        item for item in ambiguities
        if item.get("routing") in {"ai_plan_required", "mixed"}
    ]
    if evidence_required:
        mode = "blocked"
        reasons.extend(item["code"] for item in evidence_required)
    elif evidence["coverage"]["ratio"] != 1.0:
        mode = "blocked"
        reasons.append("reconciler_coverage_incomplete")
    elif user_required:
        mode = "clarify"
        reasons.extend(item["code"] for item in user_required)
    elif ai_required:
        mode = "forensic"
        reasons.extend(item["code"] for item in ai_required)
    elif any(
        not conflict.get("resolved")
        for conflict in evidence["conflicts"]
    ) or any(
        review["disposition"] == "forensic" for review in reviews
    ):
        mode = "forensic"
        reasons.extend(
            sorted({
                conflict["code"]
                for conflict in evidence["conflicts"]
                if not conflict.get("resolved")
            })
        )
        reasons.extend(
            sorted({
                review["code"]
                for review in reviews
                if review["disposition"] == "forensic"
            })
        )
    elif any(review["disposition"] == "clarify" for review in reviews):
        mode = "clarify"
        reasons.extend(
            sorted({
                review["code"]
                for review in reviews
                if review["disposition"] == "clarify"
            })
        )
    if mode not in RISK_MODES:
        raise AssertionError(f"无效 risk mode: {mode}")
    return {
        "mode": mode,
        "reasons": list(dict.fromkeys(reasons)),
        "fail_closed": mode == "blocked",
    }


def _adjustment_plan(
    request,
    evidence,
    reviews,
    *,
    ambiguities=None,
):
    suggestions = []
    for review in reviews:
        if review["disposition"] == "auto_resolved_excluded_timeline":
            suggestions.append({
                "kind": "exclude_window",
                "code": review["code"],
                "blocking": False,
                "reason": "Only excluded timeline events reference the window.",
            })
    for action in evidence["actions"]:
        unresolved_conflicts = [
            conflict
            for conflict in action["conflicts"]
            if not _conflict_resolved_for_action(
                evidence["conflicts"],
                action.get("step_id"),
                action["action_id"],
                conflict,
            )
        ]
        if unresolved_conflicts:
            suggestions.append({
                "kind": "inspect_action_conflict",
                "step_id": action.get("step_id"),
                "action_id": action["action_id"],
                "blocking": True,
                "conflicts": unresolved_conflicts,
            })
        if (
            action.get("type") == "keyboard"
            and action.get("text_change")
            and _input_source_requires_confirmation(action)
        ):
            suggestions.append({
                "kind": "confirm_input_source",
                "step_id": action.get("step_id"),
                "action_id": action["action_id"],
                "blocking": True,
                "observed_value": action["text_change"].get("after_value"),
            })
    blocking = [item for item in suggestions if item.get("blocking")]
    blocking = [item for item in suggestions if item.get("blocking")]
    return {
        "status": (
            "required"
            if blocking
            else "suggested"
            if suggestions
            else "not_needed"
        ),
        "blocking_count": len(blocking),
        "suggestions": suggestions,
        "interaction": _adjustment_interaction(
            ambiguities or [],
            has_blocking=bool(blocking),
        ),
    }


def _adjustment_interaction(ambiguities, *, has_blocking):
    if not has_blocking:
        return "none"
    routing = {str(item.get("routing") or "") for item in ambiguities or ()}
    if "evidence_required" in routing:
        return "repair_evidence"
    if "user_decision_required" in routing:
        return "structured_decision_batch"
    if "ai_plan_required" in routing:
        return "ai_plan"
    mixed_authorities = {
        str(outcome.get("authority") or "")
        for item in ambiguities or ()
        if item.get("routing") == "mixed"
        for outcome in item.get("allowed_outcomes") or ()
    }
    if "ai" in mixed_authorities:
        return "ai_plan"
    if "user" in mixed_authorities:
        return "structured_decision_batch"
    if "evidence" in mixed_authorities:
        return "repair_evidence"
    return "ai_plan"


def _build_agent_tasks(request, evidence, ambiguities):
    actions_by_step = {}
    for action in evidence.get("actions") or ():
        if (action.get("role") or "business") == "noise":
            continue
        actions_by_step.setdefault(
            str(action.get("step_id") or ""),
            [],
        ).append(action)
    ambiguities_by_step = {}
    for ambiguity in ambiguities or ():
        ambiguities_by_step.setdefault(
            str(ambiguity.get("step_id") or ""),
            [],
        ).append(ambiguity)
    steps = []
    for step in (request.get("target") or {}).get("steps") or ():
        step_id = str(step.get("id") or "")
        actions = actions_by_step.get(step_id, [])
        step_ambiguities = ambiguities_by_step.get(step_id, [])
        steps.append({
            "step_id": step_id,
            "action_ids": [
                str(action.get("action_id"))
                for action in actions
                if action.get("action_id")
            ],
            "action_types": list(dict.fromkeys(
                str(action.get("type") or "unknown")
                for action in actions
            )),
            "ambiguity_ids": [
                str(item.get("ambiguity_id"))
                for item in step_ambiguities
                if item.get("ambiguity_id")
            ],
        })
    return {
        "version": "1.0",
        "responsibility": "ai_owns_complete_implementation_reasoning",
        "steps": steps,
        "investigation_tools": ["evidence", "compare-takes"],
    }


def _revision_seal(request, context, reviews, memory):
    value = {
        "request_id": request.get("request_id"),
        "evidence_fingerprint": request.get("evidence_fingerprint"),
        "context_fingerprint": context.get("context_fingerprint"),
        "review_digest": _stable_hash(reviews),
        "memory_revision": memory.get("revision"),
        "takes": _take_revisions(request),
        "policy_version": RECONCILER_VERSION,
    }
    if (
        (request.get("identity_basis") or {}).get(
            "request_identity_profile"
        )
        != "business-v1"
    ):
        value.update({
            "contract_hash": (
                request.get("framework_contract") or {}
            ).get("contract_hash"),
            "api_signature_hash": (
                request.get("framework_contract") or {}
            ).get("api_signature_hash"),
        })
    value["seal"] = _stable_hash(value)
    return value


def _input_source_requires_confirmation(action):
    binding = str(action.get("value_binding") or "").strip()
    if not binding:
        return False
    return not binding.startswith((
        "examples.",
        "data.",
        "context.",
        "table.",
    ))


def _conflict_resolved_for_action(
        conflicts,
    step_id,
        action_id,
        code,
):
    return any(
    str(conflict.get("step_id") or "") == str(step_id or "")
    and conflict.get("action_id") == action_id
        and conflict.get("code") == code
        and conflict.get("resolved")
        for conflict in conflicts
    )


def _target_summary(request):
    target = request.get("target") or {}
    feature = target.get("feature") or {}
    scenario = target.get("scenario") or {}
    return {
        "feature": {
            "id": feature.get("id"),
            "name": feature.get("name"),
            "description": _compact_lines(feature.get("description")),
            "line": feature.get("line"),
            "tags": list(feature.get("tags") or ()),
            "source_relpath": feature.get("source_relpath"),
        },
        "scenario": {
            "id": scenario.get("id"),
            "name": scenario.get("name"),
            "kind": scenario.get("kind"),
            "logical_template_id": scenario.get("logical_template_id"),
            "example_id": scenario.get("example_id"),
            "example_values": scenario.get("example_values") or {},
            "tags": list(scenario.get("tags") or ()),
            "generation_scope": scenario.get("generation_scope") or {},
            "step_scope_binding": _compact_step_scope_binding(
                scenario.get("step_scope_binding")
            ),
            "specification": _compact_specification(
                scenario.get("specification")
            ),
        },
        "steps": [
            {
                "id": step.get("id"),
                "keyword": step.get("keyword"),
                "semantic_type": step.get("semantic_type"),
                "text": step.get("text"),
                "table": _compact_table(step.get("table")),
                **(
                    {
                        "step_user_context": _compact_step_user_context(
                            step.get("step_user_context")
                        )
                    }
                    if step.get("step_user_context") is not None
                    else {}
                ),
                **(
                    {
                        "step_user_context_revision": step.get(
                            "step_user_context_revision"
                        )
                    }
                    if step.get("step_user_context_revision") is not None
                    else {}
                ),
                "observation_intents": [
                    _compact_observation_intent(item)
                    for item in step.get("observation_intents") or ()
                    if isinstance(item, dict)
                ],
            }
            for step in target.get("steps") or []
        ],
    }


def _compact_step_scope_binding(value):
    value = value if isinstance(value, dict) else {}
    scope = value.get("resolved_step_scope") or {}
    if not scope:
        return {}
    return {
        "scope_binding_version": value.get("scope_binding_version"),
        "binding_fingerprint": value.get("binding_fingerprint"),
        "resolved_step_scope": {
            "files": list(scope.get("files") or ()),
            "entry_file": scope.get("entry_file"),
            "origin": scope.get("origin"),
            "file_statuses": dict(scope.get("file_statuses") or {}),
            "step_behavior_files": dict(
                scope.get("step_behavior_files") or {}
            ),
        },
    }


def _compact_step_user_context(value):
    if not isinstance(value, dict):
        return None
    result = {
        "annotation_id": value.get("annotation_id"),
        "annotation_version": value.get("annotation_version"),
        "step_id": value.get("step_id"),
        "authority": value.get("authority"),
        "revision": value.get("revision"),
        "active": bool(value.get("active")),
    }
    if value.get("annotation_version") == "2.0":
        result["business_context"] = str(
            value.get("business_context") or ""
        )[:1200]
    else:
        result["purpose"] = str(value.get("purpose") or "")[:600]
        result["constraints"] = str(
            value.get("constraints") or ""
        )[:600]
    return result


def _compact_observation_intent(value):
    return {
        "annotation_id": value.get("annotation_id"),
        "authority": value.get("authority"),
        "revision": value.get("revision"),
        "step_id": value.get("step_id"),
        "take_id": value.get("take_id"),
        "event_id": value.get("event_id"),
        "action_id": value.get("action_id"),
        "focus": value.get("focus"),
        "relation": value.get("relation"),
        "expected_source": value.get("expected_source") or {},
        "property_name": value.get("property_name"),
        "business_meaning": str(value.get("business_meaning") or "")[:1000],
    }


def _compact_specification(value, *, max_examples=24):
    value = value if isinstance(value, dict) else {}
    rule = value.get("rule") if isinstance(value.get("rule"), dict) else None
    backgrounds = []
    for background in value.get("backgrounds") or ():
        if not isinstance(background, dict):
            continue
        backgrounds.append({
            "name": str(background.get("name") or ""),
            "description": _compact_lines(background.get("description")),
            "line": background.get("line"),
            "steps": [
                _compact_specification_step(step)
                for step in (background.get("steps") or ())[:12]
                if isinstance(step, dict)
            ],
        })
    template = (
        value.get("template")
        if isinstance(value.get("template"), dict)
        else {}
    )
    examples = []
    remaining = max_examples
    for example_group in template.get("examples") or ():
        if not isinstance(example_group, dict):
            continue
        raw_rows = list(example_group.get("rows") or ())
        rows = [
            {
                str(key): str(item)[:120]
                for key, item in row.items()
            }
            for row in raw_rows[:remaining]
            if isinstance(row, dict)
        ]
        remaining = max(0, remaining - len(rows))
        examples.append({
            "name": str(example_group.get("name") or ""),
            "line": example_group.get("line"),
            "tags": list(example_group.get("tags") or ()),
            "headings": [
                str(item) for item in example_group.get("headings") or ()
            ],
            "rows": rows,
            "row_count": len(raw_rows),
            "truncated": len(rows) < len(raw_rows),
        })
        if remaining <= 0:
            break
    return {
        "rule": (
            {
                "name": str(rule.get("name") or ""),
                "description": _compact_lines(rule.get("description")),
                "line": rule.get("line"),
                "tags": list(rule.get("tags") or ()),
            }
            if rule is not None
            else None
        ),
        "backgrounds": backgrounds,
        "template": {
            "name": str(template.get("name") or ""),
            "description": _compact_lines(template.get("description")),
            "line": template.get("line"),
            "kind": str(template.get("kind") or ""),
            "steps": [
                _compact_specification_step(step)
                for step in (template.get("steps") or ())[:24]
                if isinstance(step, dict)
            ],
            "examples": examples,
        },
    }


def _compact_specification_step(step):
    return {
        "keyword": str(step.get("keyword") or ""),
        "text": str(step.get("text") or "")[:240],
        "line": step.get("line"),
        "text_block": (
            str(step.get("text_block"))[:400]
            if step.get("text_block") is not None
            else None
        ),
        "table": _compact_table(step.get("table")),
    }


def _compact_lines(values, *, max_lines=12, max_length=240):
    return [
        str(value)[:max_length]
        for value in (values or ())[:max_lines]
        if str(value).strip()
    ]


def _compact_table(table, *, max_rows=8, max_cell_length=120):
    if not isinstance(table, dict) or not table:
        return None
    headings = [str(item) for item in table.get("headings") or ()]
    rows = [
        [str(cell)[:max_cell_length] for cell in row]
        for row in (table.get("rows") or ())[:max_rows]
    ]
    row_count = len(table.get("rows") or ())
    return {
        "headings": headings,
        "rows": rows,
        "row_count": row_count,
        "truncated": row_count > len(rows),
        "source": "request.target.steps[].table",
    }


def _compact_brief(reconciliation):
    risk = reconciliation["risk"]
    semantic_actions = (
        (reconciliation.get("semantics") or {}).get("actions") or {}
    )
    actions = []
    for action in reconciliation["actions"]:
        compact_action = _without_empty({
            "id": action.get("action_id"),
            "step_id": action.get("step_id"),
            "n": action.get("ordinal"),
            "type": action.get("type"),
            "role": action.get("role"),
            "target": _compact_brief_target(action.get("target") or {}),
            "canonical_action": _compact_canonical_action(
                action.get("canonical_action")
            ),
            "binding": action.get("value_binding"),
            "note": action.get("note"),
            "correction": action.get("correction"),
            "parameters": action.get("parameters") or {},
            "evidence": action.get("evidence_ids") or [],
        })
        action_semantics = _compact_action_semantics(
            semantic_actions.get(_action_scope_key(
                action.get("step_id"),
                action.get("action_id"),
            )) or {},
            action,
        )
        if action_semantics:
            compact_action["semantics"] = action_semantics
        actions.append(compact_action)
    brief = {
        "schema_version": SCHEMA_VERSION,
        "brief_version": BRIEF_VERSION,
        "reconciler_version": RECONCILER_VERSION,
        "request_id": reconciliation.get("request_id"),
        "created_at": reconciliation.get("created_at"),
        "reconciliation_fingerprint": reconciliation[
            "reconciliation_fingerprint"
        ],
        "risk": risk,
        "revision": reconciliation["revision"],
        "annotation_snapshot": reconciliation.get(
            "annotation_snapshot"
        ) or {},
        "target": reconciliation["target"],
        "actions": actions,
        "ambiguities": [
            _compact_ambiguity(item)
            for item in reconciliation.get("ambiguities") or []
            if isinstance(item, dict)
        ],
        "window_ownership": _compact_window_ownership(
            reconciliation["window_ownership"]
        ),
        "conflicts": [
            {
                "code": item.get("code"),
                "step_id": item.get("step_id"),
                "action_id": item.get("action_id"),
                "resolved": item.get("resolved", False),
                "resolution": item.get("resolution"),
            }
            for item in reconciliation["conflicts"]
        ],
        "required_forensic_evidence": reconciliation[
            "required_forensic_evidence"
        ],
        "adjustment": reconciliation["adjustment"],
        "agent_tasks": reconciliation.get("agent_tasks") or {},
        "coverage": {
            "ratio": reconciliation["coverage"]["ratio"],
            "required": reconciliation["coverage"]["required"],
            "classified": reconciliation["coverage"]["classified"],
        },
        "memory_digest": reconciliation.get("memory") or {},
        "semantics": {
            "available": (reconciliation.get("semantics") or {}).get(
                "available",
                False,
            ),
            "packs": [
                _compact_semantic_pack(item)
                for item in (
                    (reconciliation.get("semantics") or {}).get("packs")
                    or []
                )
            ],
            "window_causality": (
                (reconciliation.get("semantics") or {}).get(
                    "window_causality"
                ) or []
            )[:8],
            "step_continuity": (
                (reconciliation.get("semantics") or {}).get(
                    "step_continuity"
                ) or []
            )[:12],
            "reuse_candidates": [
                _compact_reuse_candidate(item)
                for item in (
                    (reconciliation.get("semantics") or {}).get(
                        "reuse_candidates"
                    ) or []
                )[:8]
            ],
            "environment_dependencies": [
                dict(item)
                for item in (
                    (reconciliation.get("semantics") or {}).get(
                        "environment_dependencies"
                    ) or ()
                )[:12]
                if isinstance(item, dict)
            ],
            "reuse_index": _compact_reuse_index(
                (reconciliation.get("semantics") or {}).get("reuse_index")
                or {}
            ),
        },
        "generation": reconciliation["generation"],
    }
    brief["scenario_intelligence"] = build_scenario_intelligence(brief)
    brief["target"] = _target_without_specification(brief["target"])
    return brief


def _target_without_specification(target):
    target = dict(target or {})
    scenario = dict(target.get("scenario") or {})
    scenario.pop("specification", None)
    target["scenario"] = scenario
    return target


def build_scenario_intelligence(value):
    value = value if isinstance(value, dict) else {}
    target = value.get("target") or {}
    feature = target.get("feature") or {}
    scenario = target.get("scenario") or {}
    specification = {
        "authority": "feature_declared",
        "source": {
            "path": feature.get("source_relpath"),
            "feature_id": feature.get("id"),
            "scenario_id": scenario.get("id"),
        },
        "feature": {
            "name": feature.get("name"),
            "description": feature.get("description") or [],
        },
        "scenario": {
            "name": scenario.get("name"),
            "example_values": scenario.get("example_values") or {},
            "specification": scenario.get("specification") or {},
        },
    }
    source_actions = [
        item
        for item in value.get("actions") or ()
        if isinstance(item, dict)
    ]
    selected_actions = _coverage_preserving_actions(
        source_actions,
        target.get("steps") or (),
        value.get("ambiguities") or (),
        value.get("conflicts") or (),
    )
    episodes = []
    for action in selected_actions:
        episodes.append(_without_empty({
            "action_id": action.get("id") or action.get("action_id"),
            "scoped_action_id": _scoped_action_id(
                action.get("step_id"),
                action.get("id") or action.get("action_id"),
            ),
            "step_id": action.get("step_id"),
            "ordinal": action.get("n") or action.get("ordinal"),
            "type": action.get("type"),
            "role": action.get("role"),
        }))
    references = []
    for candidate in (
        ((value.get("semantics") or {}).get("reuse_candidates") or ())[:8]
    ):
        if not isinstance(candidate, dict):
            continue
        references.append(_without_empty({
            "authority": "code_verified",
            "candidate_id": candidate.get("candidate_id"),
            "kind": candidate.get("kind"),
        }))
    source_gaps = _scenario_intelligence_gaps(value)
    gaps = _coverage_preserving_gaps(source_gaps)
    structured_observations = [
        _compact_structured_observation(observation)
        for pack in ((value.get("semantics") or {}).get("packs") or ())
        if isinstance(pack, dict)
        for observation in pack.get("structured_observations") or ()
        if isinstance(observation, dict)
    ]
    structured_observation_ids = _unique_nonempty_strings([
        event_id
        for pack in ((value.get("semantics") or {}).get("packs") or ())
        if isinstance(pack, dict)
        for event_id in (
            (pack.get("structured_observation_coverage") or {}).get(
                "source_event_ids"
            )
            or [
                observation.get("event_id")
                for observation in pack.get("structured_observations") or ()
                if isinstance(observation, dict)
            ]
        )
    ])
    selected_structured_observations = structured_observations[:12]
    selected_structured_ids = {
        str(item.get("event_id") or "")
        for item in selected_structured_observations
        if item.get("event_id")
    }
    omitted_structured_ids = [
        event_id
        for event_id in structured_observation_ids
        if event_id not in selected_structured_ids
    ]
    structured_source_truncated = any(
        bool(pack.get("structured_observations_truncated"))
        for pack in ((value.get("semantics") or {}).get("packs") or ())
        if isinstance(pack, dict)
    )
    return {
        "scenario_intelligence_version": "1.1",
        "specification": specification,
        "demonstration": {
            "authority": "runtime_observed",
            "episodes": episodes,
            "episode_details_source": "brief.actions",
            "structured_observations": selected_structured_observations,
            "structured_observations_truncated": (
                structured_source_truncated
                or bool(omitted_structured_ids)
            ),
            "structured_observation_coverage": {
                "total_count": len(structured_observation_ids),
                "included_event_ids": [
                    event_id
                    for event_id in structured_observation_ids
                    if event_id in selected_structured_ids
                ],
                "omitted_event_ids": omitted_structured_ids,
                "expand_from": "semantic_pack.structured_observations",
            },
            "truncated": len(source_actions) > len(episodes),
        },
        "references": references,
        "reference_details_source": "brief.semantics.reuse_candidates",
        "environment_dependencies": [
            {
                **dict(item),
                "authority": "code_verified",
                "generation_allowed": False,
            }
            for item in (
                ((value.get("semantics") or {}).get(
                    "environment_dependencies"
                ) or ())[:12]
            )
            if isinstance(item, dict)
        ],
        "gaps": gaps,
        "coverage_manifest": _scenario_intelligence_coverage(
            target.get("steps") or (),
            source_actions,
            selected_actions,
            source_gaps,
            gaps,
        ),
    }


def _coverage_preserving_actions(
        actions,
        target_steps,
        ambiguities,
        conflicts,
        *,
        limit=24,
    ):
    required_ids = {
        _scoped_action_id(ambiguity.get("step_id"), action_id)
        for ambiguity in ambiguities or ()
        if isinstance(ambiguity, dict)
        for action_id in ambiguity.get("action_ids") or ()
        if action_id
    }
    required_ids.update(
        _scoped_action_id(
            conflict.get("step_id"),
            conflict.get("action_id"),
        )
        for conflict in conflicts or ()
        if isinstance(conflict, dict)
        and not conflict.get("resolved")
        and conflict.get("action_id")
    )
    target_step_ids = [
        str(step.get("id"))
        for step in target_steps or ()
        if isinstance(step, dict) and step.get("id")
    ]
    for step_id in target_step_ids:
        representative = next((
            action
            for action in actions
            if str(action.get("step_id") or "") == step_id
        ), None)
        if representative is not None and representative.get("id"):
            required_ids.add(_scoped_action_id(
                step_id,
                representative["id"],
            ))
    selected_ids = set(required_ids)
    target_count = max(limit, len(required_ids))
    for action in actions:
        action_id = _scoped_action_id(
            action.get("step_id"),
            action.get("id") or action.get("action_id"),
        )
        if len(selected_ids) >= target_count:
            break
        if action_id:
            selected_ids.add(action_id)
    return [
        action
        for action in actions
        if _scoped_action_id(
            action.get("step_id"),
            action.get("id") or action.get("action_id"),
        )
        in selected_ids
    ]


def _scenario_intelligence_gaps(value):
    gaps = []
    covered_conflicts = set()
    for ambiguity in value.get("ambiguities") or ():
        if not isinstance(ambiguity, dict):
            continue
        action_ids = [
            str(item) for item in ambiguity.get("action_ids") or ()
        ]
        gaps.append(_without_empty({
            "source": "frozen_ambiguity",
            "gap_id": ambiguity.get("ambiguity_id"),
            "code": ambiguity.get("code"),
            "routing": ambiguity.get("routing"),
            "step_id": ambiguity.get("step_id"),
            "action_ids": action_ids,
            "scoped_action_ids": [
                _scoped_action_id(ambiguity.get("step_id"), action_id)
                for action_id in action_ids
            ],
            "evidence_ids": ambiguity.get("evidence_ids") or [],
        }))
        covered_conflicts.update(
            (
                str(ambiguity.get("code") or ""),
                str(ambiguity.get("step_id") or ""),
                action_id,
            )
            for action_id in action_ids or [""]
        )
    for conflict in value.get("conflicts") or ():
        if not isinstance(conflict, dict):
            continue
        identity = (
            str(conflict.get("code") or ""),
            str(conflict.get("step_id") or ""),
            str(conflict.get("action_id") or ""),
        )
        if identity in covered_conflicts:
            continue
        gaps.append(_without_empty({
            "source": "evidence_conflict",
            "gap_id": "conflict:" + ":".join(identity),
            "code": conflict.get("code"),
            "routing": (
                "resolved"
                if conflict.get("resolved")
                else "ai_plan_required"
            ),
            "step_id": conflict.get("step_id"),
            "action_id": conflict.get("action_id"),
        }))
    return gaps


def _coverage_preserving_gaps(gaps, *, limit=12):
    if len(gaps) <= limit:
        return list(gaps)
    selected_ids = set()
    represented_steps = set()
    for gap in gaps:
        step_id = str(gap.get("step_id") or "")
        gap_id = str(gap.get("gap_id") or "")
        if not gap_id or step_id in represented_steps:
            continue
        selected_ids.add(gap_id)
        represented_steps.add(step_id)
    target_count = max(limit, len(selected_ids))
    for gap in gaps:
        gap_id = str(gap.get("gap_id") or "")
        if len(selected_ids) >= target_count:
            break
        if gap_id:
            selected_ids.add(gap_id)
    return [
        gap
        for gap in gaps
        if str(gap.get("gap_id") or "") in selected_ids
    ]


def _scenario_intelligence_coverage(
        target_steps,
        actions,
        selected_actions,
        source_gaps,
        selected_gaps,
    ):
    target_ids = [
        str(step.get("id"))
        for step in target_steps or ()
        if isinstance(step, dict) and step.get("id")
    ]
    action_ids = [
        _scoped_action_id(
            action.get("step_id"),
            action.get("id") or action.get("action_id"),
        )
        for action in actions
    ]
    selected_ids = {
        _scoped_action_id(
            action.get("step_id"),
            action.get("id") or action.get("action_id"),
        )
        for action in selected_actions
    }
    runtime_step_ids = {
        str(action.get("step_id") or "")
        for action in actions
        if action.get("step_id")
    }
    represented_step_ids = {
        str(action.get("step_id") or "")
        for action in selected_actions
        if action.get("step_id")
    }
    source_gap_ids = [
        str(gap.get("gap_id") or "")
        for gap in source_gaps
        if gap.get("gap_id")
    ]
    selected_gap_ids = {
        str(gap.get("gap_id") or "")
        for gap in selected_gaps
        if gap.get("gap_id")
    }
    return {
        "steps": {
            "target_ids": target_ids,
            "represented_ids": [
                step_id
                for step_id in target_ids
                if step_id in represented_step_ids
            ],
            "without_runtime_actions": [
                step_id
                for step_id in target_ids
                if step_id not in runtime_step_ids
            ],
            "missing_ids": [
                step_id
                for step_id in target_ids
                if step_id in runtime_step_ids
                and step_id not in represented_step_ids
            ],
        },
        "actions": {
            "total_count": len(actions),
            "included_ids": [
                action_id
                for action_id in action_ids
                if action_id in selected_ids
            ],
            "omitted_ids": [
                action_id
                for action_id in action_ids
                if action_id and action_id not in selected_ids
            ],
            "expand_from": "brief.actions",
        },
        "gaps": {
            "total_count": len(source_gaps),
            "included_ids": [
                gap_id
                for gap_id in source_gap_ids
                if gap_id in selected_gap_ids
            ],
            "omitted_ids": [
                gap_id
                for gap_id in source_gap_ids
                if gap_id not in selected_gap_ids
            ],
            "expand_from": "brief.ambiguities + brief.conflicts",
        },
    }


def _scoped_action_id(step_id, action_id):
    step_id = str(step_id or "")
    action_id = str(action_id or "")
    if not step_id or not action_id:
        return ""
    return f"action:{step_id}:{action_id}"


def _window_ownership_candidates(
        actions,
        semantics,
        *,
        request_evidence=(),
):
    identities_by_event = _window_identities_by_event(request_evidence)
    windows = {}
    roots_by_step = {}
    unowned_action_ids = []
    for action in actions or ():
        action_id = str(action.get("action_id") or "")
        step_id = str(action.get("step_id") or "")
        target = action.get("target") or {}
        root_name = str(target.get("root_name") or "")
        if not root_name:
            if action_id:
                unowned_action_ids.append(action_id)
            continue
        window = windows.setdefault(root_name, {
            "root_name": root_name,
            "root_criteria": dict(
                ((semantics or {}).get("recorded_window_roots") or {}).get(
                    root_name
                ) or {}
            ),
            "step_ids": [],
            "action_ids": [],
            "action_types": [],
            "locator_names": [],
            "control_types": [],
            "window_identities": [],
        })
        for key, value in (
            ("step_ids", step_id),
            ("action_ids", action_id),
            ("action_types", str(action.get("type") or "")),
            ("locator_names", str(target.get("locator_name") or "")),
            ("control_types", str(target.get("control_type") or "")),
        ):
            if value and value not in window[key]:
                window[key].append(value)
        identities = {
            identity
            for event_id in (
                list(action.get("event_ids") or [])
                + list(action.get("media_event_ids") or [])
            )
            for identity in identities_by_event.get(
                (step_id, str(event_id)),
                (),
            )
        }
        for identity in sorted(identities):
            value = dict(identity)
            if value not in window["window_identities"]:
                window["window_identities"].append(value)
        if step_id:
            roots_by_step.setdefault(step_id, set()).add(root_name)
    for window in windows.values():
        identity_count = len(window["window_identities"])
        window["identity_status"] = (
            "resolved"
            if identity_count == 1
            else "ambiguous"
            if identity_count > 1
            else "unavailable"
        )
        window["owner_match"] = match_window_owner_candidates(
            (semantics or {}).get("window_asset_catalog") or {},
            window,
        )
    return {
        "model": "desktop_window_package_v1",
        "required_for_new_plan": bool(actions),
        "windows": [windows[name] for name in sorted(windows)],
        "cross_window_steps": sorted(
            step_id
            for step_id, root_names in roots_by_step.items()
            if len(root_names) > 1
        ),
        "unowned_action_ids": list(dict.fromkeys(unowned_action_ids)),
        "window_causality": list(
            (semantics or {}).get("window_causality") or []
        )[:12],
        "view_ownership": "ai_reasoning_required",
    }


def _window_identities_by_event(request_evidence):
    result = {}
    for evidence in request_evidence or ():
        step_id = str((evidence.get("step") or {}).get("id") or "")
        if not step_id:
            continue
        for item in evidence.get("window_evidence") or ():
            if item.get("comparable") is False:
                continue
            window = item.get("window") or {}
            identity = tuple(sorted({
                "title": str(window.get("title") or ""),
                "class_name": str(window.get("class_name") or ""),
            }.items()))
            if not any(value for _key, value in identity):
                continue
            for event_id in item.get("event_ids") or ():
                result.setdefault(
                    (step_id, str(event_id)),
                    set(),
                ).add(identity)
    return result


def _compact_brief_target(target):
    result = {
        key: target.get(key)
        for key in (
            "root_name",
            "control_type",
            "name",
            "auto_id",
            "locator_name",
            "locator",
            "locator_strategy",
            "locator_stability",
            "locator_validation",
            "locator_candidate_id",
            "target_fingerprint",
            "interaction_confidence",
            "positional_fallback",
        )
        if target.get(key) not in (None, "")
    }
    candidates = [
        {
            "candidate_id": item.get("candidate_id"),
            "by": (item.get("locator") or {}).get("by", "child"),
            "reason": item.get("reason"),
            "stability": (item.get("stability") or {}).get("status"),
            "locator": dict(item.get("locator") or {}),
            "validation": {
                "status": (item.get("validation") or {}).get("status"),
                "target_matches": (
                    item.get("validation") or {}
                ).get("target_matches"),
            },
        }
        for item in target.get("locator_candidates") or ()
        if isinstance(item, dict) and item.get("candidate_id")
    ]
    if candidates:
        result["locator_candidates"] = candidates[:4]
    return result


def _compact_target(target):
    element = target.get("element") or {}
    locator = target.get("locator") or {}
    result = {
        "control_type": target.get("control_type"),
        "name": element.get("name"),
        "auto_id": element.get("auto_id"),
        "root_name": target.get("root_name"),
        "locator_name": target.get("locator_name"),
        "locator_strategy": target.get("locator_strategy"),
        "locator_stability": _without_empty({
            "status": (target.get("locator_stability") or {}).get("status"),
        }),
        "locator_validation": target.get("locator_validation"),
        "target_fingerprint": target.get("target_fingerprint"),
        "interaction_confidence": target.get("interaction_confidence"),
    }
    if (
        str(locator.get("by") or "").casefold() == "xpath"
        and re.search(r"\[\s*-?\d+\s*\]\s*$", str(locator.get("value") or ""))
    ):
        result["positional_fallback"] = True
    return result


def _compact_action_semantics(value, action):
    value = dict(value or {})
    result = {}
    runtime_value_sources = (
        (value.get("facts") or {}).get("runtime_value_sources") or {}
    )
    if runtime_value_sources:
        result["runtime_value_sources"] = _without_empty({
            "text": bool(runtime_value_sources.get("text")),
            "attributes": sorted(
                str(item)
                for item in runtime_value_sources.get("attributes") or ()
                if item
            ),
        })
    effect = dict(value.get("effect") or {})
    if any((
        effect.get("result") not in (None, "no_semantic_change_observed"),
        effect.get("changes"),
        effect.get("after_state"),
        effect.get("windows_opened"),
        effect.get("windows_closed"),
        effect.get("visual_stability") == "visual_still_changing",
    )):
        result["effect"] = _without_empty({
            "information_class": "frozen_observation_facts",
            "result": effect.get("result"),
            "changes": effect.get("changes") or [],
            "after_state": effect.get("after_state") or {},
            "windows_opened": effect.get("windows_opened") or [],
            "windows_closed": effect.get("windows_closed") or [],
            "visual_stability": effect.get("visual_stability"),
        })
    if not _has_assertion_ambiguity(value):
        constraints = [
            _without_empty({
                "information_class": "evidence_bound_implementation_constraint",
                "constraint": candidate.get("implementation_constraint"),
                "operation": candidate.get("operation"),
                "target": candidate.get("target"),
                "parameters": candidate.get("parameters"),
                "evidence_ids": candidate.get("evidence_ids") or [],
            })
            for candidate in value.get("assertion_candidates") or ()
            if isinstance(candidate, dict)
            and candidate.get("implementation_constraint")
        ]
        if constraints:
            result["implementation_constraints"] = sorted(
                constraints,
                key=_implementation_constraint_key,
            )
    if value.get("locator_fallback"):
        result["locator_fallback"] = value["locator_fallback"]
    return result


def _has_assertion_ambiguity(value):
    if value.get("assertion_requires_decision"):
        return True
    return any(
        str(item.get("code") or "").startswith("assertion_")
        or str(item.get("code") or "") in {
            "observation_intent_missing",
            "collection_assertion_unsupported",
            "region_text_assertion_unsupported",
        }
        for item in value.get("unresolved_decisions") or ()
        if isinstance(item, dict)
    )


def _implementation_constraint_key(candidate):
    return (
        str(candidate.get("constraint") or ""),
        str(candidate.get("operation") or ""),
        str(candidate.get("target") or ""),
        json.dumps(
            candidate.get("parameters") or {},
            ensure_ascii=False,
            sort_keys=True,
        ),
    )


def _compact_ambiguity(value):
    value = dict(value or {})
    code = str(value.get("code") or "")
    facts = dict(value.get("facts") or {})
    target = facts.get("target") or {}
    if code == "assertion_implementation":
        candidates = [
            _without_empty({
                "information_class": (
                    "evidence_bound_implementation_constraint"
                ),
                "operation": candidate.get("operation"),
                "target": candidate.get("target"),
                "parameters": candidate.get("parameters"),
            })
            for candidate in facts.get("assertion_candidates") or ()
            if isinstance(candidate, dict)
        ]
        facts = {
            "assertion_candidates": sorted(
                candidates,
                key=lambda candidate: (
                    str(candidate.get("operation") or ""),
                    str(candidate.get("target") or ""),
                    json.dumps(
                        candidate.get("parameters") or {},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ),
            ),
            "candidate_order": "deterministic_no_preference",
            "target": _without_empty({
                "locator_name": target.get("locator_name"),
                "control_type": target.get("control_type"),
            }),
        }
    elif code == "action_implementation":
        facts = {
            "action_type": facts.get("action_type"),
            "parameters": facts.get("parameters") or {},
            "conflicts": facts.get("conflicts") or [],
            "target": _without_empty({
                "locator_name": target.get("locator_name"),
                "control_type": target.get("control_type"),
            }),
        }
    elif code == "assertion_value_unobserved":
        facts = {
            "declared_expectations": facts.get(
                "declared_expectations"
            ) or [],
            "target": _without_empty({
                "locator_name": target.get("locator_name"),
            }),
        }
    result = {
        key: value.get(key)
        for key in (
            "ambiguity_id",
            "code",
            "routing",
            "step_id",
            "action_ids",
            "event_ids",
            "evidence_ids",
            "allowed_outcomes",
            "source",
        )
    }
    result["facts"] = _without_empty(facts)
    compact = _without_empty(result)
    if "allowed_outcomes" in value:
        compact["allowed_outcomes"] = list(
            value.get("allowed_outcomes") or ()
        )
    return compact


def _compact_semantic_pack(value):
    value = dict(value or {})
    source_structured = [
        item
        for item in value.get("structured_observations") or ()
        if isinstance(item, dict)
    ]
    structured = [
        _compact_structured_observation(item)
        for item in source_structured[:8]
    ]
    source_event_ids = _unique_nonempty_strings([
        item.get("event_id") for item in source_structured
    ])
    included_event_ids = _unique_nonempty_strings([
        item.get("event_id") for item in structured
    ])
    included_set = set(included_event_ids)
    return _without_empty({
        "step_id": value.get("step_id"),
        "take_id": value.get("take_id"),
        "semantic_fingerprint": value.get("semantic_fingerprint"),
        "pic_template_audit": (
            value.get("pic_template_audit")
            if ((value.get("pic_template_audit") or {}).get("summary") or {}).get(
                "candidate_count"
            )
            else None
        ),
        "unresolved_decisions": value.get("unresolved_decisions") or [],
        "structured_observations": structured,
        "structured_observations_truncated": (
            len(source_structured) > len(structured)
        ),
        "structured_observation_coverage": {
            "source_event_ids": source_event_ids,
            "included_event_ids": included_event_ids,
            "omitted_event_ids": [
                event_id
                for event_id in source_event_ids
                if event_id not in included_set
            ],
            "expand_from": "semantic_pack.structured_observations",
        },
    })


def _compact_structured_observation(item):
    raw_names = list(item.get("item_names") or ())[:200]
    raw_values = list(item.get("item_values") or ())[:200]
    raw_confidences = list(item.get("item_confidences") or ())[:200]
    raw_rectangles = list(item.get("item_rectangles") or ())[:200]
    names = [str(value)[:120] for value in raw_names]
    values = [
        str(value)[:120] if value is not None else None
        for value in raw_values
    ]
    values_truncated = any(
        len(str(value)) > 120
        for value in [*raw_names, *raw_values]
        if value is not None
    )
    return {
        "event_id": item.get("event_id"),
        "provider": item.get("provider"),
        "provider_version": item.get("provider_version"),
        "status": item.get("status"),
        "item_names": names,
        "item_values": values,
        "item_confidences": raw_confidences,
        "item_rectangles": raw_rectangles,
        "item_count": item.get("item_count"),
        "truncated": bool(item.get("truncated")),
        "values_truncated": values_truncated,
        "region": item.get("region") or {},
        "target": item.get("target") or {},
        "receipt": item.get("receipt") or {},
    }


def _unique_nonempty_strings(values):
    return list(dict.fromkeys(
        str(value)
        for value in values or ()
        if value not in (None, "")
    ))


def _compact_reuse_candidate(value):
    value = dict(value or {})
    return _without_empty({
        key: value.get(key)
        for key in (
            "candidate_id",
            "kind",
            "path",
            "symbol",
            "signature",
            "key",
            "file_sha256",
            "step_patterns",
            "matched_step_texts",
            "operations",
            "call_sequence",
            "references",
            "table_usage_hint",
            "quality",
            "semantic_contract",
            "score",
            "reasons",
        )
    })


def _compact_window_ownership(value):
    value = dict(value or {})
    windows = []
    for raw_window in value.get("windows") or []:
        window = dict(raw_window or {})
        owner_match = dict(window.get("owner_match") or {})
        compact_candidates = []
        for raw_candidate in owner_match.get("candidates") or []:
            candidate = dict(raw_candidate or {})
            compact = _without_empty({
                key: candidate.get(key)
                for key in (
                    "candidate_id",
                    "kind",
                    "page_object",
                    "page_class",
                    "root_locator_file",
                    "root_locator",
                    "criteria",
                    "page_sha256",
                    "locator_sha256",
                    "score",
                    "strength",
                    "reasons",
                )
            })
            methods = [
                _compact_reuse_candidate(item)
                for item in candidate.get("method_candidates") or []
            ]
            if methods:
                compact["method_candidates"] = methods
            compact_candidates.append(compact)
        compact_owner_match = _without_empty({
            "suggested_strategy": owner_match.get("suggested_strategy"),
            "candidates": compact_candidates,
        })
        compact_window = _without_empty({
            key: window.get(key)
            for key in (
                "root_name",
                "root_criteria",
                "step_ids",
                "action_ids",
                "action_types",
                "locator_names",
                "control_types",
                "window_identities",
                "identity_status",
            )
        })
        if compact_owner_match:
            compact_window["owner_match"] = compact_owner_match
        windows.append(compact_window)
    return _without_empty({
        "model": value.get("model"),
        "required_for_new_plan": value.get("required_for_new_plan"),
        "windows": windows,
        "cross_window_steps": value.get("cross_window_steps") or [],
        "roots_by_step": value.get("roots_by_step") or {},
        "unowned_action_ids": value.get("unowned_action_ids") or [],
    })


def _compact_reuse_index(value):
    value = dict(value or {})
    return _without_empty({
        "available": value.get("available"),
        "index_fingerprint": value.get("index_fingerprint"),
        "stats": value.get("stats") or {},
        "warnings": value.get("warnings") or [],
    })


def _without_empty(value):
    return {
        key: item
        for key, item in value.items()
        if item not in (None, "", [], {})
    }


def _compact_text_change(value):
    if not value:
        return None
    return {
        "status": value.get("status"),
        "before_value": value.get("before_value"),
        "after_value": value.get("after_value"),
        "value_delta": value.get("value_delta"),
        "key_sequence": value.get("key_sequence") or [],
        "value_binding": value.get("value_binding"),
    }


def _compact_canonical_action(value):
    if not value:
        return None
    return _without_empty({
        "canonical_action_version": value.get("canonical_action_version"),
        "command": value.get("command") or {},
        "observed_after": value.get("observed_after") or {},
        "business_expectation": value.get("business_expectation") or {},
    })


def _compact_media(value):
    if not value:
        return None
    return {
        "before": (value.get("before") or {}).get("path"),
        "after": (value.get("after") or {}).get("path"),
        "stability": (value.get("stability") or {}).get("status"),
        "outcome": (value.get("outcome") or {}).get("result"),
    }


def _story(actions):
    story = []
    for action in actions:
        target = action.get("target") or {}
        text = action.get("text_change") or {}
        story.append({
            "step_id": action.get("step_id"),
            "ordinal": action.get("ordinal"),
            "type": action.get("type"),
            "role": action.get("role"),
            "target": (
                target.get("name")
                or target.get("auto_id")
                or target.get("control_type")
            ),
            "value": (
                text.get("after_value")
                if action.get("type") == "keyboard"
                else None
            ),
        })
    return story


def _forensic_evidence_ids(context, action, conflict_codes):
    deferred = context.get("deferred_artifacts") or []
    kinds = set(EVENT_ARTIFACT_KINDS)
    if any(code.startswith("fallback_") for code in conflict_codes):
        kinds.update({"image", "ui_tree"})
    if "positional_locator_unstable" in conflict_codes:
        kinds.add("ui_tree")
    if "visual_state_unsettled" in conflict_codes:
        kinds.add("video")
    event_ids = set(action.get("event_ids") or []) | set(
        action.get("media_event_ids") or []
    )
    result = []
    for item in deferred:
        if item.get("kind") not in kinds:
            continue
        path = str(item.get("path") or "")
        if item.get("kind") in EVENT_ARTIFACT_KINDS | {"video"}:
            result.append(item["evidence_id"])
            continue
        if item.get("kind") == "ui_tree":
            result.append(item["evidence_id"])
            continue
        if any(event_id.replace("event-", "e") in path for event_id in event_ids):
            result.append(item["evidence_id"])
    return result


def _review_event_ids(review):
    values = {
        str(item)
        for item in review.get("event_ids") or []
        if item
    }
    evidence = review.get("evidence")
    if isinstance(evidence, str) and evidence.startswith("event-"):
        values.add(evidence)
    if isinstance(evidence, dict):
        values.update(
            str(item)
            for item in evidence.get("event_ids") or []
            if item
        )
        event_id = evidence.get("event_id")
        if event_id:
            values.add(str(event_id))
    last_action = (
        (((review.get("recovery") or {}).get("inventory") or {}).get(
            "last_action"
        ) or {})
    )
    values.update(
        str(item)
        for item in last_action.get("event_ids") or []
        if item
    )
    return values


def _action_event_ids(action, request):
    del request
    yield from action.get("event_ids") or []
    yield from action.get("media_event_ids") or []


def _take_revisions(request):
    return [
        {
            "step_id": (entry.get("step") or {}).get("id"),
            "take_id": (entry.get("selected_take") or {}).get("id"),
            "timeline_revision": entry.get("timeline_revision"),
            "graph_fingerprint": (
                entry.get("evidence_graph") or {}
            ).get("graph_fingerprint"),
        }
        for entry in request.get("evidence") or []
    ]


def _stable_hash(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _action_scope_key(step_id, action_id):
    return f"{str(step_id)}\x1f{str(action_id)}"