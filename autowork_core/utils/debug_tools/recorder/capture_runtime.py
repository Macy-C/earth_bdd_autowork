from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from autowork_core.utils.debug_tools.recorder.input_capture import (
    InputCaptureEngine,
)
from autowork_core.utils.debug_tools.recorder.raw_event_journal import (
    validate_capture_integrity,
)
from autowork_core.utils.debug_tools.recorder.target_hover import (
    CursorHoverController,
)
from autowork_core.utils.debug_tools.recorder.video import StepVideoRecorder


def load_capture_config(artifact_dir):
    artifact_dir = Path(artifact_dir).resolve()
    for directory in (artifact_dir, *artifact_dir.parents):
        path = directory / "manifest.json"
        if not path.exists():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        config = value.get("capture_config") or {}
        return dict(config) if isinstance(config, dict) else {}
    return {}


@dataclass(frozen=True)
class CaptureRuntimeResult:
    events: list
    video_path: Path | None
    errors: tuple[str, ...]
    window_lifecycle: list[dict]
    input_started_at: str | None
    video_to_event_offset_ms: int | None
    capture_integrity: dict = field(default_factory=dict)
    timings_ms: dict[str, float] = field(default_factory=dict)

    def timeline_metadata(self, event_time_origin):
        return {
            "event_time_origin": str(event_time_origin),
            "input_started_at": self.input_started_at,
            "sync_method": "shared_process_monotonic_clock",
            "sync_accuracy": "estimated",
            "video_to_event_offset_ms": self.video_to_event_offset_ms,
        }


