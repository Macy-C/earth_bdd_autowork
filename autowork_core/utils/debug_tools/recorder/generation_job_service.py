from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from config.paths import Paths

from autowork_core.utils.debug_tools.recorder.decision_pack import (
    ANSWER_VERSION,
    answer_pointer,
    load_answer_record,
    load_decision_pack,
    persist_answers,
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
from autowork_core.utils.debug_tools.recorder.code_manifest import (
    code_manifest_matches_transaction,
)
from autowork_core.utils.debug_tools.recorder.evidence_context import (
    compare_request_takes,
    query_request_evidence,
)
from autowork_core.utils.debug_tools.recorder.generation_contract import (
    compact_ai_capability_contract,
    generation_contract_lease,
    generation_contract_lease_matches,
)
from autowork_core.utils.debug_tools.recorder.generation_capsule import (
    build_generation_capsule,
    generation_capsule_input_snapshot,
    generation_capsule_pointer,
    generation_capsule_source_files,
    load_generation_capsule,
    persist_generation_capsule,
)
from autowork_core.utils.debug_tools.recorder.generation_workspace_projection import (
    build_generation_workspace_projection,
    generation_workspace_projection_pointer,
    generation_workspace_projection_summary,
    load_generation_workspace_projection,
    persist_generation_workspace_projection,
)
from autowork_core.utils.debug_tools.recorder.generation_job import (
    GENERATION_JOB_VERSION,
    build_generation_job,
    generation_job_design_mode,
    generation_job_lease,
    generation_job_lease_is_valid,
    generation_job_pointer,
    load_generation_job,
    persist_generation_job,
)
from autowork_core.utils.debug_tools.recorder.generation_job_result import (
    JOB_STAGE_NAMES,
    advance_job_to_oracle,
    delivery_review_channel_from_source,
    delivery_source_from_implementation_receipt,
    generation_job_result_identity_is_valid,
    load_generation_job_result,
    publish_pretransaction_job_failure,
    publish_runtime_job_outcome,
)
from autowork_core.utils.debug_tools.recorder.generation_design import (
    GENERATION_AMBIGUITY_CHOICE_PATCH_VERSION,
    GENERATION_ASSERTION_CHOICE_PATCH_VERSION,
    GENERATION_DESIGN_VERSION,
    GENERATION_METHOD_CHOICE_PATCH_VERSION,
    GENERATION_NAMING_PATCH_VERSION,
    GENERATION_OPERATION_CHOICE_PATCH_VERSION,
    GENERATION_VALUE_SOURCE_CHOICE_PATCH_VERSION,
    GenerationAmbiguityChoiceRequired,
    GenerationAssertionChoiceRequired,
    GenerationDesignValidationError,
    GenerationMethodChoiceRequired,
    GenerationOperationChoiceRequired,
    GenerationValueSourceChoiceRequired,
    build_generation_baseline_design,
    compile_generation_design,
)
from autowork_core.utils.debug_tools.recorder.identity import (
    assertion_candidate_key,
    operation_choice_key,
)
from autowork_core.utils.debug_tools.recorder.code_reuse_index import (
    candidate_step_pattern_contracts,
    step_pattern_contract_matches,
)
from autowork_core.utils.debug_tools.recorder.generation_plan import (
    PLAN_VERSION,
    load_generation_plan,
    plan_pointer,
)
from autowork_core.utils.debug_tools.recorder.generation_validation import (
    validate_implementation_resolution_snapshot,
    validate_owner_resolution_snapshot,
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
    ALLOWED_WRITE_ROOTS,
    _abort_generation_transaction_locked,
    abort_generation_transaction,
    finish_generation_transaction,
    prepare_generation_transaction,
    transaction_code_snapshot_matches,
)
from autowork_core.utils.debug_tools.recorder.generation_task_bundle import (
    generation_task_bundle_pointer,
    load_generation_task_bundle,
    load_generation_task_fragment,
    persist_generation_task_bundle,
    project_generation_task_bundle_index,
    validate_generation_task_bundle_closure,
)
from autowork_core.utils.debug_tools.recorder.business_review import (
    build_business_review_questions,
    build_business_review_patch_from_decisions,
    build_business_review_requirement,
    build_business_review_workset,
    validate_business_review_patch,
)
from autowork_core.utils.debug_tools.recorder.generation_diff import (
    build_generation_diff_summary,
    generation_diff_pointer_is_valid,
    load_generation_diff,
)
from autowork_core.utils.debug_tools.recorder.generation_file_lock import (
    generation_path_has_reparse_point,
    validate_generation_file_lease,
)
from autowork_core.utils.debug_tools.recorder.implementation_manifest import (
    build_implementation_packet,
    implementation_manifest_identity_is_valid,
)
from autowork_core.utils.debug_tools.recorder.implementation_materializer import (
    load_implementation_scaffold_candidate,
    materialize_implementation_candidate,
    materialize_implementation_scaffold,
)
from autowork_core.utils.debug_tools.recorder.generation_profile import (
    project_generation_admission,
    resolve_generation_profile,
)
from autowork_core.utils.debug_tools.recorder.generation_product_state import (
    related_product_locator_paths,
    resolve_product_state_facts,
)
from autowork_core.utils.debug_tools.recorder.request_repository import (
    request_identity_is_valid,
    session_dir_for_request_path,
)
from autowork_core.utils.debug_tools.recorder.run_lock import RunWriteLock
from autowork_core.utils.debug_tools.recorder.writer import (
    _atomic_write_text,
    write_json_atomic,
)
from autowork_core.utils.debug_tools.recorder.workflow_state import (
    WORKFLOW_STATE_VERSION,
    claim_generation_job,
    fail_generation_job_integrity,
    load_workflow_state,
    publish_generation_job,
    record_generation_job_interaction,
    refresh_running_generation_job,
    replace_generation_job,
    retired_job_entry,
    transition_generation_job,
    write_workflow_state,
)
from autowork_core.utils.debug_tools.recorder.workflow_service import (
    _compiled_decision_patch,
    fresh_workflow_state_for_generation_admission,
    inspect_workflow,
    submit_generation_design,
)
from autowork_core.utils.debug_tools.recorder.transaction_integrity import (
    TRANSACTION_VERSION,
    completed_report_fingerprint,
    transaction_result_fingerprint,
)
from autowork_core.runtime.reporting.oracle_registry import (
    latest_runtime_matrix_receipt,
)
from autowork_core.runtime.reporting.run_result_bridge import (
    generation_provenance_from_artifacts,
    latest_matching_run_result,
)


GENERATION_JOB_SERVICE_VERSION = "1.16"
RECORDER_HOST_CONTROL_VERSION = "1.14"
CANDIDATE_DELIVERY_MANIFEST_VERSION = "1.0"
CANDIDATE_INDEX_VERSION = "1.0"
CANDIDATE_INDEX_MAX_LINES_PER_READ = 50
_TYPED_PATCH_DESIGN_REQUIRED_REASONS = frozenset({
    "system_baseline_needs_ai_naming",
    "system_baseline_needs_ai_ambiguity_choice",
    "system_baseline_needs_ai_assertion_choice",
    "system_baseline_needs_ai_method_choice",
    "system_baseline_needs_ai_operation_choice",
    "system_baseline_needs_ai_value_source_choice",
})


def _record_job_interaction(
        session_dir,
        job,
        state,
        *,
        event,
        status,
        next_action,
        details=None,
    ):
    execution = state.get("job_execution") or {}
    if state.get("status") != "running" or execution.get("phase") != "design":
        return state
    job_id = job.get("job_id")
    job_fingerprint = job.get("job_fingerprint")
    if not job_id or not job_fingerprint:
        return state
    return record_generation_job_interaction(
        session_dir,
        (job.get("request") or {})["request_id"],
        event=event,
        status=status,
        next_action=next_action,
        job_id=job_id,
        job_fingerprint=job_fingerprint,
        claim_id=execution.get("claim_id"),
        expected_epoch=execution.get("epoch"),
        expected_phase="design",
        details=details,
    )

def record_generation_job_result_interaction(
        job_path,
        result,
        *,
        event,
        status,
        next_action,
        details=None,
    ):
    session_dir, job, state = _resolve_current_job(job_path)
    transition = (result or {}).get("job_transition") or {}
    execution = state.get("job_execution") or {}
    if any((
            transition.get("phase") != "design",
            execution.get("phase") != "design",
            transition.get("epoch") != execution.get("epoch"),
            transition.get("claim_id") != execution.get("claim_id"),
    )):
        return state
    return _record_job_interaction(
        session_dir,
        job,
        state,
        event=event,
        status=status,
        next_action=next_action,
        details=details,
    )


def admit_generation_job(request_path, *, profile_id=None, force_new=False):
    request_path = Path(request_path).resolve()
    request = _read_json(request_path)
    session_dir = session_dir_for_request_path(request_path, request)
    lock = RunWriteLock(session_dir).acquire()
    try:
        existing = load_workflow_state(
            session_dir,
            request.get("request_id"),
        )
        if force_new:
            _validate_force_new_generation_job(existing)
        else:
            terminal_job = _current_terminal_job_if_still_valid(
                session_dir,
                request,
                existing,
                profile_id=profile_id,
            )
            if terminal_job is not None:
                return _job_result(session_dir, terminal_job, existing)
        if existing.get("current_job"):
            existing = _refresh_current_job_for_admission(
                session_dir,
                request,
                request_path,
                existing,
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
                execution = existing.get("job_execution") or {}
                if force_new:
                    replacement = {
                        "pointer": dict(existing["current_job"]),
                        "epoch": execution.get("epoch"),
                        "reason": (
                            "failed_current_job_cleanup"
                            if _failed_current_job_can_be_replaced(existing)
                            else "force_new_generation_job"
                        ),
                    }
                    if replacement["epoch"] is None:
                        raise ValueError(
                            "当前Generation Job缺少可替换epoch"
                        )
                elif _legacy_job_needs_capsule_refresh(
                        session_dir,
                        existing.get("current_job") or {},
                        request,
                ) and not (
                        existing.get("status") == "running"
                        or execution.get("phase") in {
                            "design",
                            "implementation",
                            "runtime",
                            "oracle",
                        }
                ):
                    replacement = {
                        "pointer": dict(existing["current_job"]),
                        "epoch": execution.get("epoch"),
                        "reason": "job_contract_refresh",
                    }
                    if replacement["epoch"] is None:
                        raise ValueError(
                            "当前Generation Job缺少可替换epoch"
                        )
                elif any((
                        existing.get("status") != "failed",
                        execution.get("phase") != "failed",
                        "job_integrity_failed"
                        not in set(existing.get("errors") or ()),
                )):
                    raise ValueError("Workflow current Job identity无效")
                else:
                    replacement = {
                        "pointer": dict(existing["current_job"]),
                        "epoch": execution["epoch"],
                        "reason": "integrity_failed",
                    }
            else:
                selected = str(profile_id or "generation_first")
                current_profile = (job.get("profile_lease") or {}).get("profile_id")
                execution = existing.get("job_execution") or {}
                if force_new:
                    replacement = {
                        "pointer": dict(existing["current_job"]),
                        "epoch": execution.get("epoch"),
                        "reason": (
                            "failed_current_job_cleanup"
                            if _failed_current_job_can_be_replaced(existing)
                            else "force_new_generation_job"
                        ),
                    }
                    if replacement["epoch"] is None:
                        raise ValueError(
                            "当前Generation Job缺少可替换epoch"
                        )
                elif _current_job_blocks_admission(existing):
                    if current_profile != selected:
                        raise ValueError(
                            "运行中的Generation Job不能切换Profile"
                        )
                    return _job_result(session_dir, job, existing)
                elif (
                        existing.get("status") == "ready"
                        and execution.get("phase") == "ready"
                        and current_profile == selected
                        and generation_contract_lease_matches(
                            session_dir,
                            job.get("generation_contract_lease"),
                        )
                ):
                    source_errors = _frozen_job_source_candidate_errors(
                        session_dir,
                        job,
                    )
                    if not source_errors:
                        return _job_result(session_dir, job, existing)
                    _archive_generation_job_brief(session_dir, job)
                    publish_pretransaction_job_failure(
                        session_dir,
                        request.get("request_id"),
                        claim_id=None,
                        expected_epoch=execution["epoch"],
                        expected_phase="ready",
                        category="source_candidates_stale",
                        next_action="refresh_generation_job",
                        issue_owner={
                            "type": "frozen_source_candidates",
                            "errors": source_errors,
                        },
                    )
                    existing = load_workflow_state(
                        session_dir,
                        request.get("request_id"),
                    )
                else:
                    replacement = {
                        "pointer": dict(existing["current_job"]),
                        "epoch": execution.get("epoch"),
                        "reason": (
                            "failed_current_job_cleanup"
                            if _failed_current_job_can_be_replaced(existing)
                            else
                            "switch_profile"
                            if current_profile != selected
                            else "job_refresh"
                        ),
                    }
                    if replacement["epoch"] is None:
                        raise ValueError(
                            "当前Generation Job缺少可替换epoch"
                        )

        return _publish_new_generation_job_locked(
            session_dir,
            request,
            request_path,
            profile_id=profile_id,
            replacement=replacement,
        )
    finally:
        lock.release()


def _validate_force_new_generation_job(state):
    return


def _failed_current_job_can_be_replaced(state):
    execution = (state or {}).get("job_execution") or {}
    active = (state or {}).get("active_transaction") or {}
    return bool(
        (state or {}).get("status") == "failed"
        and (state or {}).get("current_job")
        and not active.get("transaction_id")
        and execution.get("phase") in {
            "design",
            "implementation",
            "runtime",
            "oracle",
            "failed",
        }
    )


def _current_job_blocks_admission(state):
    if _failed_current_job_can_be_replaced(state):
        return False
    execution = (state or {}).get("job_execution") or {}
    return bool(
        (state or {}).get("status") == "running"
        or execution.get("phase") in {
            "design",
            "implementation",
            "runtime",
            "oracle",
        }
    )


def _refresh_current_job_for_admission(
        session_dir,
        request,
        request_path,
        existing,
    ):
    inspected = inspect_workflow(request_path, write=False)
    if inspected.get("current_job") != existing.get("current_job"):
        return inspected
    errors = list(inspected.get("errors") or [])
    execution = existing.get("job_execution") or {}
    if existing.get("status") == "failed" and existing.get("current_job"):
        return inspected
    if (
            "job_integrity_failed" in set(errors)
            and _legacy_job_needs_capsule_refresh(
                session_dir,
                existing.get("current_job") or {},
                request,
            )
            and not (
                existing.get("status") == "running"
                or execution.get("phase") in {
                    "design",
                    "implementation",
                    "runtime",
                    "oracle",
                }
            )
    ):
        return existing
    if inspected.get("status") != "failed" or not errors:
        return inspected
    if "job_integrity_failed" in set(errors):
        return inspected
    expected_epoch = execution.get("epoch")
    if expected_epoch is None:
        return inspected
    active = existing.get("active_transaction") or {}
    report_path = _session_pointer_path(session_dir, active.get("path"))
    if report_path and report_path.is_file():
        try:
            _abort_generation_transaction_locked(
                report_path,
                reason=(
                    "Current Generation Job became stale before readmission: "
                    + "; ".join(str(item) for item in errors)
                ),
                project_root=_project_root_for_generation_session(session_dir),
                generation_job_claim_id=execution.get("claim_id"),
                generation_job_expected_epoch=expected_epoch,
                allow_project_guard_drift=True,
                allow_generation_root_drift=True,
            )
        except ValueError as error:
            if not _active_transaction_protocol_identity_error(
                    report_path,
                    error,
            ):
                raise
            publish_pretransaction_job_failure(
                session_dir,
                request.get("request_id"),
                claim_id=execution.get("claim_id"),
                expected_epoch=expected_epoch,
                expected_phase=str(execution.get("phase") or "implementation"),
                category="transaction_protocol_changed",
                next_action="refresh_generation_job",
                issue_owner={
                    "type": "generation_transaction_protocol",
                    "transaction_id": active.get("transaction_id"),
                    "errors": [str(error)],
                },
            )
        return load_workflow_state(session_dir, request.get("request_id"))
    publish_pretransaction_job_failure(
        session_dir,
        request.get("request_id"),
        claim_id=execution.get("claim_id"),
        expected_epoch=expected_epoch,
        expected_phase=str(execution.get("phase") or "ready"),
        category=str(errors[0] or "job_freshness_failed"),
        next_action="refresh_generation_job",
        issue_owner={
            "type": "generation_admission_gap",
            "errors": errors,
        },
    )
    return load_workflow_state(session_dir, request.get("request_id"))


def _legacy_job_needs_capsule_refresh(session_dir, pointer, request):
    raw_path = _session_pointer_path(session_dir, (pointer or {}).get("path"))
    if raw_path is None or not raw_path.is_file():
        return False
    try:
        raw_job = _read_json(raw_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    job_request = raw_job.get("request") or {}
    return bool(
        job_request.get("request_id") == request.get("request_id")
        and job_request.get("request_fingerprint")
        == request.get("request_fingerprint")
        and (
            raw_job.get("generation_job_version") != GENERATION_JOB_VERSION
            or not raw_job.get("generation_capsule")
        )
    )


def _active_transaction_protocol_identity_error(report_path, error):
    message = str(error)
    if (
            "GenerationTransactionV3 身份无效" not in message
            or "candidate_preflight" not in message
    ):
        return False
    try:
        report = _read_json(report_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return bool(
        report.get("status") == "running"
        and (
            report.get("transaction_version") != TRANSACTION_VERSION
            or not report.get("candidate_preflight")
        )
    )


def _current_terminal_job_if_still_valid(
        session_dir,
        request,
        state,
        *,
        profile_id=None,
    ):
    if any((
        state.get("workflow_state_version") != WORKFLOW_STATE_VERSION,
        state.get("current_job"),
        state.get("status") != "completed",
        not state.get("last_job_result"),
    )):
        return None
    result = load_generation_job_result(
        session_dir,
        state.get("last_job_result") or {},
    )
    if result is None or result.get("status") != "completed":
        return None
    retired = retired_job_entry(
        state,
        job_id=(result.get("job") or {}).get("job_id"),
        job_fingerprint=(result.get("job") or {}).get("job_fingerprint"),
    )
    if retired is None:
        return None
    job = load_generation_job(session_dir, (retired.get("job") or {}))
    if job is None:
        return None
    selected = str(profile_id or "generation_first")
    if (job.get("profile_lease") or {}).get("profile_id") != selected:
        return None
    job_request = job.get("request") or {}
    if any((
        job_request.get("request_id") != request.get("request_id"),
        job_request.get("request_fingerprint")
        != request.get("request_fingerprint"),
        job_request.get("revision_seal")
        != (request.get("revision_snapshot") or {}).get("seal"),
    )):
        return None
    owner = (
        ((result.get("stages") or {}).get("transaction") or {}).get(
            "owner"
        ) or {}
    )
    if owner.get("owner") != "generation_transaction":
        return None
    try:
        report_path = _transaction_report_path(session_dir, owner)
        report = _read_json(report_path)
    except (OSError, ValueError):
        return None
    project_root = _project_root_for_generation_session(session_dir)
    plan_fingerprint = (
        (report.get("generation_plan") or {}).get("plan_fingerprint")
        or ((report.get("lease") or {}).get("plan") or {}).get(
            "plan_fingerprint"
        )
    )
    if any((
        report.get("status") not in {"completed", "completed_no_changes"},
        not generation_contract_lease_matches(
            session_dir,
            report.get("generation_contract_lease") or {},
        ),
        completed_report_fingerprint(report)
        != report.get("completion_fingerprint"),
        transaction_result_fingerprint(report)
        != report.get("result_fingerprint"),
        not code_manifest_matches_transaction(
            report.get("code_manifest"),
            request_id=request.get("request_id"),
            plan_fingerprint=plan_fingerprint,
            project_root=project_root,
            plan_audit=report.get("plan_conformance_audit"),
        ),
        not transaction_code_snapshot_matches(report, project_root),
    )):
        return None
    return job


def _publish_new_generation_job_locked(
        session_dir,
        request,
        request_path,
        *,
        profile_id=None,
        replacement=None,
        running_refresh=None,
    ):
    from autowork_core.utils.debug_tools.recorder.generation_workflow import (
        build_ai_context_budget,
        inspect_generation,
    )

    if replacement is not None:
        _archive_replaced_generation_job_brief(session_dir, replacement)
    refresh_job_snapshot = running_refresh is not None or replacement is not None
    inspected = inspect_generation(
        request_path,
        generation_profile_id=profile_id,
        write=not refresh_job_snapshot,
        ignore_current_job=refresh_job_snapshot,
    )
    state = load_workflow_state(
        session_dir,
        request.get("request_id"),
    )
    admission_source_state = (
        fresh_workflow_state_for_generation_admission(request_path)
        if refresh_job_snapshot
        else state
    )
    admission_state = (
        _running_refresh_admission_state(admission_source_state)
        if refresh_job_snapshot
        else admission_source_state
    )
    shadow_admission = inspected.get("generation_admission") or {}
    if (
            running_refresh is None
            and replacement is None
            and shadow_admission.get("status") != "passed"
    ):
        return {
            "generation_job_service_version": GENERATION_JOB_SERVICE_VERSION,
            "status": "rejected",
            "request_id": request.get("request_id"),
            "generation_admission": shadow_admission,
            "errors": list(
                shadow_admission.get("blocking_codes") or []
            ),
            "warnings": [],
        }
    pack, answers = _decision_artifacts(session_dir, request, admission_state)
    contract_lease = generation_contract_lease(session_dir, write=False)
    context_budget = inspected.get("ai_context_budget") or {}
    brief_for_workload = load_generation_brief(
        _session_pointer_path(
            session_dir,
            (admission_state.get("brief") or {}).get("path"),
        )
    )
    action_count = len(brief_for_workload.get("actions") or ())
    step_count = len(
        (brief_for_workload.get("target") or {}).get("steps") or ()
    )
    orchestration = (
        resolve_generation_profile(profile_id).get("orchestration_policy")
        or {}
    )
    page_size = max(
        1,
        int(orchestration.get("max_actions_per_fragment") or 20),
    )
    design_mode = (
        "paged"
        if action_count > page_size or step_count > page_size
        else "direct"
    )
    capsule = build_generation_capsule(
        request,
        admission_state,
        contract_lease,
        project_root=_project_root_for_generation_session(session_dir),
        input_roots=ALLOWED_WRITE_ROOTS,
        exact_files=_job_capsule_exact_paths(request),
        source_paths=_job_capsule_source_paths(
            _project_root_for_generation_session(session_dir),
            brief_for_workload,
            request=request,
        ),
    )
    capsule_path, capsule = persist_generation_capsule(
        session_dir,
        capsule,
    )
    capsule_pointer = generation_capsule_pointer(
        session_dir,
        capsule,
        capsule_path,
    )
    workspace_projection = build_generation_workspace_projection(
        request,
        brief_for_workload,
        capsule.get("generation_input_snapshot") or {},
        capsule.get("source_files") or [],
        project_root=_project_root_for_generation_session(session_dir),
        terminal_results=_terminal_results_for_workspace_projection(
            session_dir,
            admission_source_state,
        ),
    )
    workspace_projection_path, workspace_projection = (
        persist_generation_workspace_projection(
            session_dir,
            workspace_projection,
        )
    )
    workspace_projection_pointer = generation_workspace_projection_pointer(
        session_dir,
        workspace_projection,
        workspace_projection_path,
    )
    job = None
    admission = None
    budget_task_bundle = None
    for _attempt in range(8):
        admission = project_generation_admission(
            request=request,
            state=admission_state,
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
            admission_state,
            admission,
            contract_lease,
            activation="active",
            action_count=action_count,
            step_count=step_count,
            design_mode=design_mode,
            generation_capsule=capsule_pointer,
            generation_workspace_projection=workspace_projection_pointer,
        )
        projected = _candidate_job_inspect(
            session_dir,
            request,
            admission_state,
            job,
            budget_task_bundle=budget_task_bundle,
        )
        budget_task_bundle = projected.get("generation_task_bundle") or {}
        next_budget = build_ai_context_budget(
            session_dir=session_dir,
            request_path=request_path,
            request=request,
            state=admission_state,
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
    if generation_job_design_mode(job) == "paged":
        brief_path = _brief_path_for_job(session_dir, job)
        brief = load_generation_brief(brief_path)
        persist_generation_task_bundle(
            session_dir,
            brief,
            job,
            brief_path=brief_path.relative_to(session_dir),
        )
    pointer = generation_job_pointer(session_dir, job, path)
    if running_refresh is not None:
        workflow = refresh_running_generation_job(
            session_dir,
            request["request_id"],
            pointer,
            expected_job_pointer=running_refresh["pointer"],
            expected_epoch=running_refresh["epoch"],
            claim_id=running_refresh["claim_id"],
            expected_phase=running_refresh["phase"],
            retire_reason=running_refresh.get("reason"),
            errors=running_refresh.get("errors"),
            admission_snapshot=admission_state,
        )
    elif replacement is not None:
        workflow = replace_generation_job(
            session_dir,
            request["request_id"],
            pointer,
            expected_job_pointer=replacement["pointer"],
            expected_epoch=replacement["epoch"],
            retire_reason=replacement.get("reason"),
            allow_active=replacement.get("reason") == "force_new_generation_job",
            admission_snapshot=admission_state,
        )
    else:
        workflow = publish_generation_job(
            session_dir,
            request["request_id"],
            pointer,
            expected_epoch=0,
            admission_snapshot=admission_state,
        )
    return _with_job_transition(
        _job_result(session_dir, job, workflow),
        session_dir,
        job,
    )


def _running_refresh_admission_state(state):
    value = copy.deepcopy(state)
    value.update({
        "status": "ready",
        "next_action": "start_generation_job",
        "current_job": None,
        "job_execution": {
            "phase": "ready",
            "epoch": 0,
            "claim_id": None,
            "claimed_at": None,
            "attempt_no": 0,
            "plan": None,
            "transaction": None,
            "last_issue_fingerprint": None,
        },
        "active_transaction": None,
        "plan": {},
        "errors": [],
        "warnings": [],
    })
    return value


def _terminal_results_for_workspace_projection(session_dir, state):
    pointers = []
    if (state or {}).get("last_job_result"):
        pointers.append((state or {}).get("last_job_result"))
    for retired in (state or {}).get("retired_jobs") or ():
        pointer = (retired or {}).get("last_job_result")
        if pointer:
            pointers.append(pointer)
    results = []
    seen = set()
    for pointer in pointers:
        key = (
            pointer.get("path"),
            pointer.get("result_fingerprint"),
        ) if isinstance(pointer, dict) else None
        if not key or key in seen:
            continue
        seen.add(key)
        result = load_generation_job_result(session_dir, pointer)
        if result is not None:
            report = _terminal_result_report_for_projection(session_dir, result)
            if report:
                result = {
                    **result,
                    "terminal_report_audit": report,
                }
            results.append(result)
    return results


def _terminal_result_report_for_projection(session_dir, result):
    owner = (((result.get("stages") or {}).get("transaction") or {}).get(
        "owner"
    ) or {})
    report_path = _session_pointer_path(session_dir, owner.get("path"))
    if not report_path or not report_path.is_file():
        return None
    try:
        report = _read_json(report_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if any((
            owner.get("transaction_id") != report.get("transaction_id"),
            owner.get("result_fingerprint") != report.get("result_fingerprint"),
            owner.get("completion_fingerprint")
            != report.get("completion_fingerprint"),
            transaction_result_fingerprint(report)
            != report.get("result_fingerprint"),
    )):
        return None
    return report


def _refresh_running_generation_job_locked(
        session_dir,
        request_path,
        job,
        workflow,
        *,
        phase,
        reason,
        errors,
        profile_id=None,
    ):
    request = _read_json(request_path)
    execution = workflow.get("job_execution") or {}
    return _publish_new_generation_job_locked(
        session_dir,
        request,
        request_path,
        profile_id=profile_id,
        running_refresh={
            "pointer": dict(workflow.get("current_job") or {}),
            "epoch": execution.get("epoch"),
            "claim_id": execution.get("claim_id"),
            "phase": phase,
            "reason": reason,
            "errors": list(errors or []),
        },
    )


def start_generation_job(
        job_path,
        *,
        expected_epoch,
    command_sent_at=None,
    timing_source=None,
    ):
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
        workflow = claim_generation_job(
            session_dir,
            (job.get("request") or {})["request_id"],
            job_id=job["job_id"],
            job_fingerprint=job["job_fingerprint"],
            expected_epoch=expected_epoch,
            service_level_target_seconds=(
                job.get("workload") or {}
            ).get("static_service_level_target_seconds"),
            command_sent_at=command_sent_at,
            timing_source=timing_source,
        )
        if current.get("current_job") != workflow.get("current_job"):
            raise ValueError("Generation Job pointer在claim期间发生变化")
        claimed_execution = workflow.get("job_execution") or {}
        claim_id = claimed_execution.get("claim_id")
        claim_epoch = claimed_execution.get("epoch")
        if generation_job_design_mode(job) == "paged":
            bundle = load_generation_task_bundle(session_dir, job)
            if bundle is None:
                publish_pretransaction_job_failure(
                    session_dir,
                    request_id,
                    claim_id=claim_id,
                    expected_epoch=claim_epoch,
                    expected_phase="design",
                    category="job_task_bundle_integrity_failed",
                    next_action="review_generation_failure",
                    issue_owner={"type": "task_bundle_integrity"},
                )
                raise ValueError("Generation Job TaskBundle identity无效")
            try:
                validate_generation_task_bundle_closure(
                    session_dir,
                    job,
                    bundle,
                )
            except (KeyError, OSError, ValueError) as error:
                publish_pretransaction_job_failure(
                    session_dir,
                    request_id,
                    claim_id=claim_id,
                    expected_epoch=claim_epoch,
                    expected_phase="design",
                    category="job_task_bundle_integrity_failed",
                    next_action="review_generation_failure",
                    issue_owner={"type": "task_bundle_integrity"},
                )
                raise ValueError(
                    "Generation Job TaskBundle闭包无效"
                ) from error
        request_path = _request_path_for_job(session_dir, job)
        fresh = inspect_workflow(request_path, write=False)
        if any((
            fresh.get("status") != "running",
            (fresh.get("job_execution") or {}).get("phase") != "design",
            fresh.get("current_job") != workflow.get("current_job"),
        )):
            errors = list(fresh.get("errors") or ["job_freshness_failed"])
            publish_pretransaction_job_failure(
                session_dir,
                (job.get("request") or {})["request_id"],
                claim_id=claim_id,
                expected_epoch=claim_epoch,
                expected_phase="design",
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
        source_candidate_errors = _frozen_job_source_candidate_errors(
            session_dir,
            job,
        )
        if source_candidate_errors:
            return _refresh_running_generation_job_locked(
                session_dir,
                request_path,
                job,
                workflow,
                phase="design",
                reason="source_candidates_stale",
                errors=source_candidate_errors,
                profile_id=(job.get("profile_lease") or {}).get(
                    "profile_id"
                ),
            )
        if generation_job_design_mode(job) == "direct":
            source_errors = _direct_generation_source_preflight(
                job_path,
                session_dir,
                fresh,
                job,
            )
            if source_errors:
                return _refresh_running_generation_job_locked(
                    session_dir,
                    request_path,
                    job,
                    workflow,
                    phase="design",
                    reason="stale_during_generation",
                    errors=source_errors,
                    profile_id=(job.get("profile_lease") or {}).get(
                        "profile_id"
                    ),
                )
        return _with_job_transition(
            _job_result(session_dir, job, workflow),
            session_dir,
            job,
        )
    finally:
        lock.release()


def _direct_generation_source_preflight(job_path, session_dir, state, job):
    try:
        brief = load_generation_brief(_session_pointer_path(
            session_dir,
            (state.get("brief") or {}).get("path"),
        ))
        baseline = build_generation_job_baseline_design(job_path)
        plan = compile_generation_design(baseline, brief)
        generation_input_snapshot = _job_generation_input_snapshot(
            session_dir,
            job,
        )
    except (KeyError, OSError, ValueError):
        return []
    project_root = Path(_project_root_for_generation_session(session_dir)).resolve()
    owner_errors, _owner_warnings = validate_owner_resolution_snapshot(
        project_root,
        plan.get("window_owners") or {},
        brief,
        generation_input_snapshot=generation_input_snapshot,
    )
    implementation_errors, _implementation_warnings = (
        validate_implementation_resolution_snapshot(
            project_root,
            {"plan_version": PLAN_VERSION, "plan": plan},
            brief,
            generation_input_snapshot=generation_input_snapshot,
            reject_existing_create=True,
        )
    )
    return [*owner_errors, *implementation_errors]


def _project_root_for_generation_session(session_dir):
    session_dir = Path(session_dir).resolve()
    for candidate in (session_dir, *session_dir.parents):
        if (
                candidate.name.casefold() == "recording_sessions"
                and candidate.parent.name.casefold() == "artifacts"
        ):
            return candidate.parent.parent.resolve()
    return Path(Paths.BASE_DIR).resolve()


def prepare_generation_job_design_draft(job_path, *, project_root=None):
    job_path = Path(job_path).resolve()
    session_dir, job, state = _resolve_current_job(job_path)
    project_root = Path(project_root or Paths.BASE_DIR).resolve()
    _validate_design_draft_project_root(project_root, session_dir)
    lock = RunWriteLock(session_dir).acquire()
    try:
        session_dir, job, state = _resolve_current_job(job_path)
        phase = str((state.get("job_execution") or {}).get("phase") or "")
        if phase != "design" or state.get(
                "next_action"
        ) != "submit_generation_design":
            raise ValueError(
                "Generation Job当前阶段不能准备Design草稿: "
                f"{phase or 'unknown'}"
            )
        _validate_design_draft_project_root(project_root, session_dir)
        draft_dir = (
            project_root / ".copilot" / "recorder-drafts" / job["job_id"]
        ).resolve()
        try:
            relative_draft_dir = draft_dir.relative_to(project_root).as_posix()
        except ValueError as error:
            raise ValueError("Generation Job Design草稿目录越出项目目录") from error
        if generation_path_has_reparse_point(project_root, relative_draft_dir):
            raise ValueError("Generation Job Design草稿目录不能包含重解析点")
        draft_dir.mkdir(parents=True, exist_ok=True)
        if generation_path_has_reparse_point(project_root, relative_draft_dir):
            raise ValueError("Generation Job Design草稿目录不能包含重解析点")

        binding_path = draft_dir / "binding.json"
        design_draft_path = draft_dir / "design.json"
        design_seed_path = draft_dir / "baseline-seed.json"
        for path in (binding_path, design_draft_path, design_seed_path):
            relative_path = path.relative_to(project_root).as_posix()
            if generation_path_has_reparse_point(project_root, relative_path):
                raise ValueError("Generation Job Design草稿文件不能包含重解析点")

        binding = {
            "recorder_host_control_version": RECORDER_HOST_CONTROL_VERSION,
            "job_id": job["job_id"],
            "job_fingerprint": job["job_fingerprint"],
            "request_id": (job.get("request") or {}).get("request_id"),
            "request_fingerprint": (
                (job.get("request") or {}).get("request_fingerprint")
            ),
            "generation_contract_lease_fingerprint": (
                (job.get("generation_contract_lease") or {}).get(
                    "lease_fingerprint"
                )
            ),
            "job_path": str(job_path),
            "design_mode": generation_job_design_mode(job),
            "design_draft_path": str(design_draft_path),
            "design_seed_path": str(design_seed_path),
        }
        if binding_path.exists():
            if not binding_path.is_file() or _read_json(binding_path) != binding:
                raise ValueError("Generation Job Design草稿绑定无效或已漂移")
        else:
            write_json_atomic(binding_path, binding)

        warnings = []
        if generation_job_design_mode(job) == "paged":
            baseline = _empty_generation_design()
            seed_source = "task_bundle_template"
            seed_reason = "large_job_uses_read_only_task_bundle_pages"
        else:
            try:
                baseline = build_generation_job_baseline_design(job_path)
                seed_source = "system_baseline_seed"
                seed_reason = None
            except (KeyError, OSError, ValueError) as error:
                baseline = _empty_generation_design()
                seed_source = "baseline_unavailable"
                seed_reason = str(error)
                warnings.append(
                    "系统 baseline 不能完整表达当前任务；"
                    "请基于冻结 Design Context 完成同一份 Design。"
                )
        write_json_atomic(design_seed_path, baseline)
        design_created = not design_draft_path.exists()
        if design_created:
            write_json_atomic(design_draft_path, baseline)
        elif not design_draft_path.is_file():
            raise ValueError("Generation Job Design草稿不是普通文件")

        return {
            "recorder_host_control_version": RECORDER_HOST_CONTROL_VERSION,
            "status": "prepared",
            "next_action": "review_and_submit_generation_design",
            "request_id": (job.get("request") or {}).get("request_id"),
            "job_id": job.get("job_id"),
            "job_transition": _job_transition(state),
            "binding_path": str(binding_path),
            "design_draft": {
                "path": str(design_draft_path),
                "created": design_created,
            },
            "design_seed": {
                "path": str(design_seed_path),
                "source": seed_source,
                **({"reason": seed_reason} if seed_reason else {}),
            },
            "errors": [],
            "warnings": warnings,
        }
    finally:
        lock.release()


def prepare_generation_job_naming_patch_draft(job_path, *, project_root=None):
    job_path = Path(job_path).resolve()
    session_dir, job, state = _resolve_current_job(job_path)
    project_root = Path(project_root or Paths.BASE_DIR).resolve()
    _validate_design_draft_project_root(project_root, session_dir)
    lock = RunWriteLock(session_dir).acquire()
    try:
        session_dir, job, state = _resolve_current_job(job_path)
        phase = str((state.get("job_execution") or {}).get("phase") or "")
        if phase != "design" or state.get(
                "next_action"
        ) != "submit_generation_design":
            raise ValueError(
                "Generation Job当前阶段不能准备NamingPatch草稿: "
                f"{phase or 'unknown'}"
            )
        _validate_design_draft_project_root(project_root, session_dir)
        draft_dir = (
            project_root / ".copilot" / "recorder-drafts" / job["job_id"]
        ).resolve()
        try:
            relative_draft_dir = draft_dir.relative_to(project_root).as_posix()
        except ValueError as error:
            raise ValueError("Generation Job NamingPatch草稿目录越出项目目录") from error
        if generation_path_has_reparse_point(project_root, relative_draft_dir):
            raise ValueError("Generation Job NamingPatch草稿目录不能包含重解析点")
        draft_dir.mkdir(parents=True, exist_ok=True)
        if generation_path_has_reparse_point(project_root, relative_draft_dir):
            raise ValueError("Generation Job NamingPatch草稿目录不能包含重解析点")
        patch_path = draft_dir / "naming-patch.json"
        relative_patch_path = patch_path.relative_to(project_root).as_posix()
        if generation_path_has_reparse_point(project_root, relative_patch_path):
            raise ValueError("Generation Job NamingPatch草稿文件不能包含重解析点")
        baseline = build_generation_job_baseline_design(job_path)
        blanks = _naming_patch_blanks(baseline)
        if not blanks:
            raise ValueError("Generation Job NamingPatch缺少可填写命名空白")
        draft = {
            "naming_patch_version": GENERATION_NAMING_PATCH_VERSION,
            "patch_type": "naming",
            "target_names": {
                item["scope"]: ""
                for item in blanks
                if item["field"] == "target_name"
            },
            "business_names": {
                item["scope"]: ""
                for item in blanks
                if item["field"] == "business_name"
            },
        }
        created = not patch_path.exists()
        if created:
            write_json_atomic(patch_path, draft)
        elif not patch_path.is_file():
            raise ValueError("Generation Job NamingPatch草稿不是普通文件")
        return {
            "recorder_host_control_version": RECORDER_HOST_CONTROL_VERSION,
            "status": "prepared",
            "next_action": "fill_and_submit_naming_patch",
            "request_id": (job.get("request") or {}).get("request_id"),
            "job_id": job.get("job_id"),
            "job_transition": _job_transition(state),
            "naming_patch_draft": {
                "path": str(patch_path),
                "created": created,
                "blanks": blanks,
            },
            "errors": [],
            "warnings": [],
        }
    finally:
        lock.release()


def _naming_patch_blanks(design):
    blanks = []
    for step in design.get("steps") or ():
        step_id = str(step.get("step_id") or "")
        for operation in step.get("operations") or ():
            action_id = str(operation.get("target_action_id") or "")
            if action_id and not str(operation.get("target_name") or ""):
                blanks.append({
                    "field": "target_name",
                    "scope": f"{step_id}/{action_id}",
                })
    for owner in design.get("window_ownership") or ():
        root_name = str(owner.get("root_name") or "")
        if root_name and not str(owner.get("business_name") or ""):
            blanks.append({
                "field": "business_name",
                "scope": root_name,
            })
    return blanks


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
    try:
        brief_path = _brief_path_for_job(session_dir, job)
    except ValueError:
        if retired is None or not result.get("last_job_result"):
            raise
        result.update({
            "job_path": Path(job_path).resolve().relative_to(
                session_dir
            ).as_posix(),
            "request_path": request_path.relative_to(session_dir).as_posix(),
            "brief_path": None,
            "plan_path": None,
            "generation_task_bundle": {},
            "ai_context_envelope": None,
            "ai_context_budget": None,
        })
        result["warnings"] = [
            "该历史 Generation Job 的冻结 Brief 快照不可用；"
            "仅可查看已持久化的终态结果。"
        ]
        return result
    plan_value = state.get("plan") or {}
    plan_path = _session_pointer_path(session_dir, plan_value.get("path"))
    job_relative_path = Path(job_path).resolve().relative_to(
        session_dir
    ).as_posix()
    request_relative_path = request_path.relative_to(session_dir).as_posix()
    brief_relative_path = brief_path.relative_to(session_dir).as_posix()
    plan_relative_path = (
            plan_path.relative_to(session_dir).as_posix()
            if plan_path
            else None
    )
    result.update({
        "job_path": job_relative_path,
        "request_path": request_relative_path,
        "brief_path": brief_relative_path,
        "plan_path": plan_relative_path,
        "execution_boundary": _projected_execution_boundary(job),
        "ai_capabilities": compact_ai_capability_contract(),
    })
    if generation_job_design_mode(job) == "paged":
        bundle = load_generation_task_bundle(session_dir, job)
        if bundle is None:
            raise ValueError("Generation Job缺少有效TaskBundle")
        result["generation_task_bundle"] = project_generation_task_bundle_index(
            bundle,
            transition=result["job_transition"],
        )
    else:
        result["generation_task_bundle"] = {}
    plan_artifact = load_generation_plan(session_dir, state, request)
    plan_context = (
        build_ai_plan_context(plan_artifact)
        if plan_artifact is not None
        else None
    )
    if not result.get("last_job_result") and not result.get("business_review"):
        pack, _answers = _decision_artifacts(session_dir, request, state)
        review = _business_review_context(
            session_dir,
            job,
            state,
            pack,
        )
        if review:
            result["business_review"] = review
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
        generation_task_bundle=result["generation_task_bundle"],
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


def inspect_generation_job_control(job_path):
    """Project only authoritative state needed by a host control adapter."""
    session_dir, job, state = _resolve_known_job(job_path)
    return _job_result(session_dir, job, state)


def _business_review_context(session_dir, job, state, decision_pack):
    execution = state.get("job_execution") or {}
    stored = execution.get("business_review") or {}
    if stored.get("status") == "passed":
        return {}
    brief = load_generation_brief(_brief_path_for_job(session_dir, job))
    workset = build_business_review_workset(brief, decision_pack)
    if workset.get("status") != "required":
        return {}
    questions = build_business_review_questions(workset)
    return {
        **workset,
        "question_count": len(questions),
        "questions": questions,
        "requirement": build_business_review_requirement(workset),
    }


def business_review_patch_from_direct_arguments(job_path, decisions, reasons):
    session_dir, job, state = _resolve_current_job(job_path)
    request = _read_json(_request_path_for_job(session_dir, job))
    pack, _answers = _decision_artifacts(session_dir, request, state)
    workset = _business_review_context(session_dir, job, state, pack)
    if not workset:
        raise ValueError("当前Generation Job没有待审查BusinessReviewWorkset")
    return build_business_review_patch_from_decisions(
        workset,
        decisions,
        reasons,
    )


def submit_generation_job_business_answers(job_path, selections):
    session_dir, job, state = _resolve_current_job(job_path)
    request_path = _request_path_for_job(session_dir, job)
    request = _read_json(request_path)
    execution = state.get("job_execution") or {}
    if any((
        state.get("status") not in {"ready", "running"},
        execution.get("phase") not in {"ready", "design"},
        execution.get("transaction"),
        state.get("active_transaction"),
    )):
        raise ValueError("当前Generation Job不接受业务回答")
    decision = dict(state.get("decision") or {})
    if decision.get("status") != "awaiting_answers":
        raise ValueError(
            "当前Generation Job没有待回答业务问题: "
            f"decision_status={decision.get('status')}"
        )
    pack = load_decision_pack(
        session_dir,
        decision.get("pack") or {},
        request,
        brief_fingerprint=(state.get("brief") or {}).get(
            "brief_fingerprint"
        ),
    )
    if pack is None:
        raise ValueError("当前Decision Pack身份无效")
    selections = {
        str(question_id): str(option_id)
        for question_id, option_id in dict(selections or {}).items()
        if question_id and option_id
    }
    expected_ids = {
        str(question.get("question_id"))
        for question in pack.get("questions") or ()
        if question.get("blocking")
    }
    missing = sorted(expected_ids - set(selections))
    if missing:
        raise ValueError(f"阻塞业务问题尚未全部回答: {missing}")
    output, record = persist_answers(session_dir, request, pack, {
        "answer_version": ANSWER_VERSION,
        "pack_id": pack.get("pack_id"),
        "pack_fingerprint": pack.get("pack_fingerprint"),
        "revision_seal": pack.get("revision_seal"),
        "answers": [
            {
                "question_id": question_id,
                "option_id": selections[question_id],
            }
            for question_id in sorted(selections)
        ],
    })
    decision["answers"] = answer_pointer(session_dir, record, output)
    decision["status"] = (
        "forensic"
        if pack.get("forensic_blocking_count")
        else "answered"
    )
    state = dict(state)
    state["decision"] = decision
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    if state.get("status") == "running":
        state["next_action"] = "submit_generation_design"
    elif state.get("status") == "ready":
        state["next_action"] = "start_generation_job"
    write_workflow_state(session_dir, state)
    _record_job_interaction(
        session_dir,
        job,
        state,
        event="business_answers_submitted",
        status="business_answers_submitted",
        next_action="advance_job",
        details={"answer_count": len(selections)},
    )
    inspected = inspect_generation_job(job_path)
    return {
        "generation_job_service_version": GENERATION_JOB_SERVICE_VERSION,
        "status": "business_answers_submitted",
        "next_action": "advance_job",
        "request_id": request.get("request_id"),
        "job_id": job.get("job_id"),
        "job_path": inspected.get("job_path"),
        "answers_path": str(output),
        "answer_fingerprint": record.get("answer_fingerprint"),
        "job_transition": inspected.get("job_transition") or {},
        "business_answers_submitted": True,
        "errors": [],
        "warnings": [],
    }


def submit_generation_job_business_facts(
        job_path,
        *,
        freeform_answers=None,
        business_fact_patch=None,
    ):
    if not freeform_answers or not (business_fact_patch or {}).get("facts"):
        raise ValueError("自由业务事实必须同时包含原始回答和BusinessFactPatch facts")
    session_dir, job, state = _resolve_current_job(job_path)
    request_path = _request_path_for_job(session_dir, job)
    request = _read_json(request_path)
    execution = state.get("job_execution") or {}
    if any((
        state.get("status") not in {"ready", "running"},
        execution.get("phase") not in {"ready", "design"},
        execution.get("transaction"),
        state.get("active_transaction"),
    )):
        raise ValueError("当前Generation Job不接受自由业务事实")
    decision = dict(state.get("decision") or {})
    if decision.get("status") not in {"answered", "not_required"}:
        raise ValueError(
            "自由业务事实必须在选项答案完成后提交: "
            f"decision_status={decision.get('status')}"
        )
    pack = load_decision_pack(
        session_dir,
        decision.get("pack") or {},
        request,
        brief_fingerprint=(state.get("brief") or {}).get(
            "brief_fingerprint"
        ),
    )
    if pack is None:
        raise ValueError("当前Decision Pack身份无效")
    existing = load_answer_record(
        session_dir,
        decision.get("answers") or {},
        request,
        pack,
    )
    if existing is None and decision.get("status") == "not_required":
        existing = {"answers": []}
    elif existing is None:
        raise ValueError("自由业务事实缺少已提交的选项Answers")
    merged = {
        "answer_version": ANSWER_VERSION,
        "pack_id": pack.get("pack_id"),
        "pack_fingerprint": pack.get("pack_fingerprint"),
        "revision_seal": pack.get("revision_seal"),
        "answers": list(existing.get("answers") or []),
        "additional_business_answers": list(
            existing.get("additional_business_answers") or []
        ),
        "freeform_business_answers": list(freeform_answers or []),
        "business_fact_patch": dict(business_fact_patch or {}),
    }
    output, record = persist_answers(session_dir, request, pack, merged)
    decision["answers"] = answer_pointer(session_dir, record, output)
    decision["status"] = "answered"
    state = dict(state)
    state["decision"] = decision
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    if state.get("status") == "running":
        state["next_action"] = "submit_generation_design"
    elif state.get("status") == "ready":
        state["next_action"] = "start_generation_job"
    write_workflow_state(session_dir, state)
    _record_job_interaction(
        session_dir,
        job,
        state,
        event="business_facts_submitted",
        status="business_facts_submitted",
        next_action="advance_job",
        details={
            "freeform_answer_count": len(freeform_answers or []),
            "business_fact_count": len(
                (record.get("compiled_patch") or {}).get("business_facts") or []
            ),
        },
    )
    inspected = inspect_generation_job(job_path)
    return {
        "generation_job_service_version": GENERATION_JOB_SERVICE_VERSION,
        "status": "business_facts_submitted",
        "next_action": "advance_job",
        "request_id": request.get("request_id"),
        "job_id": job.get("job_id"),
        "job_path": inspected.get("job_path"),
        "answers_path": str(output),
        "answer_fingerprint": record.get("answer_fingerprint"),
        "business_fact_count": len(
            (record.get("compiled_patch") or {}).get("business_facts") or []
        ),
        "job_transition": inspected.get("job_transition") or {},
        "errors": [],
        "warnings": [],
    }


def submit_generation_job_business_review(job_path, patch):
    session_dir, job, state = _resolve_current_job(job_path)
    request_path = _request_path_for_job(session_dir, job)
    request = _read_json(request_path)
    execution = dict(state.get("job_execution") or {})
    if any((
        state.get("status") != "running",
        execution.get("phase") != "design",
        execution.get("transaction"),
        state.get("active_transaction"),
    )):
        raise ValueError("当前Generation Job不接受BusinessReviewPatch")
    pack, _answers = _decision_artifacts(session_dir, request, state)
    workset = _business_review_context(session_dir, job, state, pack)
    if not workset:
        raise ValueError("当前Generation Job没有待审查BusinessReviewWorkset")
    errors = validate_business_review_patch(workset, patch)
    if errors:
        raise ValueError(f"BusinessReviewPatch无效: {errors}")
    questions = _business_review_questions_from_patch(workset, patch)
    execution["business_review"] = {
        "status": "answers_required" if questions else "passed",
        "workset_fingerprint": workset.get("workset_fingerprint"),
        "question_count": len(questions),
        "questions": questions,
    }
    state = dict(state)
    state["job_execution"] = execution
    state["next_action"] = (
        "submit_business_review_answers"
        if questions else "submit_generation_design"
    )
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    write_workflow_state(session_dir, state)
    if questions:
        _record_job_interaction(
            session_dir,
            job,
            state,
            event="business_answers_required",
            status="business_answers_required",
            next_action="submit_business_review_answers",
            details={"question_count": len(questions)},
        )
    inspected = inspect_generation_job(job_path)
    return {
        "generation_job_service_version": GENERATION_JOB_SERVICE_VERSION,
        "status": "business_answers_required" if questions else "business_review_passed",
        "next_action": (
            "submit_business_review_answers" if questions else "advance_job"
        ),
        "request_id": request.get("request_id"),
        "job_id": job.get("job_id"),
        "job_path": inspected.get("job_path"),
        "business_questions": questions,
        "business_review": execution["business_review"],
        "job_transition": inspected.get("job_transition") or {},
        "errors": [],
        "warnings": [],
    }


def submit_generation_job_business_review_answers(job_path, answers):
    session_dir, job, state = _resolve_current_job(job_path)
    request_path = _request_path_for_job(session_dir, job)
    request = _read_json(request_path)
    execution = dict(state.get("job_execution") or {})
    review = execution.get("business_review") or {}
    if any((
        state.get("status") != "running",
        execution.get("phase") != "design",
        execution.get("transaction"),
        state.get("active_transaction"),
    )):
        raise ValueError("当前Generation Job不接受BusinessReview Answers")
    decision = dict(state.get("decision") or {})
    pack = load_decision_pack(
        session_dir,
        decision.get("pack") or {},
        request,
        brief_fingerprint=(state.get("brief") or {}).get(
            "brief_fingerprint"
        ),
    )
    if pack is None:
        raise ValueError("当前Decision Pack身份无效")
    workset = {}
    if review.get("status") == "answers_required":
        question_values = list(review.get("questions") or ())
        workset_fingerprint = review.get("workset_fingerprint")
    else:
        workset = _business_review_context(session_dir, job, state, pack)
        if not workset:
            raise ValueError("当前Generation Job没有待回答BusinessReview问题")
        question_values = list(workset.get("questions") or ())
        workset_fingerprint = workset.get("workset_fingerprint")
    questions = {
        str(question.get("question_id") or ""): question
        for question in question_values
        if isinstance(question, dict) and question.get("question_id")
    }
    selected = list((answers or {}).get("selected_options") or [])
    freeform = list((answers or {}).get("freeform_answers") or [])
    selected_ids = {
        str(item.get("question_id") or "")
        for item in selected
        if isinstance(item, dict) and item.get("question_id")
    }
    freeform_ids = {
        str(item.get("question_id") or "")
        for item in freeform
        if isinstance(item, dict) and item.get("question_id")
    }
    overlap = sorted(selected_ids & freeform_ids)
    if overlap:
        raise ValueError(f"BusinessReview问题不能同时选择选项和自由回答: {overlap}")
    missing = sorted(set(questions) - (selected_ids | freeform_ids))
    if missing:
        raise ValueError(f"BusinessReview问题尚未全部回答: {missing}")
    raw_answers = []
    facts = []
    decision_answers = []
    for item in selected:
        if not isinstance(item, dict):
            raise ValueError("BusinessReview selected_options必须是object列表")
        question_id = str(item.get("question_id") or "")
        option_id = str(item.get("option_id") or "")
        question = questions.get(question_id)
        if question is None:
            raise ValueError(f"BusinessReview回答引用未知问题: {question_id}")
        option = next((
            option for option in question.get("options") or ()
            if str(option.get("option_id") or "") == option_id
        ), None)
        if option is None:
            raise ValueError(
                f"BusinessReview问题 {question_id} 引用未知选项: {option_id}"
            )
        source_question_id = str(question.get("source_decision_question_id") or "")
        if source_question_id:
            decision_answers.append({
                "question_id": source_question_id,
                "option_id": option_id,
            })
            continue
        answer_id = f"review-answer-{question_id}"
        raw_answers.append({
            "answer_id": answer_id,
            "step_id": str(question.get("step_id") or ""),
            "question": str(question.get("prompt") or question_id),
            "answer": str(option.get("label") or option_id),
        })
        fact = dict(option.get("business_fact") or {})
        fact["step_id"] = str(question.get("step_id") or "")
        fact["source_answer_id"] = answer_id
        fact["reason"] = "User selected a BusinessReview option."
        facts.append(fact)
    if freeform:
        raw_freeform, freeform_facts = _business_review_freeform_answers(
            questions,
            freeform,
        )
        raw_answers.extend(raw_freeform)
        facts.extend(freeform_facts)
        facts.extend(list(((answers or {}).get("business_fact_patch") or {}).get("facts") or []))
    if not decision_answers and (not raw_answers or not facts):
        raise ValueError("BusinessReview Answers必须产生业务事实")
    output, record = persist_answers(session_dir, request, pack, {
        "answer_version": ANSWER_VERSION,
        "pack_id": pack.get("pack_id"),
        "pack_fingerprint": pack.get("pack_fingerprint"),
        "revision_seal": pack.get("revision_seal"),
        "answers": decision_answers,
        "freeform_business_answers": raw_answers,
        "business_fact_patch": {
            "business_fact_patch_version": "1.0",
            "patch_type": "business_facts",
            "facts": facts,
        } if facts else None,
    })
    execution = dict(state.get("job_execution") or {})
    review = dict(execution.get("business_review") or {})
    review["status"] = "passed"
    if workset_fingerprint:
        review["workset_fingerprint"] = workset_fingerprint
    if workset:
        review["question_count"] = len(question_values)
    execution["business_review"] = review
    decision["answers"] = answer_pointer(session_dir, record, output)
    decision["status"] = "answered"
    state = dict(state)
    state["decision"] = decision
    state["job_execution"] = execution
    state["next_action"] = "submit_generation_design"
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    write_workflow_state(session_dir, state)
    _record_job_interaction(
        session_dir,
        job,
        state,
        event="business_answers_submitted",
        status="business_review_answers_submitted",
        next_action="advance_job",
        details={
            "selected_option_count": len(selected),
            "freeform_answer_count": len(freeform),
            "business_fact_count": len(facts),
        },
    )
    return {
        "status": "business_review_answers_submitted",
        "next_action": "advance_job",
        "generation_job_service_version": GENERATION_JOB_SERVICE_VERSION,
        "request_id": request.get("request_id"),
        "job_id": job.get("job_id"),
        "job_path": str(job_path),
        "answers_path": str(output),
        "answer_fingerprint": record.get("answer_fingerprint"),
        "business_fact_count": len(
            (record.get("compiled_patch") or {}).get("business_facts") or []
        ),
        "job_transition": inspect_generation_job(job_path).get(
            "job_transition"
        ) or {},
        "errors": [],
        "warnings": [],
    }


def _business_review_freeform_answers(questions, values):
    raw_answers = []
    facts = []
    for index, item in enumerate(values, start=1):
        if not isinstance(item, dict):
            raise ValueError("BusinessReview freeform_answers必须是object列表")
        question_id = str(item.get("question_id") or "")
        question = questions.get(question_id)
        if question is None:
            raise ValueError(f"BusinessReview自由回答引用未知问题: {question_id}")
        answer_id = str(item.get("answer_id") or f"review-freeform-{index}")
        answer = str(item.get("answer") or "").strip()
        if not answer:
            raise ValueError(f"BusinessReview自由回答为空: {question_id}")
        raw_answers.append({
            "answer_id": answer_id,
            "step_id": str(question.get("step_id") or ""),
            "question": str(question.get("prompt") or question_id),
            "answer": answer,
        })
        fact = _business_review_freeform_fact(question, answer_id, answer)
        if fact:
            facts.append(fact)
    return raw_answers, facts


def _business_review_freeform_fact(question, answer_id, answer):
    if not bool(question.get("allow_freeform")):
        raise ValueError(
            f"BusinessReview问题不允许自由回答: {question.get('question_id')}"
        )
    action_id = str(
        ((question.get("options") or [{}])[0].get("business_fact") or {})
        .get("applies_to", {})
        .get("action_id")
        or ""
    )
    if not action_id:
        return None
    return {
        "step_id": str(question.get("step_id") or ""),
        "source_answer_id": answer_id,
        "fact_type": "value_authority",
        "fact_value": answer,
        "applies_to": {
            "scope": "action_value",
            "action_id": action_id,
        },
        "source": {"kind": "user_declared_literal"},
        "reason": "User provided a freeform BusinessReview answer.",
    }


def _business_review_questions_from_patch(workset, patch):
    units = {
        str(unit.get("unit_id") or ""): unit
        for unit in workset.get("units") or ()
        if isinstance(unit, dict) and unit.get("unit_id")
    }
    questions = []
    for decision in patch.get("unit_decisions") or ():
        if not isinstance(decision, dict):
            continue
        unit = units.get(str(decision.get("unit_id") or "")) or {}
        if (
                decision.get("decision") == "include_as_is"
                and unit.get("unit_type") == "system_decision_question"
        ):
            question = dict((unit.get("facts") or {}).get("question") or {})
            question["source_decision_question_id"] = question.get("question_id")
            questions.append(question)
            continue
        if decision.get("decision") not in {"ask_user", "rephrase_business_question"}:
            continue
        question = dict(decision.get("question") or {})
        question.update({
            "unit_id": decision.get("unit_id"),
            "unit_type": unit.get("unit_type"),
            "step_id": unit.get("step_id") or question.get("step_id"),
            "blocking": True,
        })
        questions.append(question)
    return questions


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
        design=None,
    ):
    session_dir, job, _state = _resolve_known_job(job_path)
    _require_job_query(job, "job-action-knowledge")
    brief = load_generation_brief(_brief_path_for_job(session_dir, job))
    if design is not None:
        if any((step_id, action_id, operation_names, list_only)):
            raise ValueError(
                "Design批量Action knowledge不能混用单Action参数"
            )
        if not isinstance(design, dict):
            raise ValueError("Design批量Action knowledge要求object")
        queries = [
            {
                "step_id": str(step.get("step_id") or ""),
                "action_id": str(operation.get("target_action_id") or ""),
                "operation": str(operation.get("operation") or ""),
            }
            for step in design.get("steps") or ()
            if isinstance(step, dict)
            for operation in step.get("operations") or ()
            if isinstance(operation, dict)
        ]
        if not queries or any(not all(query.values()) for query in queries):
            raise ValueError("Design批量Action knowledge查询不完整")
        return {
            "status": "projected",
            "job_id": job.get("job_id"),
            "request_id": (job.get("request") or {}).get("request_id"),
            "action_knowledge_batch_version": "1.0",
            "query_count": len(queries),
            "queries": queries,
            "action_knowledge": [
                query_action_knowledge(
                    brief,
                    step_id=query["step_id"],
                    action_id=query["action_id"],
                    operation_names=[query["operation"]],
                )
                for query in queries
            ],
        }
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
    result = _job_result(session_dir, job, state)
    blocked = _blocked_typed_patch_design_context(
        job,
        state,
        result,
        step_id=step_id,
    )
    if blocked is not None:
        return blocked
    request = _read_json(_request_path_for_job(session_dir, job))
    brief_path = _brief_path_for_job(session_dir, job)
    brief = load_generation_brief(brief_path)
    if not brief_matches_request(brief, request):
        raise ValueError("Generation Design Context Brief身份与Request不一致")
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


def _blocked_typed_patch_design_context(job, state, result, *, step_id=None):
    execution = (state or {}).get("job_execution") or {}
    marker = execution.get("design_required") or {}
    reason = str(marker.get("reason") or "")
    if any((
        step_id,
        (state or {}).get("status") != "running",
        execution.get("phase") != "design",
        marker.get("status") != "required",
        reason not in _TYPED_PATCH_DESIGN_REQUIRED_REASONS,
    )):
        return None
    details = dict(marker.get("details") or {})
    return {
        "generation_design_context_query_version": "1.0",
        "status": "blocked",
        "reason": "typed_patch_forbids_unbounded_design_context",
        "next_action": "run_generate_job_and_use_typed_patch_requirements",
        "request_id": (job.get("request") or {}).get("request_id"),
        "job_id": job.get("job_id"),
        "job_transition": result.get("job_transition") or {},
        "query": {"step_id": None},
        "typed_patch": {
            "reason": reason,
            "message": marker.get("message"),
            "details": details,
        },
        "design_context": {
            "omitted": True,
            "reason": "complete_or_unbounded_typed_patch_context_not_allowed",
        },
        "errors": [],
        "warnings": [],
    }


def build_generation_job_baseline_design(
        job_path,
        *,
    naming_overrides=None,
        ambiguity_choice_patch=None,
        value_source_choice_patch=None,
        assertion_choice_patch=None,
        method_choice_patch=None,
        operation_choice_patch=None,
    ):
    session_dir, job, state = _resolve_current_job(job_path)
    brief = load_generation_brief(_brief_path_for_job(session_dir, job))
    request = _read_json(_request_path_for_job(session_dir, job))
    product_naming_overrides = _job_product_state_naming_overrides(
        session_dir,
        job,
        brief,
    )
    naming_overrides = _merge_naming_overrides(
        product_naming_overrides,
        naming_overrides,
    )
    return build_generation_baseline_design(
        brief,
        decision_constraints=_compiled_decision_patch(
            session_dir,
            request,
            state,
        ),
        naming_overrides=naming_overrides,
        ambiguity_choice_patch=ambiguity_choice_patch,
        value_source_choice_patch=value_source_choice_patch,
        assertion_choice_patch=assertion_choice_patch,
        method_choice_patch=method_choice_patch,
        operation_choice_patch=operation_choice_patch,
    )


def discover_generation_job_typed_patch_issues(
        job_path,
        *,
        max_iterations=24,
    ):
    session_dir, job, state = _resolve_current_job(job_path)
    brief = load_generation_brief(_brief_path_for_job(session_dir, job))
    request = _read_json(_request_path_for_job(session_dir, job))
    base_naming_overrides = _job_product_state_naming_overrides(
        session_dir,
        job,
        brief,
    )
    decision_constraints = _compiled_decision_patch(
        session_dir,
        request,
        state,
    )
    scratch_naming = {"target_names": {}, "business_names": {}}
    ambiguity_choices = []
    assertion_choices = []
    method_choices = []
    operation_choices = []
    value_source_choices = []
    issues = []
    seen = set()
    terminal_error = None
    for _attempt in range(max_iterations):
        try:
            design = build_generation_baseline_design(
                brief,
                decision_constraints=decision_constraints,
                naming_overrides=_merge_naming_overrides(
                    base_naming_overrides,
                    scratch_naming,
                ),
                ambiguity_choice_patch=_choice_patch(
                    "ambiguity_choice_patch_version",
                    GENERATION_AMBIGUITY_CHOICE_PATCH_VERSION,
                    "ambiguity_choice",
                    ambiguity_choices,
                ),
                assertion_choice_patch=_choice_patch(
                    "assertion_choice_patch_version",
                    GENERATION_ASSERTION_CHOICE_PATCH_VERSION,
                    "assertion_choice",
                    assertion_choices,
                ),
                method_choice_patch=_choice_patch(
                    "method_choice_patch_version",
                    GENERATION_METHOD_CHOICE_PATCH_VERSION,
                    "method_choice",
                    method_choices,
                ),
                operation_choice_patch=_choice_patch(
                    "operation_choice_patch_version",
                    GENERATION_OPERATION_CHOICE_PATCH_VERSION,
                    "operation_choice",
                    operation_choices,
                ),
                value_source_choice_patch=_choice_patch(
                    "value_source_choice_patch_version",
                    GENERATION_VALUE_SOURCE_CHOICE_PATCH_VERSION,
                    "value_source_choice",
                    value_source_choices,
                ),
            )
            compile_generation_design(
                design,
                brief,
                require_public_locator_names=True,
            )
            terminal_error = None
            break
        except GenerationAmbiguityChoiceRequired as error:
            issue = _discovery_ambiguity_issue(error)
        except GenerationAssertionChoiceRequired as error:
            issue = _discovery_assertion_issue(error)
        except GenerationMethodChoiceRequired as error:
            issue = _discovery_method_issue(error)
        except GenerationOperationChoiceRequired as error:
            issue = _discovery_operation_issue(error)
        except GenerationValueSourceChoiceRequired as error:
            issue = _discovery_value_source_issue(error)
        except GenerationDesignValidationError as error:
            issue = _discovery_naming_issue(error, brief)
            if issue is None:
                terminal_error = f"{type(error).__name__}: {error}"
                break
        except (OSError, ValueError) as error:
            issue = _discovery_naming_issue(error, brief)
            if issue is None:
                terminal_error = f"{type(error).__name__}: {error}"
                break
        key = _discovery_issue_key(issue)
        if key in seen:
            terminal_error = f"Repeated typed patch discovery issue: {key}"
            break
        seen.add(key)
        issues.append(issue)
        if not _apply_discovery_placeholder(
                issue,
                scratch_naming,
                ambiguity_choices,
                assertion_choices,
                method_choices,
                operation_choices,
                value_source_choices,
        ):
            terminal_error = f"Typed patch discovery cannot advance: {key}"
            break
    else:
        terminal_error = "Typed patch discovery iteration limit exceeded"
    return {
        "typed_patch_issue_discovery_version": "1.0",
        "status": "required" if issues else "not_required",
        "complete": terminal_error is None,
        "issue_count": len(issues),
        "issues": issues,
        "terminal_error": terminal_error,
    }


def _choice_patch(version_key, version, patch_type, choices):
    if not choices:
        return None
    return {
        version_key: version,
        "patch_type": patch_type,
        "choices": copy.deepcopy(choices),
    }


def _merge_naming_overrides(base, scratch):
    base = base if isinstance(base, dict) else {}
    scratch = scratch if isinstance(scratch, dict) else {}
    return {
        "target_names": {
            **dict(base.get("target_names") or {}),
            **dict(scratch.get("target_names") or {}),
        },
        "business_names": {
            **dict(base.get("business_names") or {}),
            **dict(scratch.get("business_names") or {}),
        },
    }


def _naming_overrides_from_patch(naming_patch):
    if naming_patch is None:
        return None
    if not isinstance(naming_patch, dict) or any((
        naming_patch.get("naming_patch_version")
        != GENERATION_NAMING_PATCH_VERSION,
        naming_patch.get("patch_type") != "naming",
    )):
        raise ValueError("NamingPatch版本或类型无效")
    target_names = (
        naming_patch["target_names"] if "target_names" in naming_patch else {}
    )
    business_names = (
        naming_patch["business_names"]
        if "business_names" in naming_patch else {}
    )
    if not isinstance(target_names, dict) or not isinstance(business_names, dict):
        raise ValueError("NamingPatch target_names/business_names必须是对象")
    empty_target_scopes = [
        str(scope or "").strip()
        for scope, value in target_names.items()
        if not str(value or "").strip()
    ]
    empty_business_roots = [
        str(root_name or "").strip()
        for root_name, value in business_names.items()
        if not str(root_name or "").strip() or not str(value or "").strip()
    ]
    if empty_target_scopes:
        raise ValueError(
            f"NamingPatch target_name无效: {empty_target_scopes[0]}"
        )
    if empty_business_roots:
        raise ValueError(
            f"NamingPatch business_name无效: {empty_business_roots[0]}"
        )
    return {
        "target_names": {
            _naming_scope_key(scope): str(value or "").strip()
            for scope, value in target_names.items()
        },
        "business_names": {
            str(root_name or "").strip(): str(value or "").strip()
            for root_name, value in business_names.items()
        },
    }


def _naming_scope_key(scope):
    parts = str(scope or "").split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"NamingPatch scope无效: {scope}")
    return parts[0], parts[1]


def _validate_naming_patch_consumed(design, naming_patch, *, brief=None):
    target_names = (naming_patch or {}).get("target_names") or {}
    business_names = (naming_patch or {}).get("business_names") or {}
    placeholder_scopes, placeholder_roots = _placeholder_naming_scopes(
        design,
        brief,
    )
    operations = {}
    for step in (design or {}).get("steps") or ():
        if not isinstance(step, dict):
            continue
        step_id = str(step.get("step_id") or "")
        for operation in step.get("operations") or ():
            if not isinstance(operation, dict):
                continue
            operations[(
                step_id,
                str(operation.get("target_action_id") or ""),
            )] = operation
    missing_targets = []
    mismatched_targets = []
    for scope, value in target_names.items():
        scope_key = _naming_scope_key(scope)
        operation = operations.get(scope_key)
        if operation is None:
            if scope_key in placeholder_scopes:
                continue
            missing_targets.append(str(scope))
            continue
        expected = str(value or "").strip()
        actual = str(operation.get("target_name") or "").strip()
        if actual != expected:
            mismatched_targets.append(str(scope))
    owners = {
        str(owner.get("root_name") or ""): owner
        for owner in (design or {}).get("window_ownership") or ()
        if isinstance(owner, dict)
    }
    missing_business = []
    mismatched_business = []
    for root_name, value in business_names.items():
        normalized_root = str(root_name or "").strip()
        owner = owners.get(normalized_root)
        if owner is None:
            if normalized_root in placeholder_roots:
                continue
            missing_business.append(str(root_name))
            continue
        expected = str(value or "").strip()
        actual = str(owner.get("business_name") or "").strip()
        if actual != expected:
            mismatched_business.append(str(root_name))
    if any((
            missing_targets,
            mismatched_targets,
            missing_business,
            mismatched_business,
    )):
        raise ValueError(
            "NamingPatch未被baseline消费: "
            f"target_names={missing_targets}, "
            f"mismatched_target_names={mismatched_targets}, "
            f"business_names={missing_business}, "
            f"mismatched_business_names={mismatched_business}"
        )


def _placeholder_naming_scopes(design, brief):
    design = design if isinstance(design, dict) else {}
    brief = brief if isinstance(brief, dict) else {}
    ambiguities = {
        str(item.get("ambiguity_id") or ""): item
        for item in brief.get("ambiguities") or ()
        if isinstance(item, dict) and item.get("ambiguity_id")
    }
    placeholder_scopes = set()
    for choice in design.get("ambiguity_choices") or ():
        if not isinstance(choice, dict) or choice.get("outcome") != (
                "generate_issue_placeholder"
        ):
            continue
        ambiguity = ambiguities.get(str(choice.get("ambiguity_id") or ""))
        if ambiguity is None:
            continue
        step_id = str(ambiguity.get("step_id") or "")
        placeholder_scopes.update(
            (step_id, str(action_id))
            for action_id in ambiguity.get("action_ids") or ()
            if step_id and action_id
        )
    action_scopes_by_root = {}
    for action in brief.get("actions") or ():
        if not isinstance(action, dict) or action.get("role") == "noise":
            continue
        step_id = str(action.get("step_id") or "")
        action_id = str(action.get("id") or "")
        root_name = str(
            ((action.get("target") or {}).get("root_name") or "")
        )
        if step_id and action_id and root_name:
            action_scopes_by_root.setdefault(root_name, set()).add(
                (step_id, action_id)
            )
    placeholder_roots = {
        root_name
        for root_name, action_scopes in action_scopes_by_root.items()
        if action_scopes and action_scopes <= placeholder_scopes
    }
    return placeholder_scopes, placeholder_roots


def _discovery_ambiguity_issue(error):
    return {
        "kind": "system_baseline_needs_ai_ambiguity_choice",
        "patch_type": "ambiguity_choice",
        "message": str(error),
        "ambiguity_id": error.ambiguity_id,
        "outcomes": copy.deepcopy(error.outcomes),
    }


def _discovery_assertion_issue(error):
    return {
        "kind": "system_baseline_needs_ai_assertion_choice",
        "patch_type": "assertion_choice",
        "message": str(error),
        "ambiguity_id": error.ambiguity_id,
        "candidates": copy.deepcopy(error.candidates),
    }


def _discovery_method_issue(error):
    return {
        "kind": "system_baseline_needs_ai_method_choice",
        "patch_type": "method_choice",
        "message": str(error),
        "step_id": error.step_id,
        "candidates": copy.deepcopy(error.candidates),
    }


def _discovery_operation_issue(error):
    return {
        "kind": "system_baseline_needs_ai_operation_choice",
        "patch_type": "operation_choice",
        "message": str(error),
        "step_id": error.step_id,
        "action_id": error.action_id,
        "candidates": copy.deepcopy(error.candidates),
    }


def _discovery_value_source_issue(error):
    return {
        "kind": "system_baseline_needs_ai_value_source_choice",
        "patch_type": "value_source_choice",
        "message": str(error),
        "step_id": error.step_id,
        "action_id": error.action_id,
        "operation": error.operation,
        "sources": copy.deepcopy(error.sources),
    }


def _discovery_naming_issue(error, brief=None):
    text = str(error)
    business_name = _business_name_scope(text)
    if business_name:
        root_name = _root_name_for_generated_business_name(brief, business_name)
        if root_name:
            return {
                "kind": "system_baseline_needs_ai_naming",
                "patch_type": "naming",
                "message": text,
                "business_name": business_name,
                "root_name": root_name,
            }
    match = re.search(
        r"step=(?P<step>[A-Za-z0-9_-]+)\s+action=(?P<action>[A-Za-z0-9_-]+)",
        text,
    )
    if not match:
        return None
    step_id = match.group("step")
    action_id = match.group("action")
    issue = {
        "kind": "system_baseline_needs_ai_naming",
        "patch_type": "naming",
        "message": text,
        "step_id": step_id,
        "action_id": action_id,
    }
    target = _discovery_action_target(brief, step_id, action_id)
    if target:
        issue.update({
            "root_name": target.get("root_name"),
            "target_fingerprint": target.get("target_fingerprint"),
        })
    return issue


def _discovery_action_target(brief, step_id, action_id):
    for action in (brief or {}).get("actions") or ():
        if not isinstance(action, dict):
            continue
        if (
                str(action.get("step_id") or "") == str(step_id or "")
                and str(action.get("id") or "") == str(action_id or "")
        ):
            target = action.get("target") or {}
            return dict(target) if isinstance(target, dict) else {}
    return {}


def _discovery_issue_key(issue):
    issue = issue if isinstance(issue, dict) else {}
    return (
        str(issue.get("patch_type") or issue.get("kind") or ""),
        str(issue.get("ambiguity_id") or ""),
        str(issue.get("step_id") or ""),
        str(issue.get("action_id") or ""),
        str(issue.get("root_name") or ""),
        str(issue.get("operation") or ""),
    )


def _apply_discovery_placeholder(
        issue,
        scratch_naming,
        ambiguity_choices,
        assertion_choices,
        method_choices,
        operation_choices,
        value_source_choices,
    ):
    patch_type = str((issue or {}).get("patch_type") or "")
    if patch_type == "naming":
        step_id = str(issue.get("step_id") or "")
        action_id = str(issue.get("action_id") or "")
        if step_id and action_id:
            identity = (
                str(issue.get("root_name") or ""),
                str(issue.get("target_fingerprint") or ""),
            )
            identity_names = scratch_naming.setdefault(
                "_target_identity_names",
                {},
            )
            if all(identity):
                name = identity_names.get(identity)
                if name is None:
                    name = f"generated_target_{len(identity_names) + 1}"
                    identity_names[identity] = name
            else:
                name = f"generated_target_{len(scratch_naming['target_names']) + 1}"
            scratch_naming.setdefault("target_names", {})[(step_id, action_id)] = (
                name
            )
            return True
        root_name = str(issue.get("root_name") or "")
        if root_name:
            scratch_naming.setdefault("business_names", {})[root_name] = (
                f"generated_area_{len(scratch_naming['business_names']) + 1}"
            )
            return True
        return False
    if patch_type == "ambiguity_choice":
        outcome = _first_discovery_outcome(issue.get("outcomes") or [])
        if not outcome:
            return False
        ambiguity_choices.append({
            "ambiguity_id": issue.get("ambiguity_id"),
            "outcome": outcome,
        })
        return True
    if patch_type == "assertion_choice":
        candidate = _first_dict(issue.get("candidates") or [])
        if not candidate:
            return False
        assertion_choices.append({
            "ambiguity_id": issue.get("ambiguity_id"),
            "candidate_key": (
                candidate.get("candidate_key")
                or assertion_candidate_key(candidate)
            ),
        })
        return True
    if patch_type == "method_choice":
        candidate = _first_dict(issue.get("candidates") or [])
        if not candidate or not candidate.get("candidate_id"):
            return False
        method_choices.append({
            "step_id": issue.get("step_id"),
            "candidate_id": candidate.get("candidate_id"),
        })
        return True
    if patch_type == "operation_choice":
        candidate = _first_dict(issue.get("candidates") or [])
        if not candidate:
            return False
        operation_choices.append({
            "step_id": issue.get("step_id"),
            "action_id": issue.get("action_id"),
            "choice_key": candidate.get("choice_key") or operation_choice_key(candidate),
        })
        return True
    if patch_type == "value_source_choice":
        source = _first_dict(issue.get("sources") or [])
        if not source:
            return False
        value_source_choices.append({
            "step_id": issue.get("step_id"),
            "action_id": issue.get("action_id"),
            "operation": issue.get("operation"),
            "source": source,
        })
        return True
    return False


def _first_dict(values):
    return next((item for item in values if isinstance(item, dict)), None)


def _first_discovery_outcome(outcomes):
    for item in outcomes:
        if not isinstance(item, dict):
            continue
        if any((
                item.get("effect") != "plan_coverage",
                item.get("candidate_id"),
                item.get("candidate_ids"),
        )):
            continue
        return str(item.get("outcome") or "") or None
    return None


def _business_name_scope(message):
    match = re.search(
        r"window business_name[^:]*:\s*(?P<name>[A-Za-z0-9_\-]+)",
        str(message or ""),
    )
    return match.group("name") if match else None


def _root_name_for_generated_business_name(brief, business_name):
    windows = ((brief or {}).get("window_ownership") or {}).get("windows")
    for window in windows or ():
        root_name = str((window or {}).get("root_name") or "")
        if not root_name:
            continue
        if str(business_name) in _generated_business_name_candidates(root_name):
            return root_name
    return None


def _generated_business_name_candidates(root_name):
    full = str(root_name).replace("_window_", "_")
    full = re.sub(r"[^0-9A-Za-z_]+", "_", full).strip("_").lower()
    full = re.sub(r"_+", "_", full) or "generated"
    prefix = str(root_name).split("_window_", 1)[0]
    prefix = re.sub(r"[^0-9A-Za-z_]+", "_", prefix).strip("_").lower()
    prefix = re.sub(r"_+", "_", prefix)
    return {value for value in (full, prefix) if value}


def _job_product_state_naming_overrides(session_dir, job, brief):
    pointer = job.get("generation_capsule") or {}
    if not pointer:
        return {}
    capsule = load_generation_capsule(
        session_dir,
        pointer,
    )
    facts = resolve_product_state_facts(
        brief,
        generation_capsule_source_files(capsule),
    )
    return facts["naming_overrides"]


def _empty_generation_design():
    return {
        "design_version": GENERATION_DESIGN_VERSION,
        "summary": "",
        "window_ownership": [],
        "steps": [],
        "ambiguity_choices": [],
    }


def query_generation_job_task_bundle(job_path, *, fragment_id=None):
    session_dir, job, state = _resolve_known_job(job_path)
    _require_job_query(job, "job-task-bundle")
    bundle = load_generation_task_bundle(session_dir, job)
    if bundle is None:
        raise ValueError("Generation Job缺少有效TaskBundle")
    transition = _job_result(session_dir, job, state)["job_transition"]
    if fragment_id:
        return {
            "generation_task_bundle_query_version": "1.0",
            "status": "projected",
            "request_id": (job.get("request") or {}).get("request_id"),
            "job_id": job.get("job_id"),
            "job_transition": transition,
            "query": {"fragment_id": str(fragment_id)},
            "fragment": load_generation_task_fragment(
                session_dir,
                bundle,
                fragment_id,
            ),
        }
    return {
        "generation_task_bundle_query_version": "1.0",
        "status": "projected",
        "request_id": (job.get("request") or {}).get("request_id"),
        "job_id": job.get("job_id"),
        "job_transition": transition,
        "query": {"fragment_id": None},
        "generation_task_bundle": project_generation_task_bundle_index(
            bundle,
            transition=transition,
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
    if (report.get("system_materialization") or {}).get(
            "status"
    ) != "candidate_prepared":
        raise ValueError("Implementation Packet候选尚未准备")
    capsule = load_generation_capsule(
        session_dir,
        job.get("generation_capsule") or {},
    )
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
            source_files=generation_capsule_source_files(capsule),
            step_id=step_id,
            path=path,
        ),
    }


def query_generation_job_implementation_candidate(
        report_path,
        *,
        path=None,
    include_all=False,
    ):
    session_dir, job = _job_for_transaction_report(report_path)
    _require_job_query(job, "job-implementation-candidate")
    report_path = Path(report_path).resolve()
    report = _read_json(report_path)
    state = load_workflow_state(
        session_dir,
        (job.get("request") or {}).get("request_id"),
    )
    _validate_implementation_packet_query(state, job, report, report_path)
    manifest = report.get("implementation_manifest") or {}
    pointer = (report.get("system_materialization") or {}).get(
        "candidate"
    ) or {}
    candidate = load_implementation_scaffold_candidate(
        session_dir,
        pointer,
        transaction_id=report.get("transaction_id"),
    )
    if any((
        candidate.get("manifest_id")
        != manifest.get("implementation_manifest_id"),
        candidate.get("manifest_fingerprint")
        != manifest.get("implementation_manifest_fingerprint"),
    )):
        raise ValueError("Implementation candidate与Manifest不一致")
    if path and include_all:
        raise ValueError("Implementation candidate不能同时指定path和all")
    selected_path = str(path or "").replace("\\", "/") or None
    files = list(candidate.get("files") or ())
    total_content_bytes = sum(
        len(str(item.get("content") or "").encode("utf-8"))
        for item in files
    )
    all_included = bool(
        include_all
        and selected_path is None
        and total_content_bytes <= 24 * 1024
    )
    if all_included:
        projected_files = files
    elif selected_path is None:
        projected_files = [{
            key: item.get(key)
            for key in (
                "path",
                "before_exists",
                "before_sha256",
                "sha256",
            )
        } for item in files]
    else:
        projected_files = [
            item for item in files
            if item.get("path") == selected_path
        ]
        if len(projected_files) != 1:
            raise ValueError(
                f"Implementation candidate不存在目标文件: {selected_path}"
            )
    projected_files = _with_native_edit_source_files(
        session_dir,
        report_path,
        candidate,
        projected_files,
    )
    candidate_manifest = None
    candidate_index = None
    if selected_path is None and not all_included:
        candidate_manifest = _persist_candidate_delivery_manifest(
            session_dir,
            report_path,
            job,
            report,
            candidate,
            projected_files,
        )
        candidate_index = _persist_candidate_index(
            session_dir,
            report_path,
            job,
            report,
            candidate,
            candidate_manifest,
            projected_files,
        )
        projected_files = []
    result = _job_result(session_dir, job, state)
    return {
        "implementation_candidate_query_version": "1.5",
        "status": "projected",
        "request_id": (job.get("request") or {}).get("request_id"),
        "job_id": job.get("job_id"),
        "transaction_id": report.get("transaction_id"),
        "report_path": str(report_path),
        "job_transition": result["job_transition"],
        "candidate_fingerprint": candidate.get("candidate_fingerprint"),
        "query": {
            "path": selected_path,
            "all": bool(include_all),
        },
        "file_count": len(files),
        "total_content_bytes": total_content_bytes,
        "all_included": all_included,
        **({"candidate_manifest": candidate_manifest} if candidate_manifest else {}),
        **({"candidate_index": candidate_index} if candidate_index else {}),
        "files": projected_files,
        "mechanical_copy_contract": {
            "version": "1.1",
            "boundary_role": "internal_diagnostic_candidate_artifact",
            "normal_delivery_write_channel": "system_materializer",
            "operation": "diagnostic_inspection_only",
            "operation_field": "candidate_index.lines[].op",
            "manifest_operation_field": "files[].operation",
            "editable_list_field": "candidate_index.lines",
            "source_file_field": "candidate_index.source_root + lines[].source",
            "target_file_field": "candidate_index.lines[].target",
            "source_file_owner": "system_candidate_artifact",
            "verification": "target_sha256_must_equal_expected_sha256",
            "rule": (
                "Use only for internal diagnostic candidate inspection; normal "
                "system-owned delivery is written by system_materializer and "
                "reviewed through implementation_diff."
            ),
            "do_not": [
                "do_not_use_as_delivery_plan",
                "do_not_copy_candidate_files_with_agent_editor_edit",
                "do_not_retype_candidate_content",
                "do_not_edit_source_files",
                "do_not_reformat_or_normalize_text",
            ],
        },
    }


def _persist_candidate_delivery_manifest(
        session_dir,
        report_path,
        job,
        report,
        candidate,
        files,
    ):
    manifest_files = []
    for item in files or ():
        if not isinstance(item, dict):
            continue
        source_path = str(item.get("source_workspace_path") or "")
        target_path = str(item.get("target_path") or item.get("path") or "")
        if not source_path or not target_path:
            raise ValueError("Candidate delivery manifest缺少source或target路径")
        _validate_project_relative_candidate_path(source_path)
        _validate_project_relative_candidate_path(target_path)
        manifest_files.append({
            "source_workspace_path": source_path,
            "target_path": target_path,
            "operation": item.get("operation"),
            "expected_sha256": item.get("expected_sha256") or item.get("sha256"),
            "before_exists": bool(item.get("before_exists")),
            "before_sha256": item.get("before_sha256"),
        })
    manifest_value = {
        "candidate_delivery_manifest_version": CANDIDATE_DELIVERY_MANIFEST_VERSION,
        "request_id": (job.get("request") or {}).get("request_id"),
        "job_id": job.get("job_id"),
        "transaction_id": report.get("transaction_id"),
        "candidate_fingerprint": candidate.get("candidate_fingerprint"),
        "file_count": len(manifest_files),
        "files": manifest_files,
    }
    manifest_value["manifest_fingerprint"] = _fingerprint(manifest_value)
    path = report_path.parent / "candidate-delivery-manifest.json"
    if path.exists():
        existing = _read_json(path)
        if existing != manifest_value:
            raise ValueError("Candidate delivery manifest fingerprint conflict")
    else:
        write_json_atomic(path, manifest_value)
    project_root = _project_root_for_generation_session(session_dir)
    return {
        "candidate_delivery_manifest_version": CANDIDATE_DELIVERY_MANIFEST_VERSION,
        "path": path.relative_to(project_root).as_posix(),
        "session_path": path.relative_to(session_dir).as_posix(),
        "manifest_fingerprint": manifest_value["manifest_fingerprint"],
        "candidate_fingerprint": candidate.get("candidate_fingerprint"),
        "file_count": len(manifest_files),
        "source": "transaction_native_edit_sources",
    }


def _persist_candidate_index(
        session_dir,
        report_path,
        job,
        report,
        candidate,
        candidate_manifest,
        files,
    ):
    project_root = Path(_project_root_for_generation_session(session_dir)).resolve()
    report_path = Path(report_path).resolve()
    fingerprint = str(candidate.get("candidate_fingerprint") or "")
    source_root = (report_path.parent / "native-edit-sources" / fingerprint).resolve()
    records = []
    for index, item in enumerate(files or (), start=1):
        if not isinstance(item, dict):
            continue
        source_workspace_path = str(item.get("source_workspace_path") or "")
        target_path = str(item.get("target_path") or item.get("path") or "")
        expected_sha = str(item.get("expected_sha256") or item.get("sha256") or "")
        if not source_workspace_path or not target_path or not expected_sha:
            raise ValueError("Candidate index缺少source、target或sha")
        _validate_project_relative_candidate_path(source_workspace_path)
        _validate_project_relative_candidate_path(target_path)
        source_path = (project_root / source_workspace_path).resolve()
        try:
            source_relative_path = source_path.relative_to(source_root).as_posix()
        except ValueError as error:
            raise ValueError("Candidate index source path不在source_root内") from error
        _validate_project_relative_candidate_path(source_relative_path)
        records.append({
            "i": index,
            "source": source_relative_path,
            "target": target_path,
            "op": item.get("operation"),
            "sha256": expected_sha,
            "before_exists": bool(item.get("before_exists")),
            "before_sha256": item.get("before_sha256"),
        })
    lines = [
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for record in records
    ]
    body = "\n".join(lines) + ("\n" if lines else "")
    path = report_path.parent / "candidate-index.jsonl"
    encoded = body.encode("utf-8")
    index_fingerprint = hashlib.sha256(encoded).hexdigest()
    if path.exists():
        if path.read_bytes() != encoded:
            raise ValueError("Candidate index fingerprint conflict")
    else:
        _atomic_write_text(path, body)
    line_count = len(records)
    recommended_end = min(CANDIDATE_INDEX_MAX_LINES_PER_READ, line_count)
    return {
        "candidate_index_version": CANDIDATE_INDEX_VERSION,
        "path": path.relative_to(project_root).as_posix(),
        "session_path": path.relative_to(session_dir).as_posix(),
        "index_fingerprint": index_fingerprint,
        "index_size_bytes": len(encoded),
        "line_count": line_count,
        "max_lines_per_read": CANDIDATE_INDEX_MAX_LINES_PER_READ,
        "recommended_read": {
            "start_line": 1 if line_count else 0,
            "end_line": recommended_end,
        },
        "source_root": source_root.relative_to(project_root).as_posix(),
        "manifest_path": candidate_manifest.get("path"),
        "manifest_fingerprint": candidate_manifest.get("manifest_fingerprint"),
        "candidate_fingerprint": candidate.get("candidate_fingerprint"),
        "record_format": {
            "line_format": "json_object_per_line",
            "source_field": "source",
            "target_field": "target",
            "operation_field": "op",
            "expected_sha256_field": "sha256",
            "before_exists_field": "before_exists",
            "before_sha256_field": "before_sha256",
        },
        "read_strategy": "read_file_line_window",
        "source": "transaction_native_edit_sources",
        "request_id": (job.get("request") or {}).get("request_id"),
        "job_id": job.get("job_id"),
        "transaction_id": report.get("transaction_id"),
    }


def _validate_project_relative_candidate_path(path):
    value = str(path or "").replace("\\", "/")
    if not value or value.startswith("/") or ".." in Path(value).parts:
        raise ValueError("Candidate delivery manifest path invalid")


def _with_native_edit_source_files(session_dir, report_path, candidate, files):
    session_dir = Path(session_dir).resolve()
    report_path = Path(report_path).resolve()
    project_root = Path(_project_root_for_generation_session(session_dir)).resolve()
    fingerprint = str(candidate.get("candidate_fingerprint") or "")
    source_root = report_path.parent / "native-edit-sources" / fingerprint
    candidate_files = {
        str(item.get("path") or "").replace("\\", "/"): item
        for item in candidate.get("files") or ()
        if isinstance(item, dict) and item.get("path")
    }
    projected = []
    for item in files or ():
        if not isinstance(item, dict):
            continue
        value = dict(item)
        value["content_included"] = "content" in value
        relative = str(value.get("path") or "").replace("\\", "/")
        if not relative or relative.startswith("/") or ".." in Path(relative).parts:
            raise ValueError("Implementation candidate source path invalid")
        value["operation"] = (
            "replace_file" if value.get("before_exists") else "create_file"
        )
        source_item = candidate_files.get(relative) or {}
        content = source_item.get("content")
        expected_sha = source_item.get("sha256") or value.get("sha256")
        if isinstance(content, str):
            source_path = source_root / relative
            source_path.parent.mkdir(parents=True, exist_ok=True)
            body = content.encode("utf-8")
            actual_sha = hashlib.sha256(body).hexdigest()
            if actual_sha != expected_sha:
                raise ValueError("Implementation candidate source sha mismatch")
            if source_path.exists() and source_path.read_bytes() != body:
                raise ValueError("Implementation candidate source file conflict")
            if not source_path.exists():
                source_path.write_bytes(body)
            try:
                source_workspace_path = source_path.relative_to(
                    project_root,
                ).as_posix()
            except ValueError:
                source_workspace_path = source_path.relative_to(
                    session_dir,
                ).as_posix()
            target_path = project_root / relative
            value.update({
                "source_path": source_path.relative_to(session_dir).as_posix(),
                "source_workspace_path": source_workspace_path,
                "source_absolute_path": str(source_path),
                "source_sha256": actual_sha,
                "target_path": relative,
                "target_absolute_path": str(target_path),
                "expected_sha256": expected_sha,
            })
        projected.append(value)
    return projected


def query_generation_job_diff(report_path, *, offset=0, limit=None):
    report_path, report = _terminal_transaction_report_for_query(report_path)
    if report.get("status") not in {"completed", "completed_no_changes"}:
        return _generation_diff_not_available(report_path, report)
    session_dir, job = _job_for_transaction_report(report_path)
    _require_job_query(job, "job-code-diff")
    pointer = report.get("implementation_diff") or {}
    state = load_workflow_state(
        session_dir,
        (job.get("request") or {}).get("request_id"),
    )
    retired = retired_job_entry(state, job_id=job.get("job_id"))
    execution = (
        (retired or {}).get("job_execution")
        or state.get("job_execution")
        or {}
    )
    owner = execution.get("transaction") or {}
    if any((
        transaction_result_fingerprint(report)
        != report.get("result_fingerprint"),
        completed_report_fingerprint(report)
        != report.get("completion_fingerprint"),
        owner.get("transaction_id") != report.get("transaction_id"),
        owner.get("result_fingerprint") != report.get("result_fingerprint"),
        owner.get("completion_fingerprint")
        != report.get("completion_fingerprint"),
        not generation_diff_pointer_is_valid(
            pointer,
            transaction_id=report.get("transaction_id"),
        ),
    )):
        raise ValueError("Generation Job Result缺少implementation diff")
    result = load_generation_diff(
        session_dir,
        pointer,
        offset=offset,
        limit=limit,
    )
    return {
        **result,
        "implementation_diff_summary": _implementation_diff_summary_from_artifacts(
            session_dir,
            pointer,
            report,
        ),
        "request_id": (job.get("request") or {}).get("request_id"),
        "job_id": job.get("job_id"),
        "transaction_id": report.get("transaction_id"),
    }


def _terminal_transaction_report_for_query(path):
    path = Path(path).resolve()
    value = _read_json(path)
    if value.get("generation_job_result_version"):
        return _transaction_report_from_job_result_path(path, value)
    return path, value


def _transaction_report_from_job_result_path(result_path, result):
    if not generation_job_result_identity_is_valid(result):
        raise ValueError("Generation Job Result身份无效")
    if len(result_path.parents) < 4:
        raise ValueError("Generation Job Result路径无效")
    session_dir = result_path.parents[3]
    root = (session_dir / "ai" / "generation-job-results").resolve()
    try:
        relative = result_path.relative_to(root)
    except ValueError as error:
        raise ValueError("Generation Job Result路径越界") from error
    job = result.get("job") or {}
    expected = (
        str(job.get("job_id") or ""),
        f"result-{result.get('result_fingerprint')}.json",
    )
    if tuple(relative.parts) != expected:
        raise ValueError("Generation Job Result路径与identity不一致")
    transaction = ((result.get("stages") or {}).get("transaction") or {})
    owner = transaction.get("owner") or {}
    report_path = _session_pointer_path(session_dir, owner.get("path"))
    if report_path is None or not report_path.is_file():
        raise ValueError("Generation Job Result缺少Transaction report")
    report = _read_json(report_path)
    if any((
        owner.get("transaction_id") != report.get("transaction_id"),
        owner.get("result_fingerprint") != report.get("result_fingerprint"),
        owner.get("completion_fingerprint")
        != report.get("completion_fingerprint"),
    )):
        raise ValueError("Generation Job Result与Transaction report不一致")
    return report_path, report


def _generation_diff_not_available(report_path, report):
    transaction_id = str(report.get("transaction_id") or "")
    if not transaction_id or report_path.name != "report.json":
        raise ValueError("Generation Job Transaction report路径无效")
    if len(report_path.parents) < 4:
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
    lease = report.get("generation_job_lease") or {}
    if not generation_job_lease_is_valid(lease):
        raise ValueError("Generation Job Transaction lease无效")
    return {
        "generation_diff_query_version": "1.0",
        "status": "not_available",
        "reason": "implementation_diff_available_after_terminal_result",
        "next_action": "run_generate_job_or_apply_candidate_files",
        "request_id": report.get("request_id"),
        "job_id": lease.get("job_id"),
        "transaction_id": report.get("transaction_id"),
        "transaction_status": report.get("status"),
        "report_path": str(report_path),
        "errors": [],
        "warnings": [],
    }


def submit_generation_job_design(
        job_path,
        design,
        *,
        claim_id,
        expected_epoch,
        note="",
    confirmation_source="ai_generated",
    plan_origin="external_ai",
    ):
    session_dir, job, _state = _resolve_current_job(job_path)
    lock = RunWriteLock(session_dir).acquire()
    try:
        session_dir, job, state = _resolve_current_job(job_path)
        request_path = _request_path_for_job(session_dir, job)
        lease = generation_job_lease(job)
        artifact = submit_generation_design(
            request_path,
            design,
            note=note,
            confirmation_source=confirmation_source,
            plan_origin=plan_origin,
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


def submit_generation_job_naming_patch(
        job_path,
        naming_patch,
        *,
        claim_id,
        expected_epoch,
    ):
    return submit_generation_job_typed_patch_batch(
        job_path,
        claim_id=claim_id,
        expected_epoch=expected_epoch,
        naming_patch=naming_patch,
    )


def _submitted_typed_patch_types(
        *,
        naming_patch=None,
        ambiguity_choice_patch=None,
        value_source_choice_patch=None,
        assertion_choice_patch=None,
        method_choice_patch=None,
        operation_choice_patch=None,
    ):
    return [
        name for name, value in (
            ("naming", naming_patch),
            ("ambiguity_choice", ambiguity_choice_patch),
            ("value_source_choice", value_source_choice_patch),
            ("assertion_choice", assertion_choice_patch),
            ("method_choice", method_choice_patch),
            ("operation_choice", operation_choice_patch),
        )
        if value is not None
    ]


def submit_generation_job_typed_patch_batch(
        job_path,
        *,
        claim_id,
        expected_epoch,
        naming_patch=None,
        ambiguity_choice_patch=None,
        value_source_choice_patch=None,
        assertion_choice_patch=None,
        method_choice_patch=None,
        operation_choice_patch=None,
    ):
    session_dir, job, state = _resolve_current_job(job_path)
    _record_job_interaction(
        session_dir,
        job,
        state,
        event="typed_patch_submitted",
        status="typed_patch_submitted",
        next_action="advance_job",
        details={
            "patch_types": _submitted_typed_patch_types(
                naming_patch=naming_patch,
                ambiguity_choice_patch=ambiguity_choice_patch,
                value_source_choice_patch=value_source_choice_patch,
                assertion_choice_patch=assertion_choice_patch,
                method_choice_patch=method_choice_patch,
                operation_choice_patch=operation_choice_patch,
            ),
        },
    )
    baseline_kwargs = {
        "ambiguity_choice_patch": ambiguity_choice_patch,
        "value_source_choice_patch": value_source_choice_patch,
        "assertion_choice_patch": assertion_choice_patch,
        "method_choice_patch": method_choice_patch,
        "operation_choice_patch": operation_choice_patch,
    }
    naming_overrides = _naming_overrides_from_patch(naming_patch)
    baseline = build_generation_job_baseline_design(
        job_path,
        naming_overrides=naming_overrides,
        **baseline_kwargs,
    )
    if naming_patch is not None:
        brief = load_generation_brief(_brief_path_for_job(session_dir, job))
        _validate_naming_patch_consumed(
            baseline,
            naming_patch,
            brief=brief,
        )
        compile_generation_design(
            baseline,
            brief,
            require_public_locator_names=True,
        )
        design = baseline
    else:
        design = baseline
    return submit_generation_job_design(
        job_path,
        design,
        claim_id=claim_id,
        expected_epoch=expected_epoch,
        confirmation_source="ai_generated",
        plan_origin="deterministic_surrogate",
    )


def submit_generation_job_ambiguity_choice_patch(
        job_path,
        ambiguity_choice_patch,
        *,
        claim_id,
        expected_epoch,
    ):
    if not isinstance(ambiguity_choice_patch, dict) or any((
        ambiguity_choice_patch.get("ambiguity_choice_patch_version")
        != GENERATION_AMBIGUITY_CHOICE_PATCH_VERSION,
        ambiguity_choice_patch.get("patch_type") != "ambiguity_choice",
    )):
        raise ValueError("AmbiguityChoicePatch版本或类型无效")
    design = build_generation_job_baseline_design(
        job_path,
        ambiguity_choice_patch=ambiguity_choice_patch,
    )
    return submit_generation_job_design(
        job_path,
        design,
        claim_id=claim_id,
        expected_epoch=expected_epoch,
        confirmation_source="ai_generated",
        plan_origin="deterministic_surrogate",
    )


def submit_generation_job_value_source_choice_patch(
        job_path,
        value_source_choice_patch,
        *,
        claim_id,
        expected_epoch,
    ):
    if not isinstance(value_source_choice_patch, dict) or any((
        value_source_choice_patch.get("value_source_choice_patch_version")
        != GENERATION_VALUE_SOURCE_CHOICE_PATCH_VERSION,
        value_source_choice_patch.get("patch_type") != "value_source_choice",
    )):
        raise ValueError("ValueSourceChoicePatch版本或类型无效")
    design = build_generation_job_baseline_design(
        job_path,
        value_source_choice_patch=value_source_choice_patch,
    )
    return submit_generation_job_design(
        job_path,
        design,
        claim_id=claim_id,
        expected_epoch=expected_epoch,
        confirmation_source="ai_generated",
        plan_origin="deterministic_surrogate",
    )


def submit_generation_job_assertion_choice_patch(
        job_path,
        assertion_choice_patch,
        *,
        claim_id,
        expected_epoch,
    ):
    if not isinstance(assertion_choice_patch, dict) or any((
        assertion_choice_patch.get("assertion_choice_patch_version")
        != GENERATION_ASSERTION_CHOICE_PATCH_VERSION,
        assertion_choice_patch.get("patch_type") != "assertion_choice",
    )):
        raise ValueError("AssertionChoicePatch版本或类型无效")
    design = build_generation_job_baseline_design(
        job_path,
        assertion_choice_patch=assertion_choice_patch,
    )
    return submit_generation_job_design(
        job_path,
        design,
        claim_id=claim_id,
        expected_epoch=expected_epoch,
        confirmation_source="ai_generated",
        plan_origin="deterministic_surrogate",
    )


def submit_generation_job_method_choice_patch(
        job_path,
        method_choice_patch,
        *,
        claim_id,
        expected_epoch,
    ):
    if not isinstance(method_choice_patch, dict) or any((
        method_choice_patch.get("method_choice_patch_version")
        != GENERATION_METHOD_CHOICE_PATCH_VERSION,
        method_choice_patch.get("patch_type") != "method_choice",
    )):
        raise ValueError("MethodChoicePatch版本或类型无效")
    design = build_generation_job_baseline_design(
        job_path,
        method_choice_patch=method_choice_patch,
    )
    return submit_generation_job_design(
        job_path,
        design,
        claim_id=claim_id,
        expected_epoch=expected_epoch,
        confirmation_source="ai_generated",
        plan_origin="deterministic_surrogate",
    )


def submit_generation_job_operation_choice_patch(
        job_path,
        operation_choice_patch,
        *,
        claim_id,
        expected_epoch,
    ):
    if not isinstance(operation_choice_patch, dict) or any((
        operation_choice_patch.get("operation_choice_patch_version")
        != GENERATION_OPERATION_CHOICE_PATCH_VERSION,
        operation_choice_patch.get("patch_type") != "operation_choice",
    )):
        raise ValueError("OperationChoicePatch版本或类型无效")
    design = build_generation_job_baseline_design(
        job_path,
        operation_choice_patch=operation_choice_patch,
    )
    return submit_generation_job_design(
        job_path,
        design,
        claim_id=claim_id,
        expected_epoch=expected_epoch,
        confirmation_source="ai_generated",
        plan_origin="deterministic_surrogate",
    )


def mark_generation_job_design_required(job_path, *, reason, message, details=None):
    job_path = Path(job_path).resolve()
    session_dir, job, state = _resolve_current_job(job_path)
    execution = dict(state.get("job_execution") or {})
    if state.get("status") != "running" or execution.get("phase") != "design":
        raise ValueError("Generation Job当前阶段不能标记AI Design需求")
    execution["design_required"] = {
        "status": "required",
        "reason": str(reason or "system_baseline_unavailable"),
        "message": str(message or ""),
        "details": dict(details or {}),
    }
    state["job_execution"] = execution
    state["updated_at"] = datetime.now().isoformat(timespec="milliseconds")
    write_workflow_state(session_dir, state)
    if str(reason or "") in _TYPED_PATCH_DESIGN_REQUIRED_REASONS:
        _record_job_interaction(
            session_dir,
            job,
            state,
            event="typed_patch_required",
            status="typed_patch_required",
            next_action="submit_all_complete_typed_patch_arguments",
            details={"reason": str(reason or "")},
        )
    return _with_job_transition(_job_result(session_dir, job, state), session_dir, job)


def require_generation_job_ai_design_entry(job_path):
    _session_dir, job, state = _resolve_current_job(Path(job_path).resolve())
    if _ai_design_entry_allowed(job, state):
        return True
    raise ValueError(
        "Generation Job必须先运行generate-job尝试系统baseline；"
        "只有generate-job返回design_required后才允许AI Design"
    )


def _ai_design_entry_allowed(job, state):
    if generation_job_design_mode(job) == "paged":
        return True
    execution = state.get("job_execution") or {}
    marker = execution.get("design_required") or {}
    return marker.get("status") == "required"


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
    if result.get("status") == "transaction_superseded":
        state = load_workflow_state(
            session_dir,
            (job.get("request") or {})["request_id"],
        )
        execution = state.get("job_execution") or {}
        result = prepare_generation_transaction(
            request_path,
            project_root=project_root,
            generation_job_lease=generation_job_lease(job),
            generation_job_claim_id=execution.get("claim_id"),
            generation_job_expected_epoch=execution.get("epoch"),
        )
    if result.get("status") == "job_blocked" and _job_block_is_refreshable(
            result
    ):
        state = load_workflow_state(
            session_dir,
            (job.get("request") or {})["request_id"],
        )
        return _refresh_running_generation_job_locked(
            session_dir,
            request_path,
            job,
            state,
            phase="implementation",
            reason=str(
                result.get("job_failure_category")
                or "refresh_generation_job"
            ),
            errors=list(result.get("errors") or []),
            profile_id=(job.get("profile_lease") or {}).get("profile_id"),
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


def _job_block_is_refreshable(result):
    category = str(result.get("job_failure_category") or "")
    errors = [str(item) for item in result.get("errors") or ()]
    if category in {"generation_contract_changed", "missing_generation_plan"}:
        return True
    if category == "stale_during_generation" and errors and all(
            _source_asset_drift_error(error) for error in errors
    ):
        return True
    return False


def _source_asset_drift_error(error):
    text = str(error or "")
    return (
        "快照已变化" in text
        and any(marker in text for marker in (
            "candidate",
            "复用候选",
            "page_object",
            "root_locator_file",
            "locator",
            "method",
        ))
    )


def materialize_prepared_system_candidate(report_path, *, project_root=None):
    report_path = Path(report_path).resolve()
    report = _read_json(report_path)
    project_root = Path(project_root or Paths.BASE_DIR).resolve()
    manifest = report.get("implementation_manifest") or {}
    candidate_pointer = (report.get("system_materialization") or {}).get(
        "candidate"
    )
    if candidate_pointer:
        session_dir, _job = _job_for_transaction_report(report_path)
        candidate = load_implementation_scaffold_candidate(
            session_dir,
            candidate_pointer,
            transaction_id=report.get("transaction_id"),
        )
        audit = materialize_implementation_candidate(
            project_root,
            manifest,
            report.get("generation_input_snapshot") or {},
            candidate,
            lease=report.get("generation_file_lease") or {},
            journal_path=report_path.parent / "materialization-journal.json",
        )
    else:
        audit = materialize_implementation_scaffold(
            project_root,
            manifest,
            report.get("generation_input_snapshot") or {},
            lease=report.get("generation_file_lease") or {},
            journal_path=report_path.parent / "materialization-journal.json",
        )
    report["system_materialization_commit"] = audit
    write_json_atomic(report_path, report)
    return audit


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
    workflow_brief = (state.get("brief") or {}).get("brief_fingerprint")
    job_brief = (job.get("brief") or {}).get("brief_fingerprint")
    if workflow_brief != job_brief:
        raise ValueError("Generation Job Workflow Brief与Job不一致")
    return session_dir, job, state


def _validate_design_draft_project_root(project_root, session_dir):
    if not project_root.is_dir():
        raise ValueError("Generation Job Design项目目录不存在")
    try:
        Path(session_dir).resolve().relative_to(project_root)
    except ValueError as error:
        raise ValueError("Generation Job Design项目目录与Job不一致") from error


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


def _candidate_job_inspect(
        session_dir,
        request,
        state,
        job,
        *,
        budget_task_bundle=None,
    ):
    result = {
        "generation_job_service_version": GENERATION_JOB_SERVICE_VERSION,
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
        "generation_capsule": copy.deepcopy(
            job.get("generation_capsule") or {}
        ),
        **(
            {"generation_workspace_projection": copy.deepcopy(
                job.get("generation_workspace_projection") or {}
            )}
            if job.get("generation_workspace_projection")
            else {}
        ),
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
    if (
        budget_task_bundle is None
        and generation_job_design_mode(job) == "paged"
    ):
        brief_path = _brief_path_for_job(session_dir, job)
        brief = load_generation_brief(brief_path)
        from autowork_core.utils.debug_tools.recorder.generation_task_bundle import (
            build_generation_task_bundle,
        )
        bundle, _fragments = build_generation_task_bundle(
            brief,
            job,
            brief_path=brief_path.relative_to(session_dir),
        )
        budget_task_bundle = project_generation_task_bundle_index(
            bundle,
            transition=result["job_execution"],
        )
    result["generation_task_bundle"] = copy.deepcopy(
        budget_task_bundle or {}
    )
    return result


def _admission_rejected_result(request, admission):
    return {
        "generation_job_service_version": GENERATION_JOB_SERVICE_VERSION,
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


def _frozen_job_source_candidate_errors(session_dir, job):
    try:
        brief = load_generation_brief(
            _brief_path_for_job(session_dir, job)
        )
        generation_input_snapshot = _job_generation_input_snapshot(
            session_dir,
            job,
        )
    except (OSError, ValueError) as error:
        return [
            "Generation Job冻结源码候选无法读取: "
            f"{type(error).__name__}"
        ]
    project_root = _project_root_for_generation_session(session_dir)
    expected_sources = {}
    errors = []
    for path_value, expected_hash, source in _brief_source_candidates(brief):
        path = _generation_candidate_path(project_root, path_value)
        if path is None:
            errors.append(f"冻结源码候选路径无效: {source}")
            continue
        relative = path.relative_to(project_root).as_posix()
        expected_hash = str(expected_hash or "")
        current = expected_sources.get(relative)
        if current is not None and current["sha256"] != expected_hash:
            errors.append(
                f"冻结源码候选哈希冲突: {relative}"
            )
            continue
        expected_sources.setdefault(relative, {
            "path": path,
            "sha256": expected_hash,
            "sources": [],
        })["sources"].append(source)
    for relative, expected in sorted(expected_sources.items()):
        if not expected["sha256"]:
            errors.append(f"冻结源码候选缺少哈希: {relative}")
            continue
        record = (generation_input_snapshot.get("files") or {}).get(relative)
        if record is None:
            if _missing_source_candidate_can_recreate(relative):
                continue
            errors.append(f"冻结源码候选未封存: {relative}")
            continue
        if record.get("is_symlink") is True:
            errors.append(f"冻结源码候选指向符号链接: {relative}")
            continue
        if record.get("sha256") != expected["sha256"]:
            errors.append(f"冻结源码候选与Capsule不一致: {relative}")
    return errors


def _job_generation_input_snapshot(session_dir, job):
    capsule = load_generation_capsule(
        session_dir,
        job.get("generation_capsule") or {},
    )
    return generation_capsule_input_snapshot(capsule)


def _job_capsule_source_paths(project_root, brief, *, request=None):
    project_root = Path(project_root).resolve()
    paths = []
    feature_path = str(
        (((request or {}).get("target") or {}).get("feature") or {}).get(
            "source_relpath"
        ) or ""
    )
    if feature_path:
        paths.append(feature_path)
    paths.extend(related_product_locator_paths(project_root, brief))
    for path_value, _expected_hash, _source in _brief_source_candidates(brief):
        path = _generation_candidate_path(project_root, path_value)
        if path is None:
            continue
        relative = Path(path.relative_to(project_root).as_posix())
        if not any(
                relative == root or root in relative.parents
                for root in ALLOWED_WRITE_ROOTS
        ):
            continue
        paths.append(relative.as_posix())
    return sorted(set(paths))


def _job_capsule_exact_paths(request):
    source_relpath = str(
        (((request or {}).get("target") or {}).get("feature") or {}).get(
            "source_relpath"
        ) or ""
    )
    return [source_relpath] if source_relpath else []


def _missing_source_candidate_can_recreate(relative):
    path = Path(str(relative or "").replace("\\", "/"))
    return bool(
        path.parts[:2] in {
            ("Bdd", "steps"),
            ("Bdd", "page_obj"),
            ("Bdd", "locators"),
        }
        and path.suffix.casefold() in {".py", ".yaml", ".yml"}
    )


def _brief_source_candidates(brief):
    semantics = (brief.get("semantics") or {})
    target_steps = list((brief.get("target") or {}).get("steps") or ())
    reusable_step_candidate_ids = {
        str(candidate.get("candidate_id") or "")
        for candidate in semantics.get("reuse_candidates") or ()
        if isinstance(candidate, dict)
        and candidate.get("kind") == "step_definition"
        and any(
            _step_candidate_matches_current_target(candidate, target_step, brief)
            for target_step in target_steps
        )
        and candidate.get("candidate_id")
    }
    action_identities = {
        (
            str(action.get("step_id") or ""),
            str(action.get("id") or ""),
            str((action.get("target") or {}).get("root_name") or ""),
            str((action.get("target") or {}).get("locator_name") or ""),
            str((action.get("target") or {}).get(
                "target_fingerprint"
            ) or ""),
        )
        for action in (brief.get("actions") or ())
        if isinstance(action, dict)
    }
    referenced_roots = {
        identity[2]
        for identity in action_identities
        if identity[2]
    }
    windows = (
        (brief.get("window_ownership") or {}).get("windows") or ()
    )
    owner_page_paths = set()
    selected_owner_candidates = []
    for window in windows:
        if str(window.get("root_name") or "") not in referenced_roots:
            continue
        for candidate in (
                (window.get("owner_match") or {}).get("candidates") or ()
        ):
            if not isinstance(candidate, dict):
                continue
            selected_owner_candidates.append(candidate)
            page_path = str(candidate.get("page_object") or "")
            if page_path:
                owner_page_paths.add(page_path.replace("\\", "/"))
    for candidate in semantics.get("reuse_candidates") or ():
        if not isinstance(candidate, dict):
            continue
        candidate_id = str(candidate.get("candidate_id") or "")
        path = str(candidate.get("path") or "").replace("\\", "/")
        if not any((
            candidate_id in reusable_step_candidate_ids,
                candidate.get("kind") == "page_object_method"
                and path in owner_page_paths,
        )):
            continue
        candidate_path = candidate.get("path")
        file_sha256 = candidate.get("file_sha256")
        if candidate_path or file_sha256:
            yield (
                candidate_path,
                file_sha256,
                "reuse:" + (candidate_id or "unknown"),
            )
    for match in semantics.get("locator_reuse_matches") or ():
        if not isinstance(match, dict):
            continue
        action_identity = (
            str(match.get("step_id") or ""),
            str(match.get("action_id") or ""),
            str(match.get("root_name") or ""),
            str(match.get("evidence_name") or ""),
            str(match.get("target_fingerprint") or ""),
        )
        if any((
                match.get("status") != "unique_same_target",
                action_identity not in action_identities,
        )):
            continue
        path = match.get("locator_file")
        locator_sha256 = match.get("locator_sha256")
        if path or locator_sha256:
            yield (
                path,
                locator_sha256,
                "locator:" + str(match.get("match_id") or "unknown"),
            )
    for candidate in selected_owner_candidates:
        candidate_id = str(candidate.get("candidate_id") or "unknown")
        page_path = candidate.get("page_object")
        page_sha256 = candidate.get("page_sha256")
        if page_path or page_sha256:
            yield (
                page_path,
                page_sha256,
                f"window:{candidate_id}:page",
            )
        locator_path = candidate.get("root_locator_file")
        locator_sha256 = candidate.get("locator_sha256")
        if locator_path or locator_sha256:
            yield (
                locator_path,
                locator_sha256,
                f"window:{candidate_id}:locator",
            )
        for method in candidate.get("method_candidates") or ():
            if not isinstance(method, dict):
                continue
            path = method.get("path")
            file_sha256 = method.get("file_sha256")
            if path or file_sha256:
                yield (
                    path,
                    file_sha256,
                    "method:" + str(
                        method.get("candidate_id") or "unknown"
                    ),
                )


def _step_candidate_matches_current_target(candidate, target_step, brief):
    path = str(candidate.get("path") or "").replace("\\", "/")
    scope = (
        (((brief.get("target") or {}).get("scenario") or {}).get(
            "step_scope_binding"
        ) or {}).get("resolved_step_scope")
        or {}
    )
    visible_files = {
        str(item).replace("\\", "/")
        for item in scope.get("files") or ()
    }
    if visible_files and path not in visible_files:
        return False
    return any(
        step_pattern_contract_matches(contract, target_step)
        for contract in candidate_step_pattern_contracts(candidate)
    )


def _generation_candidate_path(project_root, value):
    if not value:
        return None
    project_root = Path(project_root).resolve()
    path = Path(str(value))
    path = path.resolve() if path.is_absolute() else (
        project_root / path
    ).resolve()
    try:
        path.relative_to(project_root)
    except ValueError:
        return None
    return path


def _archive_generation_job_brief(session_dir, job):
    brief_path = _brief_path_for_job(session_dir, job)
    value = _read_json(brief_path)
    expected_fingerprint = str(
        (job.get("brief") or {}).get("brief_fingerprint") or ""
    )
    if any((
        not expected_fingerprint,
        value.get("brief_fingerprint") != expected_fingerprint,
        load_generation_brief(brief_path).get("brief_fingerprint")
        != expected_fingerprint,
    )):
        raise ValueError("Generation Job冻结Brief无法归档")
    archive_path = _archived_generation_brief_path(session_dir, job)
    if archive_path.is_file():
        archived = load_generation_brief(archive_path)
        if archived.get("brief_fingerprint") != expected_fingerprint:
            raise ValueError("Generation Job冻结Brief归档冲突")
        return archive_path
    write_json_atomic(archive_path, value, compact=True)
    return archive_path


def _archive_replaced_generation_job_brief(session_dir, replacement):
    pointer = (replacement or {}).get("pointer") or {}
    job = load_generation_job(session_dir, pointer)
    if job is None:
        return None
    try:
        return _archive_generation_job_brief(session_dir, job)
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _archived_generation_brief_path(session_dir, job):
    return (
        Path(session_dir).resolve()
        / "ai"
        / "generation-briefs"
        / "archive"
        / str((job.get("request") or {}).get("request_id") or "")
        / (
            "brief-"
            + str((job.get("brief") or {}).get("brief_fingerprint") or "")
            + ".json"
        )
    )


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
    expected_fingerprint = str(
        (job.get("brief") or {}).get("brief_fingerprint") or ""
    )
    if _generation_brief_matches_fingerprint(path, expected_fingerprint):
        return path
    archive_path = _archived_generation_brief_path(session_dir, job)
    if _generation_brief_matches_fingerprint(
            archive_path,
            expected_fingerprint,
    ):
        return archive_path
    if not path.is_file():
        raise ValueError("Generation Job Brief不存在")
    raise ValueError("Generation Job冻结Brief已变化且缺少归档快照")


def _generation_brief_matches_fingerprint(path, expected_fingerprint):
    if not expected_fingerprint or not Path(path).is_file():
        return False
    try:
        brief = load_generation_brief(path)
    except (OSError, ValueError):
        return False
    return brief.get("brief_fingerprint") == expected_fingerprint


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


def _project_implementation_packet(
        packet,
        *,
        source_files=(),
        step_id=None,
        path=None,
    ):
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
    known_paths.update(
        str(item).replace("\\", "/")
        for item in packet.get("read_only_reuse") or ()
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
    source_paths = {
        str(item.get("path") or "").replace("\\", "/")
        for item in [*steps, *pages, *methods]
        if item.get("path")
    }
    source_paths.update(str(item).replace("\\", "/") for item in (
        packet.get("ai_editable_changes") or ()
    ))
    source_paths.update(str(item).replace("\\", "/") for item in (
        packet.get("read_only_reuse") or ()
    ))
    if requested_path:
        source_paths = {requested_path}
    projected_sources = _project_capsule_source_files(
        source_files,
        source_paths,
        include_content=bool(requested_path) or _source_content_fits_budget(
            source_files,
            source_paths,
        ),
    )
    return {
        "implementation_packet_projection_version": "1.0",
        "implementation_packet_version": packet.get(
            "implementation_packet_version"
        ),
        "packet_fingerprint": _fingerprint(packet),
        "derived_from": dict(packet.get("derived_from") or {}),
        "ai_editable_changes": list(packet.get("ai_editable_changes") or ()),
        "system_owned_changes": list(packet.get("system_owned_changes") or ()),
        "read_only_reuse": list(packet.get("read_only_reuse") or ()),
        "asset_resolution": dict(packet.get("asset_resolution") or {}),
        "source_files": projected_sources,
        "pages": pages,
        "steps": steps,
        "methods": methods,
        "rule": packet.get("rule"),
    }


def _source_content_fits_budget(source_files, source_paths):
    selected = {
        str(path).replace("\\", "/")
        for path in source_paths or ()
        if str(path or "")
    }
    total = 0
    for item in source_files or ():
        if str(item.get("path") or "") not in selected:
            continue
        if item.get("content_encoding") == "utf-8":
            total += len(str(item.get("content") or "").encode("utf-8"))
        elif item.get("content_encoding") == "base64":
            total += len(str(item.get("content_base64") or ""))
        if total > 48 * 1024:
            return False
    return True


def _project_capsule_source_files(source_files, source_paths, *, include_content):
    selected = {
        str(path).replace("\\", "/")
        for path in source_paths or ()
        if str(path or "")
    }
    projected = []
    for item in source_files or ():
        path = str(item.get("path") or "").replace("\\", "/")
        if path not in selected:
            continue
        if include_content or item.get("is_symlink") is True:
            projected.append(copy.deepcopy(item))
            continue
        projected.append({
            "source_file_version": item.get("source_file_version"),
            "path": path,
            "is_symlink": bool(item.get("is_symlink")),
            "sha256": item.get("sha256"),
            "size": item.get("size"),
            "content_omitted": True,
            "next_action": "query job-implementation-packet with path",
        })
    return sorted(projected, key=lambda item: item["path"])


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
    result_pointer = (
        (retired or {}).get("last_job_result")
        or workflow.get("last_job_result")
        or None
    )
    terminal_result = (
        load_generation_job_result(session_dir, result_pointer)
        if result_pointer
        else None
    )
    if terminal_result is not None and any((
        (terminal_result.get("job") or {}).get("job_id")
        != job.get("job_id"),
        (terminal_result.get("job") or {}).get("job_fingerprint")
        != job.get("job_fingerprint"),
    )):
        terminal_result = None
    if terminal_result is None:
        result_pointer = None
    errors = list(
        (retired or {}).get("errors")
        or workflow.get("errors")
        or ()
    )
    result = {
        "generation_job_service_version": GENERATION_JOB_SERVICE_VERSION,
        "status": (retired or {}).get("status") or workflow.get("status"),
        "next_action": (retired or {}).get("next_action") or workflow.get("next_action"),
        "request_id": (job.get("request") or {}).get("request_id"),
        "job_id": job.get("job_id"),
        "job_path": str(path),
        "job_fingerprint": job.get("job_fingerprint"),
        "workload": copy.deepcopy(job.get("workload") or {}),
        "design_execution": _design_execution(job),
        "generation_profile": _projected_generation_profile(job),
        "generation_admission": job.get("admission_receipt") or {},
        "generation_capsule": copy.deepcopy(
            job.get("generation_capsule") or {}
        ),
        **(
            {"generation_workspace_projection": copy.deepcopy(
                job.get("generation_workspace_projection") or {}
            )}
            if job.get("generation_workspace_projection")
            else {}
        ),
        **(
            {"workspace_projection_summary": _job_workspace_projection_summary(
                session_dir,
                job,
            )}
            if job.get("generation_workspace_projection")
            else {}
        ),
        "job_execution": execution,
        "job_lifecycle_timing": execution.get("job_lifecycle_timing"),
        "execution_boundary": _projected_execution_boundary(job),
        "business_answers_submitted": bool(
            ((workflow.get("decision") or {}).get("answers") or {}).get(
                "path"
            )
        ),
        "last_job_result": result_pointer,
        "errors": errors,
        "warnings": [],
    }
    review = execution.get("business_review") or {}
    if review.get("status") == "answers_required":
        result["business_questions"] = copy.deepcopy(
            review.get("questions") or []
        )
        result["business_review"] = {
            "status": "answers_required",
            "question_count": len(review.get("questions") or []),
            "workset_fingerprint": review.get("workset_fingerprint"),
        }
    elif terminal_result is None:
        request = _read_json(_request_path_for_job(session_dir, job))
        pack, _answers = _decision_artifacts(session_dir, request, workflow)
        current_review = _business_review_context(
            session_dir,
            job,
            workflow,
            pack,
        )
        if current_review:
            result["business_questions"] = copy.deepcopy(
                current_review.get("questions") or []
            )
            result["business_review"] = current_review
    if not result.get("business_review"):
        result.update(_pending_business_answer_context(session_dir, job, workflow))
    if terminal_result is not None:
        result["category"] = terminal_result.get("category")
        result["stages"] = copy.deepcopy(terminal_result.get("stages") or {})
        result["service_level"] = copy.deepcopy(
            terminal_result.get("service_level")
        )
        result["health_issues"] = copy.deepcopy(
            terminal_result.get("health_issues") or []
        )
        result["implementation_diff"] = copy.deepcopy(
            terminal_result.get("implementation_diff")
        )
        result["implementation_diff_summary"] = copy.deepcopy(
            terminal_result.get("implementation_diff_summary") or {}
        )
        result["git_diff_visibility"] = copy.deepcopy(
            terminal_result.get("git_diff_visibility") or {}
        )
        if terminal_result.get("workspace_projection_summary"):
            result["workspace_projection_summary"] = copy.deepcopy(
                terminal_result.get("workspace_projection_summary") or {}
            )
        result["generation_timing_ledger"] = copy.deepcopy(
            terminal_result.get("generation_timing_ledger") or {}
        )
        result.update(_terminal_delivery_fields(session_dir, terminal_result))
    elif result.get("status") == "failed":
        result["failure_summary"] = _job_failure_summary(
            execution,
            retired=retired,
            errors=errors,
        )
    result["job_transition"] = _job_transition(result)
    return result


def _pending_business_answer_context(session_dir, job, workflow):
    decision = workflow.get("decision") or {}
    if decision.get("status") != "awaiting_answers":
        return {}
    request_path = _request_path_for_job(session_dir, job)
    request = _read_json(request_path)
    pack = load_decision_pack(
        session_dir,
        decision.get("pack") or {},
        request,
        brief_fingerprint=(workflow.get("brief") or {}).get(
            "brief_fingerprint"
        ),
    )
    if pack is None:
        return {}
    answers = load_answer_record(
        session_dir,
        decision.get("answers") or {},
        request,
        pack,
    ) or {}
    answered_ids = {
        str(item.get("question_id") or "")
        for item in answers.get("answers") or ()
        if isinstance(item, dict) and item.get("question_id")
    }
    questions = [
        copy.deepcopy(question)
        for question in pack.get("questions") or ()
        if isinstance(question, dict)
        and question.get("blocking")
        and str(question.get("question_id") or "") not in answered_ids
    ]
    if not questions:
        return {}
    return {
        "business_questions": questions,
        "decision_answer_contract": {
            "answer_version": ANSWER_VERSION,
            "pack_id": pack.get("pack_id"),
            "pack_fingerprint": pack.get("pack_fingerprint"),
            "revision_seal": pack.get("revision_seal"),
            "submit_policy": "all_blocking_once",
            "answer_format": "question_id=declared_option_id",
        },
    }


def _job_workspace_projection_summary(session_dir, job, *, manifest=None):
    pointer = copy.deepcopy(job.get("generation_workspace_projection") or {})
    if not pointer:
        return {}
    try:
        projection = load_generation_workspace_projection(session_dir, pointer)
    except (OSError, ValueError, TypeError):
        projection = pointer
    return generation_workspace_projection_summary(
        projection,
        manifest=manifest,
    )


def _job_failure_summary(execution, *, retired=None, errors=None):
    execution = execution if isinstance(execution, dict) else {}
    retired = retired if isinstance(retired, dict) else {}
    fingerprint = str(execution.get("last_issue_fingerprint") or "") or None
    errors = list(errors or retired.get("errors") or [])
    reason = str(retired.get("reason") or "") or None
    primary_error = str(errors[0]) if errors else None
    return {
        "code": primary_error or "job_failed_without_result",
        "message": (
            "Generation Job failed before a terminal Job Result was published; "
            "report failure_summary/errors directly instead of repeatedly "
            "querying report/result paths."
        ),
        "reason": reason,
        "phase": execution.get("phase"),
        "attempt_no": execution.get("attempt_no"),
        "last_issue_fingerprint": fingerprint,
        "errors": errors,
        "has_terminal_result": False,
    }


def _terminal_delivery_fields(session_dir, terminal_result):
    transaction = ((terminal_result.get("stages") or {}).get(
        "transaction"
    ) or {}).get("owner") or {}
    report_path = _session_pointer_path(session_dir, transaction.get("path"))
    health_issues = list(terminal_result.get("health_issues") or [])
    fields = {
        "transaction_id": transaction.get("transaction_id"),
        "report_path": transaction.get("path"),
        "health_issues": health_issues,
        "unresolved_issues": copy.deepcopy(
            terminal_result.get("unresolved_issues") or []
        ),
        "failure_summary": _terminal_failure_summary(terminal_result, {}),
    }
    if report_path and report_path.is_file():
        try:
            report = _read_json(report_path)
        except (OSError, ValueError):
            report = {}
        health_issues = _terminal_health_issues(
            health_issues,
            terminal_result.get("service_level") or {},
            report,
        )
        fields.update({
            "changed_files": list(report.get("changed_files") or []),
            "execution_outcome": copy.deepcopy(
                report.get("execution_outcome") or {}
            ),
            "implementation_diff_summary": _implementation_diff_summary_from_artifacts(
                session_dir,
                terminal_result.get("implementation_diff")
                or report.get("implementation_diff")
                or {},
                terminal_result,
                report,
            ),
            "git_diff_visibility": copy.deepcopy(
                terminal_result.get("git_diff_visibility")
                or report.get("git_diff_visibility")
                or {}
            ),
            **_terminal_delivery_projection(
                terminal_result,
                report,
                transaction.get("path"),
                health_issues,
            ),
            "health_issues": health_issues,
            "failure_summary": _terminal_failure_summary(
                terminal_result,
                report,
            ),
        })
    return fields


def _implementation_diff_summary_from_artifacts(session_dir, pointer, *sources):
    for source in sources:
        summary = (source or {}).get("implementation_diff_summary") or {}
        if summary:
            return copy.deepcopy(summary)
    if not generation_diff_pointer_is_valid(pointer):
        return {}
    try:
        return build_generation_diff_summary(session_dir, pointer)
    except (OSError, UnicodeError, ValueError):
        return {}


def _terminal_failure_summary(terminal_result, report):
    if (terminal_result or {}).get("status") != "failed":
        return None
    stages = (terminal_result or {}).get("stages") or {}
    failed_stage = next((
        name
        for name in JOB_STAGE_NAMES
        if (stages.get(name) or {}).get("status") == "failed"
    ), None)
    stage = stages.get(failed_stage) or {}
    owner = stage.get("owner") or {}
    abort = (report or {}).get("abort") or {}
    reason = str(
        abort.get("reason")
        or (report or {}).get("summary")
        or owner.get("reason")
        or next(iter(owner.get("errors") or ()), "")
        or ""
    ) or None
    category = str((terminal_result or {}).get("category") or "failed")
    return {
        "has_terminal_result": True,
        "code": category,
        "category": category,
        "stage": failed_stage,
        "message": reason or f"Generation failed during {failed_stage or 'unknown'} stage.",
        "reason": reason,
        "owner": copy.deepcopy(owner),
        "report_status": (report or {}).get("status"),
    }


def _terminal_delivery_projection(
        terminal_result,
        report,
        report_path,
        health_issues,
    ):
    execution = report.get("execution_outcome") or {}
    materialization = report.get("system_materialization") or {}
    materialization_commit = report.get("system_materialization_commit") or {}
    receipt = report.get("implementation_receipt") or {}
    persisted_visibility = terminal_result.get("delivery_visibility") or {}
    persisted_summary = terminal_result.get("delivery_summary") or {}
    ai_editable = list(receipt.get("ai_editable_changes") or [])
    system_owned = _terminal_system_materialized_files(
        receipt,
        materialization,
        materialization_commit,
    )
    changed_files = list(report.get("changed_files") or [])
    implementation_diff = (
        terminal_result.get("implementation_diff")
        or report.get("implementation_diff")
        or {}
    )
    delivery_source = (
        persisted_visibility.get("source")
        or persisted_summary.get("delivery_source")
    )
    review_channel = (
        persisted_visibility.get("review_channel")
        or persisted_summary.get("review_channel")
    )
    if not delivery_source or not review_channel:
        if receipt.get("delivery_write_channel"):
            delivery_source = delivery_source_from_implementation_receipt(receipt)
            review_channel = delivery_review_channel_from_source(delivery_source)
        else:
            delivery_source = "none"
            review_channel = "none"
    delivery_write_channel = (
        receipt.get("delivery_write_channel")
        or persisted_visibility.get("delivery_write_channel")
        or persisted_summary.get("delivery_write_channel")
        or _delivery_write_channel_from_source(delivery_source)
    )
    runtime_status = execution.get("runtime_status")
    oracle_status = (
        ((terminal_result.get("stages") or {}).get("oracle") or {}).get(
            "status"
        )
    )
    failure_summary = _terminal_failure_summary(terminal_result, report)
    return {
        "delivery_visibility": {
            "source": delivery_source,
            "delivery_write_channel": delivery_write_channel,
            "review_channel": review_channel,
            "host_native_edit_available": delivery_source == "host_native_edit",
            "requires_explicit_diff_review": review_channel == "implementation_diff",
            "changed_file_count": len(changed_files),
            "changed_files": changed_files,
            "report_path": report_path,
            "implementation_diff_path": implementation_diff.get("path"),
            "system_owned_file_count": len(system_owned),
            "ai_editable_count": len(ai_editable),
            "review_note": _terminal_delivery_review_note(
                delivery_source,
                review_channel,
            ),
        },
        "delivery_summary": {
            "status": terminal_result.get("status"),
            "job_id": (terminal_result.get("job") or {}).get("job_id"),
            "category": terminal_result.get("category"),
            "failed_stage": (
                failure_summary.get("stage")
                if failure_summary else None
            ),
            "failure_summary": failure_summary,
            "static_status": execution.get("static_status"),
            "runtime_status": runtime_status,
            "oracle_status": oracle_status,
            "service_level_status": (
                (terminal_result.get("service_level") or {}).get("status")
            ),
            "health_status": "attention_required" if health_issues else "ok",
            "health_issues": copy.deepcopy(health_issues),
            "delivery_source": delivery_source,
            "delivery_write_channel": delivery_write_channel,
            "review_channel": review_channel,
            "changed_file_count": len(changed_files),
            "changed_files": changed_files,
            "report_path": report_path,
            "implementation_diff_path": implementation_diff.get("path"),
            "system_materialization_commit_status": (
                materialization_commit.get("status")
            ),
        },
        "acceptance_summary": {
            "static_status": execution.get("static_status"),
            "runtime_status": runtime_status,
            "oracle_status": oracle_status,
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
                if terminal_result.get("status") == "failed"
                else "static_only"
                if runtime_status == "runtime_not_run"
                else "runtime_or_oracle_pending"
                if runtime_status in {"runtime_pending", None}
                else "runtime_evaluated"
            ),
        },
    }


def _terminal_delivery_review_note(delivery_source, review_channel):
    if review_channel == "implementation_diff":
        return (
            "System-owned files were materialized by generate-job; review "
            "changed_files or implementation_diff because they do not appear "
            "as Chat native-edit candidates."
        )
    if delivery_source == "host_native_edit":
        return "Review host-native generated candidate file changes."
    return "No generated file changes."


def _terminal_system_materialized_files(
        receipt,
        materialization,
        materialization_commit,
    ):
    if (materialization_commit or {}).get("status") == "materialized":
        return list(materialization_commit.get("system_owned_files") or [])
    return list(
        (materialization or {}).get("system_owned_files")
        or (receipt or {}).get("system_owned_changes")
        or []
    )


def _delivery_write_channel_from_source(delivery_source):
    if delivery_source == "system_materialized":
        return "system_materializer"
    if delivery_source in {"host_native_edit", "none"}:
        return delivery_source
    return None


def _terminal_health_issues(existing, service_level, report):
    issues = [copy.deepcopy(item) for item in existing or []]
    codes = {
        item.get("code")
        for item in issues
        if isinstance(item, dict)
    }
    if (service_level or {}).get("status") == "exceeded" and (
            "service_level_exceeded" not in codes
    ):
        issues.append({
            "code": "service_level_exceeded",
            "severity": "warning",
            "message": "生成总耗时超过当前Profile的静态服务目标",
        })
    ledger = (report or {}).get("implementation_validation_ledger") or {}
    try:
        attempt_count = int(ledger.get("attempt_count") or 0)
    except (TypeError, ValueError):
        attempt_count = 0
    if (
            attempt_count > 1
            and ledger.get("latest_status") == "valid"
            and _terminal_validation_retries_need_attention(report)
            and "implementation_validation_retried" not in codes
    ):
        issues.append({
            "code": "implementation_validation_retried",
            "severity": "warning",
            "message": (
                "实现验证在通过前存在失败尝试；请检查validation ledger确认"
                "是否有AI编辑漂移或候选内容被改写"
            ),
            "attempt_count": attempt_count,
        })
    return issues


def _terminal_validation_retries_need_attention(report):
    ledger_pointer = (report or {}).get("implementation_validation_ledger") or {}
    if ledger_pointer.get("retry_attention_required") is not None:
        return bool(ledger_pointer.get("retry_attention_required"))
    path = Path(str(ledger_pointer.get("path") or ""))
    if not path.is_absolute():
        report_path = Path(str((report or {}).get("report_path") or ""))
        if report_path.is_file():
            path = report_path.parent / path
    try:
        ledger = _read_json(path)
    except (OSError, ValueError, TypeError):
        return True
    for attempt in list(ledger.get("attempts") or [])[:-1]:
        if attempt.get("status") == "valid":
            continue
        if not _expected_pre_apply_validation_attempt(attempt):
            return True
    return False


def _expected_pre_apply_validation_attempt(attempt):
    issues = attempt.get("issues") or []
    if not issues:
        return False
    return all(
        _expected_pre_apply_validation_issue(issue)
        for issue in issues
        if isinstance(issue, dict)
    )


def _expected_pre_apply_validation_issue(issue):
    message = str(issue.get("message") or "")
    return any(marker in message for marker in (
        "Implementation candidate file missing",
        "behavior_file 不存在",
        "Page Object 不存在",
        "root locator 文件不存在",
        "缺少 owned Page/View 的有序计划调用",
        "生成代码缺少计划值",
    ))


def _design_execution(job):
    workload = job.get("workload") or {}
    step_count = int(workload.get("step_count") or 0)
    threshold = int(workload.get("large_job_step_threshold") or 100)
    return {
        "mode": generation_job_design_mode(job),
        "step_count": step_count,
        "fragmentation_threshold": threshold,
    }


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
    value = {
        **result,
        "job_transition": projected["job_transition"],
        "job_lifecycle_timing": projected.get("job_lifecycle_timing"),
        "last_job_result": projected.get("last_job_result"),
    }
    for key in (
            "service_level",
            "health_issues",
            "implementation_diff",
            "implementation_diff_summary",
    ):
        if key in projected:
            value[key] = copy.deepcopy(projected[key])
    if projected.get("workspace_projection_summary"):
        value["workspace_projection_summary"] = copy.deepcopy(
            projected["workspace_projection_summary"]
        )
    if projected.get("status") in {"completed", "completed_no_changes", "failed"}:
        for key in (
                "category",
                "stages",
                "failure_summary",
                "changed_files",
                "execution_outcome",
                "generation_timing_ledger",
                "delivery_visibility",
                "delivery_summary",
                "acceptance_summary",
        ):
            if key in projected:
                value[key] = copy.deepcopy(projected[key])
    return value


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