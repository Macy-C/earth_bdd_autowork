from __future__ import annotations

import json
import threading
from datetime import datetime
from functools import wraps
from pathlib import Path

from autowork_core.utils.debug_tools.recorder.action_media import (
    build_action_media,
)
from autowork_core.utils.debug_tools.recorder.analysis import (
    build_locator_bundle,
    derive_actions,
)
from autowork_core.utils.debug_tools.recorder.annotations import (
    ANNOTATION_MODEL_VERSION,
    RecordingAnnotationRepository,
    SYSTEM_INFERRED_INTENT,
)
from autowork_core.utils.debug_tools.recorder.capture_runtime import (
    CaptureRuntime,
    load_capture_config,
)
from autowork_core.utils.debug_tools.recorder.input_capture import (
    InputCaptureEngine,
)
from autowork_core.utils.debug_tools.recorder.models import (
    SCHEMA_VERSION,
    public_dict,
)
from autowork_core.utils.debug_tools.recorder.supplement_repository import (
    SupplementRepository,
)
from autowork_core.utils.debug_tools.recorder.tree_snapshot import (
    capture_tree_snapshot,
)
from autowork_core.utils.debug_tools.recorder.video import StepVideoRecorder
from autowork_core.utils.debug_tools.recorder.writer import write_json_atomic


