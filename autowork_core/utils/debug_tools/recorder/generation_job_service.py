from __future__ import annotations

import json
from pathlib import Path

from autowork_core.utils.debug_tools.recorder.decision_pack import (
    load_answer_record,
    load_decision_pack,
)
from autowork_core.utils.debug_tools.recorder.action_knowledge import (
    query_action_knowledge,
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
    generation_job_pointer,
    load_generation_job,
    persist_generation_job,
)
from autowork_core.utils.debug_tools.recorder.generation_job_result import (
    advance_job_to_oracle,
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
from autowork_core.utils.debug_tools.recorder.generation_transaction import (
    abort_generation_transaction,
    finish_generation_transaction,
    prepare_generation_transaction,
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
    JOB_WORKFLOW_STATE_VERSION,
    claim_generation_job,
    load_workflow_state,
    publish_generation_job,
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
        if existing.get("workflow_state_version") == (
                JOB_WORKFLOW_STATE_VERSION
        ) and existing.get("current_job"):
            job = load_generation_job(
                session_dir,
                existing.get("current_job") or {},
            )
            if job is None:
                raise ValueError("Workflow current Job identity无效")
            selected = str(profile_id or "generation_first")
            if (job.get("profile_lease") or {}).get("profile_id") != selected:
                raise ValueError(
                    "当前Request已有其他Generation Profile的Job"
                )
            return _job_result(session_dir, job, existing)

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
        pack, answers = _decision_artifacts(session_dir, request, state)
        contract_lease = generation_contract_lease(
            session_dir,
            write=False,
        )
        admission = project_generation_admission(
            request=request,
            state=state,
            context_budget=inspected.get("ai_context_budget") or {},
            request_identity_valid=request_identity_is_valid(request),
            profile_id=profile_id,
            decision_pack=pack,
            answer_record=answers,
            enforcement="active",
            generation_contract_lease=contract_lease,
        )
        if admission.get("status") != "passed":
            return {
                "generation_job_service_version": "1.0",
                "status": "rejected",
                "request_id": request.get("request_id"),
                "generation_admission": admission,
                "errors": list(admission.get("blocking_codes") or []),
                "warnings": [],
            }
        job = build_generation_job(
            request,
            state,
            admission,
            contract_lease,
            activation="active",
        )
        path, job = persist_generation_job(session_dir, job)
        pointer = generation_job_pointer(session_dir, job, path)
        workflow = publish_generation_job(
            session_dir,
            request["request_id"],
            pointer,
            expected_epoch=0,
        )
        return _job_result(session_dir, job, workflow)
    finally:
        lock.release()


def start_generation_job(job_path, *, expected_epoch):
    session_dir, job, state = _resolve_current_job(job_path)
    lock = RunWriteLock(session_dir).acquire()
    try:
        current = load_workflow_state(
            session_dir,
            (job.get("request") or {}).get("request_id"),
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
        return _job_result(session_dir, job, workflow)
    finally:
        lock.release()


def inspect_generation_job(job_path):
    session_dir, job, _state = _resolve_current_job(job_path)
    request_path = _request_path_for_job(session_dir, job)
    state = inspect_workflow(request_path, write=False)
    result = _job_result(session_dir, job, state)
    result["status"] = state.get("status")
    result["next_action"] = state.get("next_action")
    request = _read_json(request_path)
    brief_path = _brief_path_for_job(session_dir, job)
    plan_value = state.get("plan") or {}
    plan_path = _session_pointer_path(session_dir, plan_value.get("path"))
    result.update({
        "request_path": str(request_path),
        "brief_path": str(brief_path),
        "plan_path": str(plan_path) if plan_path else None,
        "execution_boundary": job.get("execution_boundary") or {},
        "ai_capabilities": compact_ai_capability_contract(),
    })
    from autowork_core.utils.debug_tools.recorder.generation_workflow import (
        _with_context_budget,
        build_ai_context_budget,
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
        plan_context=None,
        job_path=job_path,
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
    session_dir, job, _state = _resolve_current_job(job_path)
    result = query_request_evidence(
        _request_path_for_job(session_dir, job),
        evidence_id=evidence_id,
        step_id=step_id,
        action_id=action_id,
        list_only=list_only,
    )
    return {**result, "job_id": job.get("job_id")}


def compare_generation_job_takes(job_path, *, step_id, take_ids=()):
    session_dir, job, _state = _resolve_current_job(job_path)
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
    session_dir, job, _state = _resolve_current_job(job_path)
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
        return result
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
    return prepare_generation_transaction(
        request_path,
        project_root=project_root,
        generation_job_lease=generation_job_lease(job),
        generation_job_claim_id=claim_id,
        generation_job_expected_epoch=expected_epoch,
    )


def validate_generation_job_implementation(
        report_path,
        *,
        claim_id,
        expected_epoch,
        project_root=None,
    ):
    return finish_generation_transaction(
        report_path,
        derive_changed_files=True,
        validate_only=True,
        project_root=project_root,
        generation_job_claim_id=claim_id,
        generation_job_expected_epoch=expected_epoch,
    )


def finish_generation_job(
        report_path,
        *,
        claim_id,
        expected_epoch,
        project_root=None,
        summary="",
    ):
    return finish_generation_transaction(
        report_path,
        derive_changed_files=True,
        validate_only=False,
        summary=summary,
        project_root=project_root,
        generation_job_claim_id=claim_id,
        generation_job_expected_epoch=expected_epoch,
    )


def abort_generation_job(
        report_path,
        *,
        reason,
        claim_id,
        expected_epoch,
        project_root=None,
    ):
    return abort_generation_transaction(
        report_path,
        reason=reason,
        project_root=project_root,
        generation_job_claim_id=claim_id,
        generation_job_expected_epoch=expected_epoch,
    )


def reconcile_generation_job_runtime(
        job_path,
        *,
        claim_id,
        expected_epoch,
    ):
    session_dir, job, state = _resolve_current_job(job_path)
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
                if quality.get("independent_oracle_passed") is True
                else "failed"
            ),
        }
        if matrix is not None
        else None
    )
    passed = bool(
        quality.get("runtime_passed") is True
        and (
            not quality.get("runtime_matrix_required")
            or quality.get("independent_oracle_passed") is True
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


def _resolve_current_job(job_path):
    job_path = Path(job_path).resolve()
    if len(job_path.parents) < 4:
        raise ValueError("Generation Job path无效")
    session_dir = job_path.parents[3]
    request_id = job_path.parent.name
    state = load_workflow_state(session_dir, request_id)
    pointer = state.get("current_job") or {}
    expected_path = Path(str(pointer.get("path") or ""))
    expected_path = (
        expected_path.resolve()
        if expected_path.is_absolute()
        else (session_dir / expected_path).resolve()
    )
    if expected_path != job_path:
        raise ValueError("Generation Job不是Workflow current Job")
    job = load_generation_job(session_dir, pointer)
    if job is None:
        raise ValueError("Generation Job identity无效")
    return session_dir, job, state


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


def _job_result(session_dir, job, workflow):
    path = (
        Path(session_dir)
        / "ai"
        / "generation-jobs"
        / (job.get("request") or {})["request_id"]
        / f"job-{job['job_fingerprint']}.json"
    ).resolve()
    return {
        "generation_job_service_version": "1.0",
        "status": workflow.get("status"),
        "next_action": workflow.get("next_action"),
        "request_id": (job.get("request") or {}).get("request_id"),
        "job_id": job.get("job_id"),
        "job_path": str(path),
        "job_fingerprint": job.get("job_fingerprint"),
        "generation_profile": job.get("profile_lease") or {},
        "generation_admission": job.get("admission_receipt") or {},
        "job_execution": workflow.get("job_execution") or {},
        "execution_boundary": job.get("execution_boundary") or {},
        "errors": [],
        "warnings": [],
    }


def _read_json(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON必须是object: {path}")
    return value