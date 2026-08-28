from __future__ import annotations

import hashlib
import json
from pathlib import Path

from autowork_core.utils.debug_tools.recorder.decision_pack import (
    load_answer_record,
    load_decision_pack,
)
from autowork_core.utils.debug_tools.recorder.action_knowledge import (
    query_action_knowledge,
)
from autowork_core.utils.debug_tools.recorder.ai_context_envelope import (
    build_ai_context_envelope,
    build_envelope_brief_projection,
)
from autowork_core.utils.debug_tools.recorder.ai_plan_context import (
    build_ai_plan_context,
)
from autowork_core.utils.debug_tools.recorder.evidence_context import (
    compare_request_takes,
    query_request_evidence,
)
from autowork_core.utils.debug_tools.recorder.generation_contract import (
    compact_ai_capability_contract,
    generation_contract_lease,
)
from autowork_core.utils.debug_tools.recorder.generation_job import (
    build_generation_job,
    generation_job_lease,
    generation_job_lease_is_valid,
    generation_job_pointer,
    load_generation_job,
    persist_generation_job,
)
from autowork_core.utils.debug_tools.recorder.generation_job_result import (
    advance_job_to_oracle,
    load_generation_job_result,
    publish_pretransaction_job_failure,
    publish_runtime_job_outcome,
)
from autowork_core.utils.debug_tools.recorder.generation_plan import (
    load_generation_plan,
    plan_pointer,
)
from autowork_core.utils.debug_tools.recorder.generation_quality_gate import (
    evaluate_generation_quality,
)
from autowork_core.utils.debug_tools.recorder.reconciliation_repository import (
    load_generation_brief,
)
from autowork_core.utils.debug_tools.recorder.semantic_reconciler import (
    brief_matches_request,
)
from autowork_core.utils.debug_tools.recorder.generation_transaction import (
    abort_generation_transaction,
    finish_generation_transaction,
    prepare_generation_transaction,
)
from autowork_core.utils.debug_tools.recorder.generation_file_lock import (
    validate_generation_file_lease,
)
from autowork_core.utils.debug_tools.recorder.implementation_manifest import (
    build_implementation_packet,
    implementation_manifest_identity_is_valid,
)
from autowork_core.utils.debug_tools.recorder.generation_profile import (
    project_generation_admission,
)
from autowork_core.utils.debug_tools.recorder.request_repository import (
    request_identity_is_valid,
    session_dir_for_request_path,
)
from autowork_core.utils.debug_tools.recorder.run_lock import RunWriteLock
from autowork_core.utils.debug_tools.recorder.workflow_state import (
    WORKFLOW_STATE_VERSION,
    claim_generation_job,
    fail_generation_job_integrity,
    load_workflow_state,
    publish_generation_job,
    replace_generation_job,
    retired_job_entry,
    transition_generation_job,
)
from autowork_core.utils.debug_tools.recorder.workflow_service import (
    inspect_workflow,
    submit_generation_design,
)
from autowork_core.runtime.reporting.oracle_registry import (
    latest_runtime_matrix_receipt,
)
from autowork_core.runtime.reporting.run_result_bridge import (
    generation_provenance_from_artifacts,
    latest_matching_run_result,
)


