from __future__ import annotations

import json
import hashlib
import secrets
from datetime import datetime
from pathlib import Path

from autowork_core.utils.debug_tools.recorder.writer import write_json_atomic


WORKFLOW_STATE_VERSION = "3.0"
JOB_WORKFLOW_STATE_VERSION = "5.0"
JOB_LIFECYCLE_TIMING_LEDGER_VERSION = "1.0"
JOB_LIFECYCLE_TIMING_STATUSES = {
    "active",
    "completed",
    "failed",
    "partial",
    "unavailable",
}
WORKFLOW_STATUSES = {
    "draft",
    "ready",
    "needs_adjustment",
    "forensic",
    "blocked",
    "stale",
    "running",
    "completed",
    "failed",
}


def load_workflow_state(session_dir, request_id):
    if not request_id:
        return {}
    path = _state_path(session_dir, request_id)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def retired_job_entry(state, *, job_id, job_fingerprint=None):
    for entry in reversed(list((state or {}).get("retired_jobs") or ())):
        pointer = (entry or {}).get("job") or {}
        if pointer.get("job_id") != job_id:
            continue
        if (
            job_fingerprint is not None
            and pointer.get("job_fingerprint") != job_fingerprint
        ):
            continue
        return entry
    return None


def retired_job_entry_for_result(state, result_pointer):
    pointer = result_pointer or {}
    entry = retired_job_entry(
        state,
        job_id=pointer.get("job_id"),
    )
    if entry is None:
        return None
    stored = (entry.get("last_job_result") or {})
    if any((
        stored.get("result_id") != pointer.get("result_id"),
        stored.get("result_fingerprint") != pointer.get("result_fingerprint"),
    )):
        return None
    return entry


def write_workflow_state(session_dir, state):
    _assert_state(state)
    path = _state_path(session_dir, state["request_id"])
    write_json_atomic(path, state)
    return path


def new_generation_job_lifecycle_timing(*, started_at, epoch):
    started_at = str(started_at or "")
    if not started_at:
        raise ValueError("Generation Job lifecycle缺少开始时间")
    event = _job_lifecycle_event(
        "admitted",
        started_at,
        phase="ready",
        next_action="start_generation_job",
        epoch=epoch,
    )
    return {
        "job_lifecycle_timing_ledger_version": (
            JOB_LIFECYCLE_TIMING_LEDGER_VERSION
        ),
        "status": "active",
        "events": [event],
        "segments": [],
        "active_stage": _job_lifecycle_active_stage(
            "ready_wait",
            started_at,
            epoch,
        ),
    }


def unavailable_generation_job_lifecycle_timing():
    return {
        "job_lifecycle_timing_ledger_version": (
            JOB_LIFECYCLE_TIMING_LEDGER_VERSION
        ),
        "status": "unavailable",
        "events": [],
        "segments": [],
        "active_stage": None,
    }


def project_generation_job_lifecycle_timing(
        execution,
        *,
        previous_next_action,
        phase,
        next_action,
        transitioned_at=None,
        event=None,
):
    """Advance diagnostic timing independently from Job CAS semantics."""
    execution = dict(execution or {})
    transitioned_at = str(
        transitioned_at or datetime.now().isoformat(timespec="milliseconds")
    )
    previous_phase = str(execution.get("phase") or "")
    previous_epoch = int(execution.get("epoch") or 0)
    next_epoch = previous_epoch + 1
    timing = _normalized_job_lifecycle_timing(execution)
    active = timing.get("active_stage") or {}
    previous_stage = _job_lifecycle_stage(
        previous_phase,
        previous_next_action,
    )
    if active and active.get("name") != previous_stage:
        timing["status"] = "partial"
        active = {}
        timing["active_stage"] = None
    if active:
        segment = _completed_job_lifecycle_segment(
            active,
            transitioned_at,
            next_epoch,
        )
        if segment is None:
            timing["status"] = "partial"
        else:
            timing.setdefault("segments", []).append(segment)
    lifecycle_event = _job_lifecycle_event(
        event or _default_job_lifecycle_event(phase),
        transitioned_at,
        phase=phase,
        next_action=next_action,
        epoch=next_epoch,
    )
    timing.setdefault("events", []).append(lifecycle_event)
    next_stage = _job_lifecycle_stage(phase, next_action)
    if next_stage is None:
        timing["active_stage"] = None
        if timing.get("status") != "partial":
            timing["status"] = phase if phase in {"completed", "failed"} else "active"
    else:
        timing["active_stage"] = _job_lifecycle_active_stage(
            next_stage,
            transitioned_at,
            next_epoch,
        )
        if timing.get("status") != "partial":
            timing["status"] = "active"
    return timing


