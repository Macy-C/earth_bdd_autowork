from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path

from autowork_core.utils.debug_tools.recorder.generation_job import (
    generation_job_lease_is_valid,
    load_generation_job,
)
from autowork_core.utils.debug_tools.recorder.generation_diff import (
    generation_diff_pointer_is_valid,
    generation_diff_summary_is_valid,
)
from autowork_core.utils.debug_tools.recorder.generation_workspace_projection import (
    generation_workspace_projection_summary,
    generation_workspace_projection_summary_is_valid,
)
from autowork_core.utils.debug_tools.recorder.models import SCHEMA_VERSION
from autowork_core.utils.debug_tools.recorder.transaction_integrity import (
    transaction_result_fingerprint,
)
from autowork_core.utils.debug_tools.recorder.workflow_state import (
    generation_job_lifecycle_timing_is_valid,
    load_workflow_state,
    project_generation_job_lifecycle_timing,
    transition_generation_job,
    unavailable_generation_job_lifecycle_timing,
)
from autowork_core.utils.debug_tools.recorder.writer import write_json_atomic


GENERATION_JOB_RESULT_VERSION = "1.9"
SUPPORTED_GENERATION_JOB_RESULT_VERSIONS = {
    "1.6",
    "1.7",
    "1.8",
    GENERATION_JOB_RESULT_VERSION,
}
AGENT_WAIT_LEDGER_VERSION = "1.0"
JOB_STAGE_NAMES = (
    "semantic_selection",
    "design",
    "implementation",
    "transaction",
    "runtime",
    "oracle",
)


def delivery_source_from_implementation_receipt(receipt):
    channel = str((receipt or {}).get("delivery_write_channel") or "")
    if channel == "host_native_edit":
        return "host_native_edit"
    if channel == "system_materializer":
        return "system_materialized"
    if channel == "none":
        return "none"
    raise ValueError("Implementation receipt delivery_write_channel无效")


def delivery_review_channel_from_source(delivery_source):
    if delivery_source == "host_native_edit":
        return "host_native_edit"
    if delivery_source == "system_materialized":
        return "implementation_diff"
    if delivery_source == "none":
        return "none"
    raise ValueError("Generation delivery source无效")


def publish_static_job_outcome(
        session_dir,
        request_id,
        report_path,
        report,
    ):
    session_dir = Path(session_dir).resolve()
    state, job, lease = _bound_job(
        session_dir,
        request_id,
        report,
        expected_phases={"implementation"},
    )
    execution = state.get("job_execution") or {}
    transaction_pointer = _transaction_owner_pointer(
        session_dir,
        report_path,
        report,
    )
    static_passed = report.get("status") in {
        "completed",
        "completed_no_changes",
    }
    transition_fields = {
        "session_dir": session_dir,
        "request_id": request_id,
        "job_id": lease["job_id"],
        "job_fingerprint": lease["job_fingerprint"],
        "claim_id": report.get("generation_job_claim_id"),
        "expected_epoch": execution["epoch"],
        "expected_phase": "implementation",
        "transaction": transaction_pointer,
        "clear_active_transaction": True,
    }
    runtime_status = (
        (report.get("execution_outcome") or {}).get("runtime_status")
    )
    unresolved_issues = list(report.get("unresolved_issues") or ())
    if static_passed and runtime_status == "runtime_pending":
        return transition_generation_job(
            **transition_fields,
            phase="runtime",
            next_action="run_bound_generation_profile",
        )

    status = "completed" if static_passed else "failed"
    category = (
        "generated_with_issues"
        if static_passed and unresolved_issues
        else "static_validated"
        if static_passed
        else _static_failure_category(report)
    )
    next_action = (
        "review_generation_result"
        if static_passed
        else "review_generation_failure"
    )
    transition_time, lifecycle_timing = _terminal_lifecycle_timing(
        state,
        phase=status,
        next_action=next_action,
    )
    result = build_generation_job_result(
        job,
        status=status,
        category=category,
        next_action=next_action,
        attempts=_job_attempts(
            state,
            transaction_pointer,
        ),
        stages=_static_stages(report, transaction_pointer),
        completed_at=report.get("completed_at"),
        unresolved_issues=unresolved_issues,
        job_lifecycle_timing=lifecycle_timing,
        service_level_timing=execution.get("service_level_timing"),
        service_level_completed_at=report.get("completed_at"),
        implementation_diff=report.get("implementation_diff"),
        transaction_report={
            **report,
            "report_path": transaction_pointer.get("path"),
        },
        agent_wait_ledger=_load_agent_wait_ledger(session_dir, job),
        interaction_timing=execution.get("interaction_timing"),
        **_result_delivery_fields(
            job,
            {
                **report,
                "report_path": transaction_pointer.get("path"),
            },
            status,
            service_level=None,
        ),
    )
    result_path, result = persist_generation_job_result(
        session_dir,
        result,
    )
    pointer = generation_job_result_pointer(
        session_dir,
        result,
        result_path,
    )
    return transition_generation_job(
        **transition_fields,
        phase=status,
        next_action=next_action,
        result=pointer,
        transitioned_at=transition_time,
        job_lifecycle_timing=lifecycle_timing,
    )


def advance_job_to_oracle(
        session_dir,
        request_id,
        report_path,
        report,
        *,
        expected_epoch,
        claim_id,
    ):
    session_dir = Path(session_dir).resolve()
    state, _job, lease = _bound_job(
        session_dir,
        request_id,
        report,
        expected_phases={"runtime"},
    )
    execution = state.get("job_execution") or {}
    if any((
        execution.get("epoch") != expected_epoch,
        execution.get("claim_id") != claim_id,
    )):
        raise ValueError("Generation Job runtime CAS context无效")
    return transition_generation_job(
        session_dir,
        request_id,
        job_id=lease["job_id"],
        job_fingerprint=lease["job_fingerprint"],
        claim_id=claim_id,
        expected_epoch=expected_epoch,
        expected_phase="runtime",
        phase="oracle",
        next_action="run_required_runtime_matrix",
    )