def _lifecycle_locked(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lifecycle_lock:
            return method(self, *args, **kwargs)

    return wrapped


class SupplementCaptureSession:
    def __init__(
            self,
            take_dir,
            *,
            backend=None,
            windows=(),
            label="",
            with_video=None,
            with_screenshots=None,
            with_tree=None,
            monitor_index=None,
            capture_factory=InputCaptureEngine,
            video_factory=StepVideoRecorder,
            runtime_factory=CaptureRuntime,
            target_hover=False,
            hover_notification=None,
        ):
        self.take_dir = Path(take_dir).resolve()
        self.capture_config = load_capture_config(self.take_dir)
        self.backend = str(
            backend or self.capture_config.get("backend") or "uia"
        )
        self.windows = [_public_window(window) for window in windows or ()]
        self.label = str(label or "").strip()
        self.with_video = bool(
            self.capture_config.get("with_video", True)
            if with_video is None
            else with_video
        )
        self.with_screenshots = bool(
            self.capture_config.get("with_screenshots", True)
            if with_screenshots is None
            else with_screenshots
        )
        self.with_tree = bool(
            self.capture_config.get("with_tree", True)
            if with_tree is None
            else with_tree
        )
        self.tree_max_depth = int(
            self.capture_config.get("tree_max_depth", 12)
        )
        self.tree_max_nodes = int(
            self.capture_config.get("tree_max_nodes", 1200)
        )
        self.monitor_index = max(1, int(
            monitor_index
            if monitor_index is not None
            else self.capture_config.get("monitor_index", 1)
        ))
        self.window_capture_mode = str(
            self.capture_config.get("window_capture_mode") or "strict"
        ).strip().lower()
        self.capture_target_process_only = bool(
            self.capture_config.get("capture_target_process_only", True)
        )
        self.capture_factory = capture_factory
        self.video_factory = video_factory
        self.runtime_factory = runtime_factory
        self.target_hover = bool(target_hover)
        self.hover_notification = hover_notification
        self.repository = SupplementRepository(self.take_dir)
        self.supplement_id = self.repository.allocate_id()
        self.directory = self.repository.path_for(self.supplement_id)
        self.runtime = None
        self.started_at = None
        self.ended_at = None
        self._running = False
        self._lifecycle_lock = threading.RLock()
        self._close_requested = threading.Event()
        self._terminal_status = None
        self._terminal_artifact = None
        self._window_trees = {}
        take_path = self.take_dir / "take.json"
        take_metadata = (
            json.loads(take_path.read_text(encoding="utf-8"))
            if take_path.is_file()
            else {}
        )
        self.step_id = str(
            (take_metadata.get("step") or {}).get("id") or ""
        )
        self.annotation_repository = RecordingAnnotationRepository(
            self.directory
        )
        self._observation_receipts = {}
        self._observation_intent_errors = []

    @property
    def is_running(self):
        return self._running

    @property
    def is_paused(self):
        return bool(self.runtime is not None and self.runtime.is_paused)

    @property
    def event_count(self):
        return self.runtime.event_count if self.runtime is not None else 0

    @_lifecycle_locked
    def start(self):
        if self._running or self.runtime is not None:
            raise RuntimeError("补录片段不能重复启动")
        if self._terminal_status is not None:
            raise RuntimeError("补录片段已经结束")
        if self._close_requested.is_set():
            raise RuntimeError("补录片段已经关闭")
        self.repository.reserve(self.supplement_id, {
            "label": self.label,
            "backend": self.backend,
            "target_windows": self.windows,
        })
        for window in self.windows:
            handle = window.get("handle")
            if handle is not None:
                self._capture_window_tree(int(handle), "before")
        handles = tuple(
            int(window["handle"])
            for window in self.windows
            if window.get("handle") is not None
        )
        process_ids = tuple(sorted({
            int(window["process_id"])
            for window in self.windows
            if window.get("process_id") is not None
        }))
        try:
            self.runtime = self.runtime_factory(
                self.directory,
                backend=self.backend,
                with_video=self.with_video,
                with_screenshots=self.with_screenshots,
                monitor_index=self.monitor_index,
                allowed_process_ids=(
                    process_ids if self.capture_target_process_only else ()
                ),
                selected_window_handles=handles,
                window_capture_mode=self.window_capture_mode,
                process_filter_enabled=self.capture_target_process_only,
                on_window_discovered=self._capture_discovered_window,
                event_id_prefix=f"{self.supplement_id}-",
                video_title="Timeline supplement",
                video_label=self.label or self.supplement_id,
                capture_factory=self.capture_factory,
                video_factory=self.video_factory,
                target_hover=self.target_hover,
                hover_notification=self.hover_notification,
            )
            self.runtime.start()
            if self._close_requested.is_set():
                self._discard_locked()
                raise RuntimeError("补录片段启动期间已关闭")
        except Exception as error:
            if self._terminal_status == "discarded":
                raise
            self._terminal_status = "failed"
            self._terminal_artifact = self.repository.mark_terminal(
                self.supplement_id,
                self._terminal_status,
                f"start: {type(error).__name__}: {error}",
            )
            raise
        self.started_at = datetime.now().isoformat(timespec="milliseconds")
        self._running = True
        return self

    @_lifecycle_locked
    def finish(self):
        if not self._running or self.runtime is None:
            raise RuntimeError("补录片段尚未开始")
        try:
            result = self.runtime.stop()
            self._record_observation_receipts(
                self._drain_observation_notifications()
            )
            events = result.events
            _namespace_events(events, self.supplement_id)
            actions = derive_actions(events)
            for action in actions:
                action.setdefault("role", "business")
            if not actions:
                raise ValueError("没有捕获到完整动作；请至少完成一次点击或按键")
            for lifecycle in result.window_lifecycle:
                handle = lifecycle.get("handle")
                if handle is None:
                    continue
                self._capture_window_tree(int(handle), "before")
                self._capture_window_tree(int(handle), "after")
            tree_snapshots = [
                value[stage]
                for value in self._window_trees.values()
                for stage in ("before", "after")
                if value.get(stage)
            ]
            locator_bundle = build_locator_bundle(
                events,
                tree_snapshots=tree_snapshots,
            )
            self.ended_at = datetime.now().isoformat(timespec="milliseconds")
            metadata = {
                "annotation_model_version": ANNOTATION_MODEL_VERSION,
                "label": self.label,
                "started_at": self.started_at,
                "ended_at": self.ended_at,
                "backend": self.backend,
                "target_windows": self.windows,
                "window_lifecycle": result.window_lifecycle,
                "video": "step.mp4" if result.video_path is not None else None,
                "capture_error": "; ".join([
                    *result.errors,
                    *[
                        "ObservationIntent保存失败: "
                        f"{item['event_id']}: {item['error']}"
                        for item in self._observation_intent_errors
                    ],
                ]) or None,
                **(
                    {"capture_integrity": result.capture_integrity}
                    if result.capture_integrity
                    else {}
                ),
                "capture_config": {
                    "with_video": self.with_video,
                    "with_screenshots": self.with_screenshots,
                    "with_tree": self.with_tree,
                    "monitor_index": self.monitor_index,
                    "capture_target_process_only": (
                        self.capture_target_process_only
                    ),
                    "window_capture_mode": self.window_capture_mode,
                },
                "window_trees": [
                    {
                        "handle": handle,
                        "before": value.get("before_path"),
                        "after": value.get("after_path"),
                    }
                    for handle, value in sorted(self._window_trees.items())
                ],
                "timeline": result.timeline_metadata(
                    "supplement_input_capture_start"
                ),
            }
            artifact = self.repository.save(
                supplement_id=self.supplement_id,
                metadata=metadata,
                events=events,
                actions=actions,
                locator_bundle=locator_bundle,
            )
            action_media = build_action_media(
                self.directory,
                actions,
                events,
                metadata=metadata,
            )
            write_json_atomic(self.directory / "media-index.json", {
                "schema_version": SCHEMA_VERSION,
                "timebase": "milliseconds",
                "video": (
                    {"path": "step.mp4"}
                    if result.video_path is not None
                    else None
                ),
                "events": [
                    _event_media(event, result.video_to_event_offset_ms)
                    for event in events
                ],
                "action_media": "action-media.json",
                "actions": action_media.get("actions") or [],
                "action_contact_sheet": action_media.get("contact_sheet"),
            })
            artifact["actions"] = actions
            artifact["path"] = str(self.directory)
            self._terminal_status = "completed"
            self._terminal_artifact = artifact
            return artifact
        except Exception as error:
            self._terminal_status = "failed"
            self._terminal_artifact = self.repository.mark_terminal(
                self.supplement_id,
                self._terminal_status,
                f"finish: {type(error).__name__}: {error}",
            )
            raise
        finally:
            self._running = False

    def _capture_discovered_window(self, lifecycle):
        handle = lifecycle.get("handle")
        if handle is None:
            return {}
        handle = int(handle)
        is_cancelled = lifecycle.get("_capture_cancelled") or (lambda: False)
        commit = lifecycle.get("_capture_commit") or (
            lambda callback: (callback(), True)[1]
        )
        existing = self._window_trees.get(handle) or {}
        if existing.get("before") is not None:
            return {
                "before_tree": existing.get("before_path"),
                "capture_error": existing["before"].get("error"),
            }
        tree = self._capture_tree_data(handle)
        if is_cancelled():
            return {}
        if not commit(
            lambda: self._publish_window_tree(handle, "before", tree)
        ):
            return {}
        value = self._window_trees.get(int(handle)) or {}
        return {
            "before_tree": value.get("before_path"),
            "capture_error": tree.get("error") if tree else None,
        }

    def _capture_window_tree(self, handle, stage):
        handle = int(handle)
        value = self._window_trees.setdefault(handle, {})
        if value.get(stage) is not None:
            return value[stage]
        tree = self._capture_tree_data(handle)
        self._publish_window_tree(handle, stage, tree)
        return tree

    def _capture_tree_data(self, handle):
        if self.with_tree:
            return capture_tree_snapshot(
                backend=self.backend,
                window_handle=handle,
                max_depth=self.tree_max_depth,
                max_nodes=self.tree_max_nodes,
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "window_handle": handle,
            "nodes": [],
            "disabled": True,
            "error": None,
        }

    def _publish_window_tree(self, handle, stage, tree):
        value = self._window_trees.setdefault(int(handle), {})
        relative = Path("trees") / f"window-{handle}" / f"{stage}.json"
        write_json_atomic(self.directory / relative, tree)
        value[stage] = tree
        value[f"{stage}_path"] = relative.as_posix()

    @_lifecycle_locked
    def discard(self):
        self._close_requested.set()
        return self._discard_locked()

    def request_close(self):
        self._close_requested.set()
        if not self._lifecycle_lock.acquire(blocking=False):
            return None
        try:
            return self._discard_locked()
        finally:
            self._lifecycle_lock.release()

    def _discard_locked(self):
        if self._terminal_status is not None:
            return self._terminal_artifact
        if self.runtime is not None:
            self.runtime.abort()
        self._running = False
        self._terminal_status = "discarded"
        if self.directory.is_dir():
            self._terminal_artifact = self.repository.mark_terminal(
                self.supplement_id,
                self._terminal_status,
            )
        else:
            self._terminal_artifact = {
                "supplement_id": self.supplement_id,
                "status": self._terminal_status,
            }
        return self._terminal_artifact

    def toggle_pause(self, note=""):
        if not self._running or self.runtime is None:
            raise RuntimeError("补录片段尚未开始")
        self.runtime.toggle_pause(note="")
        return self.runtime.is_paused

    def record_observation(self, point=None, note=""):
        if not self._running or self.runtime is None:
            raise RuntimeError("补录片段尚未开始")
        return self.runtime.record_observation(point=point, note=note)

    def drain_window_notifications(self):
        return (
            self.runtime.drain_window_notifications()
            if self.runtime is not None
            else []
        )

    def drain_observation_notifications(self):
        receipts = self._drain_observation_notifications()
        return self._record_observation_receipts(receipts)

    def _drain_observation_notifications(self):
        method = getattr(
            self.runtime,
            "drain_observation_notifications",
            None,
        )
        value = method() if callable(method) else []
        return list(value) if isinstance(value, (list, tuple)) else []

    def _record_observation_receipts(self, receipts):
        result = []
        for source in receipts or ():
            receipt = dict(source or {})
            event_id = str(receipt.get("event_id") or "").strip()
            if event_id:
                self._observation_receipts[event_id] = receipt
            if receipt.get("status") in {"captured", "warning"} and event_id:
                try:
                    if not self.step_id:
                        raise ValueError("补录所属Take缺少step_id")
                    intent = self.annotation_repository.current_observation_intent(
                        self.step_id,
                        self.supplement_id,
                        event_id,
                    )
                    if intent is None:
                        structured = receipt.get("structured_observation") or {}
                        is_collection = bool(structured.get("provider"))
                        intent = self.annotation_repository.append_observation_intent(
                            self.step_id,
                            self.supplement_id,
                            event_id,
                            focus="collection" if is_collection else "auto",
                            expected_source={
                                "kind": (
                                    "observed_state"
                                    if is_collection
                                    else "auto"
                                )
                            },
                            authority=SYSTEM_INFERRED_INTENT,
                            expected_revision=0,
                        )
                    receipt["observation_intent"] = intent
                    self._observation_receipts[event_id] = receipt
                except Exception as error:
                    message = f"{type(error).__name__}: {error}"
                    receipt["observation_intent_error"] = message
                    self._observation_intent_errors.append({
                        "event_id": event_id,
                        "error": message,
                    })
            result.append(receipt)
        return result


def _namespace_events(events, supplement_id):
    event_ids = {}
    prefix = f"{supplement_id}-"
    for event in events:
        original = str(event.id)
        event.id = original if original.startswith(prefix) else f"{prefix}{original}"
        event_ids[original] = event.id
    for event in events:
        event.details = _replace_ids(event.details, event_ids)
        event.target = _replace_ids(event.target, event_ids)


def _public_window(window):
    return {
        "handle": (
            int(window["handle"])
            if window.get("handle") is not None
            else None
        ),
        "process_id": (
            int(window["process_id"])
            if window.get("process_id") is not None
            else None
        ),
        "title": str(window.get("title") or ""),
        "class_name": str(window.get("class_name") or ""),
    }


def _replace_ids(value, event_ids):
    if isinstance(value, str):
        return event_ids.get(value, value)
    if isinstance(value, list):
        return [_replace_ids(item, event_ids) for item in value]
    if isinstance(value, tuple):
        return tuple(_replace_ids(item, event_ids) for item in value)
    if isinstance(value, dict):
        return {
            key: _replace_ids(item, event_ids)
            for key, item in value.items()
        }
    return value


def _event_media(event, video_offset):
    event = public_dict(event)
    event_ms = event.get("monotonic_ms")
    details = event.get("details") or {}
    return {
        "event_id": event.get("id"),
        "event_type": event.get("event_type"),
        "event_ms": event_ms,
        "video_ms": (
            int(video_offset + event_ms)
            if video_offset is not None and event_ms is not None
            else None
        ),
        "screenshot": event.get("screenshot"),
        "screenshot_ms": details.get("screenshot_monotonic_ms"),
        "screenshot_latency_ms": details.get("screenshot_latency_ms"),
        "point": event.get("point"),
        "frames": details.get("frames") or [],
    }