def generation_job_lifecycle_timing_is_valid(value):
    if not isinstance(value, dict):
        return False
    if value.get("job_lifecycle_timing_ledger_version") != (
            JOB_LIFECYCLE_TIMING_LEDGER_VERSION
    ):
        return False
    if value.get("status") not in JOB_LIFECYCLE_TIMING_STATUSES:
        return False
    events = value.get("events")
    segments = value.get("segments")
    if not isinstance(events, list) or not isinstance(segments, list):
        return False
    if not all(_job_lifecycle_event_is_valid(item) for item in events):
        return False
    if not all(_job_lifecycle_segment_is_valid(item) for item in segments):
        return False
    active = value.get("active_stage")
    if active is not None and not _job_lifecycle_active_stage_is_valid(active):
        return False
    return not (
        value.get("status") in {"completed", "failed", "unavailable"}
        and active is not None
    )


def _normalized_job_lifecycle_timing(execution):
    timing = execution.get("job_lifecycle_timing")
    if generation_job_lifecycle_timing_is_valid(timing):
        return json.loads(json.dumps(timing))
    return {
        **unavailable_generation_job_lifecycle_timing(),
        "status": "partial",
    }


def _job_lifecycle_stage(phase, next_action):
    phase = str(phase or "")
    next_action = str(next_action or "")
    if phase == "ready":
        return "ready_wait"
    if phase == "design":
        return "design"
    if phase == "implementation":
        return (
            "prepare"
            if next_action == "prepare_generation_transaction"
            else "implementation"
        )
    if phase in {"runtime", "oracle"}:
        return phase
    return None


def _default_job_lifecycle_event(phase):
    return {
        "design": "claimed",
        "implementation": "implementation_advanced",
        "runtime": "runtime_started",
        "oracle": "oracle_started",
        "completed": "completed",
        "failed": "failed",
    }.get(str(phase or ""), "observed")


def _job_lifecycle_event(event, at, *, phase, next_action, epoch):
    return {
        "event": str(event),
        "at": str(at),
        "phase": str(phase),
        "next_action": str(next_action),
        "epoch": int(epoch),
    }


def _job_lifecycle_active_stage(name, started_at, epoch):
    return {
        "name": str(name),
        "started_at": str(started_at),
        "epoch_start": int(epoch),
    }


def _completed_job_lifecycle_segment(active, finished_at, epoch_end):
    if not _job_lifecycle_active_stage_is_valid(active):
        return None
    duration_ms = _job_lifecycle_duration_ms(
        active.get("started_at"),
        finished_at,
    )
    if duration_ms is None:
        return None
    return {
        "stage": active.get("name"),
        "source": "workflow_state",
        "started_at": active.get("started_at"),
        "finished_at": str(finished_at),
        "duration_ms": duration_ms,
        "epoch_start": active.get("epoch_start"),
        "epoch_end": int(epoch_end),
    }


def _job_lifecycle_duration_ms(started_at, finished_at):
    try:
        started = datetime.fromisoformat(str(started_at))
        finished = datetime.fromisoformat(str(finished_at))
    except (TypeError, ValueError):
        return None
    return max(0, int((finished - started).total_seconds() * 1000))


def _job_lifecycle_event_is_valid(value):
    return bool(
        isinstance(value, dict)
        and isinstance(value.get("event"), str)
        and value.get("event")
        and isinstance(value.get("at"), str)
        and value.get("at")
        and isinstance(value.get("phase"), str)
        and value.get("phase")
        and isinstance(value.get("next_action"), str)
        and value.get("next_action")
        and isinstance(value.get("epoch"), int)
        and not isinstance(value.get("epoch"), bool)
        and value.get("epoch") >= 0
    )


def _job_lifecycle_segment_is_valid(value):
    return bool(
        isinstance(value, dict)
        and isinstance(value.get("stage"), str)
        and value.get("stage")
        and value.get("source") == "workflow_state"
        and isinstance(value.get("started_at"), str)
        and value.get("started_at")
        and isinstance(value.get("finished_at"), str)
        and value.get("finished_at")
        and isinstance(value.get("duration_ms"), int)
        and not isinstance(value.get("duration_ms"), bool)
        and value.get("duration_ms") >= 0
        and isinstance(value.get("epoch_start"), int)
        and isinstance(value.get("epoch_end"), int)
    )


