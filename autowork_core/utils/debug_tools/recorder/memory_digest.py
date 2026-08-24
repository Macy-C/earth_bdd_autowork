from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


MEMORY_DIGEST_VERSION = "2.0"
MEMORY_REVISION_PREFIX = "memory-relevant-v1:"
MAX_DIGEST_BYTES = 4096
MAX_ITEMS = 6
MAX_ITEMS_PER_BUCKET = 2
MAX_OPERATIONS = 6
MAX_PATHS = 6
MAX_SCOPE_IDS = 3
MAX_TEXT = 96
_BUCKETS = (
    "confirmed_knowledge",
    "past_corrections",
    "accepted_outcomes",
    "generation_history",
    "provisional_insights",
)
_AUTHORITIES = {
    "user_confirmed",
    "generation_result",
    "code_verified",
}
_TEXT_SPACE = re.compile(r"\s+")


def build_memory_digest(context, *, revision=None):
    context = context if isinstance(context, dict) else {}
    candidates = []
    bucket_counts = {}
    for bucket in _BUCKETS:
        bucket_items = context.get(bucket) or []
        bucket_counts[bucket] = len(bucket_items)
        accepted = 0
        for event in bucket_items:
            if accepted >= MAX_ITEMS_PER_BUCKET:
                break
            if not isinstance(event, dict):
                continue
            if event.get("authority") not in _AUTHORITIES:
                continue
            item = _digest_item(event, bucket)
            if item is None:
                continue
            candidates.append(item)
            accepted += 1
            if len(candidates) >= MAX_ITEMS:
                break
        if len(candidates) >= MAX_ITEMS:
            break

    candidate_count = sum(
        min(
            MAX_ITEMS_PER_BUCKET,
            sum(
                isinstance(item, dict)
                and item.get("authority") in _AUTHORITIES
                for item in context.get(bucket) or []
            ),
        )
        for bucket in _BUCKETS
    )
    items = candidates[:MAX_ITEMS]
    journal_revision = (
        (context.get("journal") or {}).get("revision")
        or revision
    )
    while True:
        digest = _finalize_digest(
            items,
            journal_revision=journal_revision,
            relevant_count=context.get("relevant_count", 0),
            candidate_count=candidate_count,
            bucket_counts=bucket_counts,
        )
        if _encoded_size(digest) <= MAX_DIGEST_BYTES or not items:
            return digest
        items.pop()


def build_relevant_memory_revision(context):
    context = dict(context) if isinstance(context, dict) else {}
    journal = dict(context.get("journal") or {})
    journal.pop("revision", None)
    context["journal"] = journal
    digest = build_memory_digest(context)
    identity = {
        key: value
        for key, value in digest.items()
        if key not in {
            "revision",
            "journal_revision",
            "digest_fingerprint",
        }
    }
    identity["warning_hashes"] = [
        _hash_text(warning)
        for warning in context.get("warnings") or []
    ]
    return MEMORY_REVISION_PREFIX + _stable_hash(identity)


def is_relevant_memory_revision(value):
    return str(value or "").startswith(MEMORY_REVISION_PREFIX)


def _digest_item(event, bucket):
    memory_id = _identifier(event.get("memory_id"))
    if not memory_id:
        return None
    kind = _identifier(event.get("kind")) or "unknown"
    status = _identifier(event.get("status")) or "unknown"
    signal, reuse = _signal(kind, status)
    item = {
        "memory_id": memory_id,
        "kind": kind,
        "bucket": bucket,
        "authority": event.get("authority"),
        "status": status,
        "signal": signal,
        "reuse": reuse,
        "claim_hash": _hash_text(event.get("claim")),
        "scope": _scope(event.get("scope")),
        "source": _source(event.get("source")),
    }
    if event.get("authority") == "user_confirmed":
        excerpt = _text(event.get("claim"))
        if excerpt:
            item["claim_excerpt"] = excerpt
    structured = _structured_payload(kind, event.get("payload"))
    if structured:
        item["structured"] = structured
    return _clean(item)


