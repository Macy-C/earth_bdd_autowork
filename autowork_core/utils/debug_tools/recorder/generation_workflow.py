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
    load_generation_plan,
)
from autowork_core.utils.debug_tools.recorder.generation_profile import (
    generation_profile_registry,
    project_generation_admission,
)
from autowork_core.utils.debug_tools.recorder.generation_job_service import (
    admit_generation_job,
    inspect_generation_job,
    compare_generation_job_takes,
    query_generation_job_action_knowledge,
    query_generation_job_design_context,
    query_generation_job_evidence,
    query_generation_job_diff,
    query_generation_job_implementation_candidate,
    query_generation_job_implementation_packet,
    query_generation_job_task_bundle,
    submit_generation_job_business_answers,
    submit_generation_job_business_facts,
)
from autowork_core.utils.debug_tools.recorder.generation_design import (
    compact_generation_design_contract,
)
from autowork_core.utils.debug_tools.recorder.generation_orchestrator import (
    advance_generation_job,
    generate_generation_job,
    settle_generation_job,
    submit_business_review,
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


WORKFLOW_VERSION = "5.0"
AI_WORKFLOW_CONTEXT_VERSION = "1.0"
AI_CONTEXT_BUDGET_VERSION = "1.4"
AI_CONTEXT_TARGET_BYTES = 50 * 1024
TERMINAL_DIFF_FILE_PREVIEW_LIMIT = 20
TYPED_PATCH_REQUIREMENT_PREVIEW_LIMIT = 20
TYPED_PATCH_TEXT_PREVIEW_LIMIT = 180


def inspect_generation(
        request_path,
        *,
        generation_profile_id=None,
        write=True,
    ignore_current_job=False,
    ):
    request_path = Path(request_path).resolve()
    request = _read_json(request_path)
    session_dir = session_dir_for_request_path(request_path, request)
    state = inspect_workflow(
        request_path,
        write=write,
        ignore_current_job=ignore_current_job,
    )
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
        inspect_result,
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
        task_bundle = inspect_result.get("generation_task_bundle") or {}
        paged = "job-task-bundle" in set(
            ((job_value or {}).get("execution_boundary") or {}).get(
                "allowed_queries"
            )
            or ()
        )
        if not task_bundle and job_value is not None and paged:
            from autowork_core.utils.debug_tools.recorder.generation_task_bundle import (
                build_generation_task_bundle,
                project_generation_task_bundle_index,
            )
            task_manifest, _fragments = build_generation_task_bundle(
                load_generation_brief(brief_path),
                job_value,
                brief_path=Path(brief_path).resolve().relative_to(session_dir),
            )
            task_bundle = project_generation_task_bundle_index(
                task_manifest,
                transition=(inspect_result.get("job_execution") or {}),
            )
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
            generation_task_bundle=task_bundle,
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
        projected = _with_context_budget(
            inspect_result,
            _compact_budget(budget),
        )
        inspect_bytes = len(
            _serialize_cli_result(projected).encode("utf-8")
        )
        next_components = []
        for component in components:
            next_component = dict(component)
            if component["name"] == "inspect_output":
                next_component["bytes"] = inspect_bytes
                next_component["measurement"] = "compact_budget_projection"
            next_components.append(next_component)
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
        "remaining_bytes": max(0, AI_CONTEXT_TARGET_BYTES - default_total),
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
        "design-contract,profile-contract,admit,"
        "inspect-job,settle-job,"
        "generate-job,job-evidence,job-compare-takes,"
        "job-action-knowledge,job-design-context,"
        "job-task-bundle,job-implementation-packet,"
        "job-implementation-candidate,job-code-diff,benchmark"
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
    commands.add_parser("design-contract")
    commands.add_parser("profile-contract")
    admit = commands.add_parser("admit")
    admit.add_argument("request_path")
    admit.add_argument("--generation-profile")
    inspect_job = commands.add_parser("inspect-job")
    inspect_job.add_argument("job_path")
    inspect_job.add_argument("--full", action="store_true")
    settle_job = commands.add_parser("settle-job")
    settle_job.add_argument("job_path")
    submit_business = commands.add_parser("submit-business-answers")
    submit_business.add_argument("job_path")
    submit_business.add_argument("--answer", action="append", default=[])
    submit_review_answers = commands.add_parser("submit-business-review-answers")
    submit_review_answers.add_argument("job_path")
    submit_review_answers.add_argument("--answers-json")
    submit_review_answers.add_argument("--selected-option", action="append", default=[])
    submit_review_answers.add_argument("--freeform-answer", action="append", default=[])
    submit_facts = commands.add_parser("submit-business-facts")
    submit_facts.add_argument("job_path")
    submit_facts.add_argument("--freeform-answer", action="append", default=[])
    submit_facts.add_argument("--business-fact", action="append", default=[])
    submit_review = commands.add_parser("submit-business-review")
    submit_review.add_argument("job_path")
    submit_review.add_argument("--review-patch-json")
    submit_review.add_argument("--review-decision", action="append", default=[])
    submit_review.add_argument("--review-reason", action="append", default=[])
    generate_job = commands.add_parser("generate-job")
    generate_job.add_argument("job_path")
    generate_job.add_argument("--project-root")
    generate_job.add_argument("--summary", default="")
    generate_job.add_argument("--naming-patch-file")
    generate_job.add_argument("--target-name", action="append", default=[])
    generate_job.add_argument("--business-name", action="append", default=[])
    generate_job.add_argument("--ambiguity-choice", action="append", default=[])
    generate_job.add_argument("--assertion-choice", action="append", default=[])
    generate_job.add_argument("--method-choice", action="append", default=[])
    generate_job.add_argument("--operation-choice", action="append", default=[])
    generate_job.add_argument("--value-source-choice", action="append", default=[])
    advance_job = commands.add_parser("advance-job")
    advance_job.add_argument("job_path")
    advance_job.add_argument("--project-root")
    advance_job.add_argument("--summary", default="")
    advance_job.add_argument("--naming-patch-file")
    advance_job.add_argument("--target-name", action="append", default=[])
    advance_job.add_argument("--business-name", action="append", default=[])
    advance_job.add_argument("--ambiguity-choice", action="append", default=[])
    advance_job.add_argument("--assertion-choice", action="append", default=[])
    advance_job.add_argument("--method-choice", action="append", default=[])
    advance_job.add_argument("--operation-choice", action="append", default=[])
    advance_job.add_argument("--value-source-choice", action="append", default=[])
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
    job_knowledge.add_argument("--design-file")
    job_design_context = commands.add_parser("job-design-context")
    job_design_context.add_argument("job_path")
    job_design_context.add_argument("--step-id")
    job_task_bundle = commands.add_parser("job-task-bundle")
    job_task_bundle.add_argument("job_path")
    job_task_bundle.add_argument("--fragment-id")
    job_implementation_packet = commands.add_parser(
        "job-implementation-packet"
    )
    job_implementation_packet.add_argument("report_path")
    packet_selector = job_implementation_packet.add_mutually_exclusive_group()
    packet_selector.add_argument("--step-id")
    packet_selector.add_argument("--path")
    job_implementation_candidate = commands.add_parser(
        "job-implementation-candidate"
    )
    job_implementation_candidate.add_argument("report_path")
    job_implementation_candidate.add_argument("--path")
    job_implementation_candidate.add_argument("--all", action="store_true")
    job_implementation_candidate.add_argument("--full", action="store_true")
    job_code_diff = commands.add_parser("job-code-diff")
    job_code_diff.add_argument("report_path")
    job_code_diff.add_argument("--offset", type=int, default=0)
    job_code_diff.add_argument("--limit", type=int)
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
    elif args.command == "design-contract":
        result = query_generation_design_contract()
    elif args.command == "profile-contract":
        result = query_generation_profile_contract()
    elif args.command == "admit":
        result = admit_generation_job(
            args.request_path,
            profile_id=args.generation_profile,
        )
    elif args.command == "inspect-job":
        result = inspect_generation_job(args.job_path)
    elif args.command == "settle-job":
        result = settle_generation_job(args.job_path)
    elif args.command == "submit-business-answers":
        result = submit_generation_job_business_answers(
            args.job_path,
            _business_answer_selections_from_args(args.answer),
        )
    elif args.command == "submit-business-review-answers":
        from autowork_core.utils.debug_tools.recorder.generation_job_service import (
            submit_generation_job_business_review_answers,
        )
        answers_value = (
            json.loads(args.answers_json)
            if args.answers_json
            else _business_review_answers_from_args(
                args.selected_option,
                args.freeform_answer,
            )
        )
        result = submit_generation_job_business_review_answers(
            args.job_path,
            answers_value,
        )
    elif args.command == "submit-business-facts":
        result = submit_generation_job_business_facts(
            args.job_path,
            freeform_answers=_freeform_business_answers_from_args(
                args.freeform_answer,
            ),
            business_fact_patch=_business_fact_patch_from_args(
                args.business_fact,
            ),
        )
    elif args.command == "submit-business-review":
        patch_value = (
            json.loads(args.review_patch_json)
            if args.review_patch_json
            else None
        )
        result = submit_business_review(
            args.job_path,
            patch_value,
            decisions=_review_argument_map(args.review_decision),
            reasons=_review_reason_argument_map(
                args.review_reason,
                _review_argument_map(args.review_decision),
            ),
        )
    elif args.command in {"generate-job", "advance-job"}:
        design_value = None
        ambiguity_choice_patch = None
        assertion_choice_patch = None
        method_choice_patch = None
        naming_patch = None
        operation_choice_patch = None
        value_source_choice_patch = None
        naming_args = bool(args.target_name or args.business_name)
        if args.naming_patch_file and any((
                naming_args,
                args.ambiguity_choice,
                args.assertion_choice,
                args.method_choice,
                args.operation_choice,
                args.value_source_choice,
        )):
            raise ValueError("NamingPatch文件不能与直接typed patch参数混用")
        if args.ambiguity_choice:
            ambiguity_choice_patch = _ambiguity_choice_patch_from_args(
                args.ambiguity_choice,
            )
        if args.assertion_choice:
            assertion_choice_patch = _assertion_choice_patch_from_args(
                args.assertion_choice,
            )
        if args.method_choice:
            method_choice_patch = _method_choice_patch_from_args(
                args.method_choice,
            )
        if args.operation_choice:
            operation_choice_patch = _operation_choice_patch_from_args(
                args.operation_choice,
            )
        if args.value_source_choice:
            value_source_choice_patch = _value_source_choice_patch_from_args(
                args.value_source_choice,
            )
        if args.naming_patch_file:
            naming_patch = json.loads(
                Path(args.naming_patch_file).read_text(encoding="utf-8")
            )
        if naming_args:
            naming_patch = _naming_patch_from_args(
                args.target_name,
                args.business_name,
            )
        entrypoint = (
            advance_generation_job
            if args.command == "advance-job"
            else generate_generation_job
        )
        result = entrypoint(
            args.job_path,
            project_root=args.project_root,
            summary=args.summary,
            design=design_value,
            ambiguity_choice_patch=ambiguity_choice_patch,
            assertion_choice_patch=assertion_choice_patch,
            method_choice_patch=method_choice_patch,
            naming_patch=naming_patch,
            operation_choice_patch=operation_choice_patch,
            value_source_choice_patch=value_source_choice_patch,
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
        design = (
            _read_json(Path(args.design_file).resolve())
            if args.design_file
            else None
        )
        result = query_generation_job_action_knowledge(
            args.job_path,
            step_id=args.step_id,
            action_id=args.action_id,
            operation_names=args.operation,
            list_only=args.list,
            design=design,
        )
    elif args.command == "job-design-context":
        result = query_generation_job_design_context(
            args.job_path,
            step_id=args.step_id,
        )
    elif args.command == "job-task-bundle":
        result = query_generation_job_task_bundle(
            args.job_path,
            fragment_id=args.fragment_id,
        )
    elif args.command == "job-implementation-packet":
        result = query_generation_job_implementation_packet(
            args.report_path,
            step_id=args.step_id,
            path=args.path,
        )
    elif args.command == "job-implementation-candidate":
        result = query_generation_job_implementation_candidate(
            args.report_path,
            path=args.path,
            include_all=args.all,
        )
    elif args.command == "job-code-diff":
        result = query_generation_job_diff(
            args.report_path,
            offset=args.offset,
            limit=args.limit,
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
    return _cli_exit_code(result)


def _cli_exit_code(result):
    return 1 if (result or {}).get("status") in {
        "blocked",
        "aborted",
        "failed",
        "implementation_invalid",
        "stopped",
        "stale",
        "invalid",
        "rejected",
    } else 0


def _project_cli_result(result, *, full=False):
    if full:
        return _public_cli_paths(dict(result or {}))
    if (result or {}).get("status") == "business_answers_required":
        return _compact_business_answers_required_result(result)
    if _is_job_inspect_result(result):
        return _compact_job_inspect_result(result)
    if _is_job_evidence_result(result):
        return _compact_job_evidence_result(result)
    if _is_job_design_context_result(result):
        return _compact_job_design_context_result(result)
    if _is_prepare_job_design_result(result):
        return _compact_prepare_job_design_result(result)
    if _is_job_task_bundle_result(result):
        return result
    if _is_job_implementation_packet_result(result):
        return _compact_job_implementation_packet_result(result)
    if result.get("implementation_candidate_query_version"):
        return _compact_job_implementation_candidate_result(result)
    if result.get("generation_diff_query_version"):
        return result
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
            "terminal_status",
            "category",
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
            "generation_design_contract",
            "generation_design_contract_fingerprint",
            "generation_design_validation_version",
            "first_issue",
            "generation_profile_registry",
            "generation_admission",
            "generation_job_service_version",
            "job_id",
            "job_path",
            "job_fingerprint",
            "generation_workspace_projection",
            "workspace_projection_summary",
            "generation_profile",
            "workload",
            "job_execution",
            "job_transition",
            "job_lifecycle_timing",
            "orchestration_timing",
            "service_level",
            "service_level_target_seconds",
            "fragments",
            "fragment",
            "execution_boundary",
            "current_job",
            "last_job_result",
            "stages",
            "failure_summary",
            "implementation_diff_summary",
            "issues",
            "unresolved_issues",
            "compiled_plan",
            "implementation_validation_version",
            "projected_transaction_status",
            "ai_editable_changes",
            "candidate_files",
            "system_materialization",
            "workspace_candidate_status",
            "candidate_file_count",
            "candidate_mismatches",
            "binding_path",
            "design_draft",
            "design_seed",
            "naming_patch_draft",
            "naming_patch_contract",
            "naming_requirements",
            "typed_patch_requirements",
            "typed_patch_requirement_batch",
            "ambiguity_choice_patch_contract",
            "assertion_choice_patch_contract",
            "method_choice_patch_contract",
            "operation_choice_patch_contract",
            "value_source_choice_patch_contract",
            "attempt",
            "advance_summary",
            "settlement_package",
            "business_review",
            "business_review_requirement",
            "decision",
            "answers_path",
            "answer_fingerprint",
            "business_answers_submitted",
            "business_fact_count",
            "required_forensic_evidence",
            "changed_files",
            "delivery_visibility",
            "delivery_summary",
            "acceptance_summary",
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
            "decision_answer_contract",
        )
        if key in result
    }
    if result.get("business_questions"):
        projected["business_questions"] = _compact_business_questions_for_cli(
            result.get("business_questions")
        )
    if result.get("business_review_requirement"):
        projected["business_review_requirement"] = result[
            "business_review_requirement"
        ]
    elif (result.get("business_review") or {}).get("requirement"):
        projected["business_review_requirement"] = (
            result["business_review"]["requirement"]
        )
        projected["business_review"] = {
            key: value
            for key, value in (projected.get("business_review") or {}).items()
            if key != "requirement" and key != "units"
        }
        if projected["business_review"].get("questions"):
            projected["business_review"]["questions"] = (
                _compact_business_questions_for_cli(
                    projected["business_review"].get("questions")
                )
            )
    if result.get("settlement_package"):
        projected["settlement_package"] = _compact_settlement_package_for_cli(
            result.get("settlement_package")
        )
    commands = _next_commands(result)
    if commands:
        projected["next_commands"] = commands
    _compact_typed_patch_batch_for_cli(projected)
    _compact_success_delivery_for_cli(projected)
    _compact_terminal_envelope_for_cli(projected)
    return _public_cli_paths(projected)


def _compact_business_answers_required_result(result):
    result = result if isinstance(result, dict) else {}
    questions = _compact_business_questions_for_cli(
        result.get("business_questions") or []
    )
    commands = _next_commands({
        **result,
        "business_questions": result.get("business_questions") or [],
    })
    projected = {
        key: result.get(key)
        for key in (
            "generation_entrypoint_version",
            "entrypoint",
            "status",
            "next_action",
            "request_id",
            "job_id",
            "job_path",
            "job_transition",
            "decision_answer_contract",
            "business_review",
            "errors",
            "warnings",
        )
        if key in result
    }
    projected["business_question_count"] = len(questions)
    projected["business_questions"] = questions
    if commands:
        projected["next_commands"] = commands
    return _public_cli_paths(projected)


def _compact_typed_patch_batch_for_cli(projected):
    batch = projected.get("typed_patch_requirement_batch")
    if not isinstance(batch, dict):
        return
    if not (
            batch.get("status") == "required"
            and batch.get("complete") is True
    ):
        return
    requirements = []
    for requirement in batch.get("requirements") or []:
        if not isinstance(requirement, dict):
            continue
        requirements.append({
            key: requirement.get(key)
            for key in (
                "requirement_id",
                "patch_type",
                "scope",
                "submit_arguments",
                "recommended_arguments",
                "recommendation",
            )
            if requirement.get(key) not in (None, "", [], {})
        })
        if requirement.get("facts"):
            requirements[-1]["facts"] = _compact_typed_patch_facts(
                requirement.get("facts")
            )
        if requirement.get("choices"):
            requirements[-1]["choices"] = _compact_typed_patch_choices(
                requirement.get("choices")
            )
    recommended = bool(requirements) and all(
        requirement.get("recommended_arguments")
        for requirement in requirements
    )
    system_verified_unique = recommended and all(
        (requirement.get("recommendation") or {}).get("classification")
        == "system_verified_unique"
        for requirement in requirements
    )
    projected["typed_patch_requirement_batch"] = {
        "typed_patch_requirement_batch_version": batch.get(
            "typed_patch_requirement_batch_version"
        ),
        "status": batch.get("status"),
        "complete": True,
        "issue_count": batch.get("issue_count"),
        "displayed_requirement_count": min(
            len(requirements),
            TYPED_PATCH_REQUIREMENT_PREVIEW_LIMIT,
        ),
        "requirements_truncated": (
            len(requirements) > TYPED_PATCH_REQUIREMENT_PREVIEW_LIMIT
        ),
        "patch_types": [
            str(item.get("patch_type") or "")
            for item in batch.get("requirements") or ()
            if isinstance(item, dict) and item.get("patch_type")
        ],
        "recommended": recommended,
        "system_verified_unique": system_verified_unique,
        **(
            {
                "recommended_argument_count": sum(
                    len(item.get("recommended_arguments") or [])
                    for item in requirements
                ),
            }
            if recommended
            else {"requirements": requirements[:TYPED_PATCH_REQUIREMENT_PREVIEW_LIMIT]}
        ),
        "rule": (
            "This returned batch still requires Agent execution; system-verified unique batches are auto-submitted before this response. Run next_commands.primary exactly once; one concise stage progress message is allowed."
            if recommended
            else "Use requirement facts and choices; run next_commands.primary once after at most one concise stage progress message."
        ),
    }


def _compact_typed_patch_facts(facts):
    if not isinstance(facts, dict):
        return {}
    result = {}
    for key in ("step", "action", "ambiguity", "operation", "naming"):
        value = facts.get(key)
        if isinstance(value, dict):
            result[key] = {
                item_key: _preview_text(item_value)
                for item_key, item_value in value.items()
                if item_key in {
                    "step_id",
                    "action_id",
                    "ambiguity_id",
                    "code",
                    "step_text",
                    "target_display_name",
                    "control_type",
                    "root_name",
                    "evidence_name",
                    "auto_id",
                    "locator_name",
                    "locator_strategy",
                    "field",
                    "scope",
                    "submit_argument",
                    "outcome",
                }
            }
        elif value not in (None, "", [], {}):
            result[key] = _preview_text(value)
    return result


def _compact_typed_patch_choices(choices):
    result = []
    for choice in choices or []:
        if not isinstance(choice, dict):
            continue
        result.append({
            key: _preview_text(choice.get(key))
            for key in (
                "choice_key",
                "candidate_key",
                "candidate_id",
                "outcome",
                "label",
                "operation",
                "source",
                "authority",
                "effect",
            )
            if choice.get(key) not in (None, "", [], {})
        })
    return result


def _preview_text(value):
    text = str(value)
    if len(text) <= TYPED_PATCH_TEXT_PREVIEW_LIMIT:
        return value
    return text[:TYPED_PATCH_TEXT_PREVIEW_LIMIT] + "..."


def _compact_success_delivery_for_cli(projected):
    status = str(projected.get("status") or "")
    terminal_status = str(projected.get("terminal_status") or "")
    if not (
            status in {"completed", "completed_no_changes"}
            or (
                status == "terminal"
                and terminal_status in {"completed", "completed_no_changes"}
            )
    ):
        return
    projected.pop("changed_files", None)
    projected.pop("stages", None)
    for key in ("delivery_visibility", "delivery_summary"):
        value = projected.get(key)
        if isinstance(value, dict):
            value.pop("changed_files", None)
    if "final_response_summary" not in projected:
        projected["final_response_summary"] = _final_response_summary(
            projected,
        )


def _compact_terminal_envelope_for_cli(projected):
    status = str(projected.get("status") or "")
    terminal_status = str(projected.get("terminal_status") or "")
    if not (
            status in {"completed", "completed_no_changes", "failed"}
            or status == "terminal"
            or terminal_status in {"completed", "completed_no_changes", "failed"}
    ):
        return
    projected["implementation_diff_summary"] = _bounded_implementation_diff_summary(
        projected.get("implementation_diff_summary") or {}
    )
    projected["terminal_result"] = _terminal_result_envelope(projected)
    for key in (
            "abort",
            "acceptance_summary",
            "advance_summary",
            "agent_tool_timing",
            "delivery_summary",
            "delivery_visibility",
            "final_response_summary",
            "generation_progress",
            "generation_workspace_projection",
            "generation_timing_summary",
            "git_diff_visibility",
            "health_issues",
            "health_status",
            "implementation_diff",
            "job_lifecycle_timing",
            "last_job_result",
            "orchestration_timing",
            "report_path",
            "service_level",
            "static_status",
            "system_materialization_commit",
            "system_materialization_status",
            "terminal_snapshot_status",
            "transaction_id",
            "workspace_projection_summary",
            "job_lifecycle_timing",
            "job_execution",
            "generation_timing_ledger",
            "changed_files",
            "stages",
    ):
        projected.pop(key, None)


def _bounded_implementation_diff_summary(summary):
    summary = dict(summary or {})
    files = [
        dict(item)
        for item in summary.get("files") or []
        if isinstance(item, dict)
    ]
    total = int(
        summary.get("total_file_count")
        or summary.get("file_count")
        or len(files)
        or 0
    )
    preview = files[:TERMINAL_DIFF_FILE_PREVIEW_LIMIT]
    summary["file_count"] = summary.get("file_count", total)
    summary["total_file_count"] = total
    summary["displayed_file_count"] = len(preview)
    summary["files_truncated"] = total > len(preview)
    summary["files"] = preview
    return summary


def _terminal_result_envelope(projected):
    final_summary = projected.get("final_response_summary") or _final_response_summary(
        projected,
    )
    delivery = projected.get("delivery_summary") or {}
    visibility = projected.get("delivery_visibility") or {}
    acceptance = projected.get("acceptance_summary") or {}
    service_level = projected.get("service_level") or {}
    diff_summary = projected.get("implementation_diff_summary") or {}
    issues = list(projected.get("unresolved_issues") or [])
    errors = list(projected.get("errors") or [])
    warnings = list(projected.get("warnings") or [])
    failure = projected.get("failure_summary") or {}
    return {
        "terminal_result_version": "1.0",
        "status": final_summary.get("status") or projected.get("status"),
        "category": final_summary.get("category") or projected.get("category"),
        "job_id": projected.get("job_id"),
        "request_id": projected.get("request_id"),
        "static_status": final_summary.get("static_status"),
        "runtime_status": final_summary.get("runtime_status"),
        "acceptance_status": acceptance.get("acceptance_status"),
        "user_review_status": final_summary.get("user_review_status"),
        "delivery_write_channel": final_summary.get("delivery_write_channel"),
        "review_channel": final_summary.get("review_channel"),
        "delivery_source": delivery.get("delivery_source") or visibility.get("source"),
        "host_native_edit_available": bool(
            visibility.get("host_native_edit_available")
        ),
        "requires_explicit_diff_review": bool(
            visibility.get("requires_explicit_diff_review")
        ),
        "changed_file_count": final_summary.get("changed_file_count"),
        "change_summary": final_summary.get("change_summary"),
        "diff_file_count": diff_summary.get("total_file_count"),
        "diff_displayed_file_count": diff_summary.get("displayed_file_count"),
        "diff_files_truncated": bool(diff_summary.get("files_truncated")),
        "service_level_status": final_summary.get("service_level_status"),
        "service_level_target_seconds": service_level.get("target_seconds"),
        "issue_count": len(issues),
        "first_issue": issues[0] if issues else None,
        "failure_summary": failure or None,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "implementation_diff_path": final_summary.get("implementation_diff_path"),
        "report_path": final_summary.get("report_path"),
    }


def _final_response_summary(projected):
    delivery = projected.get("delivery_summary") or {}
    visibility = projected.get("delivery_visibility") or {}
    acceptance = projected.get("acceptance_summary") or {}
    service_level = projected.get("service_level") or {}
    git_visibility = projected.get("git_diff_visibility") or {}
    issues = list(projected.get("unresolved_issues") or [])
    first_issue = issues[0] if issues else {}
    return {
        "status": delivery.get("status") or projected.get("status"),
        "category": projected.get("category") or delivery.get("category"),
        "change_summary": (
            (projected.get("implementation_diff_summary") or {}).get("summary")
        ),
        "delivery_write_channel": (
            visibility.get("delivery_write_channel")
            or delivery.get("delivery_write_channel")
        ),
        "review_channel": visibility.get("review_channel") or delivery.get(
            "review_channel"
        ),
        "changed_file_count": delivery.get("changed_file_count"),
        "static_status": acceptance.get("static_status") or delivery.get(
            "static_status"
        ),
        "runtime_status": acceptance.get("runtime_status") or delivery.get(
            "runtime_status"
        ),
        "user_review_status": acceptance.get("user_review_status"),
        "service_level_status": service_level.get("status") or delivery.get(
            "service_level_status"
        ),
        "git_diff_visibility_status": git_visibility.get("status"),
        "source_control_diff_ready": bool(
            git_visibility.get("status") == "applied"
            and git_visibility.get("files")
        ),
        "report_path": delivery.get("report_path") or projected.get(
            "report_path"
        ),
        "implementation_diff_path": delivery.get(
            "implementation_diff_path"
        ) or visibility.get("implementation_diff_path") or (
            (projected.get("implementation_diff") or {}).get("path")
        ),
        "issue_count": len(issues),
        "first_issue_type": first_issue.get("issue_type"),
    }


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
        and result.get("status") in {"running", "implementation_required"}
        and result.get("system_materialization")
        and result.get("transaction_id")
        and result.get("report_path")
    )