def _job_lifecycle_active_stage_is_valid(value):
    return bool(
        isinstance(value, dict)
        and isinstance(value.get("name"), str)
        and value.get("name")
        and isinstance(value.get("started_at"), str)
        and value.get("started_at")
        and isinstance(value.get("epoch_start"), int)
        and not isinstance(value.get("epoch_start"), bool)
        and value.get("epoch_start") >= 0
    )


def transition_workflow(
        session_dir,
        request_id,
        *,
        status,
        transaction=None,
        clear_active_transaction=False,
        result=None,
):
    if status not in WORKFLOW_STATUSES:
        raise ValueError(f"无效 workflow status: {status}")
    state = load_workflow_state(session_dir, request_id)
    if not state:
        raise FileNotFoundError(f"workflow state 不存在: {request_id}")
    transitioned_at = datetime.now().isoformat(timespec="milliseconds")
    if (
        state.get("workflow_state_version") == JOB_WORKFLOW_STATE_VERSION
        and state.get("current_job")
    ):
        phase = (state.get("job_execution") or {}).get("phase")
        if not (status == "stale" and phase == "ready"):
            raise ValueError(
                "活动 Generation Job 必须通过 CAS transition 推进"
            )
        execution = state.get("job_execution") or {}
        state["job_execution"] = {
            **execution,
            "phase": "failed",
            "epoch": int(execution.get("epoch") or 0) + 1,
            "job_lifecycle_timing": project_generation_job_lifecycle_timing(
                execution,
                previous_next_action=state.get("next_action"),
                phase="failed",
                next_action=next_workflow_action(status),
                transitioned_at=transitioned_at,
                event="workflow_stale",
            ),
        }
    state["status"] = status
    state["next_action"] = next_workflow_action(status)
    state["updated_at"] = transitioned_at
    state["active_transaction"] = transaction
    if result is not None:
        state["last_result"] = result
    write_workflow_state(session_dir, state)
    return state


def publish_generation_job(
        session_dir,
        request_id,
        pointer,
        *,
        expected_epoch=0,
    ):
    state = load_workflow_state(session_dir, request_id)
    if not state:
        raise FileNotFoundError(f"workflow state 不存在: {request_id}")
    current = state.get("current_job") or {}
    execution = state.get("job_execution") or {}
    current_epoch = int(execution.get("epoch") or 0)
    if current or current_epoch != int(expected_epoch):
        raise ValueError(
            "Generation Job publish CAS冲突: "
            f"expected_epoch={expected_epoch}, current_epoch={current_epoch}"
        )
    _assert_job_pointer(pointer, request_id=request_id)
    published_at = datetime.now().isoformat(timespec="milliseconds")
    next_epoch = current_epoch + 1
    state.update({
        "workflow_state_version": JOB_WORKFLOW_STATE_VERSION,
        "status": "ready",
        "next_action": "start_generation_job",
        "updated_at": published_at,
        "current_job": dict(pointer),
        "job_execution": {
            "phase": "ready",
            "epoch": next_epoch,
            "claim_id": None,
            "claimed_at": None,
            "attempt_no": 0,
            "plan": None,
            "transaction": None,
            "last_issue_fingerprint": None,
            "job_lifecycle_timing": new_generation_job_lifecycle_timing(
                started_at=published_at,
                epoch=next_epoch,
            ),
        },
        "attempt_history": [],
        "retired_jobs": list(state.get("retired_jobs") or []),
        "last_job_result": state.get("last_job_result"),
        "plan": {},
        "active_transaction": None,
    })
    write_workflow_state(session_dir, state)
    return state


