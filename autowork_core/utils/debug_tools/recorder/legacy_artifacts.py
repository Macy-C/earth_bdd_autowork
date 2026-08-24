from __future__ import annotations

import json
from pathlib import Path

import yaml

from autowork_core.utils.debug_tools.recorder.models import SCHEMA_VERSION
from autowork_core.utils.debug_tools.recorder.writer import (
    RecordingSessionWriter,
    write_json_atomic,
)


LEGACY_ACTIONS_NAME = "actions.json"
LEGACY_LOCATORS_NAME = "locator-candidates.yaml"


def migrate_legacy_timeline_artifacts(take_dir, timeline_protocol_version):
    take_dir = Path(take_dir)
    migrated = []
    actions_auto = take_dir / "actions.auto.json"
    legacy_actions = take_dir / LEGACY_ACTIONS_NAME
    if not actions_auto.exists() and legacy_actions.exists():
        value = json.loads(legacy_actions.read_text(encoding="utf-8"))
        write_json_atomic(actions_auto, {
            "schema_version": value.get("schema_version", SCHEMA_VERSION),
            "timeline_protocol_version": timeline_protocol_version,
            "source": "migrated_legacy",
            "actions": value.get("actions") or [],
        })
        migrated.append(actions_auto)

    locators_auto = take_dir / "locator-candidates.auto.yaml"
    legacy_locators = take_dir / LEGACY_LOCATORS_NAME
    if not locators_auto.exists() and legacy_locators.exists():
        value = yaml.safe_load(
            legacy_locators.read_text(encoding="utf-8")
        ) or {}
        RecordingSessionWriter._write_yaml_path(
            locators_auto,
            {**value, "source": "migrated_legacy"},
        )
        migrated.append(locators_auto)
    return migrated


def legacy_actions_path(take_dir):
    return Path(take_dir) / LEGACY_ACTIONS_NAME


def legacy_locators_path(take_dir):
    return Path(take_dir) / LEGACY_LOCATORS_NAME