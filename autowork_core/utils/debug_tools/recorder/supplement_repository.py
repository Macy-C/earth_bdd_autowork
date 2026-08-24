from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import yaml

from autowork_core.utils.debug_tools.recorder.identity import stable_digest
from autowork_core.utils.debug_tools.recorder.models import SCHEMA_VERSION
from autowork_core.utils.debug_tools.recorder.writer import (
    RecordingSessionWriter,
    write_json_atomic,
)


SUPPLEMENT_VERSION = "1.0"
_SUPPLEMENT_ID = re.compile(r"^supplement-[a-z0-9-]+$")


class SupplementRepository:
    def __init__(self, take_dir):
        self.take_dir = Path(take_dir).resolve()
        self.root = self.take_dir / "supplements"

    def allocate_id(self):
        now = datetime.now()
        return (
            f"supplement-{now.strftime('%Y%m%d-%H%M%S-%f')}-"
            f"{stable_digest(str(self.take_dir), now.isoformat(), length=6)}"
        )

    def reserve(self, supplement_id, metadata=None):
        directory = self.path_for(supplement_id)
        directory.mkdir(parents=True, exist_ok=False)
        artifact = {
            **dict(metadata or {}),
            "schema_version": SCHEMA_VERSION,
            "supplement_version": SUPPLEMENT_VERSION,
            "supplement_id": supplement_id,
            "status": "recording",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "relative_path": directory.relative_to(self.take_dir).as_posix(),
        }
        write_json_atomic(directory / "supplement.json", artifact)
        return artifact

    def save(
            self,
            *,
            supplement_id,
            metadata,
            events,
            actions,
            locator_bundle,
        ):
        directory = self.path_for(supplement_id)
        existing = _read_json(directory / "supplement.json")
        if existing.get("status") != "recording":
            raise ValueError(f"补录片段未处于 recording 状态: {supplement_id}")
        actions = [dict(action) for action in actions or ()]
        action_ids = [str(action.get("id") or "") for action in actions]
        if not action_ids or any(not action_id for action_id in action_ids):
            raise ValueError("补录片段必须包含带稳定 ID 的动作")
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("补录片段包含重复 action ID")
        artifact = {
            **existing,
            **dict(metadata or {}),
            "schema_version": SCHEMA_VERSION,
            "supplement_version": SUPPLEMENT_VERSION,
            "supplement_id": supplement_id,
            "status": "completed",
            "ended_at": datetime.now().isoformat(timespec="seconds"),
            "relative_path": directory.relative_to(self.take_dir).as_posix(),
            "event_count": len(events or ()),
            "action_count": len(actions),
        }
        write_json_atomic(directory / "supplement.json", artifact)
        RecordingSessionWriter._write_jsonl_path(
            directory / "events.jsonl",
            events or (),
        )
        write_json_atomic(directory / "actions.captured.json", {
            "schema_version": SCHEMA_VERSION,
            "supplement_version": SUPPLEMENT_VERSION,
            "actions": actions,
        })
        RecordingSessionWriter._write_yaml_path(
            directory / "locator-candidates.captured.yaml",
            locator_bundle or {
                "schema_version": SCHEMA_VERSION,
                "roots": {},
                "locators": {},
                "event_targets": [],
                "unresolved": [],
            },
        )
        return artifact

    def mark_terminal(self, supplement_id, status, error=None):
        if status not in {"discarded", "failed"}:
            raise ValueError(f"无效补录终态: {status}")
        path = self.path_for(supplement_id) / "supplement.json"
        artifact = _read_json(path)
        artifact.update({
            "status": status,
            "ended_at": datetime.now().isoformat(timespec="seconds"),
            "error": str(error or "") or None,
        })
        write_json_atomic(path, artifact)
        return artifact

    def list_supplements(self):
        if not self.root.exists():
            return []
        result = []
        for path in sorted(self.root.glob("supplement-*/supplement.json")):
            try:
                value = _read_json(path)
                self.path_for(value.get("supplement_id"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if value.get("status") == "completed":
                result.append(value)
        return result

    def load(self, supplement_id):
        path = self.path_for(supplement_id) / "supplement.json"
        value = _read_json(path)
        if (
            value.get("supplement_version") != SUPPLEMENT_VERSION
            or value.get("supplement_id") != supplement_id
            or value.get("status") != "completed"
        ):
            raise ValueError(f"补录片段无效: {supplement_id}")
        return value

    def load_actions(self, supplement_id):
        self.load(supplement_id)
        value = _read_json(
            self.path_for(supplement_id) / "actions.captured.json"
        )
        return [
            dict(action)
            for action in value.get("actions") or ()
            if isinstance(action, dict) and action.get("id")
        ]

    def load_events(self, supplement_id):
        self.load(supplement_id)
        path = self.path_for(supplement_id) / "events.jsonl"
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def load_locator_bundle(self, supplement_id):
        self.load(supplement_id)
        path = self.path_for(supplement_id) / "locator-candidates.captured.yaml"
        if not path.exists():
            return {}
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return value if isinstance(value, dict) else {}

    def path_for(self, supplement_id):
        supplement_id = str(supplement_id or "").strip().lower()
        if not _SUPPLEMENT_ID.fullmatch(supplement_id):
            raise ValueError(f"无效 supplement ID: {supplement_id!r}")
        path = (self.root / supplement_id).resolve()
        try:
            path.relative_to(self.root.resolve())
        except ValueError as error:
            raise ValueError(f"补录路径越界: {supplement_id}") from error
        return path


def _read_json(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 必须是 object: {path}")
    return value