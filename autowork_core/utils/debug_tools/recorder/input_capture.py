from __future__ import annotations

import atexit
import heapq
import queue
import threading
import time
import ctypes
from ctypes import c_int, windll, wintypes
from datetime import datetime
from pathlib import Path

import win32gui
import win32process
import pythoncom
from mss import mss
import psutil
from pywinauto.win32_hooks import HOOKCB, Hook, KeyboardEvent, MouseEvent
from pywinauto.win32structures import MSLLHOOKSTRUCT

from autowork_core.utils.debug_tools.recorder.event_target import (
    DEFAULT_EVENT_TARGET_TIMEOUT_MS,
    EventTargetResolver,
)
from autowork_core.utils.debug_tools.recorder.inspector import (
    UIAInspector,
    event_target_from_binding,
)
from autowork_core.utils.debug_tools.recorder.models import RecordingEvent
from autowork_core.utils.debug_tools.recorder.raw_event_journal import (
    RawEventJournal,
    write_capture_completion,
)
from autowork_core.utils.debug_tools.recorder.observation_repository import (
    write_observation_receipt,
)


_STOP = object()
_RAW_JOURNAL_STOP = object()
_WINDOW_EVIDENCE_STOP = object()


class _CaptureCommitGate:
    def __init__(self):
        self._lock = threading.Lock()
        self._cancelled = False

    def is_cancelled(self):
        with self._lock:
            return self._cancelled

    def commit(self, callback):
        with self._lock:
            if self._cancelled:
                return False
            callback()
            return True

    def cancel(self):
        with self._lock:
            self._cancelled = True


