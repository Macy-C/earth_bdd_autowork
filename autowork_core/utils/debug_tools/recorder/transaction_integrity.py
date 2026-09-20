from __future__ import annotations

import hashlib
import json


TRANSACTION_VERSION = "3.3"


TRANSACTION_TRANSPORT_FIELDS = {
    "acceptance_summary",
    "category",
    "completion_fingerprint",
    "delivery_summary",
    "delivery_visibility",
    "failure_summary",
    "generation_timing_ledger",
    "health_issues",
    "host_delivery_observation",
    "job_lifecycle_timing",
    "job_transition",
    "last_job_result",
    "report_path",
    "result_fingerprint",
    "service_level",
    "stages",
    "workspace_projection_summary",
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
            "health_issues",
            "acceptance_summary",
            "category",
            "delivery_summary",
            "delivery_visibility",
            "failure_summary",
            "generation_timing_ledger",
            "host_delivery_observation",
            "job_lifecycle_timing",
            "job_transition",
            "last_job_result",
            "report_path",
            "result_fingerprint",
            "service_level",
            "stages",
            "workspace_projection_summary",
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