def publish_runtime_job_outcome(
        session_dir,
        request_id,
        report_path,
        report,
        *,
        expected_epoch,
        claim_id,
        status,
        category,
        next_action,
        runtime_owner,
        oracle_owner=None,
        completed_at=None,
    ):
    session_dir = Path(session_dir).resolve()
    state, job, lease = _bound_job(
        session_dir,
        request_id,
        report,
        expected_phases={"runtime", "oracle"},
    )
    execution = state.get("job_execution") or {}
    if any((
        execution.get("epoch") != expected_epoch,
        execution.get("claim_id") != claim_id,
    )):
        raise ValueError("Generation Job runtime CAS context无效")
    transaction_pointer = _transaction_owner_pointer(
        session_dir,
        report_path,
        report,
    )
    stages = _static_stages(report, transaction_pointer)
    stages["runtime"] = {
        "status": "passed"
        if (runtime_owner or {}).get("status") == "passed"
        else "failed",
        "owner": copy.deepcopy(runtime_owner),
    }
    if oracle_owner is None:
        stages["oracle"] = {
            "status": "not_required",
            "owner": None,
        }
    else:
        stages["oracle"] = {
            "status": "passed" if status == "completed" else "failed",
            "owner": copy.deepcopy(oracle_owner),
        }
    transition_time, lifecycle_timing = _terminal_lifecycle_timing(
        state,
        phase=status,
        next_action=next_action,
    )
    result = build_generation_job_result(
        job,
        status=status,
        category=category,
        next_action=next_action,
        attempts=_job_attempts(state, transaction_pointer),
        stages=stages,
        completed_at=completed_at,
        job_lifecycle_timing=lifecycle_timing,
        service_level_timing=execution.get("service_level_timing"),
        service_level_completed_at=report.get("completed_at"),
        implementation_diff=report.get("implementation_diff"),
        implementation_diff_summary=report.get("implementation_diff_summary"),
        transaction_report={
            **report,
            "report_path": transaction_pointer.get("path"),
        },
        agent_wait_ledger=_load_agent_wait_ledger(session_dir, job),
        interaction_timing=execution.get("interaction_timing"),
        **_result_delivery_fields(
            job,
            {
                **report,
                "report_path": transaction_pointer.get("path"),
            },
            status,
            service_level=None,
        ),
    )
    result_path, result = persist_generation_job_result(
        session_dir,
        result,
    )
    pointer = generation_job_result_pointer(
        session_dir,
        result,
        result_path,
    )
    return transition_generation_job(
        session_dir,
        request_id,
        job_id=lease["job_id"],
        job_fingerprint=lease["job_fingerprint"],
        claim_id=claim_id,
        expected_epoch=expected_epoch,
        expected_phase=execution["phase"],
        phase=status,
        next_action=next_action,
        result=pointer,
        clear_active_transaction=True,
        transitioned_at=transition_time,
        job_lifecycle_timing=lifecycle_timing,
    )


def publish_pretransaction_job_failure(
        session_dir,
        request_id,
        *,
        claim_id,
        expected_epoch,
        expected_phase,
        category,
        next_action,
        issue_owner=None,
        design_owner=None,
        implementation_owner=None,
    ):
    session_dir = Path(session_dir).resolve()
    state = load_workflow_state(session_dir, request_id)
    pointer = state.get("current_job") or {}
    job = load_generation_job(session_dir, pointer)
    execution = state.get("job_execution") or {}
    if any((
        job is None,
        state.get("status") not in {"ready", "running"},
        execution.get("phase") != expected_phase,
        execution.get("claim_id") != claim_id,
        execution.get("epoch") != expected_epoch,
    )):
        raise ValueError("Generation Job pretransaction CAS context无效")
    stages = {
        "semantic_selection": {
            "status": "not_evaluated",
            "owner": None,
        },
        "design": {
            "status": (
                "passed"
                if execution.get("plan") or design_owner
                else "failed"
            ),
            "owner": copy.deepcopy(
                execution.get("plan") or design_owner or issue_owner
            ),
        },
        "implementation": {
            "status": "failed" if implementation_owner else "not_evaluated",
            "owner": copy.deepcopy(implementation_owner),
        },
        "transaction": {
            "status": "not_evaluated",
            "owner": copy.deepcopy(issue_owner),
        },
        "runtime": {"status": "not_evaluated", "owner": None},
        "oracle": {"status": "not_evaluated", "owner": None},
    }
    transition_time, lifecycle_timing = _terminal_lifecycle_timing(
        state,
        phase="failed",
        next_action=next_action,
    )
    result = build_generation_job_result(
        job,
        status="failed",
        category=category,
        next_action=next_action,
        attempts=copy.deepcopy(state.get("attempt_history") or []),
        stages=stages,
        completed_at=transition_time,
        job_lifecycle_timing=lifecycle_timing,
        service_level_timing=execution.get("service_level_timing"),
        agent_wait_ledger=_load_agent_wait_ledger(session_dir, job),
        interaction_timing=execution.get("interaction_timing"),
    )
    path, result = persist_generation_job_result(session_dir, result)
    result_pointer = generation_job_result_pointer(
        session_dir,
        result,
        path,
    )
    return transition_generation_job(
        session_dir,
        request_id,
        job_id=job["job_id"],
        job_fingerprint=job["job_fingerprint"],
        claim_id=claim_id,
        expected_epoch=expected_epoch,
        expected_phase=expected_phase,
        phase="failed",
        next_action=next_action,
        result=result_pointer,
        clear_active_transaction=True,
        transitioned_at=transition_time,
        job_lifecycle_timing=lifecycle_timing,
    )


