from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from autowork_core.utils.debug_tools.recorder.models import SCHEMA_VERSION


ACQUISITION_RECORD_VERSION = "1.0"
ACQUISITION_RECORDS_PATH = "evidence-acquisition.jsonl"
ACQUISITION_KINDS = frozenset({
    "tree_snapshot",
    "target_observation",
    "health_probe",
})
ACQUISITION_STATUSES = frozenset({
    "captured",
    "unavailable",
    "timed_out",
    "skipped_by_health",
    "failed",
})
NEGATIVE_ACQUISITION_STATUSES = ACQUISITION_STATUSES - {"captured"}


def build_acquisition_record(
        take_dir,
        *,
        kind,
        time_anchor,
        status,
        artifact=None,
        payload=None,
        error=None,
        created_at=None,
    ):
    take_dir = Path(take_dir).resolve()
    kind = str(kind or "")
    status = str(status or "")
    if kind not in ACQUISITION_KINDS:
        raise ValueError(f"Evidence acquisition kind 无效: {kind}")
    if status not in ACQUISITION_STATUSES:
        raise ValueError(f"Evidence acquisition status 无效: {status}")
    artifact_record = _artifact_record(take_dir, artifact) if artifact else None
    payload_record = dict(payload or {}) if payload else None
    if status == "captured" and artifact_record is None and payload_record is None:
        raise ValueError("captured acquisition 必须绑定 artifact 或 payload")
    record = {
        "schema_version": SCHEMA_VERSION,
        "acquisition_record_version": ACQUISITION_RECORD_VERSION,
        "kind": kind,
        "status": status,
        "negative_evidence": status in NEGATIVE_ACQUISITION_STATUSES,
        "time_anchor": dict(time_anchor or {}),
        "artifact": artifact_record,
        "payload": payload_record,
        "error": str(error or "") or None,
        "created_at": created_at or datetime.now().isoformat(timespec="seconds"),
    }
    record["record_fingerprint"] = _fingerprint(record)
    return record


def write_acquisition_record(take_dir, record):
    if not acquisition_record_identity_is_valid(record):
        raise ValueError("EvidenceAcquisitionRecord 身份无效")
    path = Path(take_dir).resolve() / ACQUISITION_RECORDS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush()
    return path


def load_acquisition_records(take_dir):
    path = Path(take_dir).resolve() / ACQUISITION_RECORDS_PATH
    if not path.exists():
        return []
    records = []
    for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(),
            start=1,
    ):
        if not line.strip():
            continue
        value = json.loads(line)
        if not acquisition_record_identity_is_valid(value):
            raise ValueError(
                f"EvidenceAcquisitionRecord 身份无效: line={line_number}"
            )
        records.append(value)
    return records


def acquisition_record_identity_is_valid(record):
    if not isinstance(record, dict):
        return False
    if record.get("schema_version") != SCHEMA_VERSION:
        return False
    if record.get("acquisition_record_version") != ACQUISITION_RECORD_VERSION:
        return False
    status = record.get("status")
    if record.get("kind") not in ACQUISITION_KINDS:
        return False
    if status not in ACQUISITION_STATUSES:
        return False
    if bool(record.get("negative_evidence")) != (
            status in NEGATIVE_ACQUISITION_STATUSES
    ):
        return False
    expected = _fingerprint({
        key: value
        for key, value in record.items()
        if key != "record_fingerprint"
    })
    return record.get("record_fingerprint") == expected


def _artifact_record(take_dir, artifact):
    take_dir = Path(take_dir).resolve()
    path = (take_dir / str(artifact)).resolve()
    try:
        relative = path.relative_to(take_dir).as_posix()
    except ValueError as error:
        raise ValueError(f"采集产物越界: {artifact}") from error
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(path)
    content = path.read_bytes()
    return {
        "path": relative,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
    }


def _fingerprint(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