def _is_prepare_job_design_result(result):
    return bool(
        isinstance(result, dict)
        and result.get("recorder_host_control_version")
        and result.get("status") == "prepared"
        and (result.get("design_draft") or {}).get("path")
    )


def _is_job_design_context_result(result):
    return bool(
        isinstance(result, dict)
        and result.get("generation_design_context_query_version")
        and result.get("design_context")
        and result.get("job_id")
    )


def _is_job_task_bundle_result(result):
    return bool(
        isinstance(result, dict)
        and result.get("generation_task_bundle_query_version")
        and result.get("job_id")
        and (
            result.get("generation_task_bundle")
            or result.get("fragment")
        )
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
        "category": result.get("category"),
        "next_action": result.get("next_action"),
        "request_id": result.get("request_id"),
        "job_id": result.get("job_id"),
        "job_path": result.get("job_path"),
        "workload": (envelope.get("job") or {}).get("workload") or {},
        "brief_path": result.get("brief_path"),
        "plan_path": result.get("plan_path"),
        "generation_profile": result.get("generation_profile"),
        "job_execution": {
            key: execution.get(key)
            for key in ("phase", "epoch", "claim_id", "attempt_no")
        },
        "job_transition": result.get("job_transition") or {},
        "last_job_result": result.get("last_job_result") or None,
        "failure_summary": result.get("failure_summary") or None,
        "stages": result.get("stages") or {},
        "first_issue": result.get("first_issue") or None,
        "service_level": result.get("service_level"),
        "health_status": (
            "attention_required" if result.get("health_issues") else "ok"
        ),
        "health_issues": result.get("health_issues") or [],
        "implementation_diff": result.get("implementation_diff"),
        "delivery_visibility": result.get("delivery_visibility") or {},
        "delivery_summary": result.get("delivery_summary") or {},
        "acceptance_summary": result.get("acceptance_summary") or {},
        "generation_workspace_projection": result.get(
            "generation_workspace_projection"
        ) or {},
        "workspace_projection_summary": result.get(
            "workspace_projection_summary"
        ) or {},
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
        "generation_task_bundle": result.get("generation_task_bundle") or {},
        "business_questions": _compact_business_questions_for_cli(
            result.get("business_questions") or []
        ),
        "errors": result.get("errors") or [],
        "warnings": result.get("warnings") or [],
        "full_output": "Pass --full to retrieve the unchanged full Job projection.",
    }
    if brief.get("generation_design_context_version"):
        projected["design_context"] = brief
    _compact_success_delivery_for_cli(projected)
    return _public_cli_paths(projected)