def build_generation_job_result(
        job,
        *,
        status,
        category,
        next_action,
        attempts,
        stages,
        completed_at=None,
        unresolved_issues=(),
        job_lifecycle_timing=None,
        service_level_timing=None,
        service_level_completed_at=None,
        implementation_diff=None,
        implementation_diff_summary=None,
        transaction_report=None,
        delivery_visibility=None,
        delivery_summary=None,
        acceptance_summary=None,
        agent_wait_ledger=None,
        interaction_timing=None,
        workspace_projection_summary=None,
    ):
    if status not in {"completed", "failed"}:
        raise ValueError(f"无效 Generation Job result status: {status}")
    normalized_stages = {
        name: copy.deepcopy((stages or {}).get(name) or {
            "status": "not_evaluated",
            "owner": None,
        })
        for name in JOB_STAGE_NAMES
    }
    if job_lifecycle_timing is None:
        job_lifecycle_timing = unavailable_generation_job_lifecycle_timing()
    if not generation_job_lifecycle_timing_is_valid(job_lifecycle_timing):
        raise ValueError("Generation Job Result lifecycle timing无效")
    result_completed_at = str(
        completed_at or datetime.now().isoformat(timespec="milliseconds")
    )
    service_level = _service_level_result(
        job,
        service_level_timing,
        completed_at=(
            service_level_completed_at or result_completed_at
        ),
    )
    health_issues = _job_result_health_issues(
        service_level,
        normalized_stages,
    )
    timing_ledger = _generation_timing_ledger(
        job,
        job_lifecycle_timing,
        service_level,
        transaction_report=transaction_report,
        agent_wait_ledger=agent_wait_ledger,
        interaction_timing=interaction_timing,
    )
    projection_summary = _workspace_projection_summary(
        job,
        transaction_report,
        workspace_projection_summary,
    )
    value = {
        "schema_version": SCHEMA_VERSION,
        "generation_job_result_version": GENERATION_JOB_RESULT_VERSION,
        "completed_at": result_completed_at,
        "job": {
            "job_id": job.get("job_id"),
            "job_fingerprint": job.get("job_fingerprint"),
            "request_id": (job.get("request") or {}).get("request_id"),
            "profile_id": (job.get("profile_lease") or {}).get(
                "profile_id"
            ),
            "profile_fingerprint": (job.get("profile_lease") or {}).get(
                "profile_fingerprint"
            ),
        },
        "status": status,
        "category": str(category),
        "next_action": str(next_action),
        "attempts": copy.deepcopy(attempts or []),
        "stages": normalized_stages,
        "unresolved_issues": copy.deepcopy(unresolved_issues or []),
        "health_issues": health_issues,
        "job_lifecycle_timing": copy.deepcopy(job_lifecycle_timing),
        "service_level": service_level,
        "generation_timing_ledger": timing_ledger,
        "generation_progress": _generation_progress_projection(
            job,
            status,
            category,
            next_action,
            timing_ledger,
            transaction_report=transaction_report,
        ),
        "implementation_diff": copy.deepcopy(implementation_diff),
        "implementation_diff_summary": copy.deepcopy(
            implementation_diff_summary
            or (transaction_report or {}).get("implementation_diff_summary")
            or {}
        ),
        "git_diff_visibility": copy.deepcopy(
            (transaction_report or {}).get("git_diff_visibility") or {}
        ),
        "delivery_visibility": copy.deepcopy(delivery_visibility or {}),
        "delivery_summary": copy.deepcopy(delivery_summary or {}),
        "acceptance_summary": copy.deepcopy(acceptance_summary or {}),
        **(
            {"workspace_projection_summary": projection_summary}
            if projection_summary
            else {}
        ),
    }
    value["result_fingerprint"] = generation_job_result_fingerprint(value)
    value["result_id"] = f"job-result-{value['result_fingerprint'][:16]}"
    return value


def persist_generation_job_result(session_dir, result):
    session_dir = Path(session_dir).resolve()
    if not generation_job_result_identity_is_valid(result):
        raise ValueError("GenerationJobResultV1 identity无效")
    path = _result_path(
        session_dir,
        (result.get("job") or {}).get("job_id"),
        result.get("result_fingerprint"),
    )
    if path.exists():
        existing = _read_json(path)
        if existing != result:
            if _result_identity_payload(existing) == _result_identity_payload(result):
                return path, existing
            raise ValueError(f"Generation Job result fingerprint冲突: {path}")
        return path, existing
    write_json_atomic(path, result)
    return path, copy.deepcopy(result)


def generation_job_result_pointer(session_dir, result, path):
    session_dir = Path(session_dir).resolve()
    path = Path(path).resolve()
    expected = _result_path(
        session_dir,
        (result.get("job") or {}).get("job_id"),
        result.get("result_fingerprint"),
    )
    if path != expected or not generation_job_result_identity_is_valid(result):
        raise ValueError("Generation Job result path或identity无效")
    return {
        "path": path.relative_to(session_dir).as_posix(),
        "result_id": result.get("result_id"),
        "result_fingerprint": result.get("result_fingerprint"),
        "job_id": (result.get("job") or {}).get("job_id"),
        "status": result.get("status"),
        "category": result.get("category"),
    }


def load_generation_job_result(session_dir, pointer):
    session_dir = Path(session_dir).resolve()
    pointer = dict(pointer or {})
    required = {
        "path",
        "result_id",
        "result_fingerprint",
        "job_id",
        "status",
        "category",
    }
    if set(pointer) != required:
        return None
    path = Path(str(pointer.get("path") or ""))
    path = path.resolve() if path.is_absolute() else (session_dir / path).resolve()
    expected = _result_path(
        session_dir,
        pointer.get("job_id"),
        pointer.get("result_fingerprint"),
    )
    if path != expected or not path.is_file():
        return None
    try:
        result = _read_json(path)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not generation_job_result_identity_is_valid(result):
        return None
    return result if generation_job_result_pointer(
        session_dir,
        result,
        path,
    ) == pointer else None


def generation_job_result_identity_is_valid(value):
    if not isinstance(value, dict):
        return False
    stages = value.get("stages") or {}
    actual = generation_job_result_fingerprint(value)
    return bool(
        value.get("generation_job_result_version")
        in SUPPORTED_GENERATION_JOB_RESULT_VERSIONS
        and value.get("status") in {"completed", "failed"}
        and value.get("category")
        and value.get("next_action")
        and (value.get("job") or {}).get("job_id")
        and set(stages) == set(JOB_STAGE_NAMES)
        and generation_job_lifecycle_timing_is_valid(
            value.get("job_lifecycle_timing")
        )
        and _service_level_is_valid(value.get("service_level"))
        and isinstance(value.get("health_issues", []), list)
        and _implementation_diff_is_valid(
            value.get("implementation_diff"),
            status=value.get("status"),
            stages=stages,
        )
        and _implementation_diff_summary_field_is_valid(value)
        and isinstance(value.get("git_diff_visibility", {}), dict)
        and _delivery_fields_are_valid(value)
        and _workspace_projection_summary_field_is_valid(value)
        and value.get("result_fingerprint") == actual
        and value.get("result_id") == f"job-result-{actual[:16]}"
    )


def _delivery_fields_are_valid(value):
    version = value.get("generation_job_result_version")
    if version in {"1.6", "1.7"}:
        return True
    delivery_visibility = value.get("delivery_visibility")
    delivery_summary = value.get("delivery_summary")
    acceptance_summary = value.get("acceptance_summary")
    return all(isinstance(item, dict) for item in (
        delivery_visibility,
        delivery_summary,
        acceptance_summary,
    ))


def _workspace_projection_summary_field_is_valid(value):
    version = value.get("generation_job_result_version")
    summary = value.get("workspace_projection_summary")
    if version in {"1.6", "1.7"} and summary is None:
        return True
    return summary is None or generation_workspace_projection_summary_is_valid(
        summary,
    )


