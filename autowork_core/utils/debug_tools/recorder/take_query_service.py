from __future__ import annotations

import json
from pathlib import Path

from autowork_core.utils.debug_tools.recorder.action_media import (
    load_action_media,
)
from autowork_core.utils.debug_tools.recorder.diagnostics import (
    diagnostic_event_ids,
)
from autowork_core.utils.debug_tools.recorder.projection_store import (
    resolve_take_artifact,
)


class TakeQueryService:
    """Read-only artifact boundary for Take-oriented views."""

    def __init__(self, take_dir):
        self.take_dir = Path(take_dir).resolve()

    def media_index(self):
        media_path = resolve_take_artifact(
            self.take_dir,
            "media_index",
        )
        return self._read_json_path(media_path) if media_path is not None else {}

    def media_bundle(self):
        return self.media_index(), load_action_media(self.take_dir)

    def capture_backend(self):
        return str(
            self._read_json("ui/before-tree.json").get("backend") or "uia"
        )

    def evidence_graph(self):
        path = resolve_take_artifact(
            self.take_dir,
            "evidence_graph",
        )
        return (
            self._read_json_path(path)
            if path is not None and path.exists()
            else None
        )

    def effective_observation_events(self):
        path = resolve_take_artifact(
            self.take_dir,
            "events_effective",
        )
        if path is None or not path.exists():
            return ()
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return ()
        result = []
        for line in lines:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                return ()
            if (
                isinstance(event, dict)
                and event.get("event_type") == "observation"
            ):
                result.append(event)
        return tuple(result)

    def diagnostic_evidence_path(self, diagnostic):
        if diagnostic is None:
            return self.take_dir
        event_ids = set(diagnostic_event_ids([diagnostic]))
        if event_ids:
            media = self.media_index()
            for item in media.get("events") or []:
                if item.get("event_id") not in event_ids:
                    continue
                screenshot = item.get("screenshot")
                if screenshot:
                    path = self.path(screenshot, must_exist=False)
                    if path.exists():
                        return path
        relative = {
            "tree_not_comparable": "ui/tree-diff.json",
            "capture_error": "take.json",
        }.get(diagnostic.get("code"))
        if relative:
            path = self.path(relative, must_exist=False)
            if path.exists():
                return path
        return self.take_dir

    def path(self, relative_path, *, must_exist=True):
        path = (self.take_dir / str(relative_path)).resolve()
        try:
            path.relative_to(self.take_dir)
        except ValueError as error:
            raise ValueError(f"Take artifact 路径越界: {relative_path}") from error
        if must_exist and not path.exists():
            raise FileNotFoundError(path)
        return path

    def _read_json(self, relative_path):
        path = self.path(relative_path, must_exist=False)
        return self._read_json_path(path)

    @staticmethod
    def _read_json_path(path):
        if not path.exists():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}