def admit_generation_job(request_path, *, profile_id=None):
    request_path = Path(request_path).resolve()
    request = _read_json(request_path)
    session_dir = session_dir_for_request_path(request_path, request)
    lock = RunWriteLock(session_dir).acquire()
    try:
        existing = load_workflow_state(
            session_dir,
            request.get("request_id"),
        )
        replacement = None
        if existing.get("workflow_state_version") == (
            WORKFLOW_STATE_VERSION
        ) and existing.get("current_job"):
            job = load_generation_job(
                session_dir,
                existing.get("current_job") or {},
            )
            if job is None:
                if any((
                    existing.get("status") != "failed",
                    (existing.get("job_execution") or {}).get("phase")
                    != "failed",
                    "job_integrity_failed"
                    not in set(existing.get("errors") or ()),
                )):
                    raise ValueError("Workflow current Job identity无效")
                replacement = {
                    "pointer": dict(existing["current_job"]),
                    "epoch": (existing.get("job_execution") or {})[
                        "epoch"
                    ],
                    "reason": "integrity_failed",
                }
            else:
                selected = str(profile_id or "generation_first")
                current_profile = (job.get("profile_lease") or {}).get("profile_id")
                execution = existing.get("job_execution") or {}
                if existing.get("status") == "running" or execution.get("phase") in {
                    "design", "implementation", "runtime", "oracle",
                }:
                    if current_profile != selected:
                        raise ValueError(
                            "运行中的Generation Job不能切换Profile"
                        )
                    return _job_result(session_dir, job, existing)
                replacement = {
                    "pointer": dict(existing["current_job"]),
                    "epoch": execution.get("epoch"),
                    "reason": (
                        "switch_profile"
                        if current_profile != selected
                        else "retry_generation"
                    ),
                }
                if replacement["epoch"] is None:
                    raise ValueError(
                        "当前Generation Job缺少可替换epoch"
                    )

        from autowork_core.utils.debug_tools.recorder.generation_workflow import (
            inspect_generation,
        )

        inspected = inspect_generation(
            request_path,
            generation_profile_id=profile_id,
        )
        state = load_workflow_state(
            session_dir,
            request.get("request_id"),
        )
        shadow_admission = inspected.get("generation_admission") or {}
        if shadow_admission.get("status") != "passed":
            return {
                "generation_job_service_version": "1.0",
                "status": "rejected",
                "request_id": request.get("request_id"),
                "generation_admission": shadow_admission,
                "errors": list(
                    shadow_admission.get("blocking_codes") or []
                ),
                "warnings": [],
            }
        pack, answers = _decision_artifacts(session_dir, request, state)
        contract_lease = generation_contract_lease(
            session_dir,
            write=False,
        )
        from autowork_core.utils.debug_tools.recorder.generation_workflow import (
            build_ai_context_budget,
        )

        context_budget = inspected.get("ai_context_budget") or {}
        job = None
        admission = None
        for _attempt in range(8):
            admission = project_generation_admission(
                request=request,
                state=state,
                context_budget=context_budget,
                request_identity_valid=request_identity_is_valid(request),
                profile_id=profile_id,
                decision_pack=pack,
                answer_record=answers,
                enforcement="active",
                generation_contract_lease=contract_lease,
            )
            if admission.get("status") != "passed":
                return _admission_rejected_result(request, admission)
            job = build_generation_job(
                request,
                state,
                admission,
                contract_lease,
                activation="active",
            )
            projected = _candidate_job_inspect(
                session_dir,
                request,
                state,
                job,
            )
            next_budget = build_ai_context_budget(
                session_dir=session_dir,
                request_path=request_path,
                request=request,
                state=state,
                inspect_result=projected,
                capability_contract=projected["ai_capabilities"],
                brief_path=_brief_path_for_job(session_dir, job),
                plan_path=None,
                plan_context=None,
                job_value=job,
            )
            if next_budget == context_budget:
                break
            context_budget = next_budget
        else:
            raise RuntimeError("Generation Job context budget未能收敛")
        if job is None or admission is None:
            raise RuntimeError("Generation Job admission未生成候选")
        path, job = persist_generation_job(session_dir, job)
        pointer = generation_job_pointer(session_dir, job, path)
        workflow = (
            replace_generation_job(
                session_dir,
                request["request_id"],
                pointer,
                expected_job_pointer=replacement["pointer"],
                expected_epoch=replacement["epoch"],
                retire_reason=replacement.get("reason"),
            )
            if replacement is not None
            else publish_generation_job(
                session_dir,
                request["request_id"],
                pointer,
                expected_epoch=0,
            )
        )
        return _with_job_transition(
            _job_result(session_dir, job, workflow),
            session_dir,
            job,
        )
    finally:
        lock.release()


def start_generation_job(job_path, *, expected_epoch):
    job_path = Path(job_path).resolve()
    if len(job_path.parents) < 4:
        raise ValueError("Generation Job path无效")
    session_dir = job_path.parents[3]
    request_id = job_path.parent.name
    lock = RunWriteLock(session_dir).acquire()
    try:
        current = load_workflow_state(session_dir, request_id)
        pointer_path = _session_pointer_path(
            session_dir,
            (current.get("current_job") or {}).get("path"),
        )
        if pointer_path != job_path:
            raise ValueError("Generation Job不是Workflow current Job")
        job = load_generation_job(
            session_dir,
            current.get("current_job") or {},
        )
        if job is None:
            fail_generation_job_integrity(
                session_dir,
                request_id,
                expected_epoch=expected_epoch,
                error_code="job_integrity_failed",
            )
            raise ValueError("Generation Job identity无效")
        current = load_workflow_state(
            session_dir,
            (job.get("request") or {}).get("request_id"),
        )
        current_execution = current.get("job_execution") or {}
        if any((
            current.get("status") != "ready",
            current_execution.get("phase") != "ready",
            current_execution.get("epoch") != expected_epoch,
        )):
            raise ValueError("Generation Job CAS冲突: status、phase或epoch")
        request_path = _request_path_for_job(session_dir, job)
        fresh = inspect_workflow(request_path, write=False)
        if any((
            fresh.get("status") != "ready",
            (fresh.get("job_execution") or {}).get("phase") != "ready",
            fresh.get("current_job") != current.get("current_job"),
        )):
            errors = list(fresh.get("errors") or ["job_freshness_failed"])
            publish_pretransaction_job_failure(
                session_dir,
                (job.get("request") or {})["request_id"],
                claim_id=None,
                expected_epoch=current_execution["epoch"],
                expected_phase="ready",
                category=str(errors[0]),
                next_action="review_generation_failure",
                issue_owner={
                    "type": "generation_admission_gap",
                    "errors": errors,
                },
            )
            raise ValueError(
                "Generation Job freshness校验失败: "
                + "; ".join(str(item) for item in errors)
            )
        workflow = claim_generation_job(
            session_dir,
            (job.get("request") or {})["request_id"],
            job_id=job["job_id"],
            job_fingerprint=job["job_fingerprint"],
            expected_epoch=expected_epoch,
        )
        if current.get("current_job") != workflow.get("current_job"):
            raise ValueError("Generation Job pointer在claim期间发生变化")
        return _with_job_transition(
            _job_result(session_dir, job, workflow),
            session_dir,
            job,
        )
    finally:
        lock.release()