def _compact_business_questions_for_cli(questions):
    result = []
    for question in questions or ():
        if not isinstance(question, dict):
            continue
        media = question.get("media") or {}
        links = _business_question_media_links(media)
        if isinstance(media, list):
            media_status = "available" if links else None
            link_count = len(links)
        elif isinstance(media, dict):
            media_status = (media.get("degradation") or {}).get(
                "media_status"
            )
            link_count = len(links)
        else:
            media_status = None
            link_count = 0
        result.append({
            "question_id": question.get("question_id"),
            "question_type": question.get("question_type") or question.get("type"),
            "title": question.get("title"),
            "prompt": question.get("prompt") or question.get("question"),
            "step_id": question.get("step_id"),
            "step_text": question.get("step_text"),
            "blocking": question.get("blocking"),
            "option_count": len(question.get("options") or []),
            "options": [
                {
                    "option_id": option.get("option_id") or option.get("value"),
                    "label": option.get("label"),
                }
                for option in question.get("options") or ()
                if isinstance(option, dict)
            ],
            "media": {
                "media_status": media_status,
                "link_count": link_count,
                "target_region_available": bool(
                    _observed_target_overlay(media)
                ),
            },
            "screenshot_count": len(_business_question_screenshot_links(question)),
        })
    return result


def _business_question_media_links(media):
    if isinstance(media, dict):
        return media.get("links") or _media_links_from_frames(
            media.get("frames") or []
        )
    if isinstance(media, list):
        links = []
        for item in media:
            if not isinstance(item, dict) or not item.get("path"):
                continue
            role = item.get("role") or item.get("frame_role")
            links.append({
                "role": str(role or ""),
                "label": item.get("label") or _media_frame_label(role),
                "path": item.get("path"),
                "sha256": item.get("sha256"),
                "size": item.get("size"),
                "action_id": item.get("action_id"),
                "evidence_id": item.get("evidence_id"),
                "verification_status": (
                    item.get("verification_status") or "verified"
                ),
            })
        return links
    return []


