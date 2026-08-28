from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from autowork_core.utils.debug_tools.recorder.models import (
    SCHEMA_VERSION,
    public_dict,
)
from autowork_core.utils.debug_tools.recorder.writer import write_json_atomic


RAW_EVENT_JOURNAL_VERSION = "1.0"
RAW_CAPTURE_ARTIFACTS = (
    "raw-events.jsonl",
    "raw-events.seal.json",
    "capture-completion.json",
)


class RawEventJournal:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.path = self.directory / "raw-events.jsonl"
        self.seal_path = self.directory / "raw-events.seal.json"
        self._stream = None
        self._event_ids = []
        self._indices = []

    @property
    def event_ids(self):
        return tuple(self._event_ids)

    def start(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.seal_path.exists():
            raise FileExistsError(f"原始事件已经封存: {self.seal_path}")
        self._stream = self.path.open(
            "x",
            encoding="utf-8",
            newline="\n",
            buffering=1,
        )

    def append(self, event):
        if self._stream is None:
            raise RuntimeError("原始事件 journal 未启动或已经封存")
        value = public_dict(event)
        event_id = str(value.get("id") or "")
        index = value.get("index")
        if not event_id or isinstance(index, bool) or not isinstance(index, int):
            raise ValueError("原始事件缺少稳定 id/index")
        payload = {
            "schema_version": SCHEMA_VERSION,
            **value,
        }
        self._stream.write(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        self._stream.flush()
        self._event_ids.append(event_id)
        self._indices.append(index)

    def seal(self):
        if self._stream is None:
            if self.seal_path.exists():
                return json.loads(self.seal_path.read_text(encoding="utf-8"))
            raise RuntimeError("原始事件 journal 未启动")
        stream = self._stream
        try:
            stream.flush()
            os.fsync(stream.fileno())
        finally:
            try:
                stream.close()
            finally:
                self._stream = None

        content = self.path.read_bytes()
        expected_indices = list(range(1, len(self._indices) + 1))
        if self._indices != expected_indices:
            raise RuntimeError(
                "原始事件序号不连续: "
                f"expected={expected_indices}, actual={self._indices}"
            )
        seal = {
            "schema_version": SCHEMA_VERSION,
            "raw_event_journal_version": RAW_EVENT_JOURNAL_VERSION,
            "status": "sealed",
            "path": self.path.name,
            "event_count": len(self._event_ids),
            "first_event_id": self._event_ids[0] if self._event_ids else None,
            "last_event_id": self._event_ids[-1] if self._event_ids else None,
            "first_index": self._indices[0] if self._indices else None,
            "last_index": self._indices[-1] if self._indices else None,
            "event_ids_sha256": _stable_hash(self._event_ids),
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
        }
        write_json_atomic(self.seal_path, seal)
        return seal

    def close(self):
        stream = self._stream
        if stream is None:
            return
        try:
            stream.close()
        finally:
            self._stream = None


def write_capture_completion(directory, raw_seal, enriched_event_ids, *, error=None):
    enriched_event_ids = [str(value) for value in enriched_event_ids]
    raw_count = int((raw_seal or {}).get("event_count") or 0)
    raw_ids_hash = str((raw_seal or {}).get("event_ids_sha256") or "")
    enriched_ids_hash = _stable_hash(enriched_event_ids)
    complete = bool(
        error is None
        and len(enriched_event_ids) == raw_count
        and enriched_ids_hash == raw_ids_hash
    )
    value = {
        "schema_version": SCHEMA_VERSION,
        "raw_event_journal_version": RAW_EVENT_JOURNAL_VERSION,
        "status": "complete" if complete else "failed",
        "raw_seal": "raw-events.seal.json",
        "raw_event_count": raw_count,
        "enriched_event_count": len(enriched_event_ids),
        "raw_event_ids_sha256": raw_ids_hash,
        "enriched_event_ids_sha256": enriched_ids_hash,
        "error": str(error or "") or None,
    }
    write_json_atomic(Path(directory) / "capture-completion.json", value)
    return value


def validate_capture_integrity(directory, canonical_event_ids=None):
    directory = Path(directory).resolve()
    raw_path = directory / "raw-events.jsonl"
    seal_path = directory / "raw-events.seal.json"
    completion_path = directory / "capture-completion.json"
    errors = []
    raw_events = _read_jsonl(raw_path, errors)
    seal = _read_json(seal_path, errors)
    completion = _read_json(completion_path, errors)

    raw_ids = [str(event.get("id") or "") for event in raw_events]
    raw_indices = [event.get("index") for event in raw_events]
    expected_indices = list(range(1, len(raw_events) + 1))
    if any(not event_id for event_id in raw_ids):
        errors.append("raw event 缺少稳定 id")
    if raw_indices != expected_indices:
        errors.append(
            "raw event 序号不连续: "
            f"expected={expected_indices}, actual={raw_indices}"
        )
    if len(raw_ids) != len(set(raw_ids)):
        errors.append("raw event id 重复")

    if seal.get("status") != "sealed":
        errors.append("raw event seal 状态不是 sealed")
    content = raw_path.read_bytes() if raw_path.exists() else b""
    expected_seal = {
        "event_count": len(raw_ids),
        "first_event_id": raw_ids[0] if raw_ids else None,
        "last_event_id": raw_ids[-1] if raw_ids else None,
        "first_index": raw_indices[0] if raw_indices else None,
        "last_index": raw_indices[-1] if raw_indices else None,
        "event_ids_sha256": _stable_hash(raw_ids),
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
    }
    for key, expected in expected_seal.items():
        if seal.get(key) != expected:
            errors.append(
                f"raw event seal {key} 不匹配: "
                f"expected={expected!r}, actual={seal.get(key)!r}"
            )

    if completion.get("status") != "complete":
        errors.append("capture completion 状态不是 complete")
    if completion.get("raw_event_count") != len(raw_ids):
        errors.append("capture completion raw_event_count 不匹配")
    if completion.get("raw_event_ids_sha256") != _stable_hash(raw_ids):
        errors.append("capture completion raw event ID hash 不匹配")

    canonical_ids = (
        [str(value) for value in canonical_event_ids]
        if canonical_event_ids is not None
        else None
    )
    if canonical_ids is not None:
        if canonical_ids != raw_ids:
            errors.append("canonical events 与 raw event ID/顺序不一致")
        if completion.get("enriched_event_count") != len(canonical_ids):
            errors.append("capture completion enriched_event_count 不匹配")
        if (
            completion.get("enriched_event_ids_sha256")
            != _stable_hash(canonical_ids)
        ):
            errors.append("capture completion enriched event ID hash 不匹配")

    return {
        "status": "complete" if not errors else "failed",
        "raw_event_count": len(raw_ids),
        "canonical_event_count": (
            len(canonical_ids) if canonical_ids is not None else None
        ),
        "first_event_id": raw_ids[0] if raw_ids else None,
        "last_event_id": raw_ids[-1] if raw_ids else None,
        "raw_events": raw_path.name,
        "raw_seal": seal_path.name,
        "completion": completion_path.name,
        "raw_events_sha256": expected_seal["sha256"],
        "event_ids_sha256": expected_seal["event_ids_sha256"],
        "errors": errors,
    }


def requires_capture_integrity(directory, take=None):
    directory = Path(directory).resolve()
    return bool(
        ((take or {}).get("capture_integrity") or {}).get("status")
        == "complete"
        or any((directory / name).exists() for name in RAW_CAPTURE_ARTIFACTS)
    )


def _read_json(path, errors):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        errors.append(f"无法读取 {Path(path).name}: {type(error).__name__}: {error}")
        return {}
    if not isinstance(value, dict):
        errors.append(f"{Path(path).name} 必须是 object")
        return {}
    return value


def _read_jsonl(path, errors):
    result = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as error:
        errors.append(f"无法读取 {Path(path).name}: {type(error).__name__}: {error}")
        return result
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            errors.append(
                f"{Path(path).name}:{line_number} JSON 无效: {error}"
            )
            continue
        if not isinstance(value, dict):
            errors.append(f"{Path(path).name}:{line_number} 必须是 object")
            continue
        result.append(value)
    return result


def _stable_hash(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()