def replace_generation_job(
        session_dir,
        request_id,
        pointer,
        *,
        expected_job_pointer,
        expected_epoch,
    retire_reason="new_generation_job",
    ):
    state = load_workflow_state(session_dir, request_id)
    execution = state.get("job_execution") or {}
    if any((
        state.get("workflow_state_version") != JOB_WORKFLOW_STATE_VERSION,
        state.get("status") == "running",
        execution.get("phase") in {"design", "implementation", "runtime", "oracle"},
        execution.get("epoch") != expected_epoch,
        state.get("current_job") != expected_job_pointer,
    )):
        raise ValueError("Generation Job replacement CAS冲突")
    _assert_job_pointer(pointer, request_id=request_id)
    retired = list(state.get("retired_jobs") or [])
    retired.append(_retired_job_entry(
        expected_job_pointer,
        status=state.get("status"),
        execution=execution,
        reason=retire_reason or "new_generation_job",
        last_job_result=state.get("last_job_result"),
        errors=state.get("errors"),
    ))
    replaced_at = datetime.now().isoformat(timespec="milliseconds")
    next_epoch = int(execution["epoch"]) + 1
    state.update({
        "status": "ready",
        "next_action": "start_generation_job",
        "updated_at": replaced_at,
        "current_job": dict(pointer),
        "job_execution": {
            "phase": "ready",
            "epoch": next_epoch,
            "claim_id": None,
            "claimed_at": None,
            "attempt_no": 0,
            "plan": None,
            "transaction": None,
            "last_issue_fingerprint": None,
            "job_lifecycle_timing": new_generation_job_lifecycle_timing(
                started_at=replaced_at,
                epoch=next_epoch,
            ),
        },
        "retired_jobs": retired,
        "attempt_history": [],
        "last_job_result": state.get("last_job_result"),
        "last_result": None,
        "plan": {},
        "active_transaction": None,
        "errors": [],
        "warnings": [],
    })
    write_workflow_state(session_dir, state)
    return state


def claim_generation_job(
        session_dir,
        request_id,
        *,
        job_id,
        job_fingerprint,
        expected_epoch,
    ):
    state = load_workflow_state(session_dir, request_id)
    _assert_job_cas(
        state,
        job_id=job_id,
        job_fingerprint=job_fingerprint,
        expected_epoch=expected_epoch,
        claim_id=None,
        expected_phase="ready",
    )
    execution = state["job_execution"]
    claim_id = f"claim-{secrets.token_hex(16)}"
    claimed_at = datetime.now().isoformat(timespec="milliseconds")
    next_epoch = int(execution["epoch"]) + 1
    state.update({
        "status": "running",
        "next_action": "submit_generation_design",
        "updated_at": claimed_at,
        "job_execution": {
            **execution,
            "phase": "design",
            "epoch": next_epoch,
            "claim_id": claim_id,
            "claimed_at": claimed_at,
            "job_lifecycle_timing": project_generation_job_lifecycle_timing(
                execution,
                previous_next_action=state.get("next_action"),
                phase="design",
                next_action="submit_generation_design",
                transitioned_at=claimed_at,
                event="claimed",
            ),
        },
    })
    write_workflow_state(session_dir, state)
    return state


def fail_generation_job_integrity(
        session_dir,
        request_id,
        *,
        expected_epoch,
        error_code,
    ):
    state = load_workflow_state(session_dir, request_id)
    execution = state.get("job_execution") or {}
    if any((
        state.get("workflow_state_version") != JOB_WORKFLOW_STATE_VERSION,
        not state.get("current_job"),
        state.get("status") != "ready",
        execution.get("phase") != "ready",
        execution.get("claim_id") is not None,
        execution.get("epoch") != expected_epoch,
    )):
        raise ValueError("Generation Job integrity CAS冲突")
    error_code = str(error_code or "job_integrity_failed")
    failed_at = datetime.now().isoformat(timespec="milliseconds")
    failed_execution = {
        **execution,
        "phase": "failed",
        "epoch": int(execution["epoch"]) + 1,
        "last_issue_fingerprint": hashlib.sha256(
            error_code.encode("utf-8")
        ).hexdigest(),
        "job_lifecycle_timing": project_generation_job_lifecycle_timing(
            execution,
            previous_next_action=state.get("next_action"),
            phase="failed",
            next_action="review_generation_failure",
            transitioned_at=failed_at,
            event="integrity_failed",
        ),
    }
    return _retire_current_generation_job(
        session_dir,
        state,
        status="failed",
        next_action="review_generation_failure",
        execution=failed_execution,
        reason="integrity_failed",
        errors=[error_code],
        result=None,
    )


