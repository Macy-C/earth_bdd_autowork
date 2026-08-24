from __future__ import annotations

import copy
import hashlib
import json
import secrets
from datetime import datetime
from pathlib import Path

from autowork_core.utils.debug_tools.recorder.models import SCHEMA_VERSION
from autowork_core.utils.debug_tools.recorder.writer import write_json_atomic


GENERATION_JOB_VERSION = "1.0"
GENERATION_JOB_LEASE_VERSION = "1.0"
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
    ):
    request = copy.deepcopy(request or {})
    state = copy.deepcopy(state or {})
    admission = copy.deepcopy(admission or {})
    contract_lease = copy.deepcopy(contract_lease or {})
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

    value = {
        "schema_version": SCHEMA_VERSION,
        "generation_job_version": GENERATION_JOB_VERSION,
        "activation": activation,
        "created_at": str(
            created_at or datetime.now().isoformat(timespec="milliseconds")
        ),
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
        "profile_lease": profile,
        "admission_receipt": _admission_receipt(admission),
        "execution_boundary": {
            "allowed_queries": [
                "inspect-job",
                "job-evidence",
                "job-compare-takes",
                "job-action-knowledge",
                "design-contract",
            ],
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
    if any((
        job.get("generation_job_version") != GENERATION_JOB_VERSION,
        job.get("activation") not in GENERATION_JOB_ACTIVATIONS,
        not isinstance(job.get("created_at"), str),
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
    )):
        return False
    actual = generation_job_fingerprint(job)
    return bool(
        job.get("job_fingerprint") == actual
        and job.get("job_id") == f"job-{actual[:16]}"
    )


def generation_job_fingerprint(job):
    value = {
        key: item
        for key, item in copy.deepcopy(job or {}).items()
        if key not in {"job_id", "job_fingerprint", "job_path"}
    }
    return _fingerprint(value)

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
        "request_id": (job.get("request") or {}).get("request_id"),
        "profile_id": profile.get("profile_id"),
        "profile_version": profile.get("generation_profile_version"),
        "profile_fingerprint": profile.get("profile_fingerprint"),
        "admission_fingerprint": admission.get("admission_fingerprint"),
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
        "request_id",
        "profile_id",
        "profile_version",
        "profile_fingerprint",
        "admission_fingerprint",
        "lease_fingerprint",
    }
    if set(value) != fields or value.get(
            "generation_job_lease_version"
    ) != GENERATION_JOB_LEASE_VERSION:
        return False
    if not all(
        isinstance(value.get(field), str) and bool(value.get(field))
        for field in fields - {"generation_job_lease_version"}
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