def _configure_64bit_hook_api():
    hook_args = [c_int, HOOKCB, wintypes.HINSTANCE, wintypes.DWORD]
    for function_name in ("SetWindowsHookExA", "SetWindowsHookExW"):
        function = getattr(windll.user32, function_name)
        function.argtypes = hook_args
        function.restype = wintypes.HHOOK
    windll.user32.CallNextHookEx.argtypes = [
        wintypes.HHOOK,
        c_int,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    windll.user32.CallNextHookEx.restype = wintypes.LPARAM
    windll.user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
    windll.user32.UnhookWindowsHookEx.restype = wintypes.BOOL


_configure_64bit_hook_api()


class RecorderHook(Hook):
    def listen(self):
        atexit.register(
            windll.user32.UnhookWindowsHookEx,
            self.keyboard_id,
        )
        atexit.register(
            windll.user32.UnhookWindowsHookEx,
            self.mouse_id,
        )
        while self.is_hooked():
            self._process_win_msgs()
            time.sleep(0.005)

    def _mouse_ll_hdl(self, code, event_code, mouse_data_ptr):
        result = windll.user32.CallNextHookEx(
            self.mouse_id,
            code,
            event_code,
            mouse_data_ptr,
        )
        if not self.handler:
            return result
        event_code_word = 0xFFFFFFFF & event_code
        current_key = self.MOUSE_ID_TO_KEY.get(event_code_word)
        if current_key is None:
            return result
        move_handler = getattr(self, "move_handler", None)
        if current_key == "Move" and move_handler is None:
            return result
        mouse_data = MSLLHOOKSTRUCT.from_address(mouse_data_ptr)
        if move_handler is not None and current_key in {"Move", "Wheel"}:
            try:
                move_handler(
                    mouse_data.pt.x,
                    mouse_data.pt.y,
                    current_key == "Wheel",
                )
            except Exception:
                pass
        if current_key == "Move":
            return result
        event_type = self.MOUSE_ID_TO_EVENT_TYPE.get(event_code_word)
        event = MouseEvent(
            current_key,
            event_type,
            mouse_data.pt.x,
            mouse_data.pt.y,
        )
        if current_key == "Wheel":
            event.wheel_delta = ctypes.c_short(
                (int(mouse_data.mouseData) >> 16) & 0xFFFF
            ).value
        self.handler(event)
        return result


class InputCaptureEngine:
    def __init__(self, backend="uia", artifact_dir=None, monitor_index=1, ignore_process_ids=(),
                 ignore_keys=("F7", "F9", "F10", "F11"),
                 ignore_window_classes=("Shell_TrayWnd", "Shell_SecondaryTrayWnd"),
                 allowed_process_ids=(), selected_window_handles=(),
                 window_capture_mode="strict", on_window_discovered=None,
                 process_filter_enabled=True, journal_dir=None,
                 event_id_prefix="", event_target_resolver=None,
                 event_target_timeout_ms=DEFAULT_EVENT_TARGET_TIMEOUT_MS):
        self.backend = backend
        self.artifact_dir = Path(artifact_dir) if artifact_dir else None
        self.monitor_index = max(1, int(monitor_index or 1))
        self.ignore_process_ids = {
            int(process_id)
            for process_id in (ignore_process_ids or ())
            if process_id is not None
        }
        self.ignore_keys = {str(key).casefold() for key in (ignore_keys or ())}
        self.ignore_window_classes = {
            str(class_name).strip().casefold()
            for class_name in (ignore_window_classes or ())
        }
        self.allowed_process_ids = {
            int(process_id)
            for process_id in (allowed_process_ids or ())
            if process_id is not None
        }
        self.selected_window_handles = {
            int(handle)
            for handle in (selected_window_handles or ())
            if handle is not None
        }
        self.window_capture_mode = str(window_capture_mode or "strict").strip().lower()
        if self.window_capture_mode not in {"auto", "strict"}:
            raise ValueError(
                f"未知窗口采集模式: {window_capture_mode}; 只支持 auto/strict"
            )
        self.on_window_discovered = on_window_discovered
        self.process_filter_enabled = bool(process_filter_enabled)
        self.journal_dir = Path(journal_dir).resolve() if journal_dir else None
        self.event_id_prefix = str(event_id_prefix or "")
        self.event_target_timeout_ms = max(0, int(event_target_timeout_ms))
        self.event_target_resolver = (
            event_target_resolver
            if event_target_resolver is not None
            else EventTargetResolver(backend)
            if self.backend == "uia"
            else None
        )
        self._raw_journal = (
            RawEventJournal(self.journal_dir)
            if self.journal_dir is not None
            else None
        )
        self._raw_journal_queue = queue.Queue()
        self._raw_journal_thread = None
        self._raw_journal_error = None
        self.raw_seal = None
        self.capture_completion = None
        self.inspector = UIAInspector(backend)
        self.events = []
        self.error = None
        self._running = False
        self._accepting = False
        self._stopped = True
        self._restartable = True
        self._paused = False
        self._pause_started_ms = None
        self._started_monotonic = None
        self._started_at = None
        self._hook = None
        self._hook_thread = None
        self._worker_thread = None
        self._discovery_thread = None
        self._discovery_stop = threading.Event()
        self._window_lock = threading.RLock()
        self._window_evidence_queue = queue.Queue()
        self._window_evidence_thread = None
        self._window_business_evidence_pending = set()
        self._window_evidence_gate = _CaptureCommitGate()
        self._frame_thread = None
        self._frame_condition = threading.Condition()
        self._frame_jobs = []
        self._frame_records = {}
        self._frame_debounce = {}
        self._frame_sequence = 0
        self._frame_stopping = False
        self._keyboard_sequence_open = False
        self._last_keyboard_event_index = None
        self._frame_errors = []
        self._queue = queue.Queue()
        self._lifecycle_lock = threading.RLock()
        self._enqueue_lock = threading.Lock()
        self._event_lock = threading.Lock()
        self._hook_done = threading.Event()
        self._worker_cancelled = threading.Event()
        self._next_index = 1
        self._keyboard_enrichment_open = False
        self.last_target_window_handle = None
        self._window_lifecycle = {}
        self._window_notifications = queue.Queue()
        self._observation_notifications = queue.Queue()
        self._hover_handler = None
        self._initial_window_handles = set()

    @property
    def is_running(self):
        return self._running

    @property
    def event_count(self):
        with self._event_lock:
            return len(self.events)

    @property
    def is_paused(self):
        return self._paused

    @property
    def started_monotonic(self):
        return self._started_monotonic

    @property
    def started_at(self):
        return self._started_at

    @property
    def window_lifecycle(self):
        with self._window_lock:
            return [
                dict(item)
                for item in sorted(
                    self._window_lifecycle.values(),
                    key=lambda value: (
                        value.get("first_seen_ms", 0),
                        value.get("handle", 0),
                    ),
                )
            ]

    def drain_window_notifications(self):
        result = []
        while True:
            try:
                result.append(self._window_notifications.get_nowait())
            except queue.Empty:
                return result

    def drain_observation_notifications(self):
        result = []
        while True:
            try:
                result.append(self._observation_notifications.get_nowait())
            except queue.Empty:
                return result

    def set_hover_handler(self, handler):
        self._hover_handler = handler
        hook = self._hook
        if hook is not None:
            hook.move_handler = (
                self._handle_hover_point
                if callable(handler)
                else None
            )

    def hover_context(self, point):
        if not self._accepting or self._paused:
            return {"eligible": False}
        try:
            point = (int(point[0]), int(point[1]))
            window_context = _window_at_point(*point)
            admission, _relation = self._classify_window(
                window_context,
                establish_scope=False,
            )
            return {
                "point": [int(point[0]), int(point[1])],
                "process_id": window_context.get("process_id"),
                "window_handle": window_context.get("window_handle"),
                "eligible": admission != "ignored",
            }
        except Exception:
            return {"eligible": False}

    def _handle_hover_point(self, x, y, force=False):
        handler = self._hover_handler
        if handler is None or not self._accepting or self._paused:
            return
        try:
            handler((int(x), int(y)), force=bool(force))
        except Exception:
            return

    def refresh_hover_target(self, *, force=True):
        try:
            x, y = win32gui.GetCursorPos()
        except Exception:
            return
        self._handle_hover_point(x, y, force=force)

    def start(self, startup_timeout=3):
        with self._lifecycle_lock:
            if self._running:
                return
            if not self._restartable:
                raise RuntimeError("输入采集器异常停止，不能复用；请创建新的采集器")
            if not self._stopped:
                raise RuntimeError("输入采集器正在停止")
            self._queue = queue.Queue()
            self._raw_journal_queue = queue.Queue()
            self._window_evidence_queue = queue.Queue()
            self._window_evidence_gate = _CaptureCommitGate()
            self._hook_done.clear()
            self._worker_cancelled.clear()
            self._discovery_stop.clear()
            with self._frame_condition:
                self._frame_jobs = []
                self._frame_records = {}
                self._frame_debounce = {}
                self._frame_sequence = 0
                self._frame_stopping = False
                self._keyboard_sequence_open = False
                self._last_keyboard_event_index = None
                self._frame_errors = []
            self._stopped = False
            self._accepting = True
            self._running = True
            self.error = None
            with self._event_lock:
                self.events.clear()
            self._next_index = 1
            self._keyboard_enrichment_open = False
            self._paused = False
            self._pause_started_ms = None
            self.last_target_window_handle = None
            self._window_lifecycle = {}
            self._window_business_evidence_pending = set()
            self._window_notifications = queue.Queue()
            self._observation_notifications = queue.Queue()
            self._initial_window_handles = _top_level_window_handles()
            self.raw_seal = None
            self.capture_completion = None
            self._raw_journal_error = None
            if self._raw_journal is not None:
                self._raw_journal.start()
            self._hook = None
            self._started_monotonic = time.monotonic()
            self._started_at = datetime.now().isoformat(timespec="milliseconds")
            if self.event_target_resolver is not None:
                try:
                    self.event_target_resolver.start(timeout=1.0)
                except Exception:
                    pass
            if self.artifact_dir:
                (self.artifact_dir / "screenshots" / "events").mkdir(parents=True, exist_ok=True)
            self._worker_thread = threading.Thread(target=self._run_worker, daemon=True)
            self._raw_journal_thread = (
                threading.Thread(
                    target=self._run_raw_journal,
                    daemon=True,
                )
                if self._raw_journal is not None
                else None
            )
            self._window_evidence_thread = threading.Thread(
                target=self._run_window_evidence,
                daemon=True,
            )
            self._hook_thread = threading.Thread(target=self._run_hook, daemon=True)
            self._discovery_thread = threading.Thread(
                target=self._run_window_discovery,
                daemon=True,
            )
            self._frame_thread = threading.Thread(
                target=self._run_frame_capture,
                daemon=True,
            )
            self._frame_thread.start()
            if self._raw_journal_thread is not None:
                self._raw_journal_thread.start()
            self._window_evidence_thread.start()
            self._worker_thread.start()
            self._hook_thread.start()
            self._discovery_thread.start()

            deadline = time.monotonic() + max(0.1, float(startup_timeout))
            while time.monotonic() < deadline:
                hook = self._hook
                if (
                    hook is not None
                    and hook.keyboard_id
                    and hook.mouse_id
                    and hook.is_hooked()
                ):
                    return
                if self.error is not None or self._hook_done.is_set():
                    break
                time.sleep(0.01)

            startup_error = self.error or RuntimeError("Windows 输入 Hook 初始化超时或句柄无效")
            self.stop()
            raise RuntimeError(f"输入采集启动失败: {startup_error}") from startup_error

    def stop(self, timeout=30):
        with self._lifecycle_lock:
            if self._stopped:
                return self.snapshot_events()

            with self._enqueue_lock:
                self._accepting = False
            self._running = False
            self._discovery_stop.set()

            discovery_stop_error = None
            if self._discovery_thread is not None:
                self._discovery_thread.join(timeout=3)
                if self._discovery_thread.is_alive():
                    discovery_stop_error = TimeoutError(
                        "等待窗口发现线程停止超时"
                    )
                    self._set_error(discovery_stop_error)
                    self._restartable = False

            hook = self._hook
            if hook is not None:
                try:
                    hook.stop()
                except Exception as error:
                    self._set_error(error)

            hook_stop_error = None
            if self._hook_thread is not None:
                self._hook_thread.join(timeout=3)
                if self._hook_thread.is_alive():
                    hook_stop_error = TimeoutError(
                        "等待 Windows 输入 Hook 停止超时"
                    )
                    self._set_error(hook_stop_error)
                    self._restartable = False

            if self.event_target_resolver is not None:
                try:
                    self.event_target_resolver.stop(timeout=1.0)
                except Exception:
                    pass

            raw_stop_error = None
            if self._raw_journal is not None:
                self._raw_journal_queue.put(_RAW_JOURNAL_STOP)
                if self._raw_journal_thread is not None:
                    self._raw_journal_thread.join(timeout=timeout)
                    if self._raw_journal_thread.is_alive():
                        raw_stop_error = TimeoutError(
                            "等待原始事件 journal 保存超时"
                        )
                raw_stop_error = raw_stop_error or self._raw_journal_error
                if raw_stop_error is None:
                    try:
                        self.raw_seal = self._raw_journal.seal()
                    except Exception as error:
                        raw_stop_error = error
                if raw_stop_error is not None:
                    self._raw_journal.close()
                    self._set_error(raw_stop_error)
                    self._restartable = False

            self._finalize_keyboard_frames()
            with self._frame_condition:
                self._frame_stopping = True
                self._frame_condition.notify_all()
            frame_stop_error = None
            if self._frame_thread is not None:
                self._frame_thread.join(timeout=5)
                if self._frame_thread.is_alive():
                    frame_stop_error = TimeoutError(
                        "等待动作边界帧采集线程停止超时"
                    )
                    self._set_error(frame_stop_error)
                    self._restartable = False

            # _accepting 已关闭且 Hook 已停止，STOP 一定排在所有已接收事件之后。
            self._queue.put(_STOP)
            worker_stop_error = None
            if self._worker_thread is not None:
                self._worker_thread.join(timeout=timeout)
                if self._worker_thread.is_alive():
                    worker_stop_error = TimeoutError(
                        "等待输入事件证据保存超时"
                    )
                    self._set_error(worker_stop_error)
                    self._restartable = False
                    self._worker_cancelled.set()

            self._window_evidence_queue.put(_WINDOW_EVIDENCE_STOP)
            window_evidence_stop_error = None
            if self._window_evidence_thread is not None:
                self._window_evidence_thread.join(timeout=timeout)
                if self._window_evidence_thread.is_alive():
                    window_evidence_stop_error = TimeoutError(
                        "等待窗口证据保存超时"
                    )
                    self._window_evidence_gate.cancel()
                    self._set_error(window_evidence_stop_error)
                    self._restartable = False
            self._stopped = True
            enriched_event_ids = [
                event.id
                for event in self.snapshot_events()
            ]
            completion_error = (
                raw_stop_error
                or discovery_stop_error
                or hook_stop_error
                or frame_stop_error
                or worker_stop_error
                or window_evidence_stop_error
            )
            if self.journal_dir is not None and self.raw_seal is not None:
                self.capture_completion = write_capture_completion(
                    self.journal_dir,
                    self.raw_seal,
                    enriched_event_ids,
                    error=completion_error,
                )
                if self.capture_completion.get("status") != "complete":
                    completion_error = completion_error or RuntimeError(
                        "原始事件与富化事件集合不一致"
                    )
                    self._set_error(completion_error)
                    self._restartable = False
            if raw_stop_error is not None:
                raise RuntimeError(
                    "原始事件 journal 未完整封存"
                ) from raw_stop_error
            if discovery_stop_error is not None:
                raise RuntimeError(
                    "窗口发现线程未停止，拒绝提交 Take"
                ) from discovery_stop_error
            if hook_stop_error is not None:
                raise RuntimeError(
                    "Windows 输入 Hook 未停止，拒绝提交 Take"
                ) from hook_stop_error
            if frame_stop_error is not None:
                raise RuntimeError(
                    "动作边界帧线程未停止，拒绝提交 Take"
                ) from frame_stop_error
            if worker_stop_error is not None:
                raise RuntimeError(
                    "输入事件证据未完整保存，拒绝返回部分事件"
                ) from worker_stop_error
            if window_evidence_stop_error is not None:
                raise RuntimeError(
                    "窗口证据未完整保存，拒绝返回部分事件"
                ) from window_evidence_stop_error
            if completion_error is not None:
                raise RuntimeError(
                    "采集完成校验失败，拒绝返回不完整事件"
                ) from completion_error

            self._attach_frame_records()
            self._finalize_window_lifecycle()
            return self.snapshot_events()

    def snapshot_events(self):
        with self._event_lock:
            return list(self.events)

    def record_observation(self, point=None, note="", provider=None):
        if not self._accepting:
            raise RuntimeError("输入采集器未运行")
        if point is None:
            point = win32gui.GetCursorPos()
        x, y = int(point[0]), int(point[1])
        window_context = _window_at_point(x, y)
        now = time.monotonic()
        raw = {
            "event_type": "observation",
            "monotonic_ms": int((now - self._started_monotonic) * 1000),
            "wall_time": datetime.now().isoformat(timespec="milliseconds"),
            "point": [x, y],
            **window_context,
            "note": str(note or ""),
            "observation_provider": str(provider or "").strip() or None,
        }
        admission, relation = self._classify_window(window_context)
        if admission == "ignored":
            raise ValueError("观察点不在当前可采集窗口范围内")
        raw["window_admission"] = admission
        raw["process_relation"] = relation
        return self._enqueue_raw(raw, required=True)

    def pause(self, note=""):
        if not self._accepting:
            raise RuntimeError("输入采集器未运行")
        if self._paused:
            return False
        now = time.monotonic()
        monotonic_ms = int((now - self._started_monotonic) * 1000)
        self._paused = True
        self._pause_started_ms = monotonic_ms
        self._enqueue_raw({
            "event_type": "pause_start",
            "monotonic_ms": monotonic_ms,
            "wall_time": datetime.now().isoformat(timespec="milliseconds"),
            "note": str(note or ""),
            **_foreground_window(),
        }, required=True)
        return monotonic_ms

    def resume(self, note=""):
        if not self._accepting:
            raise RuntimeError("输入采集器未运行")
        if not self._paused:
            return False
        now = time.monotonic()
        monotonic_ms = int((now - self._started_monotonic) * 1000)
        self._enqueue_raw({
            "event_type": "pause_end",
            "monotonic_ms": monotonic_ms,
            "wall_time": datetime.now().isoformat(timespec="milliseconds"),
            "note": str(note or ""),
            "pause_started_ms": self._pause_started_ms,
            **_foreground_window(),
        }, required=True)
        self._paused = False
        self._pause_started_ms = None
        return monotonic_ms

    def toggle_pause(self, note=""):
        return self.resume(note=note) if self._paused else self.pause(note=note)

    def _run_hook(self):
        try:
            hook = RecorderHook()
            hook.handler = self._handle_hook_event
            hook.move_handler = (
                self._handle_hover_point
                if callable(self._hover_handler)
                else None
            )
            self._hook = hook
            hook.hook(keyboard=True, mouse=True)
        except Exception as error:
            self._set_error(error)
            self._running = False
            self._accepting = False
        finally:
            self._hook_done.set()

    def _handle_hook_event(self, event):
        if not self._accepting:
            return
        raw = self._raw_event(event)
        if raw is None:
            return
        if self._paused:
            return
        admission, relation = self._classify_window(raw)
        if admission == "ignored":
            return
        raw["window_admission"] = admission
        raw["process_relation"] = relation
        if raw.get("event_type") in {"mouse_down", "mouse_wheel"}:
            resolver = self.event_target_resolver
            if resolver is not None:
                raw["_event_target_binding"] = resolver.capture(
                    tuple(raw.get("point") or (0, 0)),
                    process_id=raw.get("process_id"),
                    window_handle=raw.get("window_handle"),
                    timeout_ms=self.event_target_timeout_ms,
                    event_type=raw.get("event_type"),
                )
        self._enqueue_raw(raw, required=False)

    def _enqueue_raw(self, raw, required):
        with self._enqueue_lock:
            if not self._accepting:
                if required:
                    raise RuntimeError("输入采集器已停止接收事件")
                return False
            raw["index"] = self._next_index
            self._next_index += 1
            event_id = self._event_id(raw["index"])
            raw["id"] = event_id
            if self._raw_journal is not None:
                self._raw_journal_queue.put({
                    key: value
                    for key, value in raw.items()
                    if not str(key).startswith("_")
                })
            self._schedule_boundary_frames(raw)
            self._queue.put(raw)
            return event_id

    def _schedule_boundary_frames(self, raw):
        event_type = raw.get("event_type")
        index = int(raw["index"])
        if event_type not in {"key_down", "key_up"}:
            self._finalize_keyboard_frames()
        if event_type == "mouse_down":
            self._invalidate_frame_debounce("pointer_probe")
            self._invalidate_frame_debounce("pointer_settled")
            self._schedule_frame(index, "before", raw, delay_ms=0)
        elif event_type == "mouse_up":
            self._schedule_frame(index, "after_immediate", raw, delay_ms=60)
            self._schedule_frame(
                index,
                "after_probe",
                raw,
                delay_ms=180,
                debounce_key="pointer_probe",
            )
            self._schedule_frame(
                index,
                "after_settled",
                raw,
                delay_ms=520,
                debounce_key="pointer_settled",
            )
        elif event_type == "mouse_wheel":
            self._invalidate_frame_debounce("pointer_probe")
            self._invalidate_frame_debounce("pointer_settled")
            self._schedule_frame(index, "commit", raw, delay_ms=0)
            self._schedule_frame(
                index,
                "after_probe",
                raw,
                delay_ms=180,
                debounce_key="pointer_probe",
            )
            self._schedule_frame(
                index,
                "after_settled",
                raw,
                delay_ms=520,
                debounce_key="pointer_settled",
            )
        elif event_type == "observation":
            self._schedule_frame(index, "observation", raw, delay_ms=0)
        elif event_type == "key_down":
            if not self._keyboard_sequence_open:
                self._keyboard_sequence_open = True
                self._schedule_frame(index, "before", raw, delay_ms=0)
            self._last_keyboard_event_index = index
            self._invalidate_frame_debounce("keyboard_probe")
            self._invalidate_frame_debounce("keyboard_after")
        elif event_type == "key_up":
            self._last_keyboard_event_index = index
            self._schedule_frame(
                index,
                "after_probe",
                raw,
                delay_ms=180,
                debounce_key="keyboard_probe",
            )
            self._schedule_frame(
                index,
                "after_settled",
                raw,
                delay_ms=520,
                debounce_key="keyboard_after",
                close_keyboard=True,
            )

    def _finalize_keyboard_frames(self):
        if not self._keyboard_sequence_open or self._last_keyboard_event_index is None:
            return
        now_origin = self._started_monotonic or time.monotonic()
        raw = {
            "monotonic_ms": int((time.monotonic() - now_origin) * 1000),
        }
        self._schedule_frame(
            self._last_keyboard_event_index,
            "after_settled",
            raw,
            delay_ms=0,
            debounce_key="keyboard_after",
            close_keyboard=True,
        )

    def _schedule_frame(
            self,
            event_index,
            stage,
            raw,
            *,
            delay_ms,
            debounce_key=None,
            close_keyboard=False,
    ):
        if self.artifact_dir is None:
            if close_keyboard:
                self._keyboard_sequence_open = False
                self._last_keyboard_event_index = None
            return
        with self._frame_condition:
            self._frame_sequence += 1
            token = self._frame_sequence
            if debounce_key:
                self._frame_debounce[debounce_key] = token
            heapq.heappush(self._frame_jobs, (
                time.monotonic() + max(0, delay_ms) / 1000,
                token,
                {
                    "event_index": int(event_index),
                    "event_id": self._event_id(event_index),
                    "event_ms": int(raw.get("monotonic_ms") or 0),
                    "stage": str(stage),
                    "debounce_key": debounce_key,
                    "close_keyboard": bool(close_keyboard),
                },
            ))
            self._frame_condition.notify()

    def _invalidate_frame_debounce(self, key):
        with self._frame_condition:
            self._frame_sequence += 1
            self._frame_debounce[key] = self._frame_sequence
            self._frame_condition.notify()

    def _run_frame_capture(self):
        while True:
            with self._frame_condition:
                while not self._frame_jobs and not self._frame_stopping:
                    self._frame_condition.wait()
                if not self._frame_jobs and self._frame_stopping:
                    return
                due, token, job = self._frame_jobs[0]
                wait_seconds = due - time.monotonic()
                if wait_seconds > 0 and not self._frame_stopping:
                    self._frame_condition.wait(wait_seconds)
                    continue
                heapq.heappop(self._frame_jobs)
                debounce_key = job.get("debounce_key")
                if (
                    debounce_key
                    and self._frame_debounce.get(debounce_key) != token
                ):
                    continue
            self._capture_frame_job(job)
            if job.get("close_keyboard"):
                self._keyboard_sequence_open = False
                self._last_keyboard_event_index = None

    def _capture_frame_job(self, job):
        stage_code = {
            "before": "b",
            "commit": "c",
            "observation": "o",
            "after_immediate": "ai",
            "after_probe": "ap",
            "after_settled": "as",
        }.get(job["stage"], "f")
        relative = (
            Path("frames")
            / f"e{int(job['event_index']):05d}-{stage_code}.png"
        )
        destination = self.artifact_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with mss() as screen:
                monitor_index = min(self.monitor_index, len(screen.monitors) - 1)
                monitor = {
                    key: int(screen.monitors[monitor_index][key])
                    for key in ("left", "top", "width", "height")
                }
                screen.shot(mon=monitor_index, output=str(destination))
            captured_ms = int(
                (time.monotonic() - self._started_monotonic) * 1000
            )
            record = {
                "stage": job["stage"],
                "path": relative.as_posix(),
                "captured_ms": captured_ms,
                "event_ms": job["event_ms"],
                "latency_ms": max(0, captured_ms - job["event_ms"]),
                "monitor": monitor,
            }
            with self._frame_condition:
                self._frame_records.setdefault(job["event_id"], []).append(record)
        except Exception as error:
            with self._frame_condition:
                self._frame_errors.append({
                    "event_id": job["event_id"],
                    "stage": job["stage"],
                    "error": f"{type(error).__name__}: {error}",
                })

    def _attach_frame_records(self):
        with self._event_lock, self._frame_condition:
            records = {
                event_id: sorted(
                    values,
                    key=lambda item: (item.get("captured_ms", 0), item.get("stage", "")),
                )
                for event_id, values in self._frame_records.items()
            }
            for event in self.events:
                frames = records.get(event.id, [])
                if not frames:
                    continue
                event.details["frames"] = frames
                preferred = _preferred_event_frame(event.event_type, frames)
                if preferred:
                    event.screenshot = preferred["path"]
                    event.details["screenshot_monotonic_ms"] = preferred["captured_ms"]
                    event.details["screenshot_latency_ms"] = preferred["latency_ms"]
            if self._frame_errors and self.events:
                self.events[-1].details.setdefault("frame_capture_errors", []).extend(
                    self._frame_errors
                )

    def _raw_event(self, event):
        now = time.monotonic()
        common = {
            "monotonic_ms": int((now - self._started_monotonic) * 1000),
            "wall_time": datetime.now().isoformat(timespec="milliseconds"),
        }
        if isinstance(event, MouseEvent):
            point = [int(event.mouse_x), int(event.mouse_y)]
            window_context = _window_at_point(*point)
            if event.current_key == "Wheel":
                event_type = "mouse_wheel"
                button = "wheel"
            elif event.current_key in ("LButton", "RButton", "WheelButton"):
                action = "down" if event.event_type == "key down" else "up"
                event_type = f"mouse_{action}"
                button = {
                    "LButton": "left",
                    "RButton": "right",
                    "WheelButton": "middle",
                }[event.current_key]
            else:
                return None
            return {
                **common,
                "event_type": event_type,
                "point": point,
                "button": button,
                "wheel_delta": getattr(event, "wheel_delta", None),
                **window_context,
            }
        if isinstance(event, KeyboardEvent):
            if str(event.current_key or "").casefold() in self.ignore_keys:
                return None
            window_context = _foreground_window()
            return {
                **common,
                "event_type": "key_down" if event.event_type == "key down" else "key_up",
                "key": {
                    "name": str(event.current_key or ""),
                    "pressed": [str(key) for key in list(event.pressed_key or ())],
                },
                **window_context,
            }
        return None

    def _run_worker(self):
        com_initialized = self._initialize_worker_com()
        if self.backend == "uia" and not com_initialized:
            return
        try:
            while True:
                raw = self._queue.get()
                if raw is _STOP:
                    break
                if self._worker_cancelled.is_set():
                    continue
                try:
                    event = self._enrich_event(raw)
                    if self._worker_cancelled.is_set():
                        continue
                    with self._event_lock:
                        self.events.append(event)
                    if raw.get("event_type") == "observation":
                        self._observation_notifications.put(
                            _observation_receipt(event)
                        )
                except Exception as error:
                    self._set_error(error)
                    if raw.get("event_type") == "observation":
                        self._observation_notifications.put({
                            "event_id": str(
                                raw.get("id")
                                or self._event_id(raw.get("index") or 0)
                            ),
                            "status": "failed",
                            "message": f"{type(error).__name__}: {error}",
                        })
        finally:
            if com_initialized:
                pythoncom.CoUninitialize()

    def _run_raw_journal(self):
        while True:
            raw = self._raw_journal_queue.get()
            if raw is _RAW_JOURNAL_STOP:
                return
            if self._raw_journal_error is not None:
                continue
            try:
                self._raw_journal.append(raw)
            except Exception as error:
                self._raw_journal_error = error
                self._set_error(error)
                with self._enqueue_lock:
                    self._accepting = False

    def _run_window_discovery(self):
        while not self._discovery_stop.wait(0.25):
            if not self._accepting or not self.allowed_process_ids:
                continue
            now_ms = int((time.monotonic() - self._started_monotonic) * 1000)
            for context in _visible_top_level_windows():
                if not self._accepting:
                    break
                handle = context.get("window_handle")
                if not handle or int(handle) in self._window_lifecycle:
                    continue
                if (
                    int(handle) in self._initial_window_handles
                    and int(handle) not in self.selected_window_handles
                ):
                    continue
                admission, relation = self._classify_window(context)
                if relation not in {
                    "selected_window",
                    "same_process",
                    "child_process",
                }:
                    continue
                self._track_window(
                    {
                        **context,
                        "event_type": "window_discovered",
                        "monotonic_ms": now_ms,
                        "window_admission": admission,
                        "process_relation": relation,
                    },
                    None,
                )

    def _run_window_evidence(self):
        com_initialized = self._initialize_worker_com()
        if self.backend == "uia" and not com_initialized:
            return
        try:
            while True:
                task = self._window_evidence_queue.get()
                if task is _WINDOW_EVIDENCE_STOP:
                    return
                self._capture_window_evidence(task)
        finally:
            if com_initialized:
                pythoncom.CoUninitialize()

    def _initialize_worker_com(self):
        if self.backend != "uia":
            return False
        try:
            pythoncom.CoInitialize()
        except Exception as error:
            self._set_error(error)
            return False
        return True

    def _capture_window_evidence(self, task):
        if self.on_window_discovered is None:
            return
        try:
            evidence = self.on_window_discovered({
                **task["lifecycle"],
                "has_business_event": task["has_business_event"],
                "_capture_cancelled": self._window_evidence_gate.is_cancelled,
                "_capture_commit": self._window_evidence_gate.commit,
            })
        except Exception as error:
            with self._window_lock:
                lifecycle = self._window_lifecycle.get(task["handle"])
                if lifecycle is not None:
                    lifecycle[task["error_key"]] = (
                        f"{type(error).__name__}: {error}"
                    )
                if task["has_business_event"]:
                    self._window_business_evidence_pending.discard(
                        task["handle"]
                    )
            return
        if self._window_evidence_gate.is_cancelled():
            return
        with self._window_lock:
            lifecycle = self._window_lifecycle.get(task["handle"])
            if lifecycle is None:
                return
            if task["evidence_key"]:
                lifecycle[task["evidence_key"]] = dict(evidence or {})
            if task["has_business_event"]:
                lifecycle["business_event_confirmed"] = True
                self._window_business_evidence_pending.discard(
                    task["handle"]
                )

    def _enrich_event(self, raw):
        event_type = raw["event_type"]
        event_id = str(raw.get("id") or self._event_id(raw["index"]))
        if event_type not in {"key_down", "key_up"}:
            self._keyboard_enrichment_open = False
        self._track_window(raw, event_id)
        screenshot = None
        screenshot_monotonic_ms = None
        target = None
        late_target_observation = None
        target_binding = raw.get("_event_target_binding")
        if event_type in (
            "mouse_down",
            "mouse_wheel",
            "observation",
        ):
            point = raw.get("point")
            inspect_kwargs = {
                "event_type": _inspection_event_type(
                    event_type,
                    raw.get("button"),
                ),
            }
            if raw.get("observation_provider"):
                inspect_kwargs["provider"] = raw["observation_provider"]
            if (
                event_type in {"mouse_down", "mouse_wheel"}
                and target_binding is not None
            ):
                target = event_target_from_binding(
                    target_binding,
                    raw,
                    self.backend,
                )
                if target is None:
                    late_target_observation = (
                        self.inspector.inspect_point_capture(
                            point[0],
                            point[1],
                            **inspect_kwargs,
                        )
                    )
            else:
                target = self.inspector.inspect_point_capture(
                    point[0],
                    point[1],
                    **inspect_kwargs,
                )
        elif event_type == "mouse_up":
            point = raw.get("point")
            target = self.inspector.inspect_point_state(point[0], point[1])
        elif event_type == "key_down":
            if self._keyboard_enrichment_open:
                target = self.inspector.inspect_focus_state()
            else:
                target = self.inspector.inspect_focus_capture()
                self._keyboard_enrichment_open = _has_structured_target(
                    target
                )
        elif event_type == "key_up":
            target = self.inspector.inspect_focus_state()

        if target:
            window_handle = (target.get("window") or {}).get("handle")
            if window_handle:
                self.last_target_window_handle = int(window_handle)
        evidence_monotonic_ms = int((time.monotonic() - self._started_monotonic) * 1000)

        structured_reference = None
        structured = (
            (target or {}).pop("structured_observation", None)
            if event_type == "observation"
            else None
        )
        if structured is not None:
            if self.journal_dir is None:
                raise RuntimeError(
                    "结构化 Observation 需要可写的 Take artifact 目录"
                )
            structured_reference = write_observation_receipt(
                self.journal_dir,
                event_id,
                structured,
            )

        target_binding_record = (
            {
                key: value
                for key, value in target_binding.items()
                if key != "element"
            }
            if isinstance(target_binding, dict)
            else None
        )
        details = {
            "window_handle": raw.get("window_handle"),
            "process_id": raw.get("process_id"),
            "window_class": raw.get("window_class"),
            "window_title": raw.get("window_title"),
            "window_admission": raw.get("window_admission"),
            "process_relation": raw.get("process_relation"),
            "note": raw.get("note"),
            "observation_provider": raw.get("observation_provider"),
            "structured_observation": structured_reference,
            "pause_started_ms": raw.get("pause_started_ms"),
            "evidence_monotonic_ms": evidence_monotonic_ms,
            "evidence_latency_ms": max(
                0,
                evidence_monotonic_ms - raw["monotonic_ms"],
            ),
            "observation_phase": (
                "pre_dispatch"
                if target_binding_record is not None
                and target_binding_record.get("status") == "captured"
                else "after_commit"
                if event_type in {"mouse_up", "key_up"}
                else "late_unresolved"
                if target_binding_record is not None
                else "before_or_commit"
            ),
            "screenshot_monotonic_ms": screenshot_monotonic_ms,
            "screenshot_latency_ms": (
                max(0, screenshot_monotonic_ms - raw["monotonic_ms"])
                if screenshot_monotonic_ms is not None
                else None
            ),
        }
        if target_binding_record is not None:
            details["target_binding"] = target_binding_record
        if late_target_observation is not None:
            details["late_target_observation"] = (
                _compact_target_observation(late_target_observation)
            )
        return RecordingEvent(
            id=event_id,
            index=raw["index"],
            event_type=event_type,
            monotonic_ms=raw["monotonic_ms"],
            wall_time=raw["wall_time"],
            point=raw.get("point"),
            button=raw.get("button"),
            wheel_delta=raw.get("wheel_delta"),
            key=raw.get("key"),
            target=target,
            screenshot=screenshot,
            details=details,
        )

    def _event_id(self, index):
        return f"{self.event_id_prefix}event-{int(index):05d}"

    def _set_error(self, error):
        if self.error is None:
            self.error = error

    def _classify_window(self, window_context, *, establish_scope=True):
        process_id = window_context.get("process_id")
        window_handle = window_context.get("window_handle")
        window_class = str(window_context.get("window_class") or "").casefold()
        if (
            process_id in self.ignore_process_ids
            or window_class in self.ignore_window_classes
            or not process_id
            or not window_handle
        ):
            return "ignored", "ignored"
        if int(window_handle) in self.selected_window_handles:
            return "selected", "selected_window"
        if int(process_id) in self.allowed_process_ids:
            return "automatic", "same_process"
        if _is_descendant_process(process_id, self.allowed_process_ids):
            if self.window_capture_mode == "auto":
                return "automatic", "child_process"
            return "ignored", "child_process"
        if not self.process_filter_enabled:
            return "automatic", "process_filter_disabled"
        if self.window_capture_mode == "strict":
            return "ignored", "unrelated_process"
        if self.window_capture_mode == "auto":
            if not self.allowed_process_ids and not self._window_lifecycle:
                if establish_scope:
                    self.allowed_process_ids.add(int(process_id))
                return "automatic", "first_window"
            return "provisional", "unrelated_process"
        return "ignored", "unrelated_process"

    def _is_ignored_window(self, window_context):
        admission, _relation = self._classify_window(window_context)
        return admission == "ignored"

    def _track_window(self, raw, event_id):
        if raw.get("event_type") in {"pause_start", "pause_end"}:
            return
        handle = raw.get("window_handle")
        process_id = raw.get("process_id")
        if not handle or not process_id:
            return
        handle = int(handle)
        now_ms = int(raw.get("monotonic_ms") or 0)
        evidence_tasks = []
        with self._window_lock:
            lifecycle = self._window_lifecycle.get(handle)
            if lifecycle is None:
                lifecycle = {
                    "handle": handle,
                    "process_id": int(process_id),
                    "title": str(raw.get("window_title") or ""),
                    "class_name": str(raw.get("window_class") or ""),
                    "admission": raw.get("window_admission") or "provisional",
                    "process_relation": raw.get("process_relation") or "unknown",
                    "first_seen_ms": now_ms,
                    "last_seen_ms": now_ms,
                    "existed_at_start": handle in self._initial_window_handles,
                    "opened_during_take": handle not in self._initial_window_handles,
                    "closed_during_take": False,
                    "event_ids": [],
                    "first_seen_evidence": {},
                }
                self._window_lifecycle[handle] = lifecycle
                self._window_notifications.put(dict(lifecycle))
                evidence_tasks.append({
                    "handle": handle,
                    "lifecycle": dict(lifecycle),
                    "has_business_event": event_id is not None,
                    "evidence_key": "first_seen_evidence",
                    "error_key": "discovery_error",
                })
                if event_id is not None:
                    self._window_business_evidence_pending.add(handle)
            lifecycle["last_seen_ms"] = now_ms
            lifecycle["title"] = str(raw.get("window_title") or lifecycle["title"])
            lifecycle["class_name"] = str(
                raw.get("window_class") or lifecycle["class_name"]
            )
            if event_id is not None:
                lifecycle["event_ids"].append(event_id)
                if (
                    self.on_window_discovered is not None
                    and not lifecycle.get("business_event_confirmed")
                    and handle not in self._window_business_evidence_pending
                ):
                    self._window_business_evidence_pending.add(handle)
                    evidence_tasks.append({
                        "handle": handle,
                        "lifecycle": dict(lifecycle),
                        "has_business_event": True,
                        "evidence_key": None,
                        "error_key": "business_event_error",
                    })
        if self.on_window_discovered is not None:
            for task in evidence_tasks:
                self._window_evidence_queue.put(task)

    def _finalize_window_lifecycle(self):
        for lifecycle in self._window_lifecycle.values():
            handle = int(lifecycle["handle"])
            alive = bool(win32gui.IsWindow(handle))
            lifecycle["alive_at_end"] = alive
            lifecycle["closed_during_take"] = not alive


def _window_at_point(x, y):
    try:
        handle = win32gui.WindowFromPoint((int(x), int(y)))
        root = win32gui.GetAncestor(handle, 2) or handle
        _, process_id = win32process.GetWindowThreadProcessId(root)
        return _window_context(root, process_id)
    except Exception:
        return _empty_window_context()


def _foreground_window():
    try:
        handle = win32gui.GetForegroundWindow()
        _, process_id = win32process.GetWindowThreadProcessId(handle)
        return _window_context(handle, process_id)
    except Exception:
        return _empty_window_context()


def _window_context(handle, process_id):
    return {
        "window_handle": int(handle),
        "process_id": int(process_id),
        "window_class": str(win32gui.GetClassName(handle) or ""),
        "window_title": str(win32gui.GetWindowText(handle) or ""),
    }


def _empty_window_context():
    return {
        "window_handle": None,
        "process_id": None,
        "window_class": "",
        "window_title": "",
    }


def _top_level_window_handles():
    handles = set()

    def collect(handle, _extra):
        if win32gui.IsWindowVisible(handle):
            handles.add(int(handle))
        return True

    try:
        win32gui.EnumWindows(collect, None)
    except Exception:
        return set()
    return handles


def _visible_top_level_windows():
    result = []

    def collect(handle, _extra):
        if not win32gui.IsWindowVisible(handle):
            return True
        try:
            _, process_id = win32process.GetWindowThreadProcessId(handle)
            result.append(_window_context(handle, process_id))
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(collect, None)
    except Exception:
        return []
    return result


def _is_descendant_process(process_id, root_process_ids):
    roots = {int(value) for value in root_process_ids or ()}
    if not roots:
        return False
    try:
        parents = {parent.pid for parent in psutil.Process(int(process_id)).parents()}
    except (psutil.Error, OSError, ValueError):
        return False
    return bool(roots & parents)


def _inspection_event_type(event_type, button):
    if event_type == "observation":
        return "observation"
    if event_type == "mouse_wheel":
        return "scroll"
    if button == "right":
        return "right_click"
    if button == "middle":
        return "middle_click"
    return "click"


def _compact_target_observation(target):
    target = target or {}
    return {
        "inspection_mode": target.get("inspection_mode"),
        "point": target.get("point"),
        "window": dict(target.get("window") or {}),
        "element": dict(target.get("element") or {}),
        "error": target.get("error"),
    }


def _has_structured_target(target):
    if not (target or {}).get("element"):
        return False
    return any(
        (candidate.get("locator") or {}).get("by", "child")
        in {"child", "xpath"}
        for candidate in target.get("locator_candidates") or ()
    )


def _observation_receipt(event):
    target = event.target or {}
    element = target.get("element") or {}
    candidates = target.get("locator_candidates") or []
    selected = next(
        (
            candidate
            for candidate in candidates
            if (candidate.get("validation") or {}).get("status") == "unique"
            and (candidate.get("validation") or {}).get("target_matches")
            is True
        ),
        None,
    )
    if selected is None:
        selected = next(iter(candidates), None)
    locator = (selected or {}).get("locator") or {}
    locator_kind = str(locator.get("by") or "child")
    validation = (selected or {}).get("validation") or {}
    unique = (
        validation.get("status") == "unique"
        and validation.get("target_matches") is True
    )
    ancestors = target.get("ancestors") or []
    scope = next(
        (
            item
            for item in reversed(ancestors)
            if item.get("control_type") in {"Window", "Pane"}
            and (
                item.get("name")
                or item.get("class_name")
            )
        ),
        target.get("window") or {},
    )
    if not element:
        status = "failed"
    elif unique and locator_kind in {"child", "xpath"}:
        status = "captured"
    else:
        status = "warning"
    return {
        "event_id": event.id,
        "status": status,
        "target": {
            "name": element.get("name") or "未命名控件",
            "control_type": element.get("control_type") or "Unknown",
            "auto_id": element.get("auto_id") or None,
        },
        "scope": {
            "name": scope.get("name") or scope.get("title") or "当前窗口",
            "control_type": scope.get("control_type") or "Window",
            "class_name": scope.get("class_name") or None,
        },
        "locator": {
            "kind": locator_kind,
            "name": (selected or {}).get("name"),
            "validation": (
                "unique_target_match"
                if unique
                else validation.get("status") or "unvalidated"
            ),
        },
        "structured_observation": (
            event.details.get("structured_observation")
            if isinstance(event.details, dict)
            else None
        ),
        "message": target.get("error"),
    }


def _preferred_event_frame(event_type, frames):
    priorities = {
        "mouse_down": ("before",),
        "mouse_up": ("after_settled", "after_immediate"),
        "mouse_wheel": ("after_settled", "commit"),
        "key_down": ("before",),
        "key_up": ("after_settled",),
        "observation": ("observation",),
    }.get(event_type, ())
    for stage in priorities:
        match = next(
            (
                frame
                for frame in reversed(frames)
                if frame.get("stage") == stage
            ),
            None,
        )
        if match is not None:
            return match
    return frames[-1] if frames else None