from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path

from autowork_core.utils.debug_tools.recorder.identity import stable_digest
from autowork_core.utils.debug_tools.recorder.inspector import (
    event_target_from_binding,
)
from autowork_core.utils.debug_tools.recorder.raw_event_journal import (
    validate_capture_integrity,
)
from autowork_core.utils.debug_tools.recorder.run_lock import RunWriteLock
from autowork_core.utils.debug_tools.recorder.timeline import TimelineStore


TARGET_REPAIR_VERSION = "1.0"
MAX_FORENSIC_TREE_DELAY_MS = 50
_CONTAINER_TYPES = {
    "window",
    "pane",
    "group",
    "custom",
    "document",
    "menubar",
    "toolbar",
}
_ELEMENT_FIELDS = (
    "name",
    "auto_id",
    "control_type",
    "class_name",
    "framework_id",
    "handle",
    "process_id",
    "runtime_id",
    "enabled",
    "visible",
    "value",
    "rectangle",
)


class TargetRepairService:
    def __init__(self, take_dir):
        self.take_dir = Path(take_dir).resolve()
        self.timeline = TimelineStore(self.take_dir)

    def candidates(self, action_id):
        action = self._action(action_id)
        target_event_id = str(action.get("target_event_id") or "")
        events = self._events("events.jsonl")
        canonical_ids = [str(event.get("id") or "") for event in events]
        integrity = validate_capture_integrity(
            self.take_dir,
            canonical_event_ids=canonical_ids,
        )
        if integrity.get("status") != "complete":
            raise ValueError(
                "capture integrity failed: "
                + "; ".join(integrity.get("errors") or ())
            )
        raw_events = {
            str(event.get("id") or ""): event
            for event in self._events("raw-events.jsonl")
        }
        raw = raw_events.get(target_event_id)
        if raw is None or raw.get("event_type") not in {
            "mouse_down",
            "mouse_wheel",
        }:
            return ()
        point = tuple(raw.get("point") or ())
        if len(point) != 2:
            return ()
        trusted_artifacts = self._trusted_artifacts()
        result = []
        seen = set()
        for tree_path in self._tree_paths():
            tree_relative = tree_path.relative_to(self.take_dir).as_posix()
            expected_tree = trusted_artifacts.get(tree_relative)
            if expected_tree is None or any((
                expected_tree.get("sha256") != _sha256(tree_path),
                int(expected_tree.get("size") or -1)
                != tree_path.stat().st_size,
            )):
                raise ValueError(
                    f"trusted artifact changed: {tree_relative}"
                )
            tree = _read_json(tree_path)
            if any((
                not tree,
                tree.get("truncated") is not False,
                int(tree.get("window_handle") or 0)
                != int(raw.get("window_handle") or 0),
            )):
                continue
            delay_ms = _capture_delay_ms(raw.get("wall_time"), tree.get("captured_at"))
            if (
                delay_ms is None
                or abs(delay_ms) > MAX_FORENSIC_TREE_DELAY_MS
            ):
                continue
            nodes = list(tree.get("nodes") or ())
            leaves = [
                node for node in nodes
                if _eligible_leaf(
                    node,
                    point=point,
                    process_id=raw.get("process_id"),
                )
            ]
            if not leaves:
                continue
            max_depth = max(int(node.get("depth") or 0) for node in leaves)
            for node in leaves:
                if int(node.get("depth") or 0) != max_depth:
                    continue
                locator = _unique_locator(node, nodes)
                if locator is None:
                    continue
                element = {
                    key: node.get(key)
                    for key in _ELEMENT_FIELDS
                }
                binding = {
                    "status": "captured",
                    "element": element,
                }
                event_target = event_target_from_binding(
                    binding,
                    raw,
                    str(tree.get("backend") or "uia"),
                )
                if event_target is None:
                    continue
                key = json.dumps(locator, ensure_ascii=False, sort_keys=True)
                if key in seen:
                    continue
                seen.add(key)
                tree_sha256 = _sha256(tree_path)
                candidate_id = "target-repair-" + stable_digest(
                    action_id,
                    target_event_id,
                    tree_relative,
                    tree_sha256,
                    str(node.get("id") or ""),
                    key,
                    length=16,
                )
                result.append({
                    "target_repair_version": TARGET_REPAIR_VERSION,
                    "candidate_id": candidate_id,
                    "status": "forensic_verified",
                    "action_id": str(action_id),
                    "target_event_id": target_event_id,
                    "element": element,
                    "locator": locator,
                    "event_target": event_target,
                    "action_target": {
                        "quality": event_target.get("target_quality"),
                        "root_name": event_target.get("root_name"),
                        "element": element,
                        "suggested_action": event_target.get(
                            "suggested_action"
                        ) or {},
                    },
                    "evidence": {
                        "raw_event_id": target_event_id,
                        "raw_events_sha256": integrity.get(
                            "raw_events_sha256"
                        ),
                        "tree_path": tree_relative,
                        "tree_sha256": tree_sha256,
                        "tree_node_id": str(node.get("id") or ""),
                        "tree_delay_ms": delay_ms,
                        "window_handle": raw.get("window_handle"),
                        "process_id": raw.get("process_id"),
                        "point": list(point),
                    },
                })
        return tuple(sorted(result, key=lambda item: item["candidate_id"]))

    def repair_unique(self, action_id, *, expected_revision):
        run_directory = _run_directory(self.take_dir)
        lock = RunWriteLock(run_directory).acquire()
        try:
            self.timeline.require_revision(expected_revision)
            candidates = self.candidates(action_id)
            if not candidates:
                return {"status": "unavailable", "candidate_count": 0}
            if len(candidates) != 1:
                return {
                    "status": "ambiguous",
                    "candidate_count": len(candidates),
                    "candidate_ids": [
                        candidate["candidate_id"] for candidate in candidates
                    ],
                }
            state = self.timeline.apply_target_binding_repair(
                candidates[0],
                expected_revision=expected_revision,
            )
            _refresh_session_after_repair(run_directory, self.take_dir)
            return {
                "status": "applied",
                "candidate_count": 1,
                "candidate_id": candidates[0]["candidate_id"],
                "timeline_revision": state.get("timeline_revision"),
            }
        finally:
            lock.release()

    def _action(self, action_id):
        value = _read_json(self.take_dir / "actions.auto.json")
        matches = [
            action
            for action in value.get("actions") or ()
            if str(action.get("id") or "") == str(action_id)
        ]
        if len(matches) != 1:
            raise KeyError(f"target repair action不存在或不唯一: {action_id}")
        return matches[0]

    def _events(self, name):
        path = self.take_dir / name
        try:
            return [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"target repair无法读取{name}: {error}") from error

    def _tree_paths(self):
        paths = [
            self.take_dir / "ui" / "before-tree.json",
            self.take_dir / "ui" / "after-tree.json",
        ]
        windows = self.take_dir / "windows"
        if windows.exists():
            paths.extend(sorted(windows.glob("window-*/before-tree.json")))
            paths.extend(sorted(windows.glob("window-*/after-tree.json")))
        return [path for path in paths if path.is_file()]

    def _trusted_artifacts(self):
        snapshot = self.timeline.projections.current()
        graph_path = snapshot.path("evidence_graph") if snapshot else None
        graph = _read_json(graph_path) if graph_path is not None else {}
        artifacts = (graph.get("source") or {}).get("artifacts") or ()
        result = {
            str(artifact.get("path") or ""): artifact
            for artifact in artifacts
            if artifact.get("path")
        }
        if not result:
            raise ValueError("target repair缺少可信Evidence Graph")
        return result