def _media_links_from_frames(frames):
    result = []
    for frame in frames or ():
        if not isinstance(frame, dict) or not frame.get("path"):
            continue
        result.append({
            "role": str(frame.get("frame_role") or ""),
            "label": _media_frame_label(frame.get("frame_role")),
            "path": frame.get("path"),
            "sha256": frame.get("sha256"),
            "size": frame.get("size"),
            "action_id": frame.get("action_id"),
            "evidence_id": frame.get("evidence_id"),
            "verification_status": frame.get("verification_status"),
        })
    return result


def _media_frame_label(role):
    labels = {
        "before": "操作前截图",
        "after": "操作后截图",
        "context": "上下文截图",
    }
    return labels.get(str(role or ""), "动作截图")


def _review_argument_map(values):
    result = {}
    for value in values or ():
        text = str(value or "")
        if "=" not in text:
            raise ValueError("BusinessReview参数必须使用 unit_id=value")
        unit_id, assigned = text.split("=", 1)
        unit_id = unit_id.strip()
        assigned = assigned.strip()
        if not unit_id or not assigned:
            raise ValueError("BusinessReview参数缺少unit_id或value")
        if unit_id in result:
            raise ValueError(f"BusinessReview参数重复: {unit_id}")
        result[unit_id] = assigned
    return result


def _review_reason_argument_map(values, decisions):
    values = list(values or [])
    if len(values) == 1 and "=" not in str(values[0] or ""):
        decision_ids = list((decisions or {}).keys())
        if len(decision_ids) != 1:
            raise ValueError(
                "BusinessReview单独reason文本只允许用于单个Unit"
            )
        reason = str(values[0] or "").strip()
        if not reason:
            raise ValueError("BusinessReview reason不能为空")
        return {decision_ids[0]: reason}
    return _review_argument_map(values)


def _compact_settlement_package_for_cli(package):
    if not isinstance(package, dict):
        return package
    value = dict(package)
    questions = package.get("business_questions") or []
    value["business_question_count"] = len(questions)
    value["business_questions"] = _compact_business_questions_for_cli(
        questions
    )
    return value


def _business_answer_selections_from_args(values):
    result = {}
    for value in values or ():
        text = str(value or "")
        if "=" not in text:
            raise ValueError("业务回答必须使用 question_id=option_id")
        question_id, option_id = text.split("=", 1)
        question_id = question_id.strip()
        option_id = option_id.strip()
        if not question_id or not option_id:
            raise ValueError("业务回答缺少 question_id 或 option_id")
        if question_id in result:
            raise ValueError(f"业务问题不能重复回答: {question_id}")
        result[question_id] = option_id
    return result


def _business_answer_ask_questions_payload(questions):
    return {
        "tool": "vscode_askQuestions",
        "batch_policy": "single_call_all_questions",
        "questions": [
            {
                "header": str(question.get("question_id") or ""),
                "question": str(
                    question.get("prompt") or question.get("title") or ""
                ),
                "message": _business_question_message(question),
                "allowFreeformInput": _business_question_allows_freeform(question),
                "multiSelect": False,
                "options": [
                    {
                        "label": str(option.get("label") or ""),
                        "description": str(option.get("option_id") or ""),
                    }
                    for option in question.get("options") or ()
                    if isinstance(option, dict)
                ],
            }
            for question in questions or ()
            if isinstance(question, dict)
        ],
        "selection_map": {
            str(question.get("question_id") or ""): {
                str(option.get("label") or ""): str(
                    option.get("option_id") or ""
                )
                for option in question.get("options") or ()
                if isinstance(option, dict)
            }
            for question in questions or ()
            if isinstance(question, dict) and question.get("question_id")
        },
        "agent_mapping": {
            "selected_label": "selection_map[question_id][label] -> option_id",
            "freeText_nonempty": "submit --freeform-answer and do not also submit selected option for that question",
            "freeform": "backend_creates_user_declared_literal_business_fact_patch",
            "submit": "submit-business-review-answers",
        },
    }


def _business_question_allows_freeform(question):
    if not isinstance(question, dict) or question.get("allow_freeform") is not True:
        return False
    first_option = next((
        option for option in question.get("options") or ()
        if isinstance(option, dict)
    ), {})
    fact = first_option.get("business_fact") or {}
    applies_to = fact.get("applies_to") or {}
    return bool(
        fact.get("fact_type") == "value_authority"
        and applies_to.get("scope") == "action_value"
        and applies_to.get("action_id")
    )


def _business_question_message(question):
    parts = []
    step_text = str(question.get("step_text") or "").strip()
    if step_text:
        parts.append(f"Step: {step_text}")
    media = question.get("media") or {}
    links = _business_question_screenshot_links(question)
    if not links:
        links = _business_question_media_links(media)
    if not isinstance(media, dict) and not links:
        return "\n\n".join(parts)
    media_lines = []
    for link in links:
        if not isinstance(link, dict) or not link.get("path"):
            continue
        role = str(link.get("role") or "")
        label = str(link.get("label") or _media_frame_label(role))
        path = _media_file_uri(link.get("path"))
        status = str(link.get("verification_status") or "")
        digest = str(link.get("sha256") or "")
        suffix = []
        if status:
            suffix.append(status)
        if digest:
            suffix.append(f"sha256={digest[:12]}")
        media_lines.append(
            f"- {label}: [打开截图]({path})"
            + (f" ({', '.join(suffix)})" if suffix else "")
        )
    if media_lines:
        parts.append("动作证据:\n" + "\n".join(media_lines))
    elif isinstance(media, dict):
        degradation = media.get("degradation") or {}
        media_status = str(degradation.get("media_status") or "unavailable")
        reasons = [
            str(reason) for reason in degradation.get("reasons") or ()
            if reason
        ]
        parts.append(
            "动作证据: 截图证据不可用"
            f" (status={media_status}"
            + (f", reasons={', '.join(reasons)}" if reasons else "")
            + ")"
        )
    target_overlay = _observed_target_overlay(media)
    if target_overlay:
        parts.append(
            "目标区域: "
            + ",".join(str(value) for value in target_overlay["coordinates"])
        )
    return "\n\n".join(parts)


def _business_question_screenshot_links(question):
    screenshots = question.get("screenshots") or {}
    if not isinstance(screenshots, dict):
        return []
    links = []
    for role in ("before", "after"):
        path = screenshots.get(role)
        if path:
            links.append({
                "role": role,
                "label": _media_frame_label(role),
                "path": path,
            })
    return links


def _observed_target_overlay(media):
    if not isinstance(media, dict):
        return None
    return next((
        overlay for overlay in media.get("overlays") or []
        if isinstance(overlay, dict)
        and overlay.get("kind") == "observed_target"
        and overlay.get("coordinates")
    ), None)


def _media_file_uri(path):
    path = Path(str(path or ""))
    if not path.is_absolute():
        path = Paths.BASE_DIR / path
    return path.resolve().as_uri()


def _business_answer_argument_contract(questions):
    return {
        "rule": "append_exactly_one_answer_argument_per_question",
        "selected_option_argument": "--selected-option question_id=option_id",
        "freeform_argument": "--freeform-answer question_id=value",
        "questions": [
            {
                "question_id": str(question.get("question_id") or ""),
                "selected_option": [
                    "--selected-option",
                    f"{question.get('question_id')}=<option_id>",
                ],
                "freeform": [
                    "--freeform-answer",
                    f"{question.get('question_id')}=<freeform_value>",
                ] if _business_question_allows_freeform(question) else None,
                "freeform_allowed": _business_question_allows_freeform(question),
                "exclusive": True,
            }
            for question in questions or ()
            if isinstance(question, dict) and question.get("question_id")
        ],
    }


def _business_review_answers_from_args(selected_values, freeform_values):
    selected_map = _argument_map(
        selected_values,
        "业务选项回答",
    )
    freeform_map = _argument_map(
        freeform_values,
        "业务自由回答",
    )
    overlap = sorted(set(selected_map) & set(freeform_map))
    if overlap:
        raise ValueError(f"同一业务问题不能同时选择选项和自由回答: {overlap}")
    selected = []
    for question_id, option_id in selected_map.items():
        selected.append({
            "question_id": question_id,
            "option_id": option_id,
        })
    freeform = []
    for question_id, answer in freeform_map.items():
        freeform.append({
            "question_id": question_id,
            "answer": answer,
        })
    return {
        "selected_options": selected,
        "freeform_answers": freeform,
    }


