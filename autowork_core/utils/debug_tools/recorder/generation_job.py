from __future__ import annotations

import copy
import hashlib
import json
import secrets
from datetime import datetime
from pathlib import Path

from autowork_core.utils.debug_tools.recorder.generation_capsule import (
    generation_capsule_pointer_is_valid,
)
from autowork_core.utils.debug_tools.recorder.generation_workspace_projection import (
    generation_workspace_projection_pointer_is_valid,
)
from autowork_core.utils.debug_tools.recorder.models import SCHEMA_VERSION
from autowork_core.utils.debug_tools.recorder.writer import write_json_atomic


GENERATION_JOB_VERSION = "1.8"
GENERATION_JOB_LEASE_VERSION = "1.3"
GENERATION_JOB_ACTIVATIONS = {"shadow", "active"}


def build_generation_job(
        request,
        state,
        admission,
        contract_lease,
        *,
        activation="shadow",
        created_at=None,
        nonce=None,
        action_count=0,
        step_count=0,
        design_mode="direct",
        generation_capsule=None,
        generation_workspace_projection=None,
    ):
    request = copy.deepcopy(request or {})
    state = copy.deepcopy(state or {})
    admission = copy.deepcopy(admission or {})
    contract_lease = copy.deepcopy(contract_lease or {})
    generation_capsule = copy.deepcopy(generation_capsule or {})
    generation_workspace_projection = copy.deepcopy(
        generation_workspace_projection or {}
    )
    if activation not in GENERATION_JOB_ACTIVATIONS:
        raise ValueError(f"无效 Generation Job activation: {activation}")
    if admission.get("status") != "passed":
        raise ValueError("只有 admission passed 才能构造 Generation Job")
    if activation == "active" and any((
        admission.get("enforcement") != "active",
        admission.get("job_creation_allowed") is not True,
    )):
        raise ValueError("Active Generation Job必须来自active admission")
    if admission.get("request_id") != request.get("request_id"):
        raise ValueError("Generation admission 与 Request 不一致")
    profile = copy.deepcopy(admission.get("profile") or {})
    if not profile.get("profile_fingerprint"):
        raise ValueError("Generation admission 缺少 Profile lease")
    if not contract_lease.get("lease_fingerprint"):
        raise ValueError("Generation Job 缺少 Contract lease")
    if not (state.get("brief") or {}).get("brief_fingerprint"):
        raise ValueError("Generation Job 缺少 Brief fingerprint")
    if not generation_capsule_pointer_is_valid(generation_capsule):
        raise ValueError("Generation Job 缺少有效 Capsule pointer")
    if generation_capsule.get("request_id") != request.get("request_id"):
        raise ValueError("Generation Capsule 与 Request 不一致")
    if generation_workspace_projection:
        if not generation_workspace_projection_pointer_is_valid(
                generation_workspace_projection,
        ):
            raise ValueError("Generation Job 缺少有效 Workspace Projection pointer")
        if generation_workspace_projection.get("request_id") != request.get("request_id"):
            raise ValueError("Generation Workspace Projection 与 Request 不一致")
        if generation_workspace_projection.get(
                "generation_input_snapshot_fingerprint"
        ) != generation_capsule.get("generation_input_snapshot_fingerprint"):
            raise ValueError(
                "Generation Workspace Projection 与 Capsule snapshot 不一致"
            )

    created = _parse_job_time(
        created_at or datetime.now().isoformat(timespec="milliseconds")
    )
    orchestration = profile.get("orchestration_policy") or {}
    threshold = int(orchestration.get("large_job_step_threshold") or 100)
    max_steps = int(orchestration.get("max_supported_step_count") or 600)
    action_count = int(action_count or 0)
    step_count = int(step_count or 0)
    if action_count < 0:
        raise ValueError("Generation Job action_count不能为负数")
    if step_count < 0 or step_count > max_steps:
        raise ValueError(
            f"Generation Job只支持0到{max_steps}个BDD Step: {step_count}"
        )
    design_mode = str(design_mode or "")
    if design_mode not in {"direct", "paged"}:
        raise ValueError(f"Generation Job design_mode无效: {design_mode}")
    target_seconds = int(
        orchestration.get(
            "large_static_service_level_target_seconds"
            if step_count >= threshold
            else "ordinary_static_service_level_target_seconds"
        ) or 0
    )
    if target_seconds <= 0:
        raise ValueError("Generation Job缺少有效静态SLA policy")
    allowed_queries = [
        "inspect-job",
        "job-design-context",
        "job-implementation-packet",
        "job-evidence",
        "job-compare-takes",
        "job-action-knowledge",
        "job-implementation-candidate",
        "job-code-diff",
        "design-contract",
    ]
    if design_mode == "paged":
        allowed_queries.append("job-task-bundle")
    value = {
        "schema_version": SCHEMA_VERSION,
        "generation_job_version": GENERATION_JOB_VERSION,
        "activation": activation,
        "created_at": created.isoformat(timespec="milliseconds"),
        "workload": {
            "action_count": action_count,
            "step_count": step_count,
            "large_job_step_threshold": threshold,
            "max_supported_step_count": max_steps,
            "design_mode": design_mode,
            "static_service_level_target_seconds": target_seconds,
        },
        "nonce": str(nonce or secrets.token_hex(16)),
        "request": {
            "request_id": request.get("request_id"),
            "request_fingerprint": request.get("request_fingerprint"),
            "revision_seal": (
                (request.get("revision_snapshot") or {}).get("seal")
            ),
            "path": request.get("request_path"),
        },
        "brief": copy.deepcopy(state.get("brief") or {}),
        "decision": _decision_lease(state.get("decision") or {}),
        "generation_contract_lease": contract_lease,
        "generation_capsule": generation_capsule,
        **(
            {"generation_workspace_projection": generation_workspace_projection}
            if generation_workspace_projection
            else {}
        ),
        "profile_lease": profile,
        "admission_receipt": _admission_receipt(admission),
        "execution_boundary": {
            "allowed_queries": allowed_queries,
            "validation_stages": [
                "semantic_selection",
                "design",
                "implementation",
                "transaction",
                "runtime",
                "oracle",
            ],
            "user_interaction_policy": profile.get(
                "user_interaction_policy"
            ),
            "repair_policy": copy.deepcopy(
                profile.get("repair_policy") or {}
            ),
        },
    }
    value["job_fingerprint"] = generation_job_fingerprint(value)
    value["job_id"] = f"job-{value['job_fingerprint'][:16]}"
    return value