def retry_generation_job(job_path, *, profile_id=None):
    session_dir, job, state = _resolve_known_job(job_path)
    active_pointer = state.get("current_job") or {}
    if active_pointer and active_pointer.get("job_id") != job.get("job_id"):
        raise ValueError("当前已有活动Generation Job，不能重试历史Job")
    execution = state.get("job_execution") or {}
    if state.get("status") == "running" or execution.get("phase") in {
        "design",
        "implementation",
        "runtime",
        "oracle",
    }:
        raise ValueError("运行中的Generation Job不能retry")
    request_path = _request_path_for_job(session_dir, job)
    selected_profile = profile_id or (
        (job.get("profile_lease") or {}).get("profile_id")
    ) or "generation_first"
    return admit_generation_job(request_path, profile_id=selected_profile)


def retire_generation_job(
        job_path,
        *,
        reason,
        expected_epoch,
        claim_id=None,
    ):
    session_dir, job, state = _resolve_current_job(job_path)
    lock = RunWriteLock(session_dir).acquire()
    try:
        session_dir, job, state = _resolve_current_job(job_path)
        execution = state.get("job_execution") or {}
        phase = execution.get("phase")
        has_transaction = bool(
            execution.get("transaction")
            or state.get("active_transaction")
        )
        if phase in {"runtime", "oracle"} or (
                phase == "implementation" and has_transaction
        ):
            raise ValueError(
                "已进入Transaction的Generation Job必须使用abort-job或finish-job"
            )
        if phase not in {"ready", "design", "implementation"}:
            return _with_job_transition(
                _job_result(session_dir, job, state),
                session_dir,
                job,
            )
        if phase in {"design", "implementation"} and (
                execution.get("claim_id") != claim_id
        ):
            raise ValueError("Generation Job retire claim_id无效")
        issue_owner = {
            "type": "operator_retire",
            "reason": str(reason or "operator_retired"),
        }
        result = publish_pretransaction_job_failure(
            session_dir,
            (job.get("request") or {})["request_id"],
            claim_id=claim_id,
            expected_epoch=expected_epoch,
            expected_phase=phase,
            category="operator_retired",
            next_action="review_generation_result",
            issue_owner=issue_owner,
        )
        return _with_job_transition(result, session_dir, job)
    finally:
        lock.release()


def inspect_generation_job(job_path):
    session_dir, job, state = _resolve_known_job(job_path)
    request_path = _request_path_for_job(session_dir, job)
    retired = retired_job_entry(state, job_id=job.get("job_id"))
    if retired is None:
        state = inspect_workflow(request_path, write=False)
    else:
        result_pointer = retired.get("last_job_result") or {}
        terminal_result = load_generation_job_result(
            session_dir,
            result_pointer,
        ) if result_pointer else None
        state = {
            **state,
            "status": retired.get("status"),
            "next_action": (
                terminal_result.get("next_action")
                if terminal_result is not None
                else "review_generation_failure"
            ),
            "current_job": None,
            "job_execution": retired.get("job_execution"),
            "last_job_result": result_pointer or None,
            "plan": (
                (retired.get("job_execution") or {}).get("plan")
                or state.get("plan")
                or {}
            ),
        }
    result = _job_result(session_dir, job, state)
    request = _read_json(request_path)
    brief_path = _brief_path_for_job(session_dir, job)
    plan_value = state.get("plan") or {}
    plan_path = _session_pointer_path(session_dir, plan_value.get("path"))
    result.update({
        "job_path": Path(job_path).resolve().relative_to(
            session_dir
        ).as_posix(),
        "request_path": request_path.relative_to(session_dir).as_posix(),
        "brief_path": brief_path.relative_to(session_dir).as_posix(),
        "plan_path": (
            plan_path.relative_to(session_dir).as_posix()
            if plan_path
            else None
        ),
        "execution_boundary": _projected_execution_boundary(job),
        "ai_capabilities": compact_ai_capability_contract(),
    })
    plan_artifact = load_generation_plan(session_dir, state, request)
    plan_context = (
        build_ai_plan_context(plan_artifact)
        if plan_artifact is not None
        else None
    )
    from autowork_core.utils.debug_tools.recorder.generation_workflow import (
        _compact_workflow_context,
        _with_context_budget,
        build_ai_context_budget,
    )

    result["ai_context_envelope"] = build_ai_context_envelope(
        session_dir=session_dir,
        request=request,
        state=state,
        brief_path=brief_path,
        job_value=job,
        job_path=job_path,
        workflow_context=_compact_workflow_context(state),
        ai_capabilities=result["ai_capabilities"],
        plan_context=plan_context,
    )

    budget = build_ai_context_budget(
        session_dir=session_dir,
        request_path=request_path,
        request=request,
        state=state,
        inspect_result=result,
        capability_contract=result["ai_capabilities"],
        brief_path=brief_path,
        plan_path=plan_path,
        plan_context=plan_context,
        job_path=job_path,
        job_value=job,
    )
    return _with_context_budget(result, budget)


def query_generation_job_evidence(
        job_path,
        *,
        evidence_id=None,
        step_id=None,
        action_id=None,
        list_only=False,
    ):
    session_dir, job, _state = _resolve_known_job(job_path)
    _require_job_query(job, "job-evidence")
    result = query_request_evidence(
        _request_path_for_job(session_dir, job),
        evidence_id=evidence_id,
        step_id=step_id,
        action_id=action_id,
        list_only=list_only,
    )
    return {**result, "job_id": job.get("job_id")}