def transition_generation_job(
        session_dir,
        request_id,
        *,
        job_id,
        job_fingerprint,
        claim_id,
        expected_epoch,
        expected_phase,
        phase,
        next_action,
        plan=None,
        transaction=None,
        issue_fingerprint=None,
        result=None,
        clear_active_transaction=False,
        transitioned_at=None,
        lifecycle_event=None,
        job_lifecycle_timing=None,
    ):
    allowed_phases = {
        "design",
        "implementation",
        "runtime",
        "oracle",
        "completed",
        "failed",
    }
    if phase not in allowed_phases:
        raise ValueError(f"无效 Generation Job phase: {phase}")
    state = load_workflow_state(session_dir, request_id)
    _assert_job_cas(
        state,
        job_id=job_id,
        job_fingerprint=job_fingerprint,
        expected_epoch=expected_epoch,
        claim_id=claim_id,
        expected_phase=expected_phase,
    )
    execution = state["job_execution"]
    history = list(state.get("attempt_history") or [])
    if transaction is not None and execution.get("transaction"):
        history.append({
            "attempt_no": execution.get("attempt_no"),
            "plan": execution.get("plan"),
            "transaction": execution.get("transaction"),
            "issue_fingerprint": execution.get("last_issue_fingerprint"),
        })
    terminal = phase in {"completed", "failed"}
    transition_time = str(
        transitioned_at or datetime.now().isoformat(timespec="milliseconds")
    )
    if job_lifecycle_timing is None:
        timing = project_generation_job_lifecycle_timing(
            execution,
            previous_next_action=state.get("next_action"),
            phase=phase,
            next_action=next_action,
            transitioned_at=transition_time,
            event=lifecycle_event,
        )
    else:
        if not generation_job_lifecycle_timing_is_valid(
                job_lifecycle_timing
        ):
            raise ValueError("Generation Job lifecycle timing无效")
        timing = json.loads(json.dumps(job_lifecycle_timing))
    next_execution = {
        **execution,
        "phase": phase,
        "epoch": int(execution["epoch"]) + 1,
        "attempt_no": (
            int(execution.get("attempt_no") or 0) + 1
            if plan is not None
            else int(execution.get("attempt_no") or 0)
        ),
        "plan": plan if plan is not None else execution.get("plan"),
        "transaction": (
            transaction if transaction is not None else execution.get("transaction")
        ),
        "last_issue_fingerprint": (
            issue_fingerprint
            if issue_fingerprint is not None
            else execution.get("last_issue_fingerprint")
        ),
        "job_lifecycle_timing": timing,
    }
    if terminal:
        return _retire_current_generation_job(
            session_dir,
            state,
            status=phase,
            next_action=next_action,
            execution=next_execution,
            reason="terminal_result",
            errors=state.get("errors"),
            result=result,
            history=history,
            plan=plan,
        )
    state.update({
        "status": "running",
        "next_action": next_action,
        "updated_at": transition_time,
        "job_execution": next_execution,
        "attempt_history": history,
    })
    if plan is not None:
        state["plan"] = dict(plan)
    if transaction is not None:
        state["active_transaction"] = dict(transaction)
    if clear_active_transaction:
        state["active_transaction"] = None
    write_workflow_state(session_dir, state)
    return state


def _retire_current_generation_job(
        session_dir,
        state,
        *,
        status,
        next_action,
        execution,
        reason,
        errors,
        result,
        history=None,
        plan=None,
    ):
    pointer = dict(state.get("current_job") or {})
    _assert_job_pointer(pointer, request_id=state.get("request_id"))
    retired = list(state.get("retired_jobs") or [])
    result_pointer = dict(result or {})
    previous_result = dict(state.get("last_job_result") or {})
    retired.append(_retired_job_entry(
        pointer,
        status=status,
        execution=execution,
        reason=reason,
        last_job_result=result_pointer,
        errors=errors,
    ))
    state.update({
        "status": status,
        "next_action": next_action,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "current_job": None,
        "job_execution": None,
        "retired_jobs": retired,
        "attempt_history": list(history or state.get("attempt_history") or []),
        "last_job_result": result_pointer or previous_result or None,
        "last_result": _terminal_transaction_result(execution)
        or state.get("last_result"),
        "active_transaction": None,
        "errors": list(errors or []),
    })
    if plan is not None:
        state["plan"] = dict(plan)
    write_workflow_state(session_dir, state)
    return state


def _retired_job_entry(
        pointer,
        *,
        status,
        execution,
        reason,
        last_job_result,
        errors,
    ):
    return {
        "job": dict(pointer or {}),
        "status": str(status or "failed"),
        "phase": (execution or {}).get("phase"),
        "job_execution": dict(execution or {}),
        "reason": str(reason or "terminal_result"),
        "last_job_result": dict(last_job_result or {}),
        "errors": list(errors or []),
        "retired_at": datetime.now().isoformat(timespec="seconds"),
    }


