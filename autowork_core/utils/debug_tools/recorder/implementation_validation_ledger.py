from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from autowork_core.utils.debug_tools.recorder.writer import write_json_atomic


LEDGER_VERSION = "1.0"


def append_validation_attempt(
        path,
        *,
        transaction_id,
        manifest_fingerprint,
        source_snapshot,
        status,
        issues,
        expected_pointer=None,
    ):
    path = Path(path).resolve()
    ledger = _load_ledger(path)
    pointer = dict(expected_pointer or {})
    if ledger:
        if any((
            Path(str(pointer.get("path") or "")).resolve() != path,
            pointer.get("head_fingerprint")
            != ledger.get("head_fingerprint"),
            pointer.get("fingerprint") != ledger.get("fingerprint"),
            pointer.get("attempt_count")
            != len(ledger.get("attempts") or ()),
        )):
            raise ValueError("validation ledger pointer mismatch")
    elif any(pointer.values()):
        raise ValueError("validation ledger pointer references missing ledger")
    if not ledger:
        ledger = {
            "implementation_validation_ledger_version": LEDGER_VERSION,
            "transaction_id": str(transaction_id),
            "manifest_fingerprint": str(manifest_fingerprint),
            "attempts": [],
            "head_fingerprint": None,
        }
        ledger["fingerprint"] = validation_ledger_fingerprint(ledger)
    _validate_ledger(
        ledger,
        transaction_id=transaction_id,
        manifest_fingerprint=manifest_fingerprint,
    )
    attempts = list(ledger["attempts"])
    attempt = {
        "attempt_id": f"implementation-validation-{len(attempts) + 1:03d}",
        "sequence": len(attempts) + 1,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "transaction_id": str(transaction_id),
        "manifest_fingerprint": str(manifest_fingerprint),
        "source_snapshot": list(source_snapshot),
        "status": str(status),
        "issues": list(issues),
        "previous_attempt_fingerprint": (
            attempts[-1]["fingerprint"] if attempts else None
        ),
    }
    attempt["fingerprint"] = _fingerprint(attempt)
    ledger["attempts"] = [*attempts, attempt]
    ledger["head_fingerprint"] = attempt["fingerprint"]
    ledger["fingerprint"] = validation_ledger_fingerprint(ledger)
    write_json_atomic(path, ledger)
    return attempt, ledger


def verify_validation_ledger(
        path,
        *,
        transaction_id,
        manifest_fingerprint,
    ):
    try:
        ledger = _load_ledger(Path(path))
        _validate_ledger(
            ledger,
            transaction_id=transaction_id,
            manifest_fingerprint=manifest_fingerprint,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        return None, [f"validation ledger invalid: {error}"]
    return ledger, []


def validation_ledger_fingerprint(ledger):
    return _fingerprint({
        key: value for key, value in dict(ledger or {}).items()
        if key != "fingerprint"
    })


def snapshot_ai_editable_files(project_root, manifest):
    project_root = Path(project_root).resolve()
    records = []
    for relative in sorted(manifest.get("ai_editable_changes") or ()):
        path = (project_root / relative).resolve()
        path.relative_to(project_root)
        if path.is_file() and not path.is_symlink():
            content = path.read_bytes()
            records.append({
                "path": str(relative),
                "exists": True,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            })
        else:
            records.append({
                "path": str(relative),
                "exists": False,
                "sha256": None,
                "size": 0,
            })
    return records


def _validate_ledger(ledger, *, transaction_id, manifest_fingerprint):
    if not isinstance(ledger, dict):
        raise ValueError("ledger must be an object")
    if any((
        ledger.get("implementation_validation_ledger_version")
        != LEDGER_VERSION,
        ledger.get("transaction_id") != str(transaction_id),
        ledger.get("manifest_fingerprint") != str(manifest_fingerprint),
        not isinstance(ledger.get("attempts"), list),
    )):
        raise ValueError("ledger identity mismatch")
    attempts = ledger["attempts"]
    previous = None
    for index, attempt in enumerate(attempts, start=1):
        payload = {
            key: value for key, value in attempt.items()
            if key != "fingerprint"
        }
        if any((
            not isinstance(attempt, dict),
            attempt.get("sequence") != index,
            attempt.get("attempt_id")
            != f"implementation-validation-{index:03d}",
            attempt.get("transaction_id") != str(transaction_id),
            attempt.get("manifest_fingerprint") != str(manifest_fingerprint),
            attempt.get("previous_attempt_fingerprint") != previous,
            attempt.get("status") not in {"valid", "invalid"},
            not isinstance(attempt.get("source_snapshot"), list),
            not isinstance(attempt.get("issues"), list),
            attempt.get("fingerprint") != _fingerprint(payload),
        )):
            raise ValueError(f"attempt identity mismatch: {index}")
        previous = attempt["fingerprint"]
    if attempts:
        if ledger.get("head_fingerprint") != attempts[-1]["fingerprint"]:
            raise ValueError("ledger head mismatch")
    elif ledger.get("head_fingerprint") not in {None, ""}:
        raise ValueError("empty ledger has a head")
    if ledger.get("fingerprint") != validation_ledger_fingerprint(ledger):
        raise ValueError("ledger fingerprint mismatch")


def _load_ledger(path):
    if not Path(path).is_file():
        return {}
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("ledger must be an object")
    return value


def _fingerprint(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