def compare_generation_job_takes(job_path, *, step_id, take_ids=()):
    session_dir, job, _state = _resolve_known_job(job_path)
    _require_job_query(job, "job-compare-takes")
    result = compare_request_takes(
        _request_path_for_job(session_dir, job),
        step_id=step_id,
        take_ids=take_ids,
    )
    return {**result, "job_id": job.get("job_id")}


def query_generation_job_action_knowledge(
        job_path,
        *,
        step_id=None,
        action_id=None,
        operation_names=(),
        list_only=False,
    ):
    session_dir, job, _state = _resolve_known_job(job_path)
    _require_job_query(job, "job-action-knowledge")
    brief = load_generation_brief(_brief_path_for_job(session_dir, job))
    return {
        "status": "projected",
        "job_id": job.get("job_id"),
        "request_id": (job.get("request") or {}).get("request_id"),
        "action_knowledge": query_action_knowledge(
            brief,
            step_id=step_id,
            action_id=action_id,
            operation_names=operation_names,
            list_only=list_only,
        ),
    }


def query_generation_job_design_context(job_path, *, step_id=None):
    session_dir, job, state = _resolve_known_job(job_path)
    _require_job_query(job, "job-design-context")
    request = _read_json(_request_path_for_job(session_dir, job))
    brief_path = _brief_path_for_job(session_dir, job)
    brief = load_generation_brief(brief_path)
    if not brief_matches_request(brief, request):
        raise ValueError("Generation Design Context Brief身份与Request不一致")
    result = _job_result(session_dir, job, state)
    return {
        "generation_design_context_query_version": "1.0",
        "status": "projected",
        "request_id": request.get("request_id"),
        "job_id": job.get("job_id"),
        "job_transition": result["job_transition"],
        "query": {"step_id": str(step_id or "") or None},
        "design_context": build_envelope_brief_projection(
            brief,
            session_dir=session_dir,
            brief_path=brief_path,
            step_id=step_id,
            expanded=bool(step_id),
        ),
    }


def query_generation_job_implementation_packet(
        report_path,
        *,
        step_id=None,
        path=None,
):
    if step_id and path:
        raise ValueError("Implementation Packet只能按step_id或path查询")
    session_dir, job = _job_for_transaction_report(report_path)
    _require_job_query(job, "job-implementation-packet")
    report_path = Path(report_path).resolve()
    report = _read_json(report_path)
    manifest = report.get("implementation_manifest") or {}
    state = load_workflow_state(
        session_dir,
        (job.get("request") or {}).get("request_id"),
    )
    _validate_implementation_packet_query(state, job, report, report_path)
    if not implementation_manifest_identity_is_valid(manifest):
        raise ValueError("Implementation Packet Manifest身份无效")
    packet = build_implementation_packet(manifest)
    if report.get("implementation_packet") != packet:
        raise ValueError("Implementation Packet与冻结Manifest不一致")
    if (report.get("system_materialization") or {}).get("status") != "materialized":
        raise ValueError("Implementation Packet系统物化未完成")
    result = _job_result(session_dir, job, state)
    return {
        "implementation_packet_query_version": "1.0",
        "status": "projected",
        "request_id": (job.get("request") or {}).get("request_id"),
        "job_id": job.get("job_id"),
        "transaction_id": report.get("transaction_id"),
        "report_path": str(report_path),
        "job_transition": result["job_transition"],
        "query": {
            "step_id": str(step_id or "") or None,
            "path": str(path or "") or None,
        },
        "implementation_packet": _project_implementation_packet(
            packet,
            step_id=step_id,
            path=path,
        ),
    }


def submit_generation_job_design(
        job_path,
        design,
        *,
        claim_id,
        expected_epoch,
        note="",
    ):
    session_dir, job, _state = _resolve_current_job(job_path)
    lock = RunWriteLock(session_dir).acquire()
    try:
        request_path = _request_path_for_job(session_dir, job)
        lease = generation_job_lease(job)
        artifact = submit_generation_design(
            request_path,
            design,
            note=note,
            generation_job_lease=lease,
            generation_job_claim_id=claim_id,
            generation_job_expected_epoch=expected_epoch,
        )
        pointer = plan_pointer(
            session_dir,
            artifact,
            artifact["plan_path"],
        )
        workflow = transition_generation_job(
            session_dir,
            (job.get("request") or {})["request_id"],
            job_id=job["job_id"],
            job_fingerprint=job["job_fingerprint"],
            claim_id=claim_id,
            expected_epoch=expected_epoch,
            expected_phase="design",
            phase="implementation",
            next_action="prepare_generation_transaction",
            plan=pointer,
        )
        result = _job_result(session_dir, job, workflow)
        result.update({
            "plan_id": artifact.get("plan_id"),
            "plan_path": artifact.get("plan_path"),
        })
        return _with_job_transition(result, session_dir, job)
    finally:
        lock.release()