def persist_generation_job(session_dir, job):
    session_dir = Path(session_dir).resolve()
    if not generation_job_identity_is_valid(job):
        raise ValueError("GenerationJobV1 identity无效")
    output = _job_path(
        session_dir,
        job["request"]["request_id"],
        job["job_fingerprint"],
    )
    if output.exists():
        existing = _read_json(output)
        if existing != job:
            raise ValueError(f"Generation Job fingerprint冲突: {output}")
        return output, existing
    write_json_atomic(output, job)
    return output, copy.deepcopy(job)


def generation_job_pointer(session_dir, job, path):
    session_dir = Path(session_dir).resolve()
    path = Path(path).resolve()
    expected = _job_path(
        session_dir,
        (job.get("request") or {}).get("request_id"),
        job.get("job_fingerprint"),
    )
    if path != expected or not generation_job_identity_is_valid(job):
        raise ValueError("Generation Job path或identity无效")
    return {
        "path": path.relative_to(session_dir).as_posix(),
        "job_id": job.get("job_id"),
        "job_fingerprint": job.get("job_fingerprint"),
        "nonce": job.get("nonce"),
        "request_id": (job.get("request") or {}).get("request_id"),
        "profile_lease_fingerprint": (
            (job.get("profile_lease") or {}).get("profile_fingerprint")
        ),
        "activation": job.get("activation"),
    }


