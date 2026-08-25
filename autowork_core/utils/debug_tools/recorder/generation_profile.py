from __future__ import annotations

import copy
import hashlib
import json


GENERATION_PROFILE_REGISTRY_VERSION = "1.0"
GENERATION_ADMISSION_VERSION = "1.0"
DEFAULT_GENERATION_PROFILE_ID = "generation_first"


_PROFILE_DEFINITIONS = (
    {
        "profile_id": "generation_first",
        "label": "专心生成",
        "start_allowed": True,
        "investigation_policy": "named_scope_only",
        "user_interaction_policy": "frontloaded_only",
        "repair_policy": {
            "design": "progress_bounded_technical",
            "implementation": "manifest_scoped",
            "authority_change": "terminate_job",
        },
    },
    {
        "profile_id": "precision",
        "label": "专心精确（已退役）",
        "start_allowed": False,
        "investigation_policy": "expanded_on_demand",
        "user_interaction_policy": "frontloaded_only",
        "repair_policy": {
            "design": "progress_bounded_technical",
            "implementation": "manifest_scoped",
            "authority_change": "terminate_job",
        },
    },
    {
        "profile_id": "legacy_script_maintenance",
        "label": "老脚本维护",
        "start_allowed": False,
        "investigation_policy": "not_implemented",
        "user_interaction_policy": "frontloaded_only",
        "repair_policy": {
            "design": "not_implemented",
            "implementation": "not_implemented",
            "authority_change": "terminate_job",
        },
    },
)

_HISTORICAL_PROFILE_DEFINITIONS = (
    {
        "profile_id": "precision",
        "label": "专心精确",
        "start_allowed": True,
        "investigation_policy": "expanded_on_demand",
        "user_interaction_policy": "frontloaded_only",
        "repair_policy": {
            "design": "progress_bounded_technical",
            "implementation": "manifest_scoped",
            "authority_change": "terminate_job",
        },
    },
)


def generation_profile_registry():
    profiles = [_profile_value(item) for item in _PROFILE_DEFINITIONS]
    value = {
        "generation_profile_registry_version": (
            GENERATION_PROFILE_REGISTRY_VERSION
        ),
        "default_profile_id": DEFAULT_GENERATION_PROFILE_ID,
        "profiles": profiles,
    }
    value["registry_fingerprint"] = _fingerprint(value)
    return value


def resolve_generation_profile(profile_id=None):
    selected = str(profile_id or DEFAULT_GENERATION_PROFILE_ID)
    registry = generation_profile_registry()
    for profile in registry["profiles"]:
        if profile["profile_id"] == selected:
            return copy.deepcopy(profile)
    raise ValueError(f"未知 Generation Profile: {selected}")


def profile_lease_is_recognized(profile):
    if not isinstance(profile, dict):
        return False
    profile_id = profile.get("profile_id")
    fingerprint = profile.get("profile_fingerprint")
    if not profile_id or not fingerprint:
        return False
    try:
        if resolve_generation_profile(profile_id).get(
                "profile_fingerprint"
        ) == fingerprint:
            return True
    except ValueError:
        return False
    return any(
        _profile_value(definition).get("profile_fingerprint") == fingerprint
        for definition in _HISTORICAL_PROFILE_DEFINITIONS
        if definition.get("profile_id") == profile_id
    )