def prepare_generation_job(
        job_path,
        *,
        claim_id,
        expected_epoch,
        project_root=None,
    ):
    session_dir, job, _state = _resolve_current_job(job_path)
    request_path = _request_path_for_job(session_dir, job)
    result = prepare_generation_transaction(
        request_path,
        project_root=project_root,
        generation_job_lease=generation_job_lease(job),
        generation_job_claim_id=claim_id,
        generation_job_expected_epoch=expected_epoch,
    )
    if result.get("status") == "job_blocked":
        issue_owner = {
            "type": "generation_admission_gap",
            "category": result.get("job_failure_category"),
            "errors": list(result.get("errors") or []),
        }
        result = publish_pretransaction_job_failure(
            session_dir,
            (job.get("request") or {})["request_id"],
            claim_id=claim_id,
            expected_epoch=expected_epoch,
            expected_phase="implementation",
            category=str(
                result.get("job_failure_category")
                or "transaction_prepare_failed"
            ),
            next_action="review_generation_failure",
            issue_owner=issue_owner,
        )
    return _with_job_transition(result, session_dir, job)


def validate_generation_job_implementation(
        report_path,
        *,
        claim_id,
        expected_epoch,
        project_root=None,
    ):
    session_dir, job = _job_for_transaction_report(report_path)
    result = finish_generation_transaction(
        report_path,
        derive_changed_files=True,
        validate_only=True,
        project_root=project_root,
        generation_job_claim_id=claim_id,
        generation_job_expected_epoch=expected_epoch,
    )
    return _with_job_transition(result, session_dir, job)


def finish_generation_job(
        report_path,
        *,
        claim_id,
        expected_epoch,
        project_root=None,
        summary="",
    ):
    session_dir, job = _job_for_transaction_report(report_path)
    result = finish_generation_transaction(
        report_path,
        derive_changed_files=True,
        validate_only=False,
        summary=summary,
        project_root=project_root,
        generation_job_claim_id=claim_id,
        generation_job_expected_epoch=expected_epoch,
    )
    return _with_job_transition(result, session_dir, job)


def abort_generation_job(
        report_path,
        *,
        reason,
        claim_id,
        expected_epoch,
        project_root=None,
        allow_project_guard_drift=False,
    ):
    session_dir, job = _job_for_transaction_report(report_path)
    result = abort_generation_transaction(
        report_path,
        reason=reason,
        project_root=project_root,
        generation_job_claim_id=claim_id,
        generation_job_expected_epoch=expected_epoch,
        allow_project_guard_drift=allow_project_guard_drift,
    )
    return _with_job_transition(result, session_dir, job)


def reconcile_generation_job_runtime(
        job_path,
        *,
        claim_id,
        expected_epoch,
    ):
    session_dir, job, state = _resolve_known_job(job_path)
    lock = RunWriteLock(session_dir).acquire()
    try:
        session_dir, job, state = _resolve_known_job(job_path)
        retired = retired_job_entry(state, job_id=job.get("job_id"))
        if retired is not None:
            execution = retired.get("job_execution") or {}
            result = load_generation_job_result(
                session_dir,
                retired.get("last_job_result") or {},
            )
            if any((
                execution.get("claim_id") != claim_id,
                result is None,
                (result.get("job") or {}).get("job_id")
                != job.get("job_id"),
                (result.get("job") or {}).get("job_fingerprint")
                != job.get("job_fingerprint"),
            )):
                raise ValueError("Generation Job terminal result不匹配")
            return _with_job_transition({
                **_job_result(session_dir, job, state),
                "last_job_result": retired.get("last_job_result") or {},
            }, session_dir, job)
        session_dir, job, state = _resolve_current_job(job_path)
        result = _reconcile_generation_job_runtime_locked(
            session_dir,
            job,
            state,
            claim_id=claim_id,
            expected_epoch=expected_epoch,
        )
        return _with_job_transition(result, session_dir, job)
    finally:
        lock.release()


