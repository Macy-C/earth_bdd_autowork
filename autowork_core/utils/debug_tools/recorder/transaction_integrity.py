from __future__ import annotations

import hashlib
import json


def completed_report_fingerprint(report):
    value = {
        key: item
        for key, item in dict(report or {}).items()
        if key not in {
            "completion_fingerprint",
            "result_fingerprint",
            "report_path",
        }
    }
    return _fingerprint(value)


def transaction_result_fingerprint(report):
    value = {
        key: item
        for key, item in dict(report or {}).items()
        if key not in {"result_fingerprint", "report_path"}
    }
    return _fingerprint(value)


def runtime_code_snapshot_fingerprint(snapshot):
    return _fingerprint(snapshot)


def _fingerprint(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()