def load_generation_job(session_dir, pointer):
    session_dir = Path(session_dir).resolve()
    pointer = dict(pointer or {})
    required = {
        "path",
        "job_id",
        "job_fingerprint",
        "nonce",
        "request_id",
        "profile_lease_fingerprint",
        "activation",
    }
    if set(pointer) != required:
        return None
    expected = _job_path(
        session_dir,
        pointer.get("request_id"),
        pointer.get("job_fingerprint"),
    )
    path = Path(str(pointer.get("path") or ""))
    path = path.resolve() if path.is_absolute() else (session_dir / path).resolve()
    if path != expected or not path.is_file():
        return None
    try:
        job = _read_json(path)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not generation_job_identity_is_valid(job):
        return None
    expected_pointer = generation_job_pointer(session_dir, job, path)
    return job if expected_pointer == pointer else None


def generation_job_identity_is_valid(job):
    if not isinstance(job, dict):
        return False
    request = job.get("request") or {}
    profile = job.get("profile_lease") or {}
    admission = job.get("admission_receipt") or {}
    contract = job.get("generation_contract_lease") or {}
    capsule = job.get("generation_capsule") or {}
    workspace_projection = job.get("generation_workspace_projection") or {}
    if any((
        job.get("generation_job_version") != GENERATION_JOB_VERSION,
        job.get("activation") not in GENERATION_JOB_ACTIVATIONS,
        not isinstance(job.get("created_at"), str),
        not isinstance((job.get("workload") or {}).get("action_count"), int),
        not isinstance((job.get("workload") or {}).get("step_count"), int),
        (job.get("workload") or {}).get("design_mode")
        not in {"direct", "paged"},
        not isinstance(job.get("nonce"), str),
        len(job.get("nonce") or "") < 16,
        not request.get("request_id"),
        not request.get("request_fingerprint"),
        not request.get("revision_seal"),
        not (job.get("brief") or {}).get("brief_fingerprint"),
        admission.get("status") != "passed",
        job.get("activation") == "active" and any((
            admission.get("enforcement") != "active",
            admission.get("job_creation_allowed") is not True,
        )),
        admission.get("request_id") != request.get("request_id"),
        not admission.get("admission_fingerprint"),
        not profile.get("profile_fingerprint"),
        not contract.get("lease_fingerprint"),
        not generation_capsule_pointer_is_valid(capsule),
        capsule.get("request_id") != request.get("request_id"),
        workspace_projection
        and not generation_workspace_projection_pointer_is_valid(
            workspace_projection,
        ),
        workspace_projection
        and workspace_projection.get("request_id") != request.get("request_id"),
        workspace_projection
        and workspace_projection.get("generation_input_snapshot_fingerprint")
        != capsule.get("generation_input_snapshot_fingerprint"),
    )):
        return False
    workload = job.get("workload") or {}
    orchestration = profile.get("orchestration_policy") or {}
    try:
        action_count = int(workload.get("action_count"))
        step_count = int(workload.get("step_count"))
        threshold = int(orchestration["large_job_step_threshold"])
        max_steps = int(orchestration["max_supported_step_count"])
        target_seconds = int(orchestration[
            "large_static_service_level_target_seconds"
            if step_count >= threshold
            else "ordinary_static_service_level_target_seconds"
        ])
        workload_valid = all((
            action_count >= 0,
            0 <= step_count <= max_steps,
            workload.get("large_job_step_threshold") == threshold,
            workload.get("max_supported_step_count") == max_steps,
            workload.get("design_mode") in {"direct", "paged"},
            workload.get("static_service_level_target_seconds")
            == target_seconds,
        ))
    except (KeyError, TypeError, ValueError):
        workload_valid = False
    actual = generation_job_fingerprint(job)
    return bool(
        workload_valid
        and job.get("job_fingerprint") == actual
        and job.get("job_id") == f"job-{actual[:16]}"
    )


def generation_job_fingerprint(job):
    value = {
        key: item
        for key, item in copy.deepcopy(job or {}).items()
        if key not in {"job_id", "job_fingerprint", "job_path"}
    }
    return _fingerprint(value)


def generation_job_design_mode(job):
    workload = (job or {}).get("workload") or {}
    return str(workload.get("design_mode") or "")