def _structured_payload(kind, payload):
    payload = payload if isinstance(payload, dict) else {}
    if kind == "plan_confirmed":
        return _plan(payload.get("plan"))
    if kind == "transaction_completed":
        trace = payload.get("decision_trace") or {}
        return _clean({
            "changed_files": _paths(payload.get("changed_files")),
            "required_validations": _identifiers(
                payload.get("required_validations"),
                MAX_PATHS,
            ),
            "validation_statuses": _validation_statuses(
                payload.get("validations"),
            ),
            "reuse_used": _identifiers(trace.get("reuse_used"), 4),
        })
    if kind == "transaction_feedback":
        return _clean({
            "changed_files": _paths(payload.get("changed_files")),
            "modified_since_generation": _paths(
                payload.get("modified_since_generation"),
            ),
        })
    return {}


def _plan(value):
    value = value if isinstance(value, dict) else {}
    operations = []
    for operation in value.get("operations") or []:
        if not isinstance(operation, dict):
            continue
        compact = _clean({
            "op": _identifier(operation.get("op")),
            "target": _text(operation.get("target")),
            "source": _safe_path(operation.get("source")),
            "reuse_reference": _identifier(
                operation.get("reuse_reference")
            ),
        })
        if compact:
            operations.append(compact)
        if len(operations) >= MAX_OPERATIONS:
            break
    return _clean({
        "behavior_owner": _text(value.get("behavior_owner")),
        "behavior_file": _safe_path(value.get("behavior_file")),
        "page_object": _text(value.get("page_object")),
        "locator_file": _safe_path(value.get("locator_file")),
        "data_file": _safe_path(value.get("data_file")),
        "operations": operations,
    })


def _scope(value):
    value = value if isinstance(value, dict) else {}
    return _clean({
        "feature_id": _identifier(value.get("feature_id")),
        "scenario_id": _identifier(value.get("scenario_id")),
        "step_ids": _identifiers(value.get("step_ids"), MAX_SCOPE_IDS),
    })


def _source(value):
    value = value if isinstance(value, dict) else {}
    return _clean({
        "request_id": _identifier(value.get("request_id")),
        "transaction_id": _identifier(value.get("transaction_id")),
        "plan_id": _identifier(value.get("plan_id")),
    })


def _signal(kind, status):
    if kind == "transaction_feedback":
        return {
            "accepted": ("positive", "candidate"),
            "revised": ("revision", "review"),
            "rejected": ("negative", "blocked"),
        }.get(status, ("feedback", "review"))
    if kind == "plan_confirmed":
        return "positive", "candidate"
    if kind == "transaction_completed":
        return "outcome", "evidence"
    return "advisory", "review"


def _finalize_digest(
        items,
        *,
        journal_revision,
        relevant_count,
        candidate_count,
        bucket_counts,
):
    value = {
        "memory_digest_version": MEMORY_DIGEST_VERSION,
        "revision": journal_revision,
        "journal_revision": journal_revision,
        "relevant_count": int(relevant_count or 0),
        "candidate_count": candidate_count,
        "selected_count": len(items),
        "truncated_count": max(0, candidate_count - len(items)),
        "bucket_counts": {
            key: int(bucket_counts.get(key, 0))
            for key in _BUCKETS
            if bucket_counts.get(key, 0)
        },
        "items": list(items),
    }
    value["digest_fingerprint"] = _stable_hash(value)
    return value


def _validation_statuses(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    for name in sorted(value):
        status = value.get(name)
        if isinstance(status, dict):
            status = status.get("status")
        name = _identifier(name)
        status = _identifier(status)
        if name and status:
            result[name] = status
        if len(result) >= MAX_PATHS:
            break
    return result


def _paths(values):
    result = []
    for value in values or []:
        path = _safe_path(value)
        if path and path not in result:
            result.append(path)
        if len(result) >= MAX_PATHS:
            break
    return result


def _safe_path(value):
    if value is None:
        return None
    text = str(value).strip().replace("\\", "/")
    path = Path(text)
    if not text or path.is_absolute() or ".." in path.parts:
        return None
    return text[:160]


def _identifiers(values, limit):
    result = []
    for value in values or []:
        item = _identifier(value)
        if item and item not in result:
            result.append(item)
        if len(result) >= limit:
            break
    return result


def _identifier(value):
    if value is None:
        return None
    return _TEXT_SPACE.sub(" ", str(value)).strip()[:128] or None


def _text(value):
    if value is None:
        return None
    return _TEXT_SPACE.sub(" ", str(value)).strip()[:MAX_TEXT] or None


def _hash_text(value):
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _clean(value):
    return {
        key: item
        for key, item in value.items()
        if item not in (None, "", [], {})
    }


def _stable_hash(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _encoded_size(value):
    return len(json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8"))