def _implementation_diff_summary_field_is_valid(value):
    version = value.get("generation_job_result_version")
    summary = value.get("implementation_diff_summary")
    if version in {"1.6", "1.7", "1.8"} and not summary:
        return True
    if not value.get("implementation_diff") and not summary:
        return True
    if value.get("status") != "completed" and not summary:
        return True
    return generation_diff_summary_is_valid(
        summary,
        pointer=value.get("implementation_diff") or {},
    )


def _workspace_projection_summary(job, transaction_report, provided):
    if provided:
        return copy.deepcopy(provided)
    report = transaction_report or {}
    manifest = report.get("implementation_manifest") or {}
    projection = (
        manifest.get("current_content_projection")
        or report.get("generation_workspace_projection")
        or job.get("generation_workspace_projection")
        or {}
    )
    if not projection:
        return {}
    return generation_workspace_projection_summary(
        projection,
        manifest=manifest,
    )


def generation_job_result_fingerprint(value):
    return _fingerprint(_result_identity_payload(value))


def _result_identity_payload(value):
    return {
        key: item
        for key, item in copy.deepcopy(value or {}).items()
        if key not in {
            "result_id",
            "result_fingerprint",
            "result_path",
            "generation_timing_ledger",
            "generation_progress",
        }
    }


def _generation_progress_projection(
        job,
        status,
        category,
        next_action,
        timing_ledger,
        *,
        transaction_report=None,
    ):
    report = transaction_report or {}
    candidate_delivery = (timing_ledger or {}).get("candidate_delivery") or {}
    agent_tool_timing = (timing_ledger or {}).get("agent_tool_timing") or {}
    agent_summary = agent_tool_timing.get("summary") or {}
    by_tool_kind = agent_summary.get("by_tool_kind") or {}
    by_wait_segment = agent_summary.get("by_wait_segment") or {}
    observed_tool_stages = {}
    for tool_kind in (
            "typed_patch_submit",
            "candidate_index_read",
            "source_reads",
            "editor_edits",
            "after_native_edit",
    ):
        bucket = by_tool_kind.get(tool_kind)
        if isinstance(bucket, dict):
            observed_tool_stages[tool_kind] = {
                key: bucket.get(key)
                for key in ("request_count", "agent_wait_ms")
                if bucket.get(key) is not None
            }
    observed_wait_segments = {}
    for segment, bucket in by_wait_segment.items():
        if isinstance(bucket, dict):
            observed_wait_segments[segment] = {
                key: bucket.get(key)
                for key in ("request_count", "agent_wait_ms")
                if bucket.get(key) is not None
            }
    value = {
        "generation_progress_version": "1.0",
        "status": status,
        "category": str(category),
        "job_id": job.get("job_id"),
        "request_id": (job.get("request") or {}).get("request_id"),
        "transaction_id": report.get("transaction_id"),
        "phase": "completed" if status == "completed" else "failed",
        "current_stage": "completed" if status == "completed" else "failed",
        "next_visible_action": str(next_action),
        "candidate_delivery": {
            "status": candidate_delivery.get("status"),
            "expected_edit_count": candidate_delivery.get("expected_edit_count"),
            "changed_file_count": candidate_delivery.get("changed_file_count"),
            "delivery_write_channel": candidate_delivery.get("delivery_write_channel"),
            "host_delivery_observation_status": candidate_delivery.get(
                "host_delivery_observation_status"
            ),
        },
        "agent_wait": {
            "status": agent_tool_timing.get("status"),
            "source": agent_tool_timing.get("source"),
            "event_count": agent_tool_timing.get("event_count"),
            "max_wait_ms": agent_summary.get("max_agent_wait_ms"),
            "max_wait_tool_kind": agent_summary.get(
                "max_agent_wait_tool_kind"
            ),
            "max_wait_segment": agent_summary.get("max_agent_wait_segment"),
            "editor_edits_done_to_after_native_edit_request_ms": (
                agent_summary.get(
                    "editor_edits_done_to_after_native_edit_request_ms"
                )
            ),
            "observed_tool_stages": observed_tool_stages,
            "observed_wait_segments": observed_wait_segments,
        },
    }
    return _drop_empty_progress(value)


def _drop_empty_progress(value):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            cleaned = _drop_empty_progress(item)
            if cleaned not in (None, {}):
                result[key] = cleaned
        return result
    return value


def _result_delivery_fields(job, report, status, *, service_level=None):
    execution = report.get("execution_outcome") or {}
    materialization = report.get("system_materialization") or {}
    materialization_commit = report.get("system_materialization_commit") or {}
    receipt = report.get("implementation_receipt") or {}
    changed_files = list(report.get("changed_files") or [])
    ai_editable = list(receipt.get("ai_editable_changes") or [])
    system_owned = _result_system_materialized_files(
        receipt,
        materialization,
        materialization_commit,
    )
    if receipt.get("delivery_write_channel"):
        delivery_source = delivery_source_from_implementation_receipt(receipt)
    elif status == "failed":
        delivery_source = "none"
    else:
        delivery_source = delivery_source_from_implementation_receipt(receipt)
    delivery_write_channel = receipt.get("delivery_write_channel")
    review_channel = delivery_review_channel_from_source(delivery_source)
    implementation_diff = report.get("implementation_diff") or {}
    runtime_status = execution.get("runtime_status")
    return {
        "delivery_visibility": {
            "source": delivery_source,
            "delivery_write_channel": delivery_write_channel,
            "review_channel": review_channel,
            "host_native_edit_available": review_channel == "host_native_edit",
            "requires_explicit_diff_review": review_channel == "implementation_diff",
            "changed_file_count": len(changed_files),
            "changed_files": changed_files,
            "report_path": report.get("report_path"),
            "implementation_diff_path": implementation_diff.get("path"),
            "system_owned_file_count": len(system_owned),
            "ai_editable_count": len(ai_editable),
        },
        "delivery_summary": {
            "status": status,
            "job_id": job.get("job_id"),
            "static_status": execution.get("static_status"),
            "runtime_status": runtime_status,
            "delivery_source": delivery_source,
            "delivery_write_channel": delivery_write_channel,
            "review_channel": review_channel,
            "changed_file_count": len(changed_files),
            "changed_files": changed_files,
            "report_path": report.get("report_path"),
            "implementation_diff_path": implementation_diff.get("path"),
            "system_materialization_commit_status": (
                materialization_commit.get("status")
            ),
        },
        "acceptance_summary": {
            "static_status": execution.get("static_status"),
            "runtime_status": runtime_status,
            "oracle_status": "not_required",
            "user_review_status": (
                "native_edit_review_required"
                if review_channel == "host_native_edit" and changed_files
                else "diff_review_required"
                if review_channel == "implementation_diff" and changed_files
                else "no_code_changes"
                if not changed_files
                else "review_required"
            ),
            "acceptance_status": (
                "failed"
                if status == "failed"
                else "static_only"
                if runtime_status == "runtime_not_run"
                else "runtime_or_oracle_pending"
                if runtime_status in {"runtime_pending", None}
                else "runtime_evaluated"
            ),
        },
    }


