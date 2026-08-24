from __future__ import annotations

import hashlib
from pathlib import Path

from autowork_core.utils.debug_tools.recorder.models import SCHEMA_VERSION
from autowork_core.utils.debug_tools.recorder.writer import write_json_atomic


OBSERVATION_RECEIPT_VERSION = "1.0"


def write_observation_receipt(take_dir, event_id, observation):
    take_dir = Path(take_dir).resolve()
    event_id = str(event_id or "").strip()
    if not event_id:
        raise ValueError("Observation receipt 缺少 event_id")
    value = dict(observation or {})
    provider = str(value.get("provider") or "").strip()
    if not provider:
        raise ValueError("Observation receipt 缺少 provider")
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "observation_receipt_version": OBSERVATION_RECEIPT_VERSION,
        "event_id": event_id,
        "provider": provider,
        "provider_version": str(value.get("provider_version") or ""),
        "status": str(value.get("status") or "unknown"),
        "payload": {
            key: item
            for key, item in value.items()
            if key not in {
                "provider",
                "provider_version",
                "status",
            }
        },
    }
    identity = hashlib.sha256(_canonical_bytes(receipt)).hexdigest()
    output = take_dir / "observations" / f"observation-{identity[:20]}.json"
    if not output.exists():
        write_json_atomic(output, receipt)
    data = output.read_bytes()
    return {
        "event_id": event_id,
        "provider": provider,
        "provider_version": receipt["provider_version"],
        "status": receipt["status"],
        "item_count": int(
            (receipt.get("payload") or {}).get("item_count")
            or len((receipt.get("payload") or {}).get("items") or ())
        ),
        "truncated": bool(
            (receipt.get("payload") or {}).get("truncated")
        ),
        "path": output.relative_to(take_dir).as_posix(),
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }


def load_observation_receipt(take_dir, reference):
    take_dir = Path(take_dir).resolve()
    reference = dict(reference or {})
    path = (take_dir / str(reference.get("path") or "")).resolve()
    try:
        path.relative_to(take_dir / "observations")
    except ValueError as error:
        raise ValueError("Observation receipt 路径越界") from error
    if not path.is_file():
        raise FileNotFoundError(path)
    data = path.read_bytes()
    if any((
        hashlib.sha256(data).hexdigest() != reference.get("sha256"),
        len(data) != reference.get("size"),
    )):
        raise ValueError("Observation receipt 完整性校验失败")
    import json

    value = json.loads(data.decode("utf-8"))
    if any((
        value.get("event_id") != reference.get("event_id"),
        value.get("provider") != reference.get("provider"),
        value.get("provider_version") != reference.get("provider_version"),
    )):
        raise ValueError("Observation receipt 身份不一致")
    return value


def _canonical_bytes(value):
    import json

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
