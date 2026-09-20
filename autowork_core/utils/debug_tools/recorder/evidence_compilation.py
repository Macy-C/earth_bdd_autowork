from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from autowork_core.utils.debug_tools.recorder.models import SCHEMA_VERSION
from autowork_core.utils.debug_tools.recorder.writer import write_json_atomic


EVIDENCE_COMPILATION_VERSION = "1.0"
EVIDENCE_COMPILATION_PATH = "evidence-compilation.json"
TERMINAL_EVIDENCE_COMPILATION_STATUSES = frozenset({
    "verified",
    "forensic_review",
    "authorization_required",
    "placeholder_required",
    "hard_missing",
    "failed",
})
COMPILED_EVIDENCE_STATUSES = frozenset({
    "verified",
    "forensic_review",
    "authorization_required",
    "placeholder_required",
    "hard_missing",
})
EVIDENCE_COMPILATION_STATUSES = TERMINAL_EVIDENCE_COMPILATION_STATUSES | {
    "not_started",
    "running",
}
_INPUT_ARTIFACTS = (
    "take.json",
    "raw-events.seal.json",
    "capture-completion.json",
    "events.jsonl",
    "media-index.json",
    "action-media.json",
    "current-projection.json",
    "evidence-acquisition.jsonl",
)
_INPUT_PREFIXES = (
    Path("frames"),
    Path("screenshots"),
    Path("ui"),
    Path("windows"),
)


def build_evidence_compilation_result(
        take_dir,
        *,
        status,
        compiled_artifacts=None,
        issues=None,
        review_required=None,
        hard_missing=None,
        created_at=None,
    ):
    take_dir = Path(take_dir).resolve()
    status = _normalize_status(status)
    result = {
        "schema_version": SCHEMA_VERSION,
        "evidence_compilation_version": EVIDENCE_COMPILATION_VERSION,
        "status": status,
        "terminal": status in TERMINAL_EVIDENCE_COMPILATION_STATUSES,
        "evidence_compiled": status in COMPILED_EVIDENCE_STATUSES,
        "created_at": created_at or datetime.now().isoformat(timespec="seconds"),
        "source_take_id": _source_take_id(take_dir),
        "timeline_revision": _timeline_revision(take_dir),
        "input_fingerprint": evidence_compilation_input_fingerprint(take_dir),
        "compiled_artifacts": _compiled_artifacts(compiled_artifacts or {}),
        "issues": [dict(item) for item in issues or ()],
        "review_required": [dict(item) for item in review_required or ()],
        "hard_missing": [dict(item) for item in hard_missing or ()],
    }
    result["output_fingerprint"] = evidence_compilation_output_fingerprint(
        result,
    )
    result["compilation_id"] = "evidence-compilation-" + result[
        "output_fingerprint"
    ][:16]
    result["result_fingerprint"] = evidence_compilation_result_fingerprint(
        result,
    )
    return result


def write_evidence_compilation_result(take_dir, result):
    take_dir = Path(take_dir).resolve()
    if not evidence_compilation_identity_is_valid(result):
        raise ValueError("EvidenceCompilationResult 身份无效")
    path = take_dir / EVIDENCE_COMPILATION_PATH
    write_json_atomic(path, result)
    return path


def load_evidence_compilation_result(take_dir):
    path = Path(take_dir).resolve() / EVIDENCE_COMPILATION_PATH
    value = json.loads(path.read_text(encoding="utf-8"))
    if not evidence_compilation_identity_is_valid(value):
        raise ValueError("EvidenceCompilationResult 身份无效")
    return value


def evidence_compilation_identity_is_valid(value):
    if not isinstance(value, dict):
        return False
    if value.get("schema_version") != SCHEMA_VERSION:
        return False
    if value.get("evidence_compilation_version") != (
            EVIDENCE_COMPILATION_VERSION
    ):
        return False
    status = value.get("status")
    if status not in EVIDENCE_COMPILATION_STATUSES:
        return False
    if bool(value.get("terminal")) != (
            status in TERMINAL_EVIDENCE_COMPILATION_STATUSES
    ):
        return False
    if bool(value.get("evidence_compiled")) != (
            status in COMPILED_EVIDENCE_STATUSES
    ):
        return False
    output_fingerprint = value.get("output_fingerprint")
    result_fingerprint = value.get("result_fingerprint")
    compilation_id = value.get("compilation_id")
    if not all(
            _is_sha256(item)
            for item in (output_fingerprint, result_fingerprint)
    ):
        return False
    if compilation_id != f"evidence-compilation-{output_fingerprint[:16]}":
        return False
    return all((
        _is_sha256(value.get("input_fingerprint")),
        output_fingerprint == evidence_compilation_output_fingerprint(value),
        result_fingerprint == evidence_compilation_result_fingerprint(value),
        isinstance(value.get("compiled_artifacts"), dict),
        isinstance(value.get("issues"), list),
        isinstance(value.get("review_required"), list),
        isinstance(value.get("hard_missing"), list),
    ))


def evidence_compilation_input_fingerprint(take_dir):
    take_dir = Path(take_dir).resolve()
    artifacts = []
    for relative in _INPUT_ARTIFACTS:
        path = take_dir / relative
        if path.exists() and path.is_file():
            artifacts.append(_artifact_record(take_dir, path))
    for prefix in _INPUT_PREFIXES:
        root = take_dir / prefix
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file():
                artifacts.append(_artifact_record(take_dir, path))
    return _fingerprint({"input_artifacts": artifacts})


def evidence_compilation_output_fingerprint(result):
    return _fingerprint({
        "status": result.get("status"),
        "compiled_artifacts": result.get("compiled_artifacts") or {},
        "issues": result.get("issues") or [],
        "review_required": result.get("review_required") or [],
        "hard_missing": result.get("hard_missing") or [],
    })


def evidence_compilation_result_fingerprint(result):
    return _fingerprint({
        key: item
        for key, item in dict(result or {}).items()
        if key != "result_fingerprint"
    })


def _compiled_artifacts(artifacts):
    result = {}
    for key, value in dict(artifacts or {}).items():
        if isinstance(value, dict):
            result[str(key)] = dict(value)
        else:
            result[str(key)] = {"path": str(value)}
    return result


def _source_take_id(take_dir):
    take = _read_json_optional(Path(take_dir) / "take.json")
    return str(take.get("id") or "") or None


def _timeline_revision(take_dir):
    pointer = _read_json_optional(Path(take_dir) / "current-projection.json")
    if pointer.get("source_revision") is not None:
        return str(pointer.get("source_revision"))
    state = _read_json_optional(Path(take_dir) / "timeline-state.json")
    if state.get("timeline_revision") is not None:
        return str(state.get("timeline_revision"))
    return None


def _artifact_record(base, path):
    record = {"path": Path(path).relative_to(base).as_posix()}
    record.update(_artifact_integrity(path))
    return record


def _artifact_integrity(path):
    path = Path(path)
    content = path.read_bytes()
    return {
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
    }


def _read_json_optional(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _normalize_status(status):
    value = str(status or "").strip()
    if value not in EVIDENCE_COMPILATION_STATUSES:
        raise ValueError(f"EvidenceCompilationResult status 无效: {status}")
    return value


def _is_sha256(value):
    text = str(value or "")
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)


def _fingerprint(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