def _terminal_transaction_result(execution):
    owner = (execution or {}).get("transaction") or {}
    if owner.get("owner") != "generation_transaction":
        return None
    return {
        "transaction_id": owner.get("transaction_id"),
        "report_path": owner.get("path"),
        "status": owner.get("status"),
        "completion_fingerprint": owner.get("completion_fingerprint"),
        "result_fingerprint": owner.get("result_fingerprint"),
    }


def workflow_status_for_request(session_dir, request):
    state = load_workflow_state(session_dir, request.get("request_id"))
    if not state:
        return "draft"
    expected = (request.get("revision_snapshot") or {}).get("seal")
    actual = (state.get("revision") or {}).get("seal")
    if not state or not expected or expected != actual:
        return "stale"
    return state.get("status") or "draft"


def next_workflow_action(status):
    return {
        "draft": "inspect",
        "ready": "generate",
        "needs_adjustment": "answer_decision_pack",
        "forensic": "inspect_named_evidence_and_submit_plan",
        "blocked": "repair_or_minimally_rerecord",
        "stale": "materialize_latest_request",
        "running": "finish_generation_transaction",
        "completed": "review_or_regenerate",
        "failed": "repair_generation_output",
    }[status]


def _assert_state(state):
    status = state.get("status")
    if status not in WORKFLOW_STATUSES:
        raise ValueError(f"workflow state 无效: {status}")
    if not state.get("request_id"):
        raise ValueError("workflow state 缺少 request_id")
    if state.get("workflow_state_version") == JOB_WORKFLOW_STATE_VERSION:
        pointer = state.get("current_job")
        execution = state.get("job_execution")
        if pointer is not None:
            _assert_job_pointer(pointer, request_id=state.get("request_id"))
            if any((
                not isinstance(execution, dict),
                execution.get("phase") not in {
                    "ready", "design", "implementation", "runtime", "oracle",
                },
                not isinstance(execution.get("epoch"), int),
                execution.get("epoch", 0) < 1,
            )):
                raise ValueError("WorkflowStateV5 active Job execution无效")
        elif execution is not None:
            raise ValueError("WorkflowStateV5 terminal state不能保留job_execution")
        for entry in state.get("retired_jobs") or []:
            if not isinstance(entry, dict):
                raise ValueError("WorkflowStateV5 retired_jobs无效")
            _assert_job_pointer(entry.get("job") or {}, request_id=state.get("request_id"))


def _assert_job_cas(
        state,
        *,
        job_id,
        job_fingerprint,
        expected_epoch,
        claim_id,
        expected_phase,
    ):
    if state.get("workflow_state_version") != JOB_WORKFLOW_STATE_VERSION:
        raise ValueError("Workflow尚未进入Generation Job协议")
    pointer = state.get("current_job") or {}
    execution = state.get("job_execution") or {}
    mismatches = []
    if pointer.get("job_id") != job_id:
        mismatches.append("job_id")
    if pointer.get("job_fingerprint") != job_fingerprint:
        mismatches.append("job_fingerprint")
    if int(execution.get("epoch") or 0) != int(expected_epoch):
        mismatches.append("epoch")
    if execution.get("phase") != expected_phase:
        mismatches.append("phase")
    if claim_id is None:
        if execution.get("claim_id") is not None:
            mismatches.append("claim_id")
    elif execution.get("claim_id") != claim_id:
        mismatches.append("claim_id")
    if mismatches:
        raise ValueError(
            "Generation Job CAS冲突: " + ", ".join(mismatches)
        )


def _assert_job_pointer(pointer, *, request_id):
    required = {
        "path",
        "job_id",
        "job_fingerprint",
        "nonce",
        "request_id",
        "profile_lease_fingerprint",
        "activation",
    }
    if set(pointer) != required or any((
        pointer.get("request_id") != request_id,
        not pointer.get("job_id"),
        not pointer.get("job_fingerprint"),
        not pointer.get("nonce"),
        pointer.get("activation") != "active",
    )):
        raise ValueError("WorkflowStateV4 Generation Job pointer无效")


def _state_path(session_dir, request_id):
    return Path(session_dir).resolve() / "ai" / "workflow" / f"{request_id}.json"