class CaptureRuntime:
    def __init__(
            self,
            artifact_dir,
            *,
            backend="uia",
            with_video=True,
            with_screenshots=True,
            monitor_index=1,
            ignore_process_ids=None,
            allowed_process_ids=(),
            selected_window_handles=(),
            window_capture_mode="strict",
            process_filter_enabled=True,
            on_window_discovered=None,
            event_id_prefix="",
            video_title="Recorder",
            video_label="Capture",
            capture_factory=InputCaptureEngine,
            video_factory=StepVideoRecorder,
            target_hover=False,
            hover_notification=None,
            hover_factory=CursorHoverController,
        ):
        self.artifact_dir = Path(artifact_dir).resolve()
        self.backend = str(backend or "uia")
        self.with_video = bool(with_video)
        self.with_screenshots = bool(with_screenshots)
        self.monitor_index = max(1, int(monitor_index or 1))
        self.ignore_process_ids = tuple(
            ignore_process_ids
            if ignore_process_ids is not None
            else (os.getpid(),)
        )
        self.allowed_process_ids = tuple(int(value) for value in allowed_process_ids)
        self.selected_window_handles = tuple(
            int(value) for value in selected_window_handles
        )
        self.window_capture_mode = str(
            window_capture_mode or "strict"
        ).strip().lower()
        self.process_filter_enabled = bool(process_filter_enabled)
        self.on_window_discovered = on_window_discovered
        self.event_id_prefix = str(event_id_prefix or "")
        self.video_title = str(video_title or "Recorder")
        self.video_label = str(video_label or "Capture")
        self.capture_factory = capture_factory
        self.video_factory = video_factory
        self.target_hover_enabled = bool(target_hover)
        self.hover_notification = hover_notification
        self.hover_factory = hover_factory
        self.capture = None
        self.video = None
        self.target_hover = None
        self.video_started_monotonic = None
        self._running = False

    @property
    def is_running(self):
        return self._running

    @property
    def is_paused(self):
        return bool(
            self.capture is not None
            and getattr(self.capture, "is_paused", False)
        )

    @property
    def event_count(self):
        return int(getattr(self.capture, "event_count", 0) or 0)

    def start(self):
        if self._running or self.capture is not None:
            raise RuntimeError("采集运行时不能重复启动")
        try:
            if self.with_video:
                self.video = self.video_factory(self.artifact_dir)
                self.video_started_monotonic = time.monotonic()
                self.video.start(self.video_title, self.video_label)
            self.capture = self.capture_factory(
                backend=self.backend,
                artifact_dir=(
                    self.artifact_dir if self.with_screenshots else None
                ),
                journal_dir=self.artifact_dir,
                event_id_prefix=self.event_id_prefix,
                monitor_index=self.monitor_index,
                ignore_process_ids=self.ignore_process_ids,
                allowed_process_ids=self.allowed_process_ids,
                selected_window_handles=self.selected_window_handles,
                window_capture_mode=self.window_capture_mode,
                on_window_discovered=self.on_window_discovered,
                process_filter_enabled=self.process_filter_enabled,
            )
            self.capture.start()
            self._start_target_hover()
        except Exception:
            self.abort()
            raise
        self._running = True
        return self

    def stop(self):
        if not self._running or self.capture is None:
            raise RuntimeError("采集运行时尚未启动")
        stop_started = time.perf_counter()
        timings_ms = {}
        errors = []
        self._stop_target_hover()
        input_stop_error = None
        input_started = time.perf_counter()
        if self.is_paused:
            try:
                self.capture.resume(note="")
            except Exception as error:
                errors.append(f"input resume: {type(error).__name__}: {error}")
        try:
            events = self.capture.stop()
        except Exception as error:
            events = []
            input_stop_error = error
            errors.append(f"input stop: {type(error).__name__}: {error}")
        timings_ms["input_stop"] = round(
            (time.perf_counter() - input_started) * 1000,
            3,
        )
        capture_error = getattr(self.capture, "error", None)
        if capture_error is not None:
            errors.append(
                f"input capture: {type(capture_error).__name__}: "
                f"{capture_error}"
            )
        video_path = None
        if self.video is not None:
            video_started = time.perf_counter()
            try:
                video_path = self.video.stop()
            except Exception as error:
                errors.append(f"video stop: {type(error).__name__}: {error}")
                self.video.abort()
            timings_ms["video_stop"] = round(
                (time.perf_counter() - video_started) * 1000,
                3,
            )
        else:
            timings_ms["video_stop"] = 0.0
        timings_ms["capture_stop_total"] = round(
            (time.perf_counter() - stop_started) * 1000,
            3,
        )
        self._running = False
        if input_stop_error is not None:
            raise RuntimeError(
                "输入采集未完整停止，拒绝返回部分事件: "
                f"{type(input_stop_error).__name__}: {input_stop_error}"
            ) from input_stop_error

        video_offset = self._video_offset(video_path)
        event_list = list(events or ())
        if (self.artifact_dir / "raw-events.seal.json").exists():
            capture_integrity = validate_capture_integrity(
                self.artifact_dir,
                [
                    str(
                        event.get("id")
                        if isinstance(event, dict)
                        else getattr(event, "id", "")
                    )
                    for event in event_list
                ],
            )
            if capture_integrity.get("status") != "complete":
                raise RuntimeError(
                    "采集完整性复核失败: "
                    + "; ".join(capture_integrity.get("errors") or ())
                )
        elif self.capture_factory is InputCaptureEngine:
            raise RuntimeError(
                "InputCaptureEngine 未生成 raw event seal，拒绝提交新 Take"
            )
        else:
            capture_integrity = {
                "status": "legacy_unavailable",
                "raw_event_count": None,
                "canonical_event_count": len(event_list),
            }
        lifecycle = getattr(self.capture, "window_lifecycle", [])
        if not isinstance(lifecycle, (list, tuple)):
            lifecycle = []
        result = CaptureRuntimeResult(
            events=event_list,
            video_path=Path(video_path) if video_path is not None else None,
            errors=tuple(errors),
            window_lifecycle=[dict(item) for item in lifecycle],
            input_started_at=getattr(self.capture, "started_at", None),
            video_to_event_offset_ms=video_offset,
            capture_integrity=capture_integrity,
            timings_ms=timings_ms,
        )
        return result

    def abort(self, timeout=2):
        self._stop_target_hover()
        if self.capture is not None:
            try:
                self.capture.stop(timeout=timeout)
            except TypeError:
                try:
                    self.capture.stop()
                except Exception:
                    pass
            except Exception:
                pass
        if self.video is not None:
            try:
                self.video.abort()
            except Exception:
                pass
        self._running = False

    def pause(self, note=""):
        self._require_running()
        result = self.capture.pause(note=note)
        self._set_target_hover_enabled(False)
        return result

    def resume(self, note=""):
        self._require_running()
        result = self.capture.resume(note=note)
        self._set_target_hover_enabled(True)
        refresh = getattr(self.capture, "refresh_hover_target", None)
        if callable(refresh):
            refresh(force=True)
        return result

    def toggle_pause(self, note=""):
        return self.resume(note=note) if self.is_paused else self.pause(note=note)

    def record_observation(self, point=None, note="", provider=None):
        self._require_running()
        if provider:
            return self.capture.record_observation(
                point=point,
                note=note,
                provider=provider,
            )
        return self.capture.record_observation(point=point, note=note)

    def drain_window_notifications(self):
        return self._drain("drain_window_notifications")

    def drain_observation_notifications(self):
        return self._drain("drain_observation_notifications")

    def _drain(self, method_name):
        method = getattr(self.capture, method_name, None)
        value = method() if callable(method) else []
        return list(value) if isinstance(value, (list, tuple)) else []

    def _require_running(self):
        if not self._running or self.capture is None:
            raise RuntimeError("采集运行时尚未启动")

    def _start_target_hover(self):
        if (
                not self.target_hover_enabled
                or self.capture is None
                or not callable(self.hover_notification)
            ):
            return
        context_provider = getattr(self.capture, "hover_context", None)
        set_handler = getattr(self.capture, "set_hover_handler", None)
        if not callable(context_provider) or not callable(set_handler):
            return
        try:
            hover = self.hover_factory(
                self.backend,
                context_provider,
                self.hover_notification,
            )
            hover.start()
            set_handler(hover.submit)
        except Exception:
            try:
                set_handler(None)
            except Exception:
                pass
            self.target_hover = None
            return
        self.target_hover = hover
        refresh = getattr(self.capture, "refresh_hover_target", None)
        if callable(refresh):
            refresh(force=True)

    def _stop_target_hover(self):
        capture = self.capture
        set_handler = getattr(capture, "set_hover_handler", None)
        if callable(set_handler):
            try:
                set_handler(None)
            except Exception:
                pass
        hover = self.target_hover
        if hover is None:
            return
        try:
            hover.stop()
        except Exception:
            pass
        self.target_hover = None

    def _set_target_hover_enabled(self, enabled):
        hover = self.target_hover
        if hover is None:
            return
        try:
            hover.set_enabled(enabled)
        except Exception:
            pass

    def _video_offset(self, video_path):
        capture_started = getattr(self.capture, "started_monotonic", None)
        if (
                video_path is None
                or capture_started is None
                or self.video_started_monotonic is None
        ):
            return None
        return int((capture_started - self.video_started_monotonic) * 1000)