def _reconcile_generation_job_runtime_locked(
        session_dir,
        job,
        state,
        *,
        claim_id,
        expected_epoch,
    ):
    execution = state.get("job_execution") or {}
    if any((
        state.get("status") != "running",
        execution.get("phase") not in {"runtime", "oracle"},
        execution.get("claim_id") != claim_id,
        execution.get("epoch") != expected_epoch,
    )):
        raise ValueError("Generation Job runtime context无效")
    transaction = execution.get("transaction") or {}
    report_path = _transaction_report_path(session_dir, transaction)
    report = _read_json(report_path)
    request_path = _request_path_for_job(session_dir, job)
    request = _read_json(request_path)
    plan = load_generation_plan(session_dir, state, request)
    if plan is None:
        raise ValueError("Generation Job runtime缺少有效Plan")
    provenance = generation_provenance_from_artifacts(
        request,
        plan,
        report,
    )
    target = request.get("target") or {}
    feature = target.get("feature") or {}
    scenario = target.get("scenario") or {}
    match = latest_matching_run_result(
        feature.get("source_relpath"),
        scenario.get("name"),
        example_id=scenario.get("example_id"),
        generation_provenance=provenance,
        project_root=report.get("project_root"),
    )
    if match is None:
        return {
            **_job_result(session_dir, job, state),
            "status": "waiting_runtime",
            "next_action": "run_bound_generation_profile",
        }
    run_result_path, run_result, _scenario = match
    matrix = latest_runtime_matrix_receipt(
        report.get("project_root"),
        report.get("transaction_id"),
    )
    quality = evaluate_generation_quality(
        request,
        plan,
        report,
        run_result,
        runtime_matrix=matrix[1] if matrix is not None else None,
    )
    project_root = Path(report.get("project_root") or "").resolve()
    try:
        run_result_relpath = Path(run_result_path).resolve().relative_to(
            project_root
        ).as_posix()
    except ValueError as error:
        raise ValueError("Run Result path越出Job项目") from error
    runtime_owner = {
        "type": "run_result",
        "path": run_result_relpath,
        "run_result_id": run_result.get("run_result_id"),
        "fingerprint": run_result.get("fingerprint"),
        "status": run_result.get("status"),
    }
    if run_result.get("status") != "passed":
        return publish_runtime_job_outcome(
            session_dir,
            request["request_id"],
            report_path,
            report,
            expected_epoch=expected_epoch,
            claim_id=claim_id,
            status="failed",
            category="runtime_failed",
            next_action="review_runtime_failure",
            runtime_owner=runtime_owner,
            completed_at=run_result.get("published_at"),
        )
    if quality.get("runtime_matrix_required") and matrix is None:
        if execution.get("phase") == "runtime":
            return advance_job_to_oracle(
                session_dir,
                request["request_id"],
                report_path,
                report,
                expected_epoch=expected_epoch,
                claim_id=claim_id,
            )
        return {
            **_job_result(session_dir, job, state),
            "status": "waiting_oracle",
            "next_action": "run_required_runtime_matrix",
        }
    oracle_owner = (
        {
            "type": "runtime_matrix",
            "path": Path(matrix[0]).resolve().relative_to(
                project_root
            ).as_posix(),
            "fingerprint": matrix[1].get("fingerprint"),
            "status": (
                "passed"
                if _oracle_passed(quality) is True
                else "failed"
            ),
        }
        if matrix is not None
        else None
    )
    passed = bool(
        _quality_passed(quality) is True
        and (
            not quality.get("runtime_matrix_required")
            or _oracle_passed(quality) is True
        )
    )
    return publish_runtime_job_outcome(
        session_dir,
        request["request_id"],
        report_path,
        report,
        expected_epoch=expected_epoch,
        claim_id=claim_id,
        status="completed" if passed else "failed",
        category="runtime_validated" if passed else "oracle_failed",
        next_action=(
            "review_generation_result"
            if passed
            else "review_oracle_failure"
        ),
        runtime_owner=runtime_owner,
        oracle_owner=oracle_owner,
        completed_at=(
            (matrix[1] if matrix is not None else run_result).get(
                "published_at"
            )
            or (matrix[1] if matrix is not None else run_result).get(
                "created_at"
            )
        ),
    )


def _quality_passed(quality):
    if not isinstance(quality, dict):
        return None
    return quality.get("quality_passed")


def _oracle_passed(quality):
    if not isinstance(quality, dict):
        return None
    return quality.get("oracle_passed")


def _resolve_current_job(job_path):
    session_dir, job, state = _resolve_known_job(job_path)
    pointer = state.get("current_job") or {}
    if pointer.get("job_id") != job.get("job_id"):
        raise ValueError("Generation Job不是Workflow current Job")
    return session_dir, job, state


def _resolve_known_job(job_path):
    job_path = Path(job_path).resolve()
    if len(job_path.parents) < 4:
        raise ValueError("Generation Job path无效")
    session_dir = job_path.parents[3]
    request_id = job_path.parent.name
    state = load_workflow_state(session_dir, request_id)
    pointers = [state.get("current_job") or {}]
    pointers.extend(
        (entry or {}).get("job") or {}
        for entry in state.get("retired_jobs") or ()
    )
    for pointer in pointers:
        expected_path = Path(str(pointer.get("path") or ""))
        expected_path = (
            expected_path.resolve()
            if expected_path.is_absolute()
            else (session_dir / expected_path).resolve()
        )
        if expected_path != job_path:
            continue
        job = load_generation_job(session_dir, pointer)
        if job is None:
            raise ValueError("Generation Job identity无效")
        return session_dir, job, state
    raise ValueError("Generation Job不是Workflow active或retired Job")


def _decision_artifacts(session_dir, request, state):
    decision = state.get("decision") or {}
    pack = load_decision_pack(
        session_dir,
        decision.get("pack") or {},
        request,
        brief_fingerprint=(state.get("brief") or {}).get(
            "brief_fingerprint"
        ),
    )
    if pack is None:
        raise ValueError("Generation admission缺少有效Decision Pack")
    answers = load_answer_record(
        session_dir,
        decision.get("answers") or {},
        request,
        pack,
    )
    return pack, answers or {}


def _candidate_job_inspect(session_dir, request, state, job):
    return {
        "generation_job_service_version": "1.0",
        "status": "ready",
        "next_action": "start_generation_job",
        "request_id": request.get("request_id"),
        "job_id": job.get("job_id"),
        "job_path": (
            Path("ai")
            / "generation-jobs"
            / request["request_id"]
            / f"job-{job['job_fingerprint']}.json"
        ).as_posix(),
        "job_fingerprint": job.get("job_fingerprint"),
        "generation_profile": _projected_generation_profile(job),
        "generation_admission": job.get("admission_receipt") or {},
        "job_execution": {
            "phase": "ready",
            "epoch": 1,
            "claim_id": None,
            "attempt_no": 0,
        },
        "execution_boundary": _projected_execution_boundary(job),
        "request_path": (job.get("request") or {}).get("path"),
        "brief_path": (job.get("brief") or {}).get("path"),
        "plan_path": None,
        "ai_capabilities": compact_ai_capability_contract(),
        "errors": [],
        "warnings": [],
    }