def generation_job_lease(job):
    if not generation_job_identity_is_valid(job):
        raise ValueError("Generation Job identity无效，不能创建lease")
    profile = job.get("profile_lease") or {}
    admission = job.get("admission_receipt") or {}
    value = {
        "generation_job_lease_version": GENERATION_JOB_LEASE_VERSION,
        "job_id": job.get("job_id"),
        "job_fingerprint": job.get("job_fingerprint"),
        "job_nonce": job.get("nonce"),
        "static_service_level_target_seconds": (
            job.get("workload") or {}
        ).get("static_service_level_target_seconds"),
        "request_id": (job.get("request") or {}).get("request_id"),
        "profile_id": profile.get("profile_id"),
        "profile_version": profile.get("generation_profile_version"),
        "profile_fingerprint": profile.get("profile_fingerprint"),
        "admission_fingerprint": admission.get("admission_fingerprint"),
        "generation_capsule_fingerprint": (
            (job.get("generation_capsule") or {}).get(
                "capsule_fingerprint"
            )
        ),
    }
    value["lease_fingerprint"] = _fingerprint(value)
    return value

def generation_job_lease_is_valid(value):
    if not isinstance(value, dict):
        return False
    fields = {
        "generation_job_lease_version",
        "job_id",
        "job_fingerprint",
        "job_nonce",
        "static_service_level_target_seconds",
        "request_id",
        "profile_id",
        "profile_version",
        "profile_fingerprint",
        "admission_fingerprint",
        "generation_capsule_fingerprint",
        "lease_fingerprint",
    }
    if set(value) != fields or value.get(
            "generation_job_lease_version"
    ) != GENERATION_JOB_LEASE_VERSION:
        return False
    string_fields = fields - {
        "generation_job_lease_version",
        "static_service_level_target_seconds",
    }
    if not all(
        isinstance(value.get(field), str) and bool(value.get(field))
        for field in string_fields
    ):
        return False
    if (
        not isinstance(
            value.get("static_service_level_target_seconds"),
            int,
        )
        or isinstance(value.get("static_service_level_target_seconds"), bool)
        or value["static_service_level_target_seconds"] <= 0
    ):
        return False
    expected = _fingerprint({
        key: item
        for key, item in value.items()
        if key != "lease_fingerprint"
    })
    return value.get("lease_fingerprint") == expected


def _admission_receipt(admission):
    return {
        "generation_admission_version": admission.get(
            "generation_admission_version"
        ),
        "status": admission.get("status"),
        "enforcement": admission.get("enforcement"),
        "job_creation_allowed": admission.get("job_creation_allowed"),
        "request_id": admission.get("request_id"),
        "checks": copy.deepcopy(admission.get("checks") or []),
        "performance_checks": copy.deepcopy(
            admission.get("performance_checks") or []
        ),
        "performance_warnings": copy.deepcopy(
            admission.get("performance_warnings") or []
        ),
        "blocking_codes": copy.deepcopy(
            admission.get("blocking_codes") or []
        ),
        "decision_batch": copy.deepcopy(
            admission.get("decision_batch") or {}
        ),
        "admission_fingerprint": admission.get("admission_fingerprint"),
    }


def _decision_lease(decision):
    pack = decision.get("pack") or {}
    answers = decision.get("answers") or {}
    return {
        "status": decision.get("status"),
        "pack_id": pack.get("pack_id"),
        "pack_fingerprint": pack.get("pack_fingerprint"),
        "answer_fingerprint": answers.get("answer_fingerprint"),
        "revision_seal": pack.get("revision_seal"),
    }


def _parse_job_time(value):
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise ValueError("Generation Job timestamp无效") from error


def _job_path(session_dir, request_id, fingerprint):
    request_id = str(request_id or "")
    fingerprint = str(fingerprint or "")
    if not request_id or any(character in request_id for character in "/\\"):
        raise ValueError("Generation Job request_id无效")
    if len(fingerprint) != 64 or any(
            character not in "0123456789abcdef"
            for character in fingerprint
    ):
        raise ValueError("Generation Job fingerprint无效")
    return (
        Path(session_dir).resolve()
        / "ai"
        / "generation-jobs"
        / request_id
        / f"job-{fingerprint}.json"
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