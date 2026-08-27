from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

from config.paths import Paths
from autowork_core.utils.debug_tools.recorder.ai_plan_context import (
    AI_PLAN_CONTEXT_SECTIONS,
    build_ai_plan_context,
    query_ai_plan_context,
)
from autowork_core.utils.debug_tools.recorder.action_knowledge import (
    query_action_knowledge,
)
from autowork_core.utils.debug_tools.recorder.ai_context_envelope import (
    AI_CONTEXT_ENVELOPE_VERSION,
    build_ai_context_envelope,
)
from autowork_core.utils.debug_tools.recorder.evidence_context import (
    compare_request_takes,
    query_request_evidence,
)
from autowork_core.utils.debug_tools.recorder.query_service import (
    query_request_decision_media,
)
from autowork_core.utils.debug_tools.recorder.generation_transaction import (
    TRANSACTION_VERSION,
)
from autowork_core.utils.debug_tools.recorder.generation_contract import (
    compact_ai_capability_contract,
    generation_contract_lease,
)
from autowork_core.utils.debug_tools.recorder.generation_plan import (
    compact_generation_intent_contract,
    load_generation_plan,
)
from autowork_core.utils.debug_tools.recorder.generation_profile import (
    generation_profile_registry,
    project_generation_admission,
)
from autowork_core.utils.debug_tools.recorder.generation_job_service import (
    abort_generation_job,
    admit_generation_job,
    finish_generation_job,
    inspect_generation_job,
    prepare_generation_job,
    compare_generation_job_takes,
    query_generation_job_action_knowledge,
    query_generation_job_design_context,
    query_generation_job_evidence,
    query_generation_job_implementation_packet,
    reconcile_generation_job_runtime,
    retire_generation_job,
    retry_generation_job,
    start_generation_job,
    submit_generation_job_design,
    validate_generation_job_implementation,
)
from autowork_core.utils.debug_tools.recorder.generation_design import (
    compact_generation_design_contract,
)
from autowork_core.utils.debug_tools.recorder.request_repository import (
    request_identity_is_valid,
    session_dir_for_request_path,
)
from autowork_core.utils.debug_tools.recorder.reconciliation_repository import (
    load_generation_brief,
)
from autowork_core.utils.debug_tools.recorder.semantic_reconciler import (
    brief_matches_request,
)
from autowork_core.utils.debug_tools.recorder.transaction_integrity import (
    transaction_result_fingerprint,
)
from autowork_core.utils.debug_tools.recorder.technical_repair import (
    RequestTechnicalRepairService,
)
from autowork_core.utils.debug_tools.recorder.workflow_service import (
    inspect_workflow,
)
from autowork_core.utils.debug_tools.recorder.workflow_state import (
    load_workflow_state,
)


WORKFLOW_VERSION = "4.0"
AI_WORKFLOW_CONTEXT_VERSION = "1.0"
AI_CONTEXT_BUDGET_VERSION = "1.3"
AI_CONTEXT_TARGET_BYTES = 50 * 1024


def inspect_generation(request_path, *, generation_profile_id=None):
    request_path = Path(request_path).resolve()
    request = _read_json(request_path)
    session_dir = session_dir_for_request_path(request_path, request)
    state = inspect_workflow(request_path, write=True)
    brief = state.get("brief") or {}
    plan = state.get("plan") or {}
    plan_path = _absolute_pointer(session_dir, plan.get("path"))
    revision_context = _revision_context(
        session_dir,
        request,
        state,
        plan_path=plan_path,
    )
    plan_artifact = (
        load_generation_plan(session_dir, state, request)
        if plan_path
        else None
    )
    plan_context = (
        build_ai_plan_context(
            plan_artifact,
            last_result=_plan_result_context(
                state,
                revision_context,
            ),
        )
        if plan_artifact is not None
        else None
    )
    decision = json.loads(json.dumps(state.get("decision") or {}))
    for pointer_name in ("pack", "answers"):
        pointer = decision.get(pointer_name) or {}
        if pointer.get("path"):
            pointer["path"] = _absolute_pointer(
                session_dir,
                pointer["path"],
            )
    result = {
        "workflow_version": WORKFLOW_VERSION,
        "status": state.get("status"),
        "next_action": state.get("next_action"),
        "request_id": request.get("request_id"),
        "request_path": str(request_path),
        "brief_path": _absolute_pointer(session_dir, brief.get("path")),
        "plan_path": plan_path,
        "plan_context": plan_context,
        "decision": decision,
        "risk": state.get("risk") or {},
        "adjustment": state.get("adjustment") or {},
        "required_forensic_evidence": state.get(
            "required_forensic_evidence"
        ) or [],
        "errors": state.get("errors") or [],
        "warnings": state.get("warnings") or [],
        "revision_context": revision_context,
        "ai_capabilities": compact_ai_capability_contract(),
    }
    profile_registry = generation_profile_registry()
    context_budget = build_ai_context_budget(
        session_dir=session_dir,
        request_path=request_path,
        request=request,
        state=state,
        inspect_result=result,
        capability_contract=result["ai_capabilities"],
        brief_path=result["brief_path"],
        plan_path=plan_path,
        plan_context=plan_context,
    )
    for _attempt in range(8):
        admission = project_generation_admission(
            request=request,
            state=state,
            context_budget=context_budget,
            request_identity_valid=request_identity_is_valid(request),
            profile_id=generation_profile_id,
            generation_contract_lease=generation_contract_lease(
                session_dir,
                write=False,
            ),
        )
        projected = {
            **result,
            "generation_profile_registry": profile_registry,
            "generation_admission": admission,
        }
        next_budget = build_ai_context_budget(
            session_dir=session_dir,
            request_path=request_path,
            request=request,
            state=state,
            inspect_result=projected,
            capability_contract=result["ai_capabilities"],
            brief_path=result["brief_path"],
            plan_path=plan_path,
            plan_context=plan_context,
        )
        if next_budget == context_budget:
            return _with_context_budget(projected, context_budget)
        context_budget = next_budget
    raise RuntimeError("Generation admission context budget 未能收敛")


def query_generation_profile_contract():
    return {
        "workflow_version": WORKFLOW_VERSION,
        "status": "projected",
        "generation_profile_registry": generation_profile_registry(),
    }