def _argument_map(values, label):
    result = {}
    for value in values or ():
        text = str(value or "")
        if "=" not in text:
            raise ValueError(f"{label}必须使用 question_id=value")
        key, assigned = text.split("=", 1)
        key = key.strip()
        assigned = assigned.strip()
        if not key or not assigned:
            raise ValueError(f"{label}缺少question_id或value")
        if key in result:
            raise ValueError(f"{label}重复: {key}")
        result[key] = assigned
    return result


def _freeform_business_answers_from_args(values):
    result = []
    seen = set()
    for value in values or ():
        text = str(value or "")
        if "=" not in text:
            raise ValueError(
                "自由业务回答必须使用 answer_id=step_id::question::answer"
            )
        answer_id, payload = text.split("=", 1)
        parts = payload.split("::", 2)
        if len(parts) != 3:
            raise ValueError(
                "自由业务回答必须使用 answer_id=step_id::question::answer"
            )
        answer_id = answer_id.strip()
        if not answer_id or answer_id in seen:
            raise ValueError(f"自由业务回答 answer_id无效或重复: {answer_id}")
        seen.add(answer_id)
        result.append({
            "answer_id": answer_id,
            "step_id": parts[0].strip(),
            "question": parts[1].strip(),
            "answer": parts[2].strip(),
        })
    return result


def _business_fact_patch_from_args(values):
    facts = []
    for value in values or ():
        parts = str(value or "").split("::", 5)
        if len(parts) != 6:
            raise ValueError(
                "业务事实必须使用 step_id::source_answer_id::fact_type::fact_value::scope::reason"
            )
        facts.append({
            "step_id": parts[0].strip(),
            "source_answer_id": parts[1].strip(),
            "fact_type": parts[2].strip(),
            "fact_value": parts[3].strip(),
            "applies_to": {"scope": parts[4].strip() or "step"},
            "reason": parts[5].strip(),
        })
    return {
        "business_fact_patch_version": "1.0",
        "patch_type": "business_facts",
        "facts": facts,
    }


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
        "reason": result.get("reason"),
        "next_action": result.get("next_action"),
        "request_id": result.get("request_id"),
        "job_id": result.get("job_id"),
        "job_transition": result.get("job_transition") or {},
        "query": result.get("query") or {},
        "typed_patch": result.get("typed_patch") or {},
        "design_context": result.get("design_context") or {},
        "errors": result.get("errors") or [],
        "warnings": result.get("warnings") or [],
    })


def _compact_prepare_job_design_result(result):
    return {
        "transport_version": "1.0",
        "transport": "compact_prepare_job_design",
        "status": result.get("status"),
        "next_action": result.get("next_action"),
        "request_id": result.get("request_id"),
        "job_id": result.get("job_id"),
        "job_transition": result.get("job_transition") or {},
        "binding_path": result.get("binding_path"),
        "design_draft": result.get("design_draft") or {},
        "design_seed": result.get("design_seed") or {},
        "errors": result.get("errors") or [],
        "warnings": result.get("warnings") or [],
    }


def _compact_job_implementation_candidate_result(result, *, inline_payload=False):
    files = []
    for item in result.get("files") or []:
        if not isinstance(item, dict):
            continue
        content_included = "content" in item
        projected = {
            "path": item.get("path"),
            "target_path": item.get("target_path") or item.get("path"),
            "operation": item.get("operation") or (
                "replace_file" if item.get("before_exists") else "create_file"
            ),
            "expected_sha256": item.get("expected_sha256") or item.get("sha256"),
        }
        projected["before_exists"] = bool(item.get("before_exists"))
        projected["before_sha256"] = item.get("before_sha256")
        if content_included:
            projected["content_included"] = True
            projected["content"] = item.get("content")
            if item.get("content_range"):
                projected["content_range"] = item.get("content_range")
        else:
            projected["source_workspace_path"] = item.get("source_workspace_path")
            projected["source_absolute_path"] = item.get("source_absolute_path")
        files.append(projected)
    projected = {
        "transport_version": "1.0",
        "transport": "compact_implementation_candidate",
        "candidate_fingerprint": result.get("candidate_fingerprint"),
        "file_count": result.get("file_count"),
        "returned_file_count": len(files),
        "total_content_bytes": result.get("total_content_bytes"),
        "all_included": bool(result.get("all_included")),
        "artifact_role": "internal_diagnostic_candidate_artifact",
        "operation_field": "files[].operation",
        "source_field": "files[].source_workspace_path",
        **(
            {"candidate_manifest": result["candidate_manifest"]}
            if result.get("candidate_manifest")
            else {}
        ),
        "files": files,
    }
    projected.update({
        "implementation_candidate_query_version": result.get(
            "implementation_candidate_query_version"
        ),
        "status": result.get("status"),
        "request_id": result.get("request_id"),
        "job_id": result.get("job_id"),
        "transaction_id": result.get("transaction_id"),
        "report_path": result.get("report_path"),
        "job_transition": result.get("job_transition") or {},
        "query": result.get("query") or {},
        "normal_delivery_write_channel": "system_materializer",
        "normal_review_channel": "implementation_diff",
        "do_not": [
            "do_not_use_as_delivery_plan",
            "do_not_copy_candidate_files_with_agent_editor_edit",
            "do_not_run_after_native_edit_for_delivery",
        ],
        "full_output": (
            "Pass --full to retrieve unchanged diagnostic source path details."
        ),
    })
    return _public_cli_paths(projected)


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
    if _scope_guard_errors(result):
        return _compact_prepare_job_scope_guard_result(result)
    materialization = result.get("system_materialization") or {}
    packet = result.get("implementation_packet") or {}
    implementation_boundary = _implementation_boundary(result)
    ai_editable_changes = (
        result.get("ai_editable_changes")
        or packet.get("ai_editable_changes")
        or []
    )
    system_owned_files = materialization.get("system_owned_files") or []
    native_edit_candidate_files = []
    packet_ref = {
        "path": result.get("report_path"),
        "json_pointer": "/implementation_packet",
        "derived_from": (packet.get("derived_from") or {}).get(
            "implementation_manifest_fingerprint"
        ),
    }
    projected = {
        "transport_version": "1.0",
        "transport": "compact_prepare_job",
        "entrypoint": result.get("entrypoint"),
        "status": result.get("status"),
        "request_id": result.get("request_id"),
        "job_path": result.get("job_path"),
        "transaction_id": result.get("transaction_id"),
        "report_path": result.get("report_path"),
        "plan_path": result.get("plan_path"),
        "ai_editable_change_count": len(ai_editable_changes),
        "system_owned_file_count": len(system_owned_files),
        "implementation_boundary": implementation_boundary,
        "implementation_boundary_reason": result.get(
            "implementation_boundary_reason"
        ),
        "native_edit_candidate_file_count": len(native_edit_candidate_files),
        "candidate_file_count": len(native_edit_candidate_files),
        "workspace_projection_summary": result.get(
            "workspace_projection_summary"
        ) or {},
        "implementation_packet_ref": packet_ref,
        "implementation_packet_summary": {
            "version": packet.get("implementation_packet_version"),
            "page_count": len(packet.get("pages") or []),
            "step_count": len(packet.get("steps") or []),
            "method_count": len(packet.get("methods") or []),
            "ai_editable_count": len(ai_editable_changes),
            "native_edit_candidate_count": len(native_edit_candidate_files),
        },
        **(
            {"job_transition": result["job_transition"]}
            if result.get("job_transition")
            else {}
        ),
        "job_lifecycle_timing": _compact_job_lifecycle_timing(
            result.get("job_lifecycle_timing")
        ),
        "orchestration_timing": result.get("orchestration_timing") or {},
        "advance_summary": result.get("advance_summary") or {},
        "generation_progress": _generation_progress_projection(result),
        "next_commands": _next_commands(result),
        "system_materialization_status": materialization.get("status"),
        "errors": result.get("errors") or [],
        "warnings": result.get("warnings") or [],
        "details": _implementation_required_details(
            implementation_boundary,
        ),
    }
    return _public_cli_paths(projected)


def _generation_progress_projection(result):
    result = result if isinstance(result, dict) else {}
    status = str(result.get("status") or "")
    transition = result.get("job_transition") or {}
    advance_summary = result.get("advance_summary") or {}
    value = {
        "generation_progress_version": "1.0",
        "status": status,
        "job_id": result.get("job_id"),
        "transaction_id": result.get("transaction_id"),
        "phase": transition.get("phase") or _progress_phase(status),
        "current_stage": _progress_stage(result),
        "next_visible_action": _progress_next_visible_action(result),
    }
    if advance_summary.get("blocked_on"):
        value["blocked_on"] = advance_summary.get("blocked_on")
    boundary = _implementation_boundary(result)
    candidate_index = result.get("candidate_index") or {}
    if (
            boundary in {"explicit_native_edit", "candidate_mismatch"}
            and isinstance(candidate_index, dict)
            and candidate_index
    ):
        line_count = _progress_int(candidate_index.get("line_count")) or 0
        max_lines = _progress_int(candidate_index.get("max_lines_per_read")) or 0
        window_count = (
            (line_count + max_lines - 1) // max_lines
            if line_count and max_lines
            else 0
        )
        value["candidate_index"] = {
            "status": "ready",
            "path": candidate_index.get("path"),
            "line_count": line_count,
            "file_count": line_count,
            "max_lines_per_read": max_lines,
            "window_count": window_count,
            "recommended_read": candidate_index.get("recommended_read") or {},
            "read_strategy": candidate_index.get("read_strategy"),
        }
    agent_timing = _progress_agent_tool_timing(result)
    if agent_timing:
        value["agent_wait"] = agent_timing
    return {
        key: item
        for key, item in value.items()
        if item is not None
    }


def _progress_phase(status):
    if status in {"completed", "completed_no_changes"}:
        return "completed"
    if status == "failed":
        return "failed"
    if status == "implementation_required":
        return "implementation"
    return status or "unknown"


def _progress_stage(result):
    status = str(result.get("status") or "")
    if status in {"completed", "completed_no_changes"}:
        return "completed"
    if status == "failed":
        return "failed"
    boundary = _implementation_boundary(result)
    if status == "implementation_required":
        return boundary
    return status or "unknown"