def _admission_rejected_result(request, admission):
    return {
        "generation_job_service_version": "1.0",
        "status": "rejected",
        "request_id": request.get("request_id"),
        "generation_admission": admission,
        "errors": list(admission.get("blocking_codes") or []),
        "warnings": [],
    }


def _request_path_for_job(session_dir, job):
    value = (job.get("request") or {}).get("path")
    if not value:
        raise ValueError("Generation Job缺少Request path")
    session_dir = Path(session_dir).resolve()
    path = Path(str(value))
    path = path.resolve() if path.is_absolute() else (session_dir / path).resolve()
    root = (session_dir / "ai" / "requests").resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError("Generation Job Request path越界") from error
    if not path.is_file():
        raise ValueError("Generation Job Request不存在")
    return path


def _brief_path_for_job(session_dir, job):
    value = (job.get("brief") or {}).get("path")
    path = _session_pointer_path(session_dir, value)
    root = (Path(session_dir).resolve() / "ai" / "generation-briefs").resolve()
    if path is None:
        raise ValueError("Generation Job缺少Brief path")
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError("Generation Job Brief path越界") from error
    if not path.is_file():
        raise ValueError("Generation Job Brief不存在")
    return path


def _session_pointer_path(session_dir, value):
    if not value:
        return None
    session_dir = Path(session_dir).resolve()
    path = Path(str(value))
    return path.resolve() if path.is_absolute() else (session_dir / path).resolve()


def _transaction_report_path(session_dir, pointer):
    value = pointer.get("path")
    if not value:
        raise ValueError("Generation Job缺少Transaction report path")
    session_dir = Path(session_dir).resolve()
    path = Path(str(value))
    path = path.resolve() if path.is_absolute() else (session_dir / path).resolve()
    root = (session_dir / "ai" / "generation-transactions").resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError("Generation Job Transaction path越界") from error
    if not path.is_file():
        raise ValueError("Generation Job Transaction report不存在")
    return path


def _require_job_query(job, query_name):
    allowed = set((job.get("execution_boundary") or {}).get(
        "allowed_queries"
    ) or ())
    if query_name not in allowed:
        raise ValueError(f"Generation Job不允许查询: {query_name}")


def _validate_implementation_packet_query(state, job, report, report_path):
    execution = (state or {}).get("job_execution") or {}
    transaction = execution.get("transaction") or {}
    manifest = report.get("implementation_manifest") or {}
    if any((
        (state or {}).get("status") != "running",
        execution.get("phase") != "implementation",
        execution.get("claim_id") != report.get("generation_job_claim_id"),
        report.get("request_id") != (job.get("request") or {}).get(
            "request_id"
        ),
        report.get("generation_job_lease") != generation_job_lease(job),
        transaction.get("transaction_id") != report.get("transaction_id"),
        transaction.get("implementation_manifest_fingerprint")
        != manifest.get("implementation_manifest_fingerprint"),
        _transaction_report_path(
            Path(report.get("session_dir") or ""), transaction
        ) != Path(report_path).resolve(),
    )):
        raise ValueError("Implementation Packet与当前Generation Job不一致")
    lease_errors = validate_generation_file_lease(
        report.get("project_root"),
        report.get("generation_file_lease"),
    )
    if lease_errors:
        raise ValueError(
            "Implementation Packet generation file lease无效: "
            + "; ".join(lease_errors)
        )


def _project_implementation_packet(packet, *, step_id=None, path=None):
    packet = dict(packet or {})
    requested_step_id = str(step_id or "").strip()
    requested_path = str(path or "").replace("\\", "/").lstrip("/")
    steps = [
        dict(item)
        for item in packet.get("steps") or ()
        if isinstance(item, dict)
    ]
    pages = [
        dict(item)
        for item in packet.get("pages") or ()
        if isinstance(item, dict)
    ]
    methods = [
        dict(item)
        for item in packet.get("methods") or ()
        if isinstance(item, dict)
    ]
    known_paths = {
        str(item.get("path") or "").replace("\\", "/")
        for item in [*steps, *pages, *methods]
        if item.get("path")
    }
    known_paths.update(
        str(item).replace("\\", "/")
        for item in packet.get("ai_editable_changes") or ()
    )
    if requested_step_id:
        steps = [
            item for item in steps
            if str(item.get("step_id") or "") == requested_step_id
        ]
        if len(steps) != 1:
            raise ValueError(
                f"Implementation Packet不存在目标Step: {requested_step_id}"
            )
    elif requested_path:
        if requested_path not in known_paths:
            raise ValueError(
                f"Implementation Packet路径不在冻结Manifest中: {requested_path}"
            )
        steps = [
            item for item in steps
            if str(item.get("path") or "").replace("\\", "/")
            == requested_path
        ]
    page_paths = {
        str((item.get("page") or {}).get("path") or "")
        for step in steps
        for item in [step, *(step.get("operations") or ())]
        if isinstance(item, dict)
        and (item.get("page") or {}).get("path")
    }
    if requested_path and requested_path in known_paths:
        page_paths.add(requested_path)
    pages = [
        item for item in pages
        if str(item.get("path") or "") in page_paths
    ]
    page_paths.update(str(item.get("path") or "") for item in pages)
    methods = [
        item for item in methods
        if str(item.get("path") or "") in page_paths
        or str(item.get("path") or "") == requested_path
    ]
    return {
        "implementation_packet_projection_version": "1.0",
        "implementation_packet_version": packet.get(
            "implementation_packet_version"
        ),
        "packet_fingerprint": _fingerprint(packet),
        "derived_from": dict(packet.get("derived_from") or {}),
        "ai_editable_changes": list(packet.get("ai_editable_changes") or ()),
        "system_owned_changes": list(packet.get("system_owned_changes") or ()),
        "pages": pages,
        "steps": steps,
        "methods": methods,
        "rule": packet.get("rule"),
    }