def _result_system_materialized_files(
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


def _service_level_result(job, timing, *, completed_at=None):
    workload = (job or {}).get("workload") or {}
    target_seconds = int(
        workload.get("static_service_level_target_seconds") or 0
    )
    timing = copy.deepcopy(timing or {})
    agent_started_at = (
        timing.get("agent_started_at")
    )
    command_sent_at = timing.get("command_sent_at")
    finished_at = str(completed_at or "") or None
    observed_ms = _duration_ms(agent_started_at, finished_at)
    total_ms = _duration_ms(command_sent_at, finished_at)
    complete = bool(command_sent_at and total_ms is not None)
    status = (
        "within_target"
        if complete and total_ms <= target_seconds * 1000
        else "exceeded"
        if complete or (
            observed_ms is not None
            and observed_ms > target_seconds * 1000
        )
        else "timing_incomplete"
    )
    coverage = (
        "complete"
        if complete
        else "lower_bound"
        if observed_ms is not None
        else "unavailable"
    )
    return {
        "service_level_version": "1.0",
        "scope": "user_command_to_static_terminal",
        "status": status,
        "target_seconds": target_seconds,
        "command_sent_at": command_sent_at,
        "agent_started_at": agent_started_at,
        "static_terminal_at": finished_at,
        "total_duration_ms": total_ms,
        "agent_observed_duration_ms": observed_ms,
        "coverage": coverage,
        "timing_source": timing.get(
            "timing_source",
            "missing_host_command_timestamp",
        ),
    }


def _service_level_is_valid(value):
    if not isinstance(value, dict) or set(value) != {
        "service_level_version",
        "scope",
        "status",
        "target_seconds",
        "command_sent_at",
        "agent_started_at",
        "static_terminal_at",
        "total_duration_ms",
        "agent_observed_duration_ms",
        "coverage",
        "timing_source",
    }:
        return False
    if any((
        value.get("service_level_version") != "1.0",
        value.get("scope") != "user_command_to_static_terminal",
        value.get("status") not in {
            "within_target", "exceeded", "timing_incomplete",
        },
        not isinstance(value.get("target_seconds"), int),
        value.get("target_seconds", 0) <= 0,
        not isinstance(value.get("static_terminal_at"), str),
    )):
        return False
    target_ms = value["target_seconds"] * 1000
    coverage = value.get("coverage")
    total_ms = value.get("total_duration_ms")
    observed_ms = value.get("agent_observed_duration_ms")
    if coverage == "unavailable":
        return bool(
            value.get("status") == "timing_incomplete"
            and value.get("command_sent_at") is None
            and value.get("agent_started_at") is None
            and total_ms is None
            and observed_ms is None
            and value.get("timing_source")
            == "missing_host_command_timestamp"
        )
    expected_observed = _duration_ms(
        value.get("agent_started_at"),
        value.get("static_terminal_at"),
    )
    if observed_ms != expected_observed:
        return False
    if coverage == "lower_bound":
        return bool(
            value.get("command_sent_at") is None
            and total_ms is None
            and value.get("timing_source")
            == "missing_host_command_timestamp"
            and value.get("status") == (
                "exceeded" if observed_ms > target_ms else "timing_incomplete"
            )
        )
    expected_total = _duration_ms(
        value.get("command_sent_at"),
        value.get("static_terminal_at"),
    )
    return bool(
        coverage == "complete"
        and isinstance(value.get("command_sent_at"), str)
        and value.get("timing_source") in {
            "host_command_metadata",
            "workbench_generation_command",
            "host_user_prompt_hook",
        }
        and total_ms == expected_total
        and isinstance(total_ms, int)
        and value.get("status") == (
            "within_target" if total_ms <= target_ms else "exceeded"
        )
    )


def _generation_timing_ledger(
        job,
        job_lifecycle_timing,
        service_level,
        *,
        transaction_report=None,
    agent_wait_ledger=None,
    interaction_timing=None,
    ):
    report = copy.deepcopy(transaction_report or {})
    workflow_segments = _timing_workflow_segments(job_lifecycle_timing)
    interaction_timing = _interaction_timing_projection(
        interaction_timing,
        job_lifecycle_timing=job_lifecycle_timing,
    )
    transaction_stages = _timing_transaction_stages(report)
    workflow_by_stage = {
        item["stage"]: item
        for item in workflow_segments
        if item.get("stage")
    }
    transaction_by_stage = {
        item["stage"]: item
        for item in transaction_stages
        if item.get("stage")
    }
    implementation_segment = workflow_by_stage.get("implementation") or {}
    validation_stage = transaction_by_stage.get("implementation") or {}
    transaction_stage = transaction_by_stage.get("transaction") or {}
    native_edit_wait_ms = _duration_ms(
        implementation_segment.get("started_at"),
        validation_stage.get("started_at"),
    )
    post_validation_terminal_ms = _duration_ms(
        validation_stage.get("finished_at"),
        transaction_stage.get("finished_at"),
    )
    receipt = report.get("implementation_receipt") or {}
    manifest = report.get("implementation_manifest") or {}
    host_delivery = report.get("host_delivery_observation") or {}
    changed_files = list(report.get("changed_files") or [])
    expected_edits = list(receipt.get("ai_editable_changes") or [])
    expected_edits.extend(receipt.get("system_owned_changes") or [])
    if not expected_edits:
        expected_edits = list(manifest.get("allowed_changes") or [])
    agent_tool_timing = _agent_tool_timing_projection(agent_wait_ledger)
    return {
        "generation_timing_ledger_version": "1.0",
        "scope": "user_request_to_terminal_result",
        "coverage": "framework_observed",
        "job_id": job.get("job_id"),
        "request_id": (job.get("request") or {}).get("request_id"),
        "service_level": {
            "command_sent_at": service_level.get("command_sent_at"),
            "agent_started_at": service_level.get("agent_started_at"),
            "static_terminal_at": service_level.get("static_terminal_at"),
            "total_duration_ms": service_level.get("total_duration_ms"),
            "agent_observed_duration_ms": service_level.get(
                "agent_observed_duration_ms"
            ),
            "status": service_level.get("status"),
            "coverage": service_level.get("coverage"),
        },
        "workflow_segments": workflow_segments,
        "transaction_stages": transaction_stages,
        "observed_windows": {
            "framework_wait_for_native_edit": {
                "started_at": implementation_segment.get("started_at"),
                "finished_at": validation_stage.get("started_at"),
                "duration_ms": native_edit_wait_ms,
                "coverage": "implementation_phase_start_to_validation_start",
            },
            "post_validation_terminal": {
                "started_at": validation_stage.get("finished_at"),
                "finished_at": transaction_stage.get("finished_at"),
                "duration_ms": post_validation_terminal_ms,
                "coverage": "validation_finished_to_transaction_finished",
            },
            "host_delivery": {
                "status": host_delivery.get("status") or "not_observed",
                "source": host_delivery.get("source"),
                "validation_started_at": host_delivery.get(
                    "validation_started_at"
                ),
                "file_count": host_delivery.get("file_count"),
                "observed_file_count": host_delivery.get(
                    "observed_file_count"
                ),
                "first_target_path": host_delivery.get("first_target_path"),
                "first_target_mtime_at": host_delivery.get(
                    "first_target_mtime_at"
                ),
                "last_target_path": host_delivery.get("last_target_path"),
                "last_target_mtime_at": host_delivery.get(
                    "last_target_mtime_at"
                ),
                "target_write_spread_ms": host_delivery.get(
                    "target_write_spread_ms"
                ),
                "last_write_to_validation_start_ms": host_delivery.get(
                    "last_write_to_validation_start_ms"
                ),
            },
        },
        "candidate_delivery": {
            "status": "observed" if report else "not_observed",
            "report_path": report.get("report_path"),
            "transaction_id": report.get("transaction_id"),
            "expected_edit_count": len(expected_edits),
            "changed_file_count": len(changed_files),
            "workspace_candidate_matches": (
                (report.get("workspace_candidate_audit") or {}).get(
                    "matches"
                )
            ),
            "delivery_write_channel": receipt.get("delivery_write_channel"),
            "host_delivery_observation_status": host_delivery.get("status"),
        },
        "interaction_timing": interaction_timing,
        "agent_tool_timing": agent_tool_timing,
        "summary": {
            "total_duration_ms": service_level.get("total_duration_ms"),
            "agent_observed_duration_ms": service_level.get(
                "agent_observed_duration_ms"
            ),
            "workflow_segment_durations_ms": {
                item["stage"]: item.get("duration_ms")
                for item in workflow_segments
            },
            "transaction_stage_durations_ms": {
                item["stage"]: item.get("duration_ms")
                for item in transaction_stages
            },
            "framework_wait_for_native_edit_ms": native_edit_wait_ms,
            "post_validation_terminal_ms": post_validation_terminal_ms,
            "target_write_spread_ms": host_delivery.get(
                "target_write_spread_ms"
            ),
            "last_write_to_validation_start_ms": host_delivery.get(
                "last_write_to_validation_start_ms"
            ),
            "interaction_timing_status": interaction_timing.get("status"),
            **(
                {
                    "business_answers_wait_ms": (
                        interaction_timing.get("summary") or {}
                    ).get("business_answers_wait_ms"),
                    "typed_patch_wait_ms": (
                        interaction_timing.get("summary") or {}
                    ).get("typed_patch_wait_ms"),
                    "design_unattributed_ms": (
                        interaction_timing.get("summary") or {}
                    ).get("design_unattributed_ms"),
                }
                if interaction_timing.get("status") == "observed"
                else {}
            ),
            "agent_tool_timing_status": agent_tool_timing.get("status"),
            **(
                {
                    "agent_tool_max_wait_ms": (
                        agent_tool_timing.get("summary") or {}
                    ).get("max_agent_wait_ms"),
                    "agent_tool_max_wait_kind": (
                        agent_tool_timing.get("summary") or {}
                    ).get("max_agent_wait_tool_kind"),
                    "agent_tool_max_wait_segment": (
                        agent_tool_timing.get("summary") or {}
                    ).get("max_agent_wait_segment"),
                    "editor_edits_done_to_after_native_edit_request_ms": (
                        agent_tool_timing.get("summary") or {}
                    ).get(
                        "editor_edits_done_to_after_native_edit_request_ms"
                    ),
                }
                if agent_tool_timing.get("status") == "observed"
                else {}
            ),
        },
    }


def _interaction_timing_projection(interaction_timing, *, job_lifecycle_timing):
    timing = job_lifecycle_timing if isinstance(job_lifecycle_timing, dict) else {}
    raw = interaction_timing if isinstance(interaction_timing, dict) else {}
    raw_events = raw.get("events") if isinstance(raw, dict) else []
    events = [
        {
            key: event.get(key)
            for key in (
                "event",
                "at",
                "phase",
                "status",
                "next_action",
                "epoch",
                "details",
            )
            if event.get(key) is not None
        }
        for event in raw_events or []
        if isinstance(event, dict)
        and event.get("event")
        and event.get("at")
    ]
    if not events:
        return {
            "status": "not_observed_by_workflow",
            "source": "workflow_state",
            "unobserved_actions": [
                "business_answers_required",
                "business_answers_submitted",
                "typed_patch_required",
                "typed_patch_submitted",
            ],
        }
    design_ms = None
    for segment in timing.get("segments") or []:
        if isinstance(segment, dict) and segment.get("stage") == "design":
            design_ms = segment.get("duration_ms")
            break
    waits = _interaction_waits(events)
    attributed = sum(value for value in waits.values() if isinstance(value, int))
    summary = {
        **waits,
        "observed_event_count": len(events),
    }
    if isinstance(design_ms, int) and not isinstance(design_ms, bool):
        summary["design_duration_ms"] = design_ms
        summary["design_unattributed_ms"] = max(0, design_ms - attributed)
    return {
        "status": "observed",
        "source": "workflow_state",
        "event_count": len(events),
        "events": events,
        "summary": summary,
    }


def _interaction_waits(events):
    return {
        "business_answers_wait_ms": _event_pair_wait_ms(
            events,
            "business_answers_required",
            {
                "business_answers_submitted",
                "business_facts_submitted",
            },
        ),
        "typed_patch_wait_ms": _event_pair_wait_ms(
            events,
            "typed_patch_required",
            {"typed_patch_submitted"},
        ),
    }


def _event_pair_wait_ms(events, start_event, end_events):
    total = 0
    started_at = None
    for event in events:
        name = str(event.get("event") or "")
        if name == start_event:
            started_at = event.get("at")
            continue
        if started_at and name in end_events:
            duration = _duration_ms(started_at, event.get("at"))
            if duration is not None:
                total += duration
            started_at = None
    return total


def _agent_tool_timing_projection(agent_wait_ledger):
    ledger = copy.deepcopy(agent_wait_ledger or {})
    if not ledger:
        return {
            "status": "not_observed_by_framework",
            "unobserved_actions": [
                "candidate_manifest_read",
                "candidate_source_reads",
                "editor_edits",
                "assistant_thinking_or_host_scheduling",
            ],
        }
    events = [
        {
            key: event.get(key)
            for key in (
                "at",
                "hook_event_name",
                "phase",
                "next_action",
                "tool_name",
                "tool_kind",
                "outcome",
                "agent_wait_ms",
                "agent_wait_segment",
                "tool_execution_ms",
            )
            if event.get(key) is not None
        }
        for event in ledger.get("events") or []
        if isinstance(event, dict)
    ]
    return {
        "status": "observed",
        "source": "copilot_hook_router",
        "ledger_version": ledger.get("agent_wait_ledger_version"),
        "event_count": len(events),
        "summary": copy.deepcopy(ledger.get("summary") or {}),
        "events": events,
    }


def _timing_workflow_segments(job_lifecycle_timing):
    return [
        _copy_timing_fields(
            item,
            ("stage", "source", "started_at", "finished_at", "duration_ms"),
            stage_key="stage",
        )
        for item in (job_lifecycle_timing or {}).get("segments") or []
        if isinstance(item, dict)
    ]


def _timing_transaction_stages(report):
    stages = ((report.get("stage_timing_ledger") or {}).get("stages") or {})
    return [
        _copy_timing_fields(
            {**dict(value or {}), "stage": name},
            ("stage", "source", "started_at", "finished_at", "duration_ms"),
            stage_key="stage",
        )
        for name, value in stages.items()
        if isinstance(value, dict)
    ]


def _copy_timing_fields(value, fields, *, stage_key):
    result = {}
    for field in fields:
        item = value.get(field)
        if item is not None:
            result[field] = item
    if stage_key not in result:
        result[stage_key] = str(value.get(stage_key) or "")
    return result


def _generation_timing_ledger_is_valid(
        value,
        *,
        service_level,
        job_lifecycle_timing,
    ):
    if value is None:
        return True
    if not isinstance(value, dict):
        return False
    summary = value.get("summary") or {}
    embedded_service = value.get("service_level") or {}
    if any((
        value.get("generation_timing_ledger_version") != "1.0",
        value.get("scope") != "user_request_to_terminal_result",
        value.get("coverage") != "framework_observed",
        embedded_service.get("total_duration_ms")
        != (service_level or {}).get("total_duration_ms"),
        summary.get("total_duration_ms")
        != (service_level or {}).get("total_duration_ms"),
        not isinstance(value.get("workflow_segments"), list),
        not isinstance(value.get("transaction_stages"), list),
        (value.get("agent_tool_timing") or {}).get("status")
        not in {"not_observed_by_framework", "observed"},
    )):
        return False
    expected_segments = len((job_lifecycle_timing or {}).get("segments") or [])
    if len(value.get("workflow_segments") or []) != expected_segments:
        return False
    return all(
        _timing_entry_is_valid(item)
        for item in [
            *(value.get("workflow_segments") or []),
            *(value.get("transaction_stages") or []),
        ]
    )


def _timing_entry_is_valid(value):
    if not isinstance(value, dict) or not value.get("stage"):
        return False
    duration = value.get("duration_ms")
    return duration is None or (
        isinstance(duration, int)
        and not isinstance(duration, bool)
        and duration >= 0
    )


def _implementation_diff_is_valid(value, *, status, stages):
    transaction = (stages.get("transaction") or {})
    owner = transaction.get("owner") or {}
    successful_transaction = bool(
        transaction.get("status") == "passed"
        and owner.get("owner") == "generation_transaction"
    )
    if not successful_transaction:
        return value is None
    return generation_diff_pointer_is_valid(
        value,
        transaction_id=owner.get("transaction_id"),
    )


def _duration_ms(started_at, finished_at):
    if not started_at or not finished_at:
        return None
    try:
        started = datetime.fromisoformat(str(started_at))
        finished = datetime.fromisoformat(str(finished_at))
        if started.tzinfo is None:
            started = started.astimezone()
        if finished.tzinfo is None:
            finished = finished.astimezone()
    except (TypeError, ValueError):
        return None
    return max(0, int((finished - started).total_seconds() * 1000))


def _load_agent_wait_ledger(session_dir, job):
    try:
        project_root = _project_root_for_agent_wait_ledger(session_dir)
        job_id = _safe_agent_wait_job_id(job.get("job_id"))
        if not job_id:
            return None
        path = (
            project_root
            / ".copilot"
            / "recorder-routing"
            / f"agent-wait-{job_id}.json"
        )
        value = _read_json(path)
        if any((
                value.get("agent_wait_ledger_version")
                != AGENT_WAIT_LEDGER_VERSION,
                value.get("job_id") != job.get("job_id"),
                value.get("job_fingerprint") != job.get("job_fingerprint"),
                not isinstance(value.get("events"), list),
                not isinstance(value.get("summary"), dict),
        )):
            return None
        return value
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _project_root_for_agent_wait_ledger(session_dir):
    path = Path(session_dir).resolve()
    for candidate in (path, *path.parents):
        if candidate.name.casefold() == "recording_sessions":
            return candidate.parent.parent.resolve()
    raise ValueError("Cannot locate project root from generation session")


def _safe_agent_wait_job_id(value):
    text = str(value or "")
    return "".join(
        char if char.isalnum() or char in {"_", ".", "-"} else "_"
        for char in text
    )


def _bound_job(session_dir, request_id, report, *, expected_phases):
    lease = report.get("generation_job_lease") or {}
    if not generation_job_lease_is_valid(lease):
        raise ValueError("Generation Transaction Job lease无效")
    state = load_workflow_state(session_dir, request_id)
    pointer = state.get("current_job") or {}
    job = load_generation_job(session_dir, pointer)
    execution = state.get("job_execution") or {}
    if any((
        job is None,
        lease.get("job_id") != pointer.get("job_id"),
        lease.get("job_fingerprint") != pointer.get("job_fingerprint"),
        lease.get("job_nonce") != pointer.get("nonce"),
        report.get("generation_job_claim_id") != execution.get("claim_id"),
        execution.get("phase") not in expected_phases,
    )):
        raise ValueError("Generation Transaction与current Job不一致")
    return state, job, lease


def _transaction_owner_pointer(session_dir, report_path, report):
    report_path = Path(report_path).resolve()
    if transaction_result_fingerprint(report) != report.get(
            "result_fingerprint"
    ):
        raise ValueError("Generation Transaction result fingerprint无效")
    return {
        "owner": "generation_transaction",
        "transaction_id": report.get("transaction_id"),
        "path": report_path.relative_to(Path(session_dir).resolve()).as_posix(),
        "status": report.get("status"),
        "result_fingerprint": report.get("result_fingerprint"),
        "completion_fingerprint": report.get("completion_fingerprint"),
    }


def _static_stages(report, owner):
    passed = report.get("status") in {"completed", "completed_no_changes"}
    has_issues = bool(report.get("unresolved_issues"))
    validation = report.get("implementation_validation_ledger") or {}
    implementation_status = (
        "passed" if validation.get("latest_status") == "valid" else "failed"
    )
    plan = report.get("generation_plan") or {}
    return {
        "semantic_selection": {
            "status": "passed",
            "owner": {
                "type": "generation_design",
                "plan_id": plan.get("plan_id"),
                "intent_fingerprint": plan.get("intent_fingerprint"),
                "independent_business_verification": False,
            },
        },
        "design": {
            "status": "passed",
            "owner": {
                "type": "generation_plan",
                "plan_id": plan.get("plan_id"),
                "plan_fingerprint": plan.get("plan_fingerprint"),
            },
        },
        "implementation": {
            "status": implementation_status,
            "owner": {
                "type": "implementation_validation_ledger",
                "fingerprint": validation.get("fingerprint"),
                "attempt_count": validation.get("attempt_count"),
                "latest_status": validation.get("latest_status"),
                "retry_attention_required": validation.get(
                    "retry_attention_required"
                ),
            },
        },
        "transaction": {
            "status": "passed" if passed else "failed",
            "owner": owner,
        },
        "runtime": {
            "status": (
                "blocked"
                if passed and has_issues
                else "not_required"
                if passed
                else "not_evaluated"
            ),
            "owner": None,
        },
        "oracle": {
            "status": (
                "blocked"
                if passed and has_issues
                else "not_required"
                if passed
                else "not_evaluated"
            ),
            "owner": None,
        },
    }


def _static_failure_category(report):
    return {
        "change_set_mismatch": "scope_violation",
        "scope_violation": "scope_violation",
        "stale_during_generation": "stale_during_generation",
        "policy_violation": "policy_violation",
        "failed_validation": "implementation_validation_failed",
        "failed_plan_conformance": "plan_conformance_failed",
        "failed_evidence_audit": "evidence_audit_failed",
        "aborted": "aborted",
    }.get(str(report.get("status") or ""), "transaction_failed")


def _job_result_health_issues(service_level, stages):
    issues = []
    if (service_level or {}).get("status") == "exceeded":
        issues.append({
            "code": "service_level_exceeded",
            "severity": "warning",
            "message": "生成总耗时超过当前Profile的静态服务目标",
        })
    implementation = stages.get("implementation") or {}
    implementation_owner = (
        (stages.get("implementation") or {}).get("owner") or {}
    )
    try:
        attempt_count = int(implementation_owner.get("attempt_count") or 0)
    except (TypeError, ValueError):
        attempt_count = 0
    if (
            attempt_count > 1
            and implementation_owner.get("latest_status") == "valid"
            and _validation_retries_need_attention(implementation)
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


def _validation_retries_need_attention(implementation_stage):
    owner = (implementation_stage or {}).get("owner") or {}
    if owner.get("retry_attention_required") is not None:
        return bool(owner.get("retry_attention_required"))
    attempts = owner.get("attempts") or []
    if not attempts:
        return True
    for attempt in attempts[:-1]:
        if attempt.get("status") == "valid":
            continue
        if not _expected_pre_apply_attempt(attempt):
            return True
    return False


def _expected_pre_apply_attempt(attempt):
    issues = attempt.get("issues") or []
    if not issues:
        return False
    return all(
        _expected_pre_apply_issue(issue)
        for issue in issues
        if isinstance(issue, dict)
    )


def _expected_pre_apply_issue(issue):
    message = str(issue.get("message") or "")
    return any(marker in message for marker in (
        "Implementation candidate file missing",
        "behavior_file 不存在",
        "Page Object 不存在",
        "root locator 文件不存在",
        "缺少 owned Page/View 的有序计划调用",
        "生成代码缺少计划值",
    ))


def _job_attempts(state, transaction_pointer):
    execution = state.get("job_execution") or {}
    current = {
        "attempt_no": int(execution.get("attempt_no") or 0),
        "plan": copy.deepcopy(execution.get("plan") or {}),
        "transaction": copy.deepcopy(transaction_pointer),
    }
    history = copy.deepcopy(state.get("attempt_history") or [])
    if not any(
        (item.get("transaction") or {}).get("transaction_id")
        == transaction_pointer.get("transaction_id")
        for item in history
        if isinstance(item, dict)
    ):
        history.append(current)
    return history


def _terminal_lifecycle_timing(state, *, phase, next_action):
    transitioned_at = datetime.now().isoformat(timespec="milliseconds")
    return transitioned_at, project_generation_job_lifecycle_timing(
        (state or {}).get("job_execution") or {},
        previous_next_action=(state or {}).get("next_action"),
        phase=phase,
        next_action=next_action,
        transitioned_at=transitioned_at,
    )


def _result_path(session_dir, job_id, fingerprint):
    job_id = str(job_id or "")
    fingerprint = str(fingerprint or "")
    if not job_id.startswith("job-") or any(
            character in job_id for character in "/\\"
    ):
        raise ValueError("Generation Job result job_id无效")
    if len(fingerprint) != 64 or any(
            character not in "0123456789abcdef"
            for character in fingerprint
    ):
        raise ValueError("Generation Job result fingerprint无效")
    return (
        Path(session_dir).resolve()
        / "ai"
        / "generation-job-results"
        / job_id
        / f"result-{fingerprint}.json"
    ).resolve()


def _read_json(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON必须是object: {path}")
    return value


def _fingerprint(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