def _progress_next_visible_action(result):
    status = str(result.get("status") or "")
    if status in {"completed", "completed_no_changes"}:
        return "review_result"
    if status == "failed":
        return "review_failure"
    boundary = _implementation_boundary(result)
    if status == "implementation_required":
        return result.get("next_action") or boundary
    next_commands = result.get("next_commands") or {}
    primary = next_commands.get("primary") or {}
    return primary.get("purpose") or result.get("next_action")


def _progress_agent_tool_timing(result):
    timing_ledger = result.get("generation_timing_ledger") or {}
    agent_tool_timing = timing_ledger.get("agent_tool_timing") or {}
    if not agent_tool_timing:
        agent_tool_timing = result.get("agent_tool_timing") or {}
    if not agent_tool_timing:
        return None
    summary = agent_tool_timing.get("summary") or {}
    ledger_summary = timing_ledger.get("summary") or {}
    by_tool_kind = summary.get("by_tool_kind") or {}
    by_wait_segment = summary.get("by_wait_segment") or {}
    visible_stages = {}
    for tool_kind in (
            "typed_patch_submit",
            "candidate_index_read",
            "source_reads",
            "editor_edits",
            "after_native_edit",
    ):
        bucket = by_tool_kind.get(tool_kind)
        if isinstance(bucket, dict):
            visible_stages[tool_kind] = {
                key: bucket.get(key)
                for key in ("request_count", "agent_wait_ms")
                if bucket.get(key) is not None
            }
    visible_wait_segments = {}
    for segment, bucket in by_wait_segment.items():
        if isinstance(bucket, dict):
            visible_wait_segments[segment] = {
                key: bucket.get(key)
                for key in ("request_count", "agent_wait_ms")
                if bucket.get(key) is not None
            }
    value = {
        "status": agent_tool_timing.get("status")
        or ledger_summary.get("agent_tool_timing_status"),
        "source": agent_tool_timing.get("source"),
        "event_count": agent_tool_timing.get("event_count"),
        "max_wait_ms": summary.get("max_agent_wait_ms")
        or ledger_summary.get("agent_tool_max_wait_ms"),
        "max_wait_tool_kind": summary.get("max_agent_wait_tool_kind")
        or ledger_summary.get("agent_tool_max_wait_kind"),
        "max_wait_segment": summary.get("max_agent_wait_segment")
        or ledger_summary.get("agent_tool_max_wait_segment"),
        "editor_edits_done_to_after_native_edit_request_ms": (
            summary.get("editor_edits_done_to_after_native_edit_request_ms")
            or ledger_summary.get(
                "editor_edits_done_to_after_native_edit_request_ms"
            )
        ),
        "observed_tool_stages": visible_stages,
        "observed_wait_segments": visible_wait_segments,
    }
    return {
        key: item
        for key, item in value.items()
        if item not in (None, {})
    }


def _progress_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


def _compact_prepare_job_scope_guard_result(result):
    candidate_audit = result.get("workspace_candidate_audit") or {}
    return _public_cli_paths({
        "transport_version": "1.0",
        "transport": "compact_prepare_job",
        "entrypoint": result.get("entrypoint"),
        "status": result.get("status"),
        "request_id": result.get("request_id"),
        "job_path": result.get("job_path"),
        "transaction_id": result.get("transaction_id"),
        "report_path": result.get("report_path"),
        "projected_transaction_status": result.get(
            "projected_transaction_status"
        ),
        **(
            {"job_transition": result["job_transition"]}
            if result.get("job_transition")
            else {}
        ),
        "workspace_candidate_status": result.get("workspace_candidate_status")
        or candidate_audit.get("status"),
        "candidate_file_count": result.get("candidate_file_count")
        or len(candidate_audit.get("files") or []),
        "scope_guard_errors": _scope_guard_errors(result),
        "first_issue": result.get("first_issue") or None,
        "next_commands": _next_commands(result),
        "errors": result.get("errors") or [],
        "warnings": result.get("warnings") or [],
        "full_output": (
            "Pass --full for lifecycle timing, candidate detail, and the full "
            "validation ledger reference."
        ),
    })


def _compact_validate_job_result(result):
    materialization = result.get("system_materialization") or {}
    candidate_audit = result.get("workspace_candidate_audit") or {}
    candidate_mismatches = [
        {
            "path": item.get("path"),
            "status": item.get("status"),
            "comparison": item.get("comparison"),
            "expected_sha256": item.get("expected_sha256"),
            "actual_sha256": item.get("actual_sha256"),
        }
        for item in candidate_audit.get("files") or []
        if item.get("status") != "matches"
    ]
    attempt = result.get("attempt") or {}
    issues = list(attempt.get("issues") or result.get("issues") or ())
    projected = {
        "transport_version": "1.0",
        "transport": "compact_validate_job_implementation",
        "status": result.get("status"),
        "request_id": result.get("request_id"),
        "transaction_id": result.get("transaction_id"),
        "report_path": result.get("report_path"),
        "job_path": result.get("job_path"),
        "projected_transaction_status": result.get(
            "projected_transaction_status"
        ),
        "attempt": {
            "status": attempt.get("status"),
            "error_count": len(issues),
            "issue_count": len(issues),
            "warning_count": len(attempt.get("warnings") or []),
        },
        "first_issue": issues[0] if issues else None,
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
        "workspace_candidate_status": candidate_audit.get("status"),
        "candidate_file_count": len(candidate_audit.get("files") or []),
        "candidate_mismatches": candidate_mismatches,
        **(
            {"candidate_manifest": result["candidate_manifest"]}
            if result.get("candidate_manifest")
            else {}
        ),
        "errors": result.get("errors") or attempt.get("errors") or [],
        "warnings": result.get("warnings") or attempt.get("warnings") or [],
        "details": (
            "Inspect report_path and implementation-validation-ledger.json "
            "for the full validation record."
        ),
    }
    commands = _next_commands(projected)
    if commands:
        projected["next_commands"] = commands
    return _public_cli_paths(projected)


def _compact_finish_job_result(result):
    execution = result.get("execution_outcome") or {}
    terminal = result.get("terminal_snapshot_audit") or {}
    service_level = result.get("service_level") or {}
    implementation_diff = result.get("implementation_diff") or {}
    last_job_result = result.get("last_job_result") or None
    health_issues = result.get("health_issues") or []
    warnings = result.get("warnings") or []
    materialization = result.get("system_materialization") or {}
    materialization_commit = result.get("system_materialization_commit") or {}
    delivery_visibility = result.get("delivery_visibility") or {}
    delivery_summary = result.get("delivery_summary") or {}
    if not delivery_visibility or not delivery_summary:
        raise ValueError("Generation Job终态结果缺少delivery投影")
    changed_files = result.get("changed_files") or []
    delivery_source = delivery_visibility.get("source")
    review_channel = delivery_visibility.get("review_channel")
    if not delivery_source or not review_channel:
        raise ValueError("Generation Job delivery投影缺少source或review_channel")
    delivery_write_channel = (
        delivery_visibility.get("delivery_write_channel")
        or delivery_summary.get("delivery_write_channel")
    )
    projected = {
        "transport_version": "1.0",
        "transport": "compact_finish_job",
        "entrypoint": result.get("entrypoint"),
        "status": result.get("status"),
        "next_action": result.get("next_action"),
        **(
            {
                "category": result.get("category"),
                "failure_summary": result.get("failure_summary"),
                "stages": result.get("stages") or {},
            }
            if result.get("status") == "failed"
            else {}
        ),
        "request_id": result.get("request_id"),
        "job_id": result.get("job_id"),
        "job_path": result.get("job_path"),
        "transaction_id": result.get("transaction_id"),
        "report_path": result.get("report_path"),
        "execution_status": execution.get("status"),
        "static_status": execution.get("static_status"),
        "runtime_status": execution.get("runtime_status"),
        "terminal_snapshot_status": terminal.get("status"),
        "service_level": result.get("service_level"),
        "health_status": "attention_required" if health_issues else "ok",
        "health_issues": health_issues,
        "implementation_diff": result.get("implementation_diff"),
        "implementation_diff_summary": result.get(
            "implementation_diff_summary"
        ) or {},
        "git_diff_visibility": result.get("git_diff_visibility") or {},
        "system_materialization_status": materialization.get("status"),
        "system_materialization_commit": materialization_commit,
        "last_job_result": last_job_result,
        "generation_workspace_projection": result.get(
            "generation_workspace_projection"
        ) or {},
        "workspace_projection_summary": result.get(
            "workspace_projection_summary"
        ) or {},
        "delivery_visibility": {
            "source": delivery_source,
            "delivery_write_channel": delivery_write_channel,
            "review_channel": review_channel,
            "host_native_edit_available": bool(
                delivery_visibility.get("host_native_edit_available")
            ),
            "requires_explicit_diff_review": bool(
                delivery_visibility.get("requires_explicit_diff_review")
            ),
            "changed_file_count": delivery_visibility.get(
                "changed_file_count", len(changed_files)
            ),
            "report_path": result.get("report_path"),
            "implementation_diff_path": implementation_diff.get("path"),
            "job_result_path": (
                (last_job_result or {}).get("path")
                if isinstance(last_job_result, dict)
                else None
            ),
            "system_materialization_status": materialization.get("status"),
            "system_materialization_commit_status": (
                materialization_commit.get("status")
            ),
            "system_owned_file_count": delivery_visibility.get(
                "system_owned_file_count", 0
            ),
            "ai_editable_count": delivery_visibility.get(
                "ai_editable_count", 0
            ),
            "review_note": delivery_visibility.get("review_note"),
        },
        "delivery_summary": {
            "status": result.get("status"),
            "job_id": result.get("job_id"),
            **(
                {
                    "category": result.get("category"),
                    "failure_summary": result.get("failure_summary"),
                }
                if result.get("status") == "failed"
                else {}
            ),
            "static_status": execution.get("static_status"),
            "runtime_status": execution.get("runtime_status"),
            "service_level_status": service_level.get("status"),
            "health_status": "attention_required" if health_issues else "ok",
            "health_issues": health_issues,
            "delivery_source": delivery_summary.get("delivery_source"),
            "delivery_write_channel": delivery_write_channel,
            "review_channel": delivery_summary.get("review_channel"),
            "changed_file_count": delivery_summary.get(
                "changed_file_count", len(changed_files)
            ),
            "change_summary": (
                (result.get("implementation_diff_summary") or {}).get("summary")
            ),
            "report_path": result.get("report_path"),
            "implementation_diff_path": implementation_diff.get("path"),
            "system_materialization_commit_status": (
                materialization_commit.get("status")
            ),
            "job_result_path": (
                (last_job_result or {}).get("path")
                if isinstance(last_job_result, dict)
                else None
            ),
            "warnings": warnings,
        },
        "acceptance_summary": {
            "static_status": execution.get("static_status"),
            "runtime_status": execution.get("runtime_status"),
            "user_review_status": (
                "diff_review_required"
                if review_channel == "implementation_diff" and changed_files
                else "native_edit_review_required"
                if review_channel == "host_native_edit" and changed_files
                else "no_code_changes"
                if not changed_files
                else "review_required"
            ),
            "acceptance_status": (
                "failed"
                if result.get("status") == "failed"
                else "static_only"
                if execution.get("runtime_status") == "runtime_not_run"
                else "runtime_or_oracle_pending"
                if execution.get("runtime_status") in {"runtime_pending", None}
                else "runtime_evaluated"
            ),
        },
        **(
            {"job_transition": result["job_transition"]}
            if result.get("job_transition")
            else {}
        ),
        "job_lifecycle_timing": _compact_job_lifecycle_timing(
            result.get("job_lifecycle_timing")
        ),
        "generation_timing_summary": (
            (result.get("generation_timing_ledger") or {}).get("summary")
            or {}
        ),
        "agent_tool_timing": {
            key: value
            for key, value in (
                (result.get("generation_timing_ledger") or {})
                .get("agent_tool_timing") or {}
            ).items()
            if key in {"status", "source", "event_count", "summary"}
        },
        "generation_progress": _generation_progress_projection(result),
        "orchestration_timing": result.get("orchestration_timing") or {},
        "advance_summary": result.get("advance_summary") or {},
        "next_commands": _next_commands(result),
        "errors": result.get("errors") or [],
        "warnings": warnings,
        "full_output": "Pass --full to retrieve the complete terminal transaction report.",
    }
    _compact_success_delivery_for_cli(projected)
    _compact_terminal_envelope_for_cli(projected)
    return _public_cli_paths(projected)


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