def _eligible_leaf(node, *, point, process_id):
    rectangle = node.get("rectangle") or ()
    if len(rectangle) != 4 or not (
        int(rectangle[0]) <= int(point[0]) < int(rectangle[2])
        and int(rectangle[1]) <= int(point[1]) < int(rectangle[3])
    ):
        return False
    if process_id is not None and int(node.get("process_id") or 0) != int(
            process_id
        ):
        return False
    if node.get("visible") is False or node.get("enabled") is False:
        return False
    control_type = str(node.get("control_type") or "").casefold()
    return bool(control_type) and control_type not in _CONTAINER_TYPES


def _unique_locator(node, nodes):
    control_type = str(node.get("control_type") or "")
    candidates = (
        ("auto_id", str(node.get("auto_id") or "")),
        ("name", str(node.get("name") or "")),
    )
    for key, value in candidates:
        if not value:
            continue
        matches = [
            item for item in nodes
            if str(item.get("control_type") or "") == control_type
            and str(item.get(key) or "") == value
        ]
        if len(matches) == 1:
            return {
                "control_type": control_type,
                key: value,
            }
    return None


def _capture_delay_ms(event_time, captured_at):
    try:
        event = datetime.fromisoformat(str(event_time))
        captured = datetime.fromisoformat(str(captured_at))
        return int(round((captured - event).total_seconds() * 1000))
    except (TypeError, ValueError):
        return None


def _read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _run_directory(take_dir):
    take_dir = Path(take_dir).resolve()
    for directory in (take_dir, *take_dir.parents):
        if (directory / "manifest.json").is_file():
            return directory
    return take_dir


def _refresh_session_after_repair(run_directory, take_dir):
    run_directory = Path(run_directory).resolve()
    if not (run_directory / "manifest.json").is_file():
        return None
    from autowork_core.utils.debug_tools.recorder.session import (
        FeatureRecordingSession,
    )

    session = FeatureRecordingSession.open_existing(run_directory)
    return session.refresh_after_timeline_edit(take_dir)