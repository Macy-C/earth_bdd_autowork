from __future__ import annotations

import threading
from pathlib import Path

from autowork_core.utils.debug_tools.recorder import request_repository
from autowork_core.utils.debug_tools.recorder.generation_request import (
    build_generation_request,
)
from autowork_core.utils.debug_tools.recorder.generation_contract import (
    ensure_generation_contract,
)
from autowork_core.utils.debug_tools.recorder.decision_pack import (
    ANSWER_VERSION,
    LEGACY_ANSWER_VERSION,
    load_decision_pack,
)
from autowork_core.utils.debug_tools.recorder.generation_job_service import (
    admit_generation_job,
    inspect_generation_job,
    retire_generation_job,
    retry_generation_job,
    start_generation_job,
)
from autowork_core.utils.debug_tools.recorder.project_memory import (
    inspect_request_memory_freshness,
)
from autowork_core.utils.debug_tools.recorder.workflow_service import (
    inspect_workflow,
    submit_decision_answers,
)
from autowork_core.utils.debug_tools.recorder.workflow_state import (
    load_workflow_state,
)


_SESSION_LOCKS = {}
_SESSION_LOCKS_GUARD = threading.Lock()


class GenerationRequestService:
    def __init__(self, session_dir):
        self.session_dir = Path(session_dir).resolve()
        self._lock = _session_lock(self.session_dir)

    def latest(self, step_ids):
        with self._lock:
            step_ids = request_repository.normalize_step_ids(step_ids)
            request = request_repository.find_latest_request(
                self.session_dir,
                step_ids,
            )
            if not request_repository.request_identity_is_valid(
                request,
                step_ids,
            ):
                return None
            state = load_workflow_state(
                self.session_dir,
                request.get("request_id"),
            )
            if state.get("status") == "running":
                return request
            if not request_repository.request_matches_current_evidence(
                self.session_dir,
                request,
                step_ids,
            ):
                return None
            freshness = inspect_request_memory_freshness(
                self.session_dir,
                request,
            )
            if freshness["status"] in {"compatible", "matched"}:
                return request
            return None

    def ensure_latest(self, step_ids, *, repair=False):
        with self._lock:
            ensure_generation_contract(self.session_dir, write=True)
            step_ids = request_repository.normalize_step_ids(step_ids)
            request = self.latest(step_ids)
            if request is not None:
                state = load_workflow_state(
                    self.session_dir,
                    request.get("request_id"),
                )
                if any((
                    state.get("current_job"),
                    state.get("last_job_result"),
                    not repair,
                )):
                    return request
            return build_generation_request(
                self.session_dir,
                steps=list(step_ids),
                write=True,
                repair=repair,
            )

    def generation_job(self, step_ids, *, profile_id="generation_first"):
        request = self.latest(step_ids)
        if request is None:
            request = self.ensure_latest(step_ids, repair=True)
        path = request_repository.resolve_request_path(
            self.session_dir,
            request["request_path"],
        )
        result = admit_generation_job(path, profile_id=profile_id)
        if result.get("status") == "rejected":
            raise ValueError(
                "生成前检查未通过: "
                + ", ".join(result.get("errors") or [])
            )
        return result

    def generation_command(self, step_ids, *, profile_id="generation_first"):
        job = self.generation_job(step_ids, profile_id=profile_id)
        path = _project_relative_job_path(
            self.session_dir,
            job["job_path"],
        )
        return f'/recorder-generate "{path}"'

    def inspect_job(self, job_path):
        with self._lock:
            return inspect_generation_job(self._resolve_job_path(job_path))

    def start_job(self, job_path, *, expected_epoch):
        with self._lock:
            return start_generation_job(
                self._resolve_job_path(job_path),
                expected_epoch=int(expected_epoch),
            )

    def retry_job(self, job_path, *, profile_id=None):
        with self._lock:
            return retry_generation_job(
                self._resolve_job_path(job_path),
                profile_id=profile_id,
            )

    def retire_job(self, job_path, *, reason, expected_epoch, claim_id=None):
        with self._lock:
            return retire_generation_job(
                self._resolve_job_path(job_path),
                reason=str(reason),
                expected_epoch=int(expected_epoch),
                claim_id=claim_id,
            )

    def answer_decision_batch(self, step_ids, selections):
        with self._lock:
            request = self.latest(step_ids)
            if request is None:
                raise ValueError("当前范围没有可回答的Generation Request")
            path = request_repository.resolve_request_path(
                self.session_dir,
                request["request_path"],
            )
            state = inspect_workflow(path, write=True)
            if state.get("status") != "needs_adjustment":
                raise ValueError(
                    "当前生成任务不接受业务回答: "
                    f"status={state.get('status')}"
                )
            decision = state.get("decision") or {}
            pack = load_decision_pack(
                self.session_dir,
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
            answer_version = (
                LEGACY_ANSWER_VERSION
                if pack.get("decision_pack_version") == "5.7"
                else ANSWER_VERSION
            )
            return submit_decision_answers(path, {
                "answer_version": answer_version,
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

    def workflow_state(self, step_ids, *, refresh=False):
        request = self.latest(step_ids)
        if request is None:
            return {}
        if not refresh:
            state = load_workflow_state(
                self.session_dir,
                request.get("request_id"),
            )
            if state:
                return state
        path = request_repository.resolve_request_path(
            self.session_dir,
            request["request_path"],
        )
        return inspect_workflow(path, write=True)

    def _resolve_job_path(self, value):
        path = Path(value)
        path = path.resolve() if path.is_absolute() else (
            self.session_dir / path
        ).resolve()
        try:
            path.relative_to(self.session_dir)
        except ValueError as error:
            raise ValueError("Generation Job路径越出录制Session") from error
        return path


def _session_lock(session_dir):
    key = str(Path(session_dir).resolve()).casefold()
    with _SESSION_LOCKS_GUARD:
        return _SESSION_LOCKS.setdefault(key, threading.RLock())


def _project_relative_job_path(session_dir, job_path):
    session_dir = Path(session_dir).resolve()
    project_root = next((
        candidate.parent.parent
        for candidate in (session_dir, *session_dir.parents)
        if candidate.name.casefold() == "recording_sessions"
        and candidate.parent.name.casefold() == "artifacts"
    ), None)
    if project_root is None:
        raise ValueError("无法确定Generation Job项目边界")
    path = Path(job_path).resolve()
    try:
        return path.relative_to(project_root.resolve()).as_posix()
    except ValueError as error:
        raise ValueError("Generation Job路径越出项目") from error