def _job_for_transaction_report(report_path):
    report_path = Path(report_path).resolve()
    report = _read_json(report_path)
    transaction_id = str(report.get("transaction_id") or "")
    if not transaction_id or report_path.name != "report.json":
        raise ValueError("Generation Job Transaction report路径无效")
    session_dir = report_path.parents[3]
    root = (session_dir / "ai" / "generation-transactions").resolve()
    try:
        relative = report_path.relative_to(root)
    except ValueError as error:
        raise ValueError("Generation Job Transaction report路径越界") from error
    if tuple(relative.parts) != (transaction_id, "report.json"):
        raise ValueError("Generation Job Transaction report路径与transaction不一致")
    reported_session = str(report.get("session_dir") or "")
    if not reported_session or Path(reported_session).resolve() != session_dir:
        raise ValueError("Generation Job Transaction session不一致")
    request_id = str(report.get("request_id") or "")
    lease = report.get("generation_job_lease") or {}
    if not generation_job_lease_is_valid(lease):
        raise ValueError("Generation Job Transaction lease无效")
    state = load_workflow_state(session_dir, request_id)
    candidates = [
        (state.get("current_job") or {}, state.get("job_execution") or {}),
        *(
            ((entry or {}).get("job") or {},
            (entry or {}).get("job_execution") or {})
            for entry in state.get("retired_jobs") or ()
        ),
    ]
    for pointer, execution in candidates:
        if any((
            pointer.get("job_id") != lease.get("job_id"),
            pointer.get("job_fingerprint") != lease.get("job_fingerprint"),
            pointer.get("nonce") != lease.get("job_nonce"),
        )):
            continue
        job = load_generation_job(session_dir, pointer)
        transaction = execution.get("transaction") or {}
        if job is None or not transaction:
            continue
        if _transaction_report_path(session_dir, transaction) != report_path:
            continue
        return session_dir, job
    raise ValueError("Generation Transaction与Generation Job不一致")


def _job_result(session_dir, job, workflow):
    path = (
        Path(session_dir)
        / "ai"
        / "generation-jobs"
        / (job.get("request") or {})["request_id"]
        / f"job-{job['job_fingerprint']}.json"
    ).resolve()
    retired = retired_job_entry(workflow, job_id=job.get("job_id"))
    execution = (
        (retired or {}).get("job_execution")
        or workflow.get("job_execution")
        or {}
    )
    result = {
        "generation_job_service_version": "1.0",
        "status": (retired or {}).get("status") or workflow.get("status"),
        "next_action": (retired or {}).get("next_action") or workflow.get("next_action"),
        "request_id": (job.get("request") or {}).get("request_id"),
        "job_id": job.get("job_id"),
        "job_path": str(path),
        "job_fingerprint": job.get("job_fingerprint"),
        "generation_profile": _projected_generation_profile(job),
        "generation_admission": job.get("admission_receipt") or {},
        "job_execution": execution,
        "job_lifecycle_timing": execution.get("job_lifecycle_timing"),
        "execution_boundary": _projected_execution_boundary(job),
        "errors": list((retired or {}).get("errors") or ()),
        "warnings": [],
    }
    result["job_transition"] = _job_transition(result)
    return result


def _job_transition(result):
    execution = result.get("job_execution") or {}
    return {
        "phase": execution.get("phase"),
        "epoch": execution.get("epoch"),
        "claim_id": execution.get("claim_id"),
        "attempt_no": execution.get("attempt_no"),
        "next_action": result.get("next_action"),
    }


def _with_job_transition(result, session_dir, job):
    workflow = load_workflow_state(
        session_dir,
        (job.get("request") or {}).get("request_id"),
    )
    projected = _job_result(session_dir, job, workflow)
    return {
        **result,
        "job_transition": projected["job_transition"],
        "job_lifecycle_timing": projected.get("job_lifecycle_timing"),
    }


def _projected_generation_profile(job):
    profile = job.get("profile_lease") or {}
    return {
        key: profile.get(key)
        for key in (
            "generation_profile_version",
            "profile_id",
            "label",
            "profile_fingerprint",
        )
        if profile.get(key) not in (None, "", [], {})
    }


def _projected_execution_boundary(job):
    boundary = job.get("execution_boundary") or {}
    return {
        key: boundary.get(key)
        for key in ("allowed_queries", "validation_stages")
        if boundary.get(key) not in (None, "", [], {})
    }


def _read_json(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON必须是object: {path}")
    return value


def _fingerprint(value):
    return hashlib.sha256(json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()