def query_generation_intent_contract():
    contract = compact_generation_intent_contract()
    fingerprint = hashlib.sha256(json.dumps(
        contract,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return {
        "workflow_version": WORKFLOW_VERSION,
        "status": "projected",
        "generation_intent_contract": contract,
        "generation_intent_contract_fingerprint": fingerprint,
    }


def query_generation_design_contract():
    contract = compact_generation_design_contract()
    fingerprint = hashlib.sha256(json.dumps(
        contract,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return {
        "workflow_version": WORKFLOW_VERSION,
        "status": "projected",
        "generation_design_contract": contract,
        "generation_design_contract_fingerprint": fingerprint,
    }


def build_ai_context_budget(
        *,
        session_dir,
        request_path,
        request,
        state,
        inspect_result=None,
        capability_contract=None,
        brief_path=None,
        plan_path=None,
        plan_context=None,
        job_path=None,
        job_value=None,
        project_root=None,
    ):
    session_dir = Path(session_dir).resolve()
    project_root = Path(project_root or Paths.BASE_DIR).resolve()
    inspect_result = inspect_result or {}
    capability_contract = (
        compact_ai_capability_contract()
        if capability_contract is None
        else capability_contract
    )
    envelope = inspect_result.get("ai_context_envelope")
    if envelope is None and (job_value is not None or job_path is not None):
        envelope = build_ai_context_envelope(
            session_dir=session_dir,
            request=request,
            state=state,
            brief_path=brief_path,
            job_value=job_value or {},
            job_path=job_path,
            workflow_version=WORKFLOW_VERSION,
            workflow_context=_compact_workflow_context(state),
            ai_capabilities=capability_contract,
            plan_context=plan_context,
        )
    envelope_mode = envelope is not None
    components = [
        _context_file_component("request", request_path, "entrypoint_backend"),
        _context_value_component(
            "workflow",
            _compact_workflow_context(state),
            "embedded_in_envelope" if envelope_mode else "embedded_in_inspect",
        ),
        _context_file_component(
            "brief",
            brief_path,
            "embedded_in_envelope" if envelope_mode else "default",
        ),
        (
            _context_value_component(
                "generation_job",
                job_value,
                "backend_identity" if envelope_mode else "default",
            )
            if job_value is not None
            else _context_file_component(
                "generation_job",
                job_path,
                "backend_identity" if envelope_mode else "default",
            )
        ),
        _context_value_component(
            "plan_context",
            plan_context,
            "embedded_in_envelope" if envelope_mode else "embedded_in_inspect",
        ),
        _context_file_component(
            "generation_plan",
            plan_path,
            "backend_identity",
        ),
        _context_file_component(
            "generation_contract",
            _session_artifact_path(
                session_dir,
                request.get("generation_contract"),
            ),
            "backend_identity",
        ),
        _context_value_component(
            "ai_capabilities",
            capability_contract,
            "embedded_in_envelope" if envelope_mode else "embedded_in_inspect",
        ),
        _context_value_component(
            "design_contract",
            compact_generation_design_contract(),
            "embedded_in_envelope" if envelope_mode else "conditional",
        ),
        _context_value_component(
            "ai_context_envelope",
            envelope,
            "default" if envelope_mode else "not_applicable",
        ),
        _context_file_component(
            "recorder_generate_prompt",
            project_root / "ai" / "prompts" / "recorder-generate.md",
            "default",
        ),
        _context_file_component(
            "bdd_generation_instructions",
            project_root / "ai" / "instructions" / "bdd-generation.md",
            "default",
        ),
        _context_file_component(
            "project_context",
            project_root / "ai" / "context" / "project.md",
            "maintenance_on_demand",
        ),
    ]
    decision = state.get("decision") or {}
    for name in ("pack", "answers"):
        pointer = decision.get(name) or {}
        components.append(_context_file_component(
            f"decision_{name}",
            _session_artifact_path(session_dir, pointer.get("path")),
            "conditional",
        ))
    inspect_result = inspect_result or {
        "workflow_version": WORKFLOW_VERSION,
        "status": state.get("status"),
        "next_action": state.get("next_action"),
        "request_id": request.get("request_id"),
        "brief_path": str(brief_path) if brief_path else None,
        "plan_path": str(plan_path) if plan_path else None,
        "plan_context": plan_context,
        "decision": state.get("decision") or {},
        "risk": state.get("risk") or {},
        "adjustment": state.get("adjustment") or {},
        "required_forensic_evidence": state.get(
            "required_forensic_evidence"
        ) or [],
        "errors": state.get("errors") or [],
        "warnings": state.get("warnings") or [],
        "revision_context": {},
        "ai_capabilities": capability_contract,
    }
    components.append(_context_value_component(
        "inspect_output",
        None,
        "transport_diagnostic" if envelope_mode else "default",
    ))
    budget = _summarize_context_budget(components)
    for _attempt in range(16):
        projected = _with_context_budget(inspect_result, budget)
        inspect_bytes = len(
            _serialize_cli_result(projected).encode("utf-8")
        )
        next_components = [
            {
                **component,
                "bytes": (
                    inspect_bytes
                    if component["name"] == "inspect_output"
                    else component["bytes"]
                ),
            }
            for component in components
        ]
        next_budget = _summarize_context_budget(next_components)
        if next_budget == budget:
            return next_budget
        components = next_components
        budget = next_budget
    raise RuntimeError("AI Context Budget 未能收敛")


def _summarize_context_budget(components):
    default_access = {"default", "default_when_ready"}
    default_total = sum(
        item["bytes"]
        for item in components
        if item["access"] in default_access
    )
    conditional_total = sum(
        item["bytes"]
        for item in components
        if item["access"] == "conditional"
    )
    ranked = sorted(
        (
            item
            for item in components
            if item["access"] in default_access and item["bytes"] > 0
        ),
        key=lambda item: (-item["bytes"], item["name"]),
    )
    return {
        "budget_version": AI_CONTEXT_BUDGET_VERSION,
        "target_bytes": AI_CONTEXT_TARGET_BYTES,
        "status": (
            "within_target"
            if default_total <= AI_CONTEXT_TARGET_BYTES
            else "over_target"
        ),
        "default_total_bytes": default_total,
        "conditional_total_bytes": conditional_total,
        "over_by_bytes": max(0, default_total - AI_CONTEXT_TARGET_BYTES),
        "largest_components": [
            {"name": item["name"], "bytes": item["bytes"]}
            for item in ranked[:3]
        ],
        "components": components,
        "enforcement": "warn_only",
    }


def _with_context_budget(result, budget):
    projected = dict(result or {})
    projected["ai_context_budget"] = budget
    message = (
        "默认AI上下文超过目标预算: "
        f"{budget['default_total_bytes']} > "
        f"{budget['target_bytes']} bytes；"
        "按ai_context_budget.largest_components优先收敛。"
    )
    warnings = [
        item
        for item in projected.get("warnings") or []
        if not str(item).startswith("默认AI上下文超过目标预算:")
    ]
    if budget["status"] == "over_target":
        warnings.append(message)
    projected["warnings"] = warnings
    return projected


def _compact_workflow_context(state):
    state = state if isinstance(state, dict) else {}
    decision = state.get("decision") or {}
    pack = decision.get("pack") or {}
    active = state.get("active_transaction") or {}
    result = state.get("last_result") or {}
    return _without_empty({
        "workflow_context_version": AI_WORKFLOW_CONTEXT_VERSION,
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


def _without_empty(value):
    return {
        key: item
        for key, item in value.items()
        if item not in (None, "", [], {})
    }


def _context_file_component(name, path, access):
    path = Path(path).resolve() if path else None
    return {
        "name": name,
        "access": access,
        "bytes": path.stat().st_size if path and path.is_file() else 0,
    }


def _context_value_component(name, value, access):
    size = (
        len(json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"))
        if value is not None
        else 0
    )
    return {
        "name": name,
        "access": access,
        "bytes": size,
    }


def _session_artifact_path(session_dir, value):
    if not value:
        return None
    path = Path(str(value))
    return path.resolve() if path.is_absolute() else (
        Path(session_dir).resolve() / path
    ).resolve()


def query_generation_plan(request_path, *, section=None, step_id=None):
    request_path = Path(request_path).resolve()
    request = _read_json(request_path)
    session_dir = session_dir_for_request_path(request_path, request)
    state = inspect_workflow(
        request_path,
        write=False,
    )
    artifact = load_generation_plan(session_dir, state, request)
    if artifact is None:
        raise ValueError("当前 Request 没有身份有效的 GenerationPlan")
    revision_context = _revision_context(
        session_dir,
        request,
        state,
        plan_path=_absolute_pointer(
            session_dir,
            (state.get("plan") or {}).get("path"),
        ),
    )
    return {
        "workflow_version": WORKFLOW_VERSION,
        "status": "projected",
        "request_id": request.get("request_id"),
        "plan_context": query_ai_plan_context(
            artifact,
            section=section,
            step_id=step_id,
            last_result=_plan_result_context(
                state,
                revision_context,
            ),
        ),
    }


def query_generation_action_knowledge(
        request_path,
        *,
    step_id=None,
        action_id=None,
        operation_names=(),
        list_only=False,
    ):
    request_path = Path(request_path).resolve()
    request = _read_json(request_path)
    session_dir = session_dir_for_request_path(request_path, request)
    state = inspect_workflow(request_path, write=False)
    if state.get("status") in {"blocked", "stale"}:
        raise ValueError(
            "当前Request不能查询Action knowledge: "
            f"status={state.get('status')}"
        )
    persisted = load_workflow_state(
        session_dir,
        request.get("request_id"),
    )
    brief_path = _absolute_pointer(
        session_dir,
        (
            (persisted.get("brief") or {}).get("path")
            or (state.get("brief") or {}).get("path")
        ),
    )
    if not brief_path:
        raise ValueError("当前Request没有身份有效的Generation Brief")
    brief_path = Path(brief_path).resolve()
    brief_root = (session_dir / "ai" / "generation-briefs").resolve()
    try:
        brief_path.relative_to(brief_root)
    except ValueError as error:
        raise ValueError("Action knowledge Brief路径越界") from error
    if brief_path.name != f"{request.get('request_id')}.json":
        raise ValueError("Action knowledge Brief路径与Request不一致")
    brief = load_generation_brief(brief_path)
    if not brief_matches_request(brief, request):
        raise ValueError("Action knowledge Brief身份与Request不一致")
    return {
        "workflow_version": WORKFLOW_VERSION,
        "status": "projected",
        "request_id": request.get("request_id"),
        "action_knowledge": query_action_knowledge(
            brief,
            step_id=step_id,
            action_id=action_id,
            operation_names=operation_names,
            list_only=list_only,
        ),
    }


def query_generation_decision_media(request_path, *, question_id=None):
    request_path = Path(request_path).resolve()
    request = _read_json(request_path)
    session_dir = session_dir_for_request_path(request_path, request)
    state = inspect_workflow(request_path, write=False)
    if state.get("status") in {"blocked", "stale"}:
        raise ValueError(
            "当前Request不能查询Decision媒体: "
            f"status={state.get('status')}"
        )
    projected = query_request_decision_media(
        session_dir,
        request,
        state.get("decision") or {},
        question_id=question_id,
    )
    return {
        "workflow_version": WORKFLOW_VERSION,
        "status": "projected",
        "request_id": request.get("request_id"),
        "revision_seal": (
            (request.get("revision_snapshot") or {}).get("seal")
        ),
        "decision_pack_id": projected["decision_pack_id"],
        "context_fingerprint": projected["context_fingerprint"],
        "decision_media": projected["questions"],
    }


def query_technical_repair_pack(request_path, *, step_id, action_id):
    service = RequestTechnicalRepairService(request_path)
    return {
        "workflow_version": WORKFLOW_VERSION,
        "status": "projected",
        "request_id": service.request["request_id"],
        "technical_repair_pack": service.build_pack(
            step_id=step_id,
            action_id=action_id,
        ),
    }


def submit_technical_repair_proposal(request_path, proposal):
    service = RequestTechnicalRepairService(request_path)
    result = service.apply_proposal(proposal)
    return {
        "workflow_version": WORKFLOW_VERSION,
        "status": result["status"],
        "request_id": service.request["request_id"],
        "technical_repair_receipt": result["receipt"],
        "next_action": "materialize_latest_request",
    }


def _plan_result_context(state, revision_context):
    result = dict((state or {}).get("last_result") or {})
    failed_checks = list(
        (revision_context or {}).get("failed_checks") or ()
    )
    if failed_checks:
        result["failed_checks"] = failed_checks
    return result


def _read_json(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 必须是 object: {path}")
    return value


def _absolute_pointer(session_dir, value):
    if not value:
        return None
    path = Path(value)
    return str(path.resolve() if path.is_absolute() else (session_dir / path).resolve())


def _revision_context(session_dir, request, state, *, plan_path):
    if state.get("status") != "failed":
        return {}
    plan = state.get("plan") or {}
    report_path, report, binding_errors = _bound_failure_report(
        session_dir,
        request,
        state,
    )
    return {
        "intent_path": plan_path,
        "intent_fingerprint": plan.get("intent_fingerprint"),
        "plan_path": plan_path,
        "plan_id": plan.get("plan_id"),
        "report_path": str(report_path) if report_path else None,
        "transaction_id": report.get("transaction_id"),
        "failure_status": report.get("status"),
        "failed_checks": _failed_checks(report),
        "report_binding_status": (
            "passed" if not binding_errors else "failed"
        ),
        "errors": [
            *binding_errors,
            *[str(item) for item in report.get("errors") or ()],
        ],
    }


def _bound_failure_report(session_dir, request, state):
    result = state.get("last_result") or {}
    transaction_id = str(result.get("transaction_id") or "")
    path_value = result.get("report_path")
    if not transaction_id or not path_value:
        return None, {}, ["Workflow 缺少绑定的失败事务报告"]
    session_dir = Path(session_dir).resolve()
    report_path = Path(path_value)
    report_path = (
        report_path.resolve()
        if report_path.is_absolute()
        else (session_dir / report_path).resolve()
    )
    root = (session_dir / "ai" / "generation-transactions").resolve()
    try:
        relative = report_path.relative_to(root)
    except ValueError:
        return None, {}, ["失败事务报告路径越界"]
    if (
        len(relative.parts) != 2
        or relative.parts[0] != transaction_id
        or not transaction_id.startswith("transaction-")
        or relative.parts[1] != "report.json"
    ):
        return None, {}, ["失败事务报告路径与 transaction_id 不一致"]
    try:
        report = _read_json(report_path)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        return None, {}, [
            f"绑定的失败事务报告不可读: {type(error).__name__}: {error}"
        ]
    lease = report.get("lease") or {}
    lease_revision = lease.get("revision") or {}
    lease_plan = lease.get("plan") or {}
    state_plan = state.get("plan") or {}
    errors = []
    if report.get("transaction_version") != TRANSACTION_VERSION:
        errors.append("失败事务报告版本无效")
    if report.get("transaction_id") != transaction_id:
        errors.append("失败事务报告 transaction_id 不匹配")
    if report.get("request_id") != request.get("request_id"):
        errors.append("失败事务报告 request_id 不匹配")
    if report.get("status") != result.get("status"):
        errors.append("失败事务报告 status 与 Workflow 不匹配")
    expected_result_fingerprint = result.get("result_fingerprint")
    if (
        not expected_result_fingerprint
        or report.get("result_fingerprint") != expected_result_fingerprint
        or transaction_result_fingerprint(report)
        != expected_result_fingerprint
    ):
        errors.append("失败事务报告 result fingerprint 无效")
    if lease_revision.get("seal") != (state.get("revision") or {}).get(
        "seal"
    ):
        errors.append("失败事务报告 revision seal 不匹配")
    if lease_plan.get("plan_fingerprint") != state_plan.get(
        "plan_fingerprint"
    ):
        errors.append("失败事务报告 Plan 指纹不匹配")
    expected_intent = state_plan.get("intent_fingerprint")
    if expected_intent and lease_plan.get("intent_fingerprint") != (
        expected_intent
    ):
        errors.append("失败事务报告 Intent 指纹不匹配")
    if errors:
        return None, {}, errors
    return report_path, report, []


def _failed_checks(report):
    failed = [
        str(name)
        for name, result in (report.get("validations") or {}).items()
        if (result or {}).get("status") not in {
            "passed",
            "not_applicable",
        }
    ]
    for name in (
        "generation_policy_audit",
        "pic_authorization_audit",
        "pic_usage_audit",
        "plan_conformance_audit",
        "evidence_audit",
    ):
        status = (report.get(name) or {}).get("status")
        if status and status not in {"passed", "not_applicable"}:
            failed.append(name)
    return list(dict.fromkeys(failed))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Recorder Generation Job workflow with V3 recovery"
    )
    visible_commands = (
        "inspect,evidence,compare-takes,plan,action-knowledge,"
        "decision-media,technical-repair-pack,technical-repair-apply,"
        "intent-contract,design-contract,profile-contract,admit,"
        "start-job,inspect-job,retry-job,retire-job,design-job,prepare-job,"
        "validate-job-implementation,finish-job,abort-job,"
        "reconcile-job-runtime,job-evidence,job-compare-takes,"
        "job-action-knowledge,job-design-context,"
        "job-implementation-packet,benchmark"
    )
    commands = parser.add_subparsers(
        dest="command",
        required=True,
        metavar="{" + visible_commands + "}",
    )

    inspect = commands.add_parser("inspect")
    inspect.add_argument("request_path")
    inspect.add_argument("--generation-profile")
    evidence = commands.add_parser("evidence")
    evidence.add_argument("request_path")
    evidence_selector = evidence.add_mutually_exclusive_group()
    evidence_selector.add_argument("--evidence-id")
    evidence_selector.add_argument("--step-id")
    evidence_selector.add_argument("--action-id")
    evidence.add_argument("--list", action="store_true")
    compare_takes = commands.add_parser("compare-takes")
    compare_takes.add_argument("request_path")
    compare_takes.add_argument("--step-id", required=True)
    compare_takes.add_argument("--take-id", action="append", default=[])
    plan = commands.add_parser("plan")
    plan.add_argument("request_path")
    plan_selector = plan.add_mutually_exclusive_group()
    plan_selector.add_argument("--step-id")
    plan_selector.add_argument(
        "--section",
        choices=AI_PLAN_CONTEXT_SECTIONS,
    )
    action_knowledge = commands.add_parser("action-knowledge")
    action_knowledge.add_argument("request_path")
    action_knowledge.add_argument("--step-id")
    action_knowledge.add_argument("--action-id")
    action_knowledge.add_argument(
        "--operation",
        action="append",
        default=[],
    )
    action_knowledge.add_argument("--list", action="store_true")
    decision_media = commands.add_parser("decision-media")
    decision_media.add_argument("request_path")
    decision_media.add_argument("--question-id")
    technical_repair_pack = commands.add_parser("technical-repair-pack")
    technical_repair_pack.add_argument("request_path")
    technical_repair_pack.add_argument("--step-id", required=True)
    technical_repair_pack.add_argument("--action-id", required=True)
    technical_repair_apply = commands.add_parser("technical-repair-apply")
    technical_repair_apply.add_argument("request_path")
    technical_repair_input = technical_repair_apply.add_mutually_exclusive_group(
        required=True
    )
    technical_repair_input.add_argument("--proposal-json")
    technical_repair_input.add_argument("--proposal-file")
    commands.add_parser("intent-contract")
    commands.add_parser("design-contract")
    commands.add_parser("profile-contract")
    admit = commands.add_parser("admit")
    admit.add_argument("request_path")
    admit.add_argument("--generation-profile")
    start_job = commands.add_parser("start-job")
    start_job.add_argument("job_path")
    start_job.add_argument("--expected-epoch", type=int, required=True)
    inspect_job = commands.add_parser("inspect-job")
    inspect_job.add_argument("job_path")
    inspect_job.add_argument("--full", action="store_true")
    retry_job = commands.add_parser("retry-job")
    retry_job.add_argument("job_path")
    retry_job.add_argument("--generation-profile")
    retire_job = commands.add_parser("retire-job")
    retire_job.add_argument("job_path")
    retire_job.add_argument("--expected-epoch", type=int, required=True)
    retire_job.add_argument("--claim-id")
    retire_job.add_argument("--reason", required=True)
    design_job = commands.add_parser("design-job")
    design_job.add_argument("job_path")
    design_job.add_argument("--claim-id", required=True)
    design_job.add_argument("--expected-epoch", type=int, required=True)
    design_job_input = design_job.add_mutually_exclusive_group(required=True)
    design_job_input.add_argument("--design-json")
    design_job_input.add_argument("--design-file")
    design_job.add_argument("--note", default="")
    prepare_job = commands.add_parser("prepare-job")
    prepare_job.add_argument("job_path")
    prepare_job.add_argument("--claim-id", required=True)
    prepare_job.add_argument("--expected-epoch", type=int, required=True)
    prepare_job.add_argument("--project-root")
    prepare_job.add_argument("--full", action="store_true")
    validate_job = commands.add_parser("validate-job-implementation")
    validate_job.add_argument("report_path")
    validate_job.add_argument("--claim-id", required=True)
    validate_job.add_argument("--expected-epoch", type=int, required=True)
    validate_job.add_argument("--project-root")
    finish_job = commands.add_parser("finish-job")
    finish_job.add_argument("report_path")
    finish_job.add_argument("--claim-id", required=True)
    finish_job.add_argument("--expected-epoch", type=int, required=True)
    finish_job.add_argument("--summary", default="")
    finish_job.add_argument("--project-root")
    abort_job = commands.add_parser("abort-job")
    abort_job.add_argument("report_path")
    abort_job.add_argument("--claim-id", required=True)
    abort_job.add_argument("--expected-epoch", type=int, required=True)
    abort_job.add_argument("--reason", required=True)
    abort_job.add_argument("--project-root")
    abort_job.add_argument(
        "--allow-project-guard-drift",
        action="store_true",
        help=(
            "Allow aborting a failed development transaction when files "
            "outside generation roots changed after prepare. The transaction "
            "still terminates as aborted and cannot become successful."
        ),
    )
    reconcile_runtime = commands.add_parser("reconcile-job-runtime")
    reconcile_runtime.add_argument("job_path")
    reconcile_runtime.add_argument("--claim-id", required=True)
    reconcile_runtime.add_argument("--expected-epoch", type=int, required=True)
    job_evidence = commands.add_parser("job-evidence")
    job_evidence.add_argument("job_path")
    job_evidence_selector = job_evidence.add_mutually_exclusive_group()
    job_evidence_selector.add_argument("--evidence-id")
    job_evidence_selector.add_argument("--step-id")
    job_evidence_selector.add_argument("--action-id")
    job_evidence.add_argument("--list", action="store_true")
    job_evidence.add_argument("--full", action="store_true")
    job_compare = commands.add_parser("job-compare-takes")
    job_compare.add_argument("job_path")
    job_compare.add_argument("--step-id", required=True)
    job_compare.add_argument("--take-id", action="append", default=[])
    job_knowledge = commands.add_parser("job-action-knowledge")
    job_knowledge.add_argument("job_path")
    job_knowledge.add_argument("--step-id")
    job_knowledge.add_argument("--action-id")
    job_knowledge.add_argument("--operation", action="append", default=[])
    job_knowledge.add_argument("--list", action="store_true")
    job_design_context = commands.add_parser("job-design-context")
    job_design_context.add_argument("job_path")
    job_design_context.add_argument("--step-id")
    job_implementation_packet = commands.add_parser(
        "job-implementation-packet"
    )
    job_implementation_packet.add_argument("report_path")
    packet_selector = job_implementation_packet.add_mutually_exclusive_group()
    packet_selector.add_argument("--step-id")
    packet_selector.add_argument("--path")
    benchmark = commands.add_parser("benchmark")
    benchmark.add_argument("request_path")
    args = parser.parse_args(argv)
    if args.command == "inspect":
        result = inspect_generation(
            args.request_path,
            generation_profile_id=args.generation_profile,
        )
    elif args.command == "evidence":
        result = query_request_evidence(
            args.request_path,
            evidence_id=args.evidence_id,
            step_id=args.step_id,
            action_id=args.action_id,
            list_only=args.list,
        )
    elif args.command == "compare-takes":
        result = compare_request_takes(
            args.request_path,
            step_id=args.step_id,
            take_ids=args.take_id,
        )
    elif args.command == "plan":
        result = query_generation_plan(
            args.request_path,
            section=args.section,
            step_id=args.step_id,
        )
    elif args.command == "action-knowledge":
        result = query_generation_action_knowledge(
            args.request_path,
            step_id=args.step_id,
            action_id=args.action_id,
            operation_names=args.operation,
            list_only=args.list,
        )
    elif args.command == "decision-media":
        result = query_generation_decision_media(
            args.request_path,
            question_id=args.question_id,
        )
    elif args.command == "technical-repair-pack":
        result = query_technical_repair_pack(
            args.request_path,
            step_id=args.step_id,
            action_id=args.action_id,
        )
    elif args.command == "technical-repair-apply":
        proposal = (
            json.loads(args.proposal_json)
            if args.proposal_json
            else json.loads(
                Path(args.proposal_file).read_text(encoding="utf-8")
            )
        )
        result = submit_technical_repair_proposal(
            args.request_path,
            proposal,
        )
    elif args.command == "intent-contract":
        result = query_generation_intent_contract()
    elif args.command == "design-contract":
        result = query_generation_design_contract()
    elif args.command == "profile-contract":
        result = query_generation_profile_contract()
    elif args.command == "admit":
        result = admit_generation_job(
            args.request_path,
            profile_id=args.generation_profile,
        )
    elif args.command == "start-job":
        result = start_generation_job(
            args.job_path,
            expected_epoch=args.expected_epoch,
        )
    elif args.command == "inspect-job":
        result = inspect_generation_job(args.job_path)
    elif args.command == "retry-job":
        result = retry_generation_job(
            args.job_path,
            profile_id=args.generation_profile,
        )
    elif args.command == "retire-job":
        result = retire_generation_job(
            args.job_path,
            reason=args.reason,
            claim_id=args.claim_id,
            expected_epoch=args.expected_epoch,
        )
    elif args.command == "design-job":
        design_value = (
            json.loads(args.design_json)
            if args.design_json
            else json.loads(
                Path(args.design_file).read_text(encoding="utf-8")
            )
        )
        result = submit_generation_job_design(
            args.job_path,
            design_value,
            claim_id=args.claim_id,
            expected_epoch=args.expected_epoch,
            note=args.note,
        )
    elif args.command == "prepare-job":
        result = prepare_generation_job(
            args.job_path,
            claim_id=args.claim_id,
            expected_epoch=args.expected_epoch,
            project_root=args.project_root,
        )
    elif args.command == "validate-job-implementation":
        result = validate_generation_job_implementation(
            args.report_path,
            claim_id=args.claim_id,
            expected_epoch=args.expected_epoch,
            project_root=args.project_root,
        )
    elif args.command == "finish-job":
        result = finish_generation_job(
            args.report_path,
            claim_id=args.claim_id,
            expected_epoch=args.expected_epoch,
            project_root=args.project_root,
            summary=args.summary,
        )
    elif args.command == "abort-job":
        result = abort_generation_job(
            args.report_path,
            reason=args.reason,
            claim_id=args.claim_id,
            expected_epoch=args.expected_epoch,
            project_root=args.project_root,
            allow_project_guard_drift=args.allow_project_guard_drift,
        )
    elif args.command == "reconcile-job-runtime":
        result = reconcile_generation_job_runtime(
            args.job_path,
            claim_id=args.claim_id,
            expected_epoch=args.expected_epoch,
        )
    elif args.command == "job-evidence":
        result = query_generation_job_evidence(
            args.job_path,
            evidence_id=args.evidence_id,
            step_id=args.step_id,
            action_id=args.action_id,
            list_only=args.list,
        )
    elif args.command == "job-compare-takes":
        result = compare_generation_job_takes(
            args.job_path,
            step_id=args.step_id,
            take_ids=args.take_id,
        )
    elif args.command == "job-action-knowledge":
        result = query_generation_job_action_knowledge(
            args.job_path,
            step_id=args.step_id,
            action_id=args.action_id,
            operation_names=args.operation,
            list_only=args.list,
        )
    elif args.command == "job-design-context":
        result = query_generation_job_design_context(
            args.job_path,
            step_id=args.step_id,
        )
    elif args.command == "job-implementation-packet":
        result = query_generation_job_implementation_packet(
            args.report_path,
            step_id=args.step_id,
            path=args.path,
        )
    else:
        request_path = Path(args.request_path).resolve()
        request = _read_json(request_path)
        session_dir = session_dir_for_request_path(request_path, request)
        started = time.perf_counter()
        state = inspect_workflow(request_path, write=True)
        workflow_ms = (time.perf_counter() - started) * 1000
        brief_pointer = state.get("brief") or {}
        brief_path = Path(brief_pointer.get("path") or "")
        if not brief_path.is_absolute():
            brief_path = (session_dir / brief_path).resolve()
        brief = (
            load_generation_brief(brief_path)
            if brief_path.is_file()
            else {}
        )
        result = {
            "workflow_version": WORKFLOW_VERSION,
            "status": "benchmarked",
            "request_id": request.get("request_id"),
            "brief_path": brief.get("brief_path"),
            "risk": state.get("risk"),
            "adjustment": brief.get("adjustment"),
            "brief_size_bytes": (
                brief_path.stat().st_size if brief_path.is_file() else None
            ),
            "workflow_ms": round(workflow_ms, 3),
            "revision_seal": (state.get("revision") or {}).get("seal"),
            "errors": [],
            "warnings": [],
        }
    print(
        _serialize_cli_result(
            result,
            full=bool(getattr(args, "full", False)),
        ),
        end="",
    )
    return 0 if result.get("status") not in {
        "blocked",
        "stale",
        "invalid",
        "rejected",
    } else 1


def _project_cli_result(result, *, full=False):
    if full:
        return _public_cli_paths(dict(result or {}))
    if _is_job_inspect_result(result):
        return _compact_job_inspect_result(result)
    if _is_job_evidence_result(result):
        return _compact_job_evidence_result(result)
    if _is_job_design_context_result(result):
        return _compact_job_design_context_result(result)
    if _is_job_implementation_packet_result(result):
        return _compact_job_implementation_packet_result(result)
    if _is_validate_job_result(result):
        return _compact_validate_job_result(result)
    if _is_prepare_job_result(result):
        return _compact_prepare_job_result(result)
    if _is_finish_job_result(result):
        return _compact_finish_job_result(result)
    projected = {
        key: result.get(key)
        for key in (
            "workflow_version",
            "evidence_query_version",
            "status",
            "next_action",
            "request_id",
            "brief_path",
            "report_path",
            "transaction_id",
            "abort",
            "risk",
            "adjustment",
            "plan_id",
            "plan_path",
            "plan_context",
            "action_knowledge",
            "decision_media",
            "decision_pack_id",
            "generation_intent_contract",
            "generation_intent_contract_fingerprint",
            "generation_design_contract",
            "generation_design_contract_fingerprint",
            "generation_design_validation_version",
            "generation_profile_registry",
            "generation_admission",
            "generation_job_service_version",
            "job_id",
            "job_path",
            "job_fingerprint",
            "generation_profile",
            "job_execution",
            "job_transition",
            "job_lifecycle_timing",
            "execution_boundary",
            "current_job",
            "last_job_result",
            "issues",
            "compiled_plan",
            "implementation_validation_version",
            "projected_transaction_status",
            "ai_editable_changes",
            "system_materialization",
            "attempt",
            "decision",
            "answers_path",
            "required_forensic_evidence",
            "changed_files",
            "validations",
            "evidence_audit",
            "generation_policy_audit",
            "pic_authorization_audit",
            "pic_usage_audit",
            "plan_conformance_audit",
            "errors",
            "warnings",
            "ai_context_budget",
            "ai_capabilities",
            "brief_size_bytes",
            "workflow_ms",
            "revision_seal",
            "revision_context",
            "context_version",
            "context_fingerprint",
            "query",
            "items",
            "item_count",
            "take_comparison_version",
            "step_id",
            "selected_take_id",
            "take_count",
            "takes",
            "differences",
            "selection_policy",
        )
        if key in result
    }
    return _public_cli_paths(projected)


def _public_cli_paths(projected):
    projected = dict(projected or {})
    for key in (
        "job_path",
        "request_path",
        "brief_path",
        "plan_path",
        "report_path",
    ):
        if projected.get(key):
            projected[key] = _public_cli_path(projected[key])
    return projected


def _is_job_inspect_result(result):
    return bool(
        isinstance(result, dict)
        and result.get("generation_job_service_version")
        and result.get("ai_context_envelope")
    )


def _is_job_evidence_result(result):
    query = (result or {}).get("query") or {}
    return bool(
        isinstance(result, dict)
        and result.get("evidence_query_version")
        and result.get("job_id")
        and not any(
            query.get(key)
            for key in ("evidence_id", "step_id", "action_id")
        )
    )


def _is_prepare_job_result(result):
    return bool(
        isinstance(result, dict)
        and result.get("status") == "running"
        and result.get("system_materialization")
        and result.get("transaction_id")
        and result.get("implementation_packet")
    )


def _is_job_design_context_result(result):
    return bool(
        isinstance(result, dict)
        and result.get("generation_design_context_query_version")
        and result.get("design_context")
        and result.get("job_id")
    )


def _is_job_implementation_packet_result(result):
    return bool(
        isinstance(result, dict)
        and result.get("implementation_packet_query_version")
        and result.get("implementation_packet")
        and result.get("transaction_id")
    )


def _is_validate_job_result(result):
    return bool(
        isinstance(result, dict)
        and result.get("implementation_validation_version")
        and result.get("transaction_id")
    )


def _is_finish_job_result(result):
    return bool(
        isinstance(result, dict)
        and result.get("transaction_id")
        and (
            "changed_files" in result
            or "execution_outcome" in result
            or "terminal_snapshot_audit" in result
        )
        and not result.get("implementation_validation_version")
    )


def _compact_job_inspect_result(result):
    envelope = result.get("ai_context_envelope") or {}
    brief = envelope.get("brief") or {}
    target = brief.get("target") or {}
    scenario = target.get("scenario") or {}
    steps = target.get("steps") or []
    actions = brief.get("actions") or []
    owners = (brief.get("window_ownership") or {}).get("windows") or []
    execution = result.get("job_execution") or {}
    projected = {
        "transport_version": "1.0",
        "transport": "compact_job_inspect",
        "status": result.get("status"),
        "next_action": result.get("next_action"),
        "request_id": result.get("request_id"),
        "job_id": result.get("job_id"),
        "job_path": result.get("job_path"),
        "brief_path": result.get("brief_path"),
        "plan_path": result.get("plan_path"),
        "generation_profile": result.get("generation_profile"),
        "job_execution": {
            key: execution.get(key)
            for key in ("phase", "epoch", "claim_id", "attempt_no")
        },
        "job_transition": result.get("job_transition") or {},
        "job_lifecycle_timing": _compact_job_lifecycle_timing(
            result.get("job_lifecycle_timing")
        ),
        "target": {
            "feature": (target.get("feature") or {}).get("name"),
            "scenario": scenario.get("name"),
            "step_count": len(steps),
            "action_count": len(actions),
            "window_count": len(owners),
        },
        "steps": [
            {
                "step_id": step.get("id"),
                "keyword": step.get("keyword"),
                "text": step.get("text"),
                "action_count": sum(
                    str(action.get("step_id") or "")
                    == str(step.get("id") or "")
                    for action in actions
                ),
            }
            for step in steps
        ],
        "ambiguity": _compact_ambiguity_counts(brief.get("ambiguities")),
        "allowed_queries": (result.get("execution_boundary") or {}).get(
            "allowed_queries"
        ) or [],
        "ai_context_budget": _compact_budget(result.get("ai_context_budget")),
        "errors": result.get("errors") or [],
        "warnings": result.get("warnings") or [],
        "full_output": "Pass --full to retrieve the unchanged full Job projection.",
    }
    if brief.get("generation_design_context_version"):
        projected["design_context"] = brief
    return _public_cli_paths(projected)


def _compact_job_evidence_result(result):
    query = result.get("query") or {}
    items = result.get("items") or []
    grouped = {}
    for item in items:
        step_id = str(item.get("step_id") or "unscoped")
        bucket = grouped.setdefault(step_id, {
            "step_id": None if step_id == "unscoped" else step_id,
            "item_count": 0,
            "kinds": {},
            "required_for_decision": 0,
        })
        bucket["item_count"] += 1
        kind = str(item.get("kind") or "unknown")
        bucket["kinds"][kind] = bucket["kinds"].get(kind, 0) + 1
        bucket["required_for_decision"] += bool(
            item.get("required_for_decision")
        )
    return _public_cli_paths({
        "transport_version": "1.0",
        "transport": "compact_job_evidence",
        "status": result.get("status"),
        "request_id": result.get("request_id"),
        "job_id": result.get("job_id"),
        "query": query,
        "item_count": result.get("item_count", len(items)),
        "steps": list(grouped.values()),
        "full_output": (
            "Use --step-id/--action-id for an exact scoped expansion or "
            "--full for the unchanged complete evidence projection."
        ),
    })


def _compact_job_design_context_result(result):
    return _public_cli_paths({
        "transport_version": "1.0",
        "transport": "compact_job_design_context",
        "status": result.get("status"),
        "request_id": result.get("request_id"),
        "job_id": result.get("job_id"),
        "job_transition": result.get("job_transition") or {},
        "query": result.get("query") or {},
        "design_context": result.get("design_context") or {},
    })


def _compact_job_implementation_packet_result(result):
    return _public_cli_paths({
        "transport_version": "1.0",
        "transport": "compact_job_implementation_packet",
        "status": result.get("status"),
        "request_id": result.get("request_id"),
        "job_id": result.get("job_id"),
        "transaction_id": result.get("transaction_id"),
        "report_path": result.get("report_path"),
        "job_transition": result.get("job_transition") or {},
        "query": result.get("query") or {},
        "implementation_packet": result.get("implementation_packet") or {},
    })


def _compact_prepare_job_result(result):
    materialization = result.get("system_materialization") or {}
    packet = result.get("implementation_packet") or {}
    ai_editable_changes = (
        result.get("ai_editable_changes")
        or packet.get("ai_editable_changes")
        or []
    )
    packet_ref = {
        "path": result.get("report_path"),
        "json_pointer": "/implementation_packet",
        "derived_from": (packet.get("derived_from") or {}).get(
            "implementation_manifest_fingerprint"
        ),
    }
    return _public_cli_paths({
        "transport_version": "1.0",
        "transport": "compact_prepare_job",
        "status": result.get("status"),
        "request_id": result.get("request_id"),
        "transaction_id": result.get("transaction_id"),
        "report_path": result.get("report_path"),
        "plan_path": result.get("plan_path"),
        "ai_editable_changes": ai_editable_changes,
        "implementation_packet_ref": packet_ref,
        "implementation_packet_summary": {
            "version": packet.get("implementation_packet_version"),
            "page_count": len(packet.get("pages") or []),
            "step_count": len(packet.get("steps") or []),
            "method_count": len(packet.get("methods") or []),
            "ai_editable_count": len(ai_editable_changes),
        },
        **(
            {"job_transition": result["job_transition"]}
            if result.get("job_transition")
            else {}
        ),
        "job_lifecycle_timing": _compact_job_lifecycle_timing(
            result.get("job_lifecycle_timing")
        ),
        "system_owned_files": materialization.get("system_owned_files") or [],
        "system_materialization_status": materialization.get("status"),
        "errors": result.get("errors") or [],
        "warnings": result.get("warnings") or [],
        "full_output": (
            "Pass --full for the complete Manifest projection, or read "
            "implementation_packet_ref from report_path for the AI code packet."
        ),
    })


def _compact_validate_job_result(result):
    materialization = result.get("system_materialization") or {}
    attempt = result.get("attempt") or {}
    return _public_cli_paths({
        "transport_version": "1.0",
        "transport": "compact_validate_job_implementation",
        "status": result.get("status"),
        "request_id": result.get("request_id"),
        "transaction_id": result.get("transaction_id"),
        "projected_transaction_status": result.get(
            "projected_transaction_status"
        ),
        "attempt": {
            "status": attempt.get("status"),
            "error_count": len(attempt.get("errors") or []),
            "warning_count": len(attempt.get("warnings") or []),
        },
        "ai_editable_changes": result.get("ai_editable_changes") or [],
        **(
            {"job_transition": result["job_transition"]}
            if result.get("job_transition")
            else {}
        ),
        "job_lifecycle_timing": _compact_job_lifecycle_timing(
            result.get("job_lifecycle_timing")
        ),
        "system_owned_files": materialization.get("system_owned_files") or [],
        "system_materialization_status": materialization.get("status"),
        "errors": result.get("errors") or attempt.get("errors") or [],
        "warnings": result.get("warnings") or attempt.get("warnings") or [],
        "full_output": (
            "Pass --full to inspect the complete implementation validation "
            "ledger entry and transaction projection."
        ),
    })


def _compact_finish_job_result(result):
    execution = result.get("execution_outcome") or {}
    terminal = result.get("terminal_snapshot_audit") or {}
    return _public_cli_paths({
        "transport_version": "1.0",
        "transport": "compact_finish_job",
        "status": result.get("status"),
        "request_id": result.get("request_id"),
        "transaction_id": result.get("transaction_id"),
        "report_path": result.get("report_path"),
        "changed_files": result.get("changed_files") or [],
        "execution_status": execution.get("status"),
        "static_status": execution.get("static_status"),
        "runtime_status": execution.get("runtime_status"),
        "terminal_snapshot_status": terminal.get("status"),
        **(
            {"job_transition": result["job_transition"]}
            if result.get("job_transition")
            else {}
        ),
        "job_lifecycle_timing": _compact_job_lifecycle_timing(
            result.get("job_lifecycle_timing")
        ),
        "errors": result.get("errors") or [],
        "warnings": result.get("warnings") or [],
        "full_output": "Pass --full to retrieve the complete terminal transaction report.",
    })


def _compact_ambiguity_counts(ambiguities):
    result = {"total": 0, "user": 0, "ai": 0, "evidence": 0}
    for item in ambiguities or ():
        result["total"] += 1
        routing = str(item.get("routing") or "")
        if routing == "user_decision_required":
            result["user"] += 1
        elif routing == "evidence_required":
            result["evidence"] += 1
        else:
            result["ai"] += 1
    return result


def _compact_budget(value):
    value = value or {}
    return {
        key: value.get(key)
        for key in (
            "status",
            "default_total_bytes",
            "target_bytes",
            "over_by_bytes",
            "enforcement",
        )
        if key in value
    }


def _compact_job_lifecycle_timing(value):
    if not isinstance(value, dict):
        return {
            "version": None,
            "status": "unavailable",
            "active_stage": None,
            "segments": [],
        }
    active = value.get("active_stage") or {}
    segments = [
        {
            key: item.get(key)
            for key in (
                "stage",
                "started_at",
                "finished_at",
                "duration_ms",
            )
            if key in item
        }
        for item in value.get("segments") or ()
        if isinstance(item, dict)
    ]
    return {
        "version": value.get("job_lifecycle_timing_ledger_version"),
        "status": value.get("status"),
        "active_stage": active.get("name"),
        "segments": segments,
    }


def _public_cli_path(value):
    path = Path(str(value))
    if not path.is_absolute():
        return path.as_posix()
    try:
        return path.resolve().relative_to(Paths.BASE_DIR.resolve()).as_posix()
    except ValueError:
        parts = path.parts
        artifact_indexes = [
            index
            for index, part in enumerate(parts)
            if part.casefold() == "artifacts"
        ]
        if artifact_indexes:
            return Path(*parts[artifact_indexes[-1]:]).as_posix()
        return path.name


def _serialize_cli_result(result, *, full=False):
    return json.dumps(
        _project_cli_result(result, full=full),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())