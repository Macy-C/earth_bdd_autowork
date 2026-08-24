from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

from autowork_core.utils.debug_tools.recorder.models import SCHEMA_VERSION
from autowork_core.utils.debug_tools.recorder.scope_binding import (
    BUSINESS_PROJECTION_VERSION,
    recording_business_fingerprint,
)
from autowork_core.utils.debug_tools.recorder.writer import write_json_atomic


_CATALOG_LOCK = threading.RLock()


def update_recording_catalog(output_root, session_dir, manifest, readiness=None):
    output_root = Path(output_root).resolve()
    session_dir = Path(session_dir).resolve()
    catalog_path = output_root / "catalog.json"
    with _CATALOG_LOCK:
        catalog = _load_catalog(catalog_path)
        relative_path = session_dir.relative_to(output_root).as_posix()
        entry = _catalog_entry(relative_path, manifest, readiness)
        sessions = [
            item
            for item in catalog.get("sessions", [])
            if item.get("session_id") != entry["session_id"]
        ]
        sessions.append(entry)
        sessions.sort(
            key=lambda item: (item.get("updated_at") or "", item.get("session_id") or ""),
            reverse=True,
        )
        catalog.update({
            "schema_version": SCHEMA_VERSION,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "sessions": sessions,
        })
        write_json_atomic(catalog_path, catalog)
    return catalog_path


def load_recording_catalog(output_root):
    return _load_catalog(Path(output_root).resolve() / "catalog.json")


def remove_recording_catalog_entry(output_root, session_id):
    output_root = Path(output_root).resolve()
    catalog_path = output_root / "catalog.json"
    with _CATALOG_LOCK:
        catalog = _load_catalog(catalog_path)
        removed = next(
            (
                item
                for item in catalog.get("sessions") or ()
                if item.get("session_id") == session_id
            ),
            None,
        )
        if removed is None:
            raise KeyError(f"录制 catalog 不存在 Session: {session_id}")
        catalog["sessions"] = [
            item
            for item in catalog.get("sessions") or ()
            if item.get("session_id") != session_id
        ]
        catalog["updated_at"] = datetime.now().isoformat(timespec="seconds")
        write_json_atomic(catalog_path, catalog)
        return removed


def restore_recording_catalog_entry(output_root, entry):
    output_root = Path(output_root).resolve()
    catalog_path = output_root / "catalog.json"
    with _CATALOG_LOCK:
        catalog = _load_catalog(catalog_path)
        sessions = [
            item
            for item in catalog.get("sessions") or ()
            if item.get("session_id") != entry.get("session_id")
        ]
        sessions.append(dict(entry))
        sessions.sort(
            key=lambda item: (
                item.get("updated_at") or "",
                item.get("session_id") or "",
            ),
            reverse=True,
        )
        catalog.update({
            "schema_version": catalog.get("schema_version") or SCHEMA_VERSION,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "sessions": sessions,
        })
        write_json_atomic(catalog_path, catalog)
        return catalog_path


def _load_catalog(path):
    if not path.exists():
        return {
            "schema_version": SCHEMA_VERSION,
            "updated_at": None,
            "sessions": [],
        }
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {"sessions": []}
    except Exception:
        return {"schema_version": SCHEMA_VERSION, "updated_at": None, "sessions": []}


def _catalog_entry(relative_path, manifest, readiness):
    feature = manifest["feature"]
    scenario = manifest["scenario"]
    return {
        "session_id": manifest["session_id"],
        "path": relative_path,
        "status": manifest["status"],
        "created_at": manifest["created_at"],
        "updated_at": manifest["updated_at"],
        "source_hash": manifest.get("source_hash"),
        "business_projection_version": BUSINESS_PROJECTION_VERSION,
        "business_fingerprint": recording_business_fingerprint(
            feature,
            scenario,
        ),
        "readiness": {
            key: (readiness or {}).get(key)
            for key in ("bundle_valid", "recording_complete", "semantic_ready", "generation_ready")
        },
        "feature": feature,
        "scenario": {
            key: scenario.get(key)
            for key in (
                "id",
                "key",
                "logical_template_id",
                "name",
                "kind",
                "example_id",
                "example_values",
            )
        },
        "steps": [
            {
                "id": entry["plan"]["id"],
                "key": entry["plan"]["key"],
                "ordinal": entry["plan"]["ordinal"],
                "keyword": entry["plan"]["keyword"],
                "text": entry["plan"]["text"],
                "status": entry["status"],
                "selected_take": entry["selected_take"],
            }
            for entry in manifest.get("steps", [])
        ],
    }