def _next_commands(result):
    result = result or {}
    status = str(result.get("status") or "")
    job_path = _public_cli_path(result.get("job_path")) if result.get("job_path") else "<job>"
    delivery_summary = result.get("delivery_summary") or {}
    delivery_visibility = result.get("delivery_visibility") or {}
    result_report_path = (
        result.get("report_path")
        or delivery_summary.get("report_path")
        or delivery_visibility.get("report_path")
    )
    report_path = (
        _public_cli_path(result_report_path)
        if result_report_path
        else "<report-path>"
    )
    value = {
        "next_commands_version": "1.1",
        "runner": "entry_command_runtime",
        "runner_fallback": "runtime_python_hint_only_when_entry_command_missing",
        "module": "autowork_core.utils.debug_tools.recorder.generation_workflow",
    }
    if status == "ready_to_generate":
        value.update({
            "primary": _workflow_command(
                "advance_job",
                ["advance-job", job_path],
            ),
            "do_not": [
                "do_not_reread_rule_files_before_generate_job",
                "do_not_query_job_design_context_before_generate_job",
            ],
        })
        return value
    if status == "business_review_required":
        value.update({
            "blocked_on": "retired_business_review_stage",
            "framework_defect": (
                "business_review_required is retired from the normal product "
                "path; advance-job must project business_answers_required "
                "with business_questions and ask_questions."
            ),
            "allowed_action": "stop_and_report_framework_defect",
            "do_not": [
                "do_not_run_submit_business_review",
                "do_not_reconstruct_business_review_patch",
                "do_not_continue_generation_before_answer_batch",
            ],
        })
        return value
    if status == "business_option_answers_required":
        value.update({
            "blocked_on": "retired_business_option_answers_stage",
            "framework_defect": (
                "business_option_answers_required is retired from the normal "
                "product path; advance-job must project business_answers_required "
                "with ask_questions and submit-business-review-answers."
            ),
            "allowed_action": "stop_and_report_framework_defect",
            "do_not": [
                "do_not_run_submit_business_answers",
                "do_not_reconstruct_legacy_answer_command",
                "do_not_continue_generation_before_answer_batch",
            ],
        })
        return value
    if status == "business_answers_required":
        ask_payload = _business_answer_ask_questions_payload(
            result.get("business_questions") or [],
        )
        value.update({
            "ask_questions": ask_payload,
            "answer_argument_contract": _business_answer_argument_contract(
                result.get("business_questions") or [],
            ),
            "primary": _workflow_command(
                "submit_business_review_answers",
                ["submit-business-review-answers", job_path],
                fill_placeholders=True,
            ),
            "do_not": [
                "do_not_present_raw_option_ids_as_user_instructions",
                "do_not_write_answers_json",
                "do_not_map_freeform_text_to_option_without_user_selection",
                "do_not_continue_generation_before_answer_submission",
            ],
            "progress_message_policy": "call_vscode_askQuestions_once_with_all_business_questions_then_submit_answers",
        })
        return value
    if status in {
        "business_answers_submitted",
        "business_facts_submitted",
        "business_review_answers_submitted",
        "business_review_passed",
    }:
        value.update({
            "primary": _workflow_command(
                "advance_job",
                ["advance-job", job_path],
            ),
            "do_not": [
                "do_not_resubmit_business_answers",
                "do_not_reread_rule_files_before_continue",
                "do_not_query_job_design_context_before_advance_job",
            ],
        })
        return value
    batch = result.get("typed_patch_requirement_batch") or {}
    if batch.get("status") == "required" and batch.get("complete") is True:
        args, recommended, error = _typed_patch_batch_arguments(batch)
        if error:
            value.update({
                "blocked_on": "typed_patch_scope_conflict",
                "framework_defect": error,
                "allowed_action": "stop_and_report_framework_defect",
                "do_not": [
                    "do_not_run_primary_with_duplicate_typed_patch_scopes",
                    "do_not_hand_deduplicate_typed_patch_arguments",
                    "do_not_continue_generation_before_framework_fix",
                ],
            })
            return value
        if args:
            value.update({
                "primary": _workflow_command(
                    "submit_all_complete_typed_patch_arguments",
                    ["advance-job", job_path, *args],
                    fill_placeholders=not recommended,
                ),
                "do_not": [
                    "do_not_query_job_design_context_for_complete_batch",
                    "do_not_emit_redundant_or_exploratory_message_before_typed_patch_primary",
                    "do_not_edit_naming_patch_file",
                    "do_not_reread_rule_files_for_complete_batch",
                ],
                "progress_message_policy": "one_concise_stage_message_allowed_before_primary",
            })
            return value
        value.update({
            "blocked_on": "typed_patch_primary_missing",
            "framework_defect": (
                "complete typed_patch_requirement_batch did not provide "
                "submit_arguments or recommended_arguments"
            ),
            "allowed_action": "stop_and_report_framework_defect",
            "do_not": [
                "do_not_rerun_advance_job_without_typed_arguments",
                "do_not_query_job_design_context_for_complete_batch",
                "do_not_call_generate_job_help",
                "do_not_reconstruct_typed_patch_arguments",
            ],
        })
        return value
    allowed_queries = list(batch.get("allowed_queries") or [])
    if batch.get("status") == "required" and allowed_queries:
        value.update({
            "allowed_queries": [
                _allowed_query_command(job_path, query)
                for query in allowed_queries
            ],
            "do_not": [
                "do_not_probe_unbounded_job_design_context",
                "do_not_query_any_context_not_listed_in_allowed_queries",
            ],
        })
        return value
    if status == "implementation_required":
        boundary = _implementation_boundary(result)
        mismatches = list(result.get("candidate_mismatches") or [])
        scope_guard_errors = _scope_guard_errors(result)
        value.update({
            "blocked_on": boundary,
            "allowed_action": result.get("next_action")
            or "review_implementation_boundary",
            "implementation_boundary_reason": result.get(
                "implementation_boundary_reason"
            ),
            "do_not": [
                "do_not_apply_candidate_files",
                "do_not_query_implementation_candidate_for_delivery",
                "do_not_query_implementation_packet_for_delivery",
                "do_not_read_candidate_delivery_manifest_when_candidate_index_exists",
                "do_not_read_candidate_index_or_native_edit_sources",
                "do_not_use_agent_editor_edit_for_delivery",
                "do_not_run_after_native_edit_for_delivery",
                "do_not_use_terminal_or_python_to_write_bdd_files",
                "do_not_continue_same_transaction_as_success",
            ],
        })
        if mismatches:
            value["candidate_mismatches"] = mismatches
            value["do_not"].extend([
                "do_not_hand_edit_candidate_mismatches",
                "do_not_reapply_candidate_with_agent_editor_edit",
                "do_not_compose_multi_file_candidate_patch",
            ])
        if scope_guard_errors:
            value["blocked_on"] = "transaction_scope_guard"
            value["allowed_action"] = "stop_and_report_guard_failure"
            value["scope_guard_errors"] = scope_guard_errors
            value["do_not"].extend([
                "do_not_reapply_candidate_after_scope_guard",
                "do_not_edit_protected_framework_files",
            ])
        return value
    if status in {"completed", "completed_no_changes"} and any(
            result.get(key)
            for key in (
                "transaction_id",
                "report_path",
                "delivery_summary",
                "delivery_visibility",
                "last_job_result",
            )
    ):
        value.update({
            "diff_review": _workflow_command(
                "review_implementation_diff",
                ["job-code-diff", report_path],
            ),
            "final_response": {
                "source_fields": [
                    "terminal_result",
                    "implementation_diff_summary",
                    "unresolved_issues",
                    "errors",
                    "warnings",
                ],
            },
            "do_not": [
                "do_not_search_report_result_or_diff_paths_after_success",
                "do_not_open_or_summarize_full_report_json_after_success",
                "do_not_list_raw_changed_files_after_success",
            ],
        })
        return value
    return {}


