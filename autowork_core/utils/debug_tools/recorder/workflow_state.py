from __future__ import annotations

import json
import secrets
from datetime import datetime
from pathlib import Path

from autowork_core.utils.debug_tools.recorder.writer import write_json_atomic


WORKFLOW_STATE_VERSION = "3.0"
JOB_WORKFLOW_STATE_VERSION = "4.0"
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


def write_workflow_state(session_dir, state):
    _assert_state(state)
    path = _state_path(session_dir, state["request_id"])
    write_json_atomic(path, state)
    return path


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
    if (
        state.get("workflow_state_version") == JOB_WORKFLOW_STATE_VERSION
        and state.get("current_job")
    ):
        phase = (state.get("job_execution") or {}).get("phase")
        if not (status == "stale" and phase == "ready"):
            raise ValueError(
                "活动 Generation Job 必须通过 CAS transition 推进"
            )
        state["job_execution"] = {
            **(state.get("job_execution") or {}),
            "phase": "failed",
            "epoch": int(
                (state.get("job_execution") or {}).get("epoch") or 0
            ) + 1,
        }
    state["status"] = status
    state["next_action"] = next_workflow_action(status)
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
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
    state.update({
        "workflow_state_version": JOB_WORKFLOW_STATE_VERSION,
        "status": "ready",
        "next_action": "start_generation_job",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "current_job": dict(pointer),
        "job_execution": {
            "phase": "ready",
            "epoch": current_epoch + 1,
            "claim_id": None,
            "claimed_at": None,
            "attempt_no": 0,
            "plan": None,
            "transaction": None,
            "last_issue_fingerprint": None,
        },
        "attempt_history": [],
        "last_job_result": None,
        "plan": {},
        "active_transaction": None,
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
    state.update({
        "status": "running",
        "next_action": "submit_generation_design",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "job_execution": {
            **execution,
            "phase": "design",
            "epoch": int(execution["epoch"]) + 1,
            "claim_id": claim_id,
            "claimed_at": datetime.now().isoformat(timespec="seconds"),
        },
    })
    write_workflow_state(session_dir, state)
    return state


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
    state.update({
        "status": phase if terminal else "running",
        "next_action": next_action,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "job_execution": {
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
                transaction
                if transaction is not None
                else execution.get("transaction")
            ),
            "last_issue_fingerprint": (
                issue_fingerprint
                if issue_fingerprint is not None
                else execution.get("last_issue_fingerprint")
            ),
        },
        "attempt_history": history,
    })
    if result is not None:
        state["last_job_result"] = dict(result)
    if plan is not None:
        state["plan"] = dict(plan)
    if transaction is not None:
        state["active_transaction"] = dict(transaction)
    if clear_active_transaction:
        state["active_transaction"] = None
    write_workflow_state(session_dir, state)
    return state


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
        _assert_job_pointer(
            state.get("current_job") or {},
            request_id=state.get("request_id"),
        )
        execution = state.get("job_execution") or {}
        if any((
            execution.get("phase") not in {
                "ready",
                "design",
                "implementation",
                "runtime",
                "oracle",
                "completed",
                "failed",
            },
            not isinstance(execution.get("epoch"), int),
            execution.get("epoch", 0) < 1,
        )):
            raise ValueError("WorkflowStateV4 Job execution无效")


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


