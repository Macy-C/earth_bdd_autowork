from __future__ import annotations

import hashlib
import json


TRANSACTION_TRANSPORT_FIELDS = {
    "completion_fingerprint",
    "job_lifecycle_timing",
    "job_transition",
    "report_path",
    "result_fingerprint",
}


def completed_report_fingerprint(report):
    value = {
        key: item
        for key, item in dict(report or {}).items()
        if key not in TRANSACTION_TRANSPORT_FIELDS
    }
    return _fingerprint(value)


def transaction_result_fingerprint(report):
    value = {
        key: item
        for key, item in dict(report or {}).items()
        if key not in {
            "job_lifecycle_timing",
            "job_transition",
            "report_path",
            "result_fingerprint",
        }
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