def project_generation_admission(
        *,
        request,
        state,
        context_budget,
        request_identity_valid,
        profile_id=None,
    decision_pack=None,
    answer_record=None,
        enforcement="shadow",
        generation_contract_lease=None,
    ):
    request = dict(request or {})
    state = dict(state or {})
    context_budget = dict(context_budget or {})
    profile = resolve_generation_profile(profile_id)
    if enforcement not in {"shadow", "active"}:
        raise ValueError(
            f"无效 Generation admission enforcement: {enforcement}"
        )
    revision_seal = (request.get("revision_snapshot") or {}).get("seal")
    state_revision_seal = (state.get("revision") or {}).get("seal")
    readiness = request.get("readiness") or {}
    ambiguity = state.get("ambiguity") or {}
    decision = state.get("decision") or {}
    decision_pack = dict(decision_pack or {})
    answer_record = dict(answer_record or {})
    generation_contract_lease = dict(generation_contract_lease or {})
    decision_batch = _decision_batch_receipt(
        decision,
        decision_pack,
        answer_record,
    )
    active_transaction = state.get("active_transaction") or {}
    hard_blockers = [
        item
        for item in readiness.get("target_review_required") or ()
        if (item.get("recovery") or {}).get("hard_blocker")
    ]

    checks = [
        _check(
            "profile_start_allowed",
            bool(profile.get("start_allowed")),
            profile.get("profile_fingerprint"),
        ),
        _check(
            "request_identity",
            bool(
                request.get("request_version") == "3.0"
                and request.get("request_id")
                and request_identity_valid
            ),
            request.get("request_fingerprint"),
        ),
        _check(
            "request_revision",
            bool(revision_seal and revision_seal == state_revision_seal),
            revision_seal,
        ),
        _check(
            "brief_identity",
            bool((state.get("brief") or {}).get("brief_fingerprint")),
            (state.get("brief") or {}).get("brief_fingerprint"),
        ),
        _check(
            "evidence_readiness",
            bool(
                readiness.get("bundle_valid")
                and not hard_blockers
                and not ambiguity.get("pending_evidence_count", 0)
            ),
            _fingerprint({
                "bundle_valid": readiness.get("bundle_valid"),
                "hard_blocker_count": len(hard_blockers),
                "pending_evidence_count": ambiguity.get(
                    "pending_evidence_count",
                    0,
                ),
            }),
        ),
        _check(
            "decision_batch",
            bool(decision_batch.get("complete")),
            _fingerprint({
                "status": decision.get("status"),
                "pack": decision.get("pack") or {},
                "answers": decision.get("answers") or {},
                "batch": decision_batch,
            }),
        ),
        _check(
            "generation_contract",
            bool(
                generation_contract_lease.get("lease_fingerprint")
                or request.get("generation_contract")
            ),
            (
                generation_contract_lease.get("lease_fingerprint")
                or _fingerprint(request.get("generation_contract"))
            ),
        ),
        _check(
            "workflow_admissible",
            state.get("status") not in {"blocked", "stale"},
            _fingerprint({
                "status": state.get("status"),
                "errors": state.get("errors") or [],
            }),
        ),
        _check(
            "generation_conflict",
            bool(
                state.get("status") != "running"
                and not active_transaction.get("transaction_id")
            ),
            _fingerprint({
                "status": state.get("status"),
                "active_transaction": active_transaction,
            }),
        ),
    ]
    blocking_codes = [
        item["code"]
        for item in checks
        if item["status"] != "passed"
    ]
    performance_warnings = []
    if context_budget.get("status") not in {None, "within_target"}:
        performance_warnings.append(_context_budget_warning(context_budget))
    value = {
        "generation_admission_version": GENERATION_ADMISSION_VERSION,
        "enforcement": enforcement,
        "status": "passed" if not blocking_codes else "rejected",
        "request_id": request.get("request_id"),
        "profile": profile,
        "decision_batch": decision_batch,
        "checks": checks,
        "performance_checks": [{
            "code": "context_budget",
            "status": context_budget.get("status") or "unknown",
            "source_fingerprint": _fingerprint(context_budget),
            "diagnostic": _context_budget_warning(context_budget),
        }],
        "performance_warnings": performance_warnings,
        "blocking_codes": blocking_codes,
        "job_creation_allowed": bool(
            enforcement == "active" and not blocking_codes
        ),
    }
    value["admission_fingerprint"] = _fingerprint(value)
    return value


def _context_budget_warning(context_budget):
    context_budget = dict(context_budget or {})
    return {
        "budget_version": context_budget.get("budget_version"),
        "status": context_budget.get("status") or "unknown",
        "default_total_bytes": context_budget.get("default_total_bytes"),
        "target_bytes": context_budget.get("target_bytes"),
        "over_by_bytes": context_budget.get("over_by_bytes"),
        "largest_components": list(context_budget.get("largest_components") or []),
        "message": (
            "默认AI上下文超过性能目标；系统将继续创建Job并依赖紧凑投影/按需查询。"
            if context_budget.get("status") == "over_target"
            else "默认AI上下文预算状态未通过性能检查。"
        ),
    }


def _decision_batch_receipt(decision, pack, answers):
    pointer = decision.get("pack") or {}
    answer_pointer = decision.get("answers") or {}
    questions = [
        item
        for item in pack.get("questions") or ()
        if isinstance(item, dict)
    ]
    blocking_ids = sorted(
        str(item.get("question_id"))
        for item in questions
        if item.get("blocking") and item.get("question_id")
    )
    answered_ids = sorted(
        str(item.get("question_id"))
        for item in answers.get("answers") or ()
        if item.get("question_id")
    )
    forensic_ids = sorted(
        str(item.get("blocker_id"))
        for item in pack.get("forensic_blockers") or ()
        if item.get("blocker_id")
    )
    batch = pack.get("batch") or {}
    return {
        "status": decision.get("status"),
        "pack_id": pack.get("pack_id") or pointer.get("pack_id"),
        "pack_fingerprint": (
            pack.get("pack_fingerprint")
            or pointer.get("pack_fingerprint")
        ),
        "answer_fingerprint": (
            answers.get("answer_fingerprint")
            or answer_pointer.get("answer_fingerprint")
        ),
        "batch_id": batch.get("batch_id"),
        "blocking_question_ids": blocking_ids,
        "answered_question_ids": answered_ids,
        "forensic_blocker_ids": forensic_ids,
        "complete": bool(
            decision.get("status") in {"answered", "not_required", "forensic"}
            and set(blocking_ids) <= set(answered_ids)
        ),
    }


def _profile_value(definition):
    value = {
        "generation_profile_version": "1.0",
        **copy.deepcopy(definition),
    }
    value["profile_fingerprint"] = _fingerprint(value)
    return value


def _check(code, passed, source_fingerprint):
    return {
        "code": code,
        "status": "passed" if passed else "failed",
        "source_fingerprint": source_fingerprint,
    }


def _fingerprint(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()