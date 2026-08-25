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
from autowork_core.utils.debug_tools.recorder.models import SCHEMA_VERSION
from autowork_core.utils.debug_tools.recorder.transaction_integrity import (
    transaction_result_fingerprint,
)
from autowork_core.utils.debug_tools.recorder.workflow_state import (
    load_workflow_state,
    transition_generation_job,
)
from autowork_core.utils.debug_tools.recorder.writer import write_json_atomic


GENERATION_JOB_RESULT_VERSION = "1.0"
JOB_STAGE_NAMES = (
    "semantic_selection",
    "design",
    "implementation",
    "transaction",
    "runtime",
    "oracle",
)


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
    if static_passed and runtime_status == "runtime_pending":
        return transition_generation_job(
            **transition_fields,
            phase="runtime",
            next_action="run_bound_generation_profile",
        )

    status = "completed" if static_passed else "failed"
    category = (
        "static_validated"
        if static_passed
        else _static_failure_category(report)
    )
    next_action = (
        "review_generation_result"
        if static_passed
        else "review_generation_failure"
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
    result = build_generation_job_result(
        job,
        status=status,
        category=category,
        next_action=next_action,
        attempts=_job_attempts(state, transaction_pointer),
        stages=stages,
        completed_at=completed_at,
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
            "status": "passed" if execution.get("plan") else "failed",
            "owner": copy.deepcopy(execution.get("plan") or issue_owner),
        },
        "implementation": {
            "status": "not_evaluated",
            "owner": None,
        },
        "transaction": {
            "status": "not_evaluated",
            "owner": copy.deepcopy(issue_owner),
        },
        "runtime": {"status": "not_evaluated", "owner": None},
        "oracle": {"status": "not_evaluated", "owner": None},
    }
    result = build_generation_job_result(
        job,
        status="failed",
        category=category,
        next_action=next_action,
        attempts=copy.deepcopy(state.get("attempt_history") or []),
        stages=stages,
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
    value = {
        "schema_version": SCHEMA_VERSION,
        "generation_job_result_version": GENERATION_JOB_RESULT_VERSION,
        "completed_at": str(
            completed_at or datetime.now().isoformat(timespec="milliseconds")
        ),
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
        == GENERATION_JOB_RESULT_VERSION
        and value.get("status") in {"completed", "failed"}
        and value.get("category")
        and value.get("next_action")
        and (value.get("job") or {}).get("job_id")
        and set(stages) == set(JOB_STAGE_NAMES)
        and value.get("result_fingerprint") == actual
        and value.get("result_id") == f"job-result-{actual[:16]}"
    )


def generation_job_result_fingerprint(value):
    payload = {
        key: item
        for key, item in copy.deepcopy(value or {}).items()
        if key not in {"result_id", "result_fingerprint", "result_path"}
    }
    return _fingerprint(payload)


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
    validation = report.get("implementation_validation_ledger") or {}
    implementation_status = (
        "passed" if validation.get("latest_status") == "valid" else "failed"
    )
    return {
        "semantic_selection": {"status": "not_evaluated", "owner": None},
        "design": {
            "status": "passed",
            "owner": {
                "type": "generation_plan",
                "plan_id": (report.get("generation_plan") or {}).get(
                    "plan_id"
                ),
                "plan_fingerprint": (
                    report.get("generation_plan") or {}
                ).get("plan_fingerprint"),
            },
        },
        "implementation": {
            "status": implementation_status,
            "owner": {
                "type": "implementation_validation_ledger",
                "fingerprint": validation.get("fingerprint"),
            },
        },
        "transaction": {
            "status": "passed" if passed else "failed",
            "owner": owner,
        },
        "runtime": {
            "status": "not_required" if passed else "not_evaluated",
            "owner": None,
        },
        "oracle": {
            "status": "not_required" if passed else "not_evaluated",
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