def _implementation_boundary(result):
    if result.get("implementation_boundary"):
        return str(result.get("implementation_boundary"))
    if (result.get("advance_summary") or {}).get("blocked_on"):
        blocked_on = str((result.get("advance_summary") or {}).get("blocked_on"))
        return "implementation_required" if blocked_on == "host_native_edit" else blocked_on
    if result.get("workspace_candidate_status") == "mismatch" or result.get(
            "candidate_mismatches"
    ):
        return "candidate_mismatch"
    if str(result.get("status") or "") == "implementation_required":
        return "implementation_required"
    return ""


def _implementation_required_details(boundary):
    return (
        "Implementation is blocked on boundary: "
        f"{boundary}. Do not read candidate indexes, apply candidate files, "
        "or edit Bdd through Agent tools; follow next_action, review the "
        "system materialization result, or report the boundary reason."
    )

def _scope_guard_errors(result):
    markers = (
        "生成事务修改了受保护文件",
        "生成事务修改了generation scope外项目文件",
        "生成事务修改了Implementation Manifest范围外文件",
        "生成事务修改了Implementation Manifest只读复用文件",
        "项目guard",
        "project guard",
        "protected path",
        "write_scope_or_protected_path_violation",
    )
    values = [str(item) for item in (result.get("errors") or [])]
    first_issue = result.get("first_issue") or {}
    if isinstance(first_issue, dict):
        values.extend(
            str(first_issue.get(key) or "")
            for key in ("code", "message", "kind")
        )
    return [
        value for value in values
        if value and any(marker in value for marker in markers)
    ]


def _workflow_command(purpose, module_args, *, fill_placeholders=False):
    return {
        "purpose": purpose,
        "module_args": [str(item) for item in module_args],
        "fill_placeholders": bool(fill_placeholders),
    }

def _typed_patch_batch_argument_templates(batch):
    args, _recommended, _error = _typed_patch_batch_arguments(batch)
    return args


def _typed_patch_batch_arguments(batch):
    requirements = [
        requirement for requirement in batch.get("requirements") or []
        if isinstance(requirement, dict)
    ]
    use_recommended = bool(requirements) and all(
        requirement.get("recommended_arguments")
        for requirement in requirements
    )
    argument_field = (
        "recommended_arguments" if use_recommended else "submit_arguments"
    )
    args = []
    for requirement in requirements:
        for argument in requirement.get(argument_field) or []:
            args.extend(_split_submit_argument(argument))
    args, error = _dedupe_typed_patch_arguments(args)
    return args, use_recommended, error


def _dedupe_typed_patch_arguments(args):
    result = []
    seen = {}
    index = 0
    while index < len(args):
        option = str(args[index] or "")
        value = str(args[index + 1] or "") if index + 1 < len(args) else ""
        if option not in {
                "--target-name",
                "--business-name",
                "--ambiguity-choice",
                "--assertion-choice",
                "--method-choice",
                "--operation-choice",
                "--value-source-choice",
        }:
            result.append(option)
            index += 1
            continue
        key = _typed_patch_argument_scope(option, value)
        if key is None:
            result.extend([option, value])
            index += 2
            continue
        existing = seen.get(key)
        if existing is not None:
            if existing != value:
                return [], (
                    f"typed patch primary contains conflicting {option} "
                    f"declarations for {key[1]}"
                )
            index += 2
            continue
        seen[key] = value
        result.extend([option, value])
        index += 2
    return result, None


def _typed_patch_argument_scope(option, value):
    text = str(value or "")
    if "=" not in text:
        return None
    scope, _assigned = text.split("=", 1)
    scope = scope.strip()
    if not scope:
        return None
    return (str(option or ""), scope)


def _split_submit_argument(argument):
    text = str(argument or "").strip()
    if not text:
        return []
    name, separator, value = text.partition(" ")
    if separator and name.startswith("--") and value:
        return [name, value]
    return [text]


def _allowed_query_command(job_path, query):
    query = query if isinstance(query, dict) else {}
    command = str(query.get("command") or "")
    args = [command or "<allowed-query>", job_path]
    if query.get("step_id"):
        args.extend(["--step-id", query.get("step_id")])
    return {
        **_workflow_command(
            "run_explicit_bounded_allowed_query",
            args,
        ),
        "reason": query.get("reason"),
    }


def _compact_budget(value):
    value = value or {}
    return {
        key: value.get(key)
        for key in (
            "status",
            "default_total_bytes",
            "target_bytes",
            "remaining_bytes",
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
    options = (
        {"sort_keys": True, "indent": 2}
        if full
        else {"sort_keys": True, "separators": (",", ":")}
    )
    return json.dumps(
        _project_cli_result(result, full=full),
        ensure_ascii=False,
        **options,
    ) + "\n"


def _naming_patch_from_args(target_names, business_names):
    return {
        "naming_patch_version": "1.0",
        "patch_type": "naming",
        "target_names": _name_assignments(target_names, "--target-name"),
        "business_names": _name_assignments(business_names, "--business-name"),
    }


def _ambiguity_choice_patch_from_args(values):
    choices = []
    seen = set()
    for value in values or ():
        text = str(value or "")
        if "=" not in text:
            raise ValueError("--ambiguity-choice 必须使用 ambiguity=outcome 格式")
        ambiguity_id, outcome = text.split("=", 1)
        ambiguity_id = ambiguity_id.strip()
        outcome = outcome.strip()
        if not ambiguity_id or not outcome:
            raise ValueError("--ambiguity-choice ambiguity和outcome不能为空")
        if ambiguity_id in seen:
            raise ValueError(f"--ambiguity-choice 重复声明: {ambiguity_id}")
        seen.add(ambiguity_id)
        choices.append({
            "ambiguity_id": ambiguity_id,
            "outcome": outcome,
        })
    return {
        "ambiguity_choice_patch_version": "1.0",
        "patch_type": "ambiguity_choice",
        "choices": choices,
    }


def _assertion_choice_patch_from_args(values):
    choices = []
    seen = set()
    for value in values or ():
        text = str(value or "")
        if "=" not in text:
            raise ValueError(
                "--assertion-choice 必须使用 ambiguity=candidate_key 格式"
            )
        ambiguity_id, candidate_key = text.split("=", 1)
        ambiguity_id = ambiguity_id.strip()
        candidate_key = candidate_key.strip()
        if not ambiguity_id or not candidate_key:
            raise ValueError("--assertion-choice ambiguity和candidate_key不能为空")
        if ambiguity_id in seen:
            raise ValueError(f"--assertion-choice 重复声明: {ambiguity_id}")
        seen.add(ambiguity_id)
        choices.append({
            "ambiguity_id": ambiguity_id,
            "candidate_key": candidate_key,
        })
    return {
        "assertion_choice_patch_version": "1.0",
        "patch_type": "assertion_choice",
        "choices": choices,
    }


def _method_choice_patch_from_args(values):
    choices = []
    seen = set()
    for value in values or ():
        text = str(value or "")
        if "=" not in text:
            raise ValueError("--method-choice 必须使用 step_id=candidate_id 格式")
        step_id, candidate_id = text.split("=", 1)
        step_id = step_id.strip()
        candidate_id = candidate_id.strip()
        if not step_id or not candidate_id:
            raise ValueError("--method-choice step_id和candidate_id不能为空")
        if step_id in seen:
            raise ValueError(f"--method-choice 重复声明: {step_id}")
        seen.add(step_id)
        choices.append({"step_id": step_id, "candidate_id": candidate_id})
    return {
        "method_choice_patch_version": "1.0",
        "patch_type": "method_choice",
        "choices": choices,
    }


def _operation_choice_patch_from_args(values):
    choices = []
    seen = set()
    for value in values or ():
        text = str(value or "")
        if "=" not in text:
            raise ValueError(
                "--operation-choice 必须使用 step/action=choice_key 格式"
            )
        scope, choice_key = text.split("=", 1)
        parts = [part.strip() for part in scope.split("/", 1)]
        choice_key = choice_key.strip()
        if len(parts) != 2 or not parts[0] or not parts[1] or not choice_key:
            raise ValueError(
                "--operation-choice step/action和choice_key不能为空"
            )
        key = tuple(parts)
        if key in seen:
            raise ValueError(f"--operation-choice 重复声明: {scope}")
        seen.add(key)
        choices.append({
            "step_id": parts[0],
            "action_id": parts[1],
            "choice_key": choice_key,
        })
    return {
        "operation_choice_patch_version": "1.0",
        "patch_type": "operation_choice",
        "choices": choices,
    }


def _value_source_choice_patch_from_args(values):
    choices = []
    seen = set()
    for value in values or ():
        text = str(value or "")
        if "=" not in text:
            raise ValueError(
                "--value-source-choice 必须使用 "
                "step/action/operation=kind:reference 格式"
            )
        scope, source_text = text.split("=", 1)
        scope_parts = [part.strip() for part in scope.split("/", 2)]
        if len(scope_parts) != 3 or not all(scope_parts):
            raise ValueError(
                "--value-source-choice scope必须是step/action/operation"
            )
        if ":" not in source_text:
            raise ValueError(
                "--value-source-choice source必须是kind:reference格式"
            )
        kind, reference = [part.strip() for part in source_text.split(":", 1)]
        if not kind or not reference:
            raise ValueError(
                "--value-source-choice source kind和reference不能为空"
            )
        key = tuple(scope_parts)
        if key in seen:
            raise ValueError(f"--value-source-choice 重复声明: {scope}")
        seen.add(key)
        source = {"kind": kind}
        if kind == "recorded_action":
            source["action_id"] = reference
        else:
            source["reference"] = reference
        choices.append({
            "step_id": scope_parts[0],
            "action_id": scope_parts[1],
            "operation": scope_parts[2],
            "source": source,
        })
    return {
        "value_source_choice_patch_version": "1.0",
        "patch_type": "value_source_choice",
        "choices": choices,
    }


def _name_assignments(values, option):
    result = {}
    for value in values or ():
        text = str(value or "")
        if "=" not in text:
            raise ValueError(f"{option} 必须使用 scope=name 格式")
        scope, name = text.split("=", 1)
        scope = scope.strip()
        name = name.strip()
        if not scope or not name:
            raise ValueError(f"{option} scope和name不能为空")
        if scope in result:
            raise ValueError(f"{option} 重复声明: {scope}")
        result[scope] = name
    return result


if __name__ == "__main__":
    raise SystemExit(main())