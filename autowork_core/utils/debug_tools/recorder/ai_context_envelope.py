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


AI_CONTEXT_ENVELOPE_VERSION = "1.1"


def build_ai_context_envelope(
        *,
        session_dir,
        request,
        state,
        brief_path,
        job_value,
        job_path=None,
        workflow_version="4.0",
        workflow_context=None,
        ai_capabilities=None,
        plan_context=None,
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
    envelope = {
        "ai_context_envelope_version": AI_CONTEXT_ENVELOPE_VERSION,
        "workflow_version": str(workflow_version),
        "job": _without_empty({
            "job_id": job.get("job_id"),
            "job_fingerprint": job.get("job_fingerprint"),
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
        "brief": brief,
        "plan_context": plan_context,
        "ai_capabilities": ai_capabilities or {},
        "design_contract": compact_generation_design_contract(),
        "query_policy": {
            "rule": "Expand omitted evidence only through allowed_queries.",
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
            "ai_capabilities",
            "design_contract",
            "query_policy",
            "envelope_fingerprint",
        ],
        "required_job_fields": ["job_id", "job_fingerprint"],
        "required_request_fields": [
            "request_id",
            "request_fingerprint",
            "revision_seal",
        ],
        "required_profile_fields": ["profile_id", "profile_fingerprint"],
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


def ai_context_envelope_identity_is_valid(value):
    if not isinstance(value, dict):
        return False
    required = set(
        compact_ai_context_envelope_contract()["required_top_level"]
    )
    if set(value) != required:
        return False
    if value.get("ai_context_envelope_version") != AI_CONTEXT_ENVELOPE_VERSION:
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
    if not isinstance(value.get("brief"), dict):
        return False
    if not isinstance(value.get("workflow"), dict):
        return False
    if not isinstance(value.get("decision"), dict):
        return False
    if not isinstance(value.get("ai_capabilities"), dict):
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
