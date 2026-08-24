from __future__ import annotations

import os
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

from autowork_core.utils.debug_tools.common import get_open_windows
from autowork_core.utils.debug_tools.recorder.capture_runtime import (
    load_capture_config,
)
from autowork_core.utils.debug_tools.recorder.hotkeys import (
    RECORDER_HOTKEYS,
    VK_F7,
    VK_F9,
    VK_F10,
    VK_F11,
    VK_SHIFT,
    is_key_down,
    poll_hotkeys,
    reset_hotkeys,
)
from autowork_core.utils.debug_tools.recorder.supplement_capture import (
    SupplementCaptureSession,
)
from autowork_core.utils.debug_tools.recorder.operation_coordinator import (
    OperationCoordinator,
)
from autowork_core.utils.debug_tools.recorder.supplement_repository import (
    SupplementRepository,
)
from autowork_core.utils.debug_tools.recorder.status_overlay import (
    RecordingStatusOverlay,
)
from autowork_core.utils.debug_tools.recorder.target_highlight import (
    RecordingTargetHighlight,
    create_recording_target_highlight,
)
from autowork_core.utils.debug_tools.recorder.window_selector import (
    RecorderWindowSelector,
    format_window_selection_summary,
)
from autowork_core.utils.debug_tools.recorder.window_identity import (
    restore_window_handles as _restore_window_handles,
)


class SupplementRecordingWindow:
    def __init__(
            self,
            parent,
            take_dir,
            *,
            on_completed,
            backend=None,
            session_factory=SupplementCaptureSession,
            window_provider=get_open_windows,
            operation_coordinator=None,
            window_controller=None,
            highlight_factory=RecordingTargetHighlight,
        ):
        self.parent = parent
        self.take_dir = Path(take_dir).resolve()
        self.on_completed = on_completed
        self.capture_config = load_capture_config(self.take_dir)
        self.backend = str(
            backend or self.capture_config.get("backend") or "uia"
        )
        self.session_factory = session_factory
        self.window_provider = window_provider
        self.window_controller = (
            window_controller or _ToplevelCaptureWindowController(parent)
        )
        self.window_capture_mode = str(
            self.capture_config.get("window_capture_mode") or "strict"
        ).strip().lower()
        self.minimize_window = bool(
            self.capture_config.get("minimize_window", True)
        )
        self.repository = SupplementRepository(self.take_dir)
        self.operations = operation_coordinator or OperationCoordinator(
            max_workers=1,
            thread_name_prefix="recorder-supplement",
        )
        self._owns_operations = operation_coordinator is None
        self.operation_prefix = f"supplement:{id(self)}:"
        self.session = None
        self.busy = False
        self.recording = False
        self.pending_operation = None
        self.closed = False
        self.poll_after_id = None
        self.windows = []
        self.window_map = {}
        self.selected_handles = ()
        self.primary_handle = None
        self._restore_original_windows = True
        self.hotkey_down = dict.fromkeys(RECORDER_HOTKEYS, False)

        number = len(self.repository.list_supplements()) + 1
        self.label_var = tk.StringVar(value=f"补录 {number}")
        self.window_summary_var = tk.StringVar(value="未选择目标窗口")
        self.capture_summary_var = tk.StringVar(
            value=_capture_profile_summary(
                self.capture_config,
                self.backend,
            )
        )
        self.status_var = tk.StringVar(value="准备补录缺失操作")
        self.event_count_var = tk.StringVar(value="已捕获 0 个事件")

        self.dialog = tk.Toplevel(parent)
        self.dialog.title("补录缺失操作")
        self.dialog.geometry("560x250+150+110")
        self.dialog.minsize(500, 230)
        self.dialog.attributes("-topmost", True)
        self.dialog.protocol("WM_DELETE_WINDOW", self.close)
        self.status_overlay = RecordingStatusOverlay(
            self.dialog,
            capture_name="补录",
            artifact_name="本次补录",
        )
        self.target_highlight = create_recording_target_highlight(
            self.dialog,
            factory=highlight_factory,
        )

        self.settings_frame = None
        self.start_button = None
        self.window_select_button = None
        self._build_ui()
        self._refresh_windows()
        self._schedule_poll()

    def show(self):
        self.dialog.deiconify()
        self.dialog.lift()
        self.dialog.focus_force()
        return self

    def _build_ui(self):
        header = ttk.Frame(self.dialog)
        header.pack(fill="x", padx=14, pady=(14, 8))
        ttk.Label(
            header,
            text="补录缺失操作",
            font=("Microsoft YaHei UI", 13, "bold"),
        ).pack(side="left")
        ttk.Label(
            header,
            textvariable=self.status_var,
        ).pack(side="right")

        self.settings_frame = ttk.Frame(self.dialog)
        self.settings_frame.pack(fill="both", expand=True, padx=14)
        self.settings_frame.columnconfigure(1, weight=1)
        ttk.Label(self.settings_frame, text="目标窗口").grid(
            row=0,
            column=0,
            sticky="w",
            pady=7,
        )
        ttk.Label(
            self.settings_frame,
            textvariable=self.window_summary_var,
            anchor="w",
        ).grid(row=0, column=1, sticky="ew", padx=(10, 8), pady=7)
        self.window_select_button = ttk.Button(
            self.settings_frame,
            text="选择窗口",
            command=self.open_window_selector,
        )
        self.window_select_button.grid(row=0, column=2, pady=7)
        ttk.Label(
            self.settings_frame,
            text="采集方式",
        ).grid(row=1, column=0, sticky="w", pady=7)
        ttk.Label(
            self.settings_frame,
            textvariable=self.capture_summary_var,
            anchor="w",
        ).grid(
            row=1,
            column=1,
            columnspan=2,
            sticky="ew",
            padx=(10, 0),
            pady=7,
        )

        actions = ttk.Frame(self.settings_frame)
        actions.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(18, 6))
        self.start_button = ttk.Button(
            actions,
            text="开始补录",
            command=self.start_recording,
        )
        self.start_button.pack(side="right")
        ttk.Button(actions, text="取消", command=self.close).pack(
            side="right",
            padx=8,
        )

    def _refresh_windows(self):
        try:
            windows = list(self.window_provider(backend=self.backend) or ())
        except TypeError:
            windows = list(self.window_provider(self.backend) or ())
        self.windows = [
            item
            for item in windows
            if item.get("handle")
            and item.get("process_id") != os.getpid()
            and str(item.get("class_name") or "").casefold()
            not in {"shell_traywnd", "shell_secondarytraywnd"}
        ]
        self.windows.sort(key=lambda item: (
            str(item.get("title") or "").casefold(),
            int(item.get("handle") or 0),
        ))
        self.window_map = {
            int(item["handle"]): item
            for item in self.windows
        }
        self.selected_handles = tuple(
            handle
            for handle in self.selected_handles
            if handle in self.window_map
        )
        if not self.selected_handles:
            if self._restore_original_windows:
                recorded_windows, recorded_primary = (
                    _load_take_window_selection(self.take_dir)
                )
                self.selected_handles = _restore_window_handles(
                    recorded_windows,
                    self.windows,
                )
                restored_primary = _restore_window_handles(
                    [recorded_primary] if recorded_primary else [],
                    self.windows,
                )
                if (
                        restored_primary
                        and restored_primary[0] in self.selected_handles
                ):
                    self.primary_handle = restored_primary[0]
        if self.primary_handle not in self.selected_handles:
            self.primary_handle = next(iter(self.selected_handles), None)
        self._update_window_summary()
        return self.windows

    def open_window_selector(self):
        if self.busy or self.recording:
            return
        RecorderWindowSelector(
            self.dialog,
            self.windows,
            self.selected_handles,
            self.primary_handle,
            self._apply_window_selection,
            self._refresh_windows,
            allow_empty=self.window_capture_mode == "auto",
        ).show()

    def _apply_window_selection(self, handles, primary_handle):
        self._restore_original_windows = False
        self.selected_handles = tuple(int(handle) for handle in handles)
        self.primary_handle = (
            int(primary_handle) if primary_handle is not None else None
        )
        self._update_window_summary()

    def _update_window_summary(self):
        summary = format_window_selection_summary(
            self.window_map,
            self.selected_handles,
            self.primary_handle,
            self.window_capture_mode,
        )
        if self.selected_handles:
            prefix = (
                "沿用原录制"
                if self._restore_original_windows
                else "本次补录已调整"
            )
            summary = f"{prefix} · {summary}"
        elif self.window_capture_mode == "auto":
            summary = "沿用原录制 · 自动跟随：首次业务操作确定主窗口"
        else:
            summary = "原录制窗口不可用 · 开始前需选择窗口"
        self.window_summary_var.set(summary)
        if self.window_select_button is not None:
            self.window_select_button.configure(
                text=(
                    "调整窗口（可选）"
                    if self.selected_handles
                    or self.window_capture_mode == "auto"
                    else "选择窗口（必需）"
                )
            )

    def start_recording(self):
        if self.busy or self.recording:
            return
        try:
            self._refresh_windows()
        except Exception as error:
            detail = f"{type(error).__name__}: {error}"
            self.status_var.set(f"刷新窗口失败: {detail}")
            messagebox.showerror(
                "无法刷新窗口",
                detail,
                parent=self.dialog,
            )
            return
        if not self.selected_handles and self.window_capture_mode == "strict":
            self.status_var.set("原录制窗口已失效，请重新选择目标窗口")
            self.open_window_selector()
            return
        windows = [
            self.window_map[handle]
            for handle in self.selected_handles
            if handle in self.window_map
        ]
        try:
            self.session = self.session_factory(
                self.take_dir,
                backend=self.backend,
                windows=windows,
                label=self.label_var.get().strip(),
                target_hover=bool(self.target_highlight.available),
                hover_notification=self.target_highlight.post_notification,
            )
        except Exception as error:
            detail = f"{type(error).__name__}: {error}"
            self.status_var.set(f"启动补录失败: {detail}")
            messagebox.showerror(
                "无法开始补录",
                detail,
                parent=self.dialog,
            )
            return
        self.busy = True
        self.pending_operation = "start"
        self.start_button.configure(state="disabled")
        self.status_var.set("正在启动")
        self.status_overlay.show(
            "preparing",
            self.label_var.get().strip(),
        )
        self.operations.submit(
            f"{self.operation_prefix}capture",
            self._start_session,
            context="start",
            pass_token=True,
        )

    def _start_session(self, token):
        token.raise_if_cancelled()
        result = self.session.start()
        if token.cancelled:
            self.session.discard()
        token.raise_if_cancelled()
        return result

    def finish_recording(self):
        if self.busy or not self.recording or self.session is None:
            return
        self.busy = True
        self.pending_operation = "finish"
        self.status_var.set("正在保存")
        self.target_highlight.clear(force=True)
        self.status_overlay.show(
            "saving",
            self.label_var.get().strip(),
        )
        self.operations.submit(
            f"{self.operation_prefix}capture",
            self.session.finish,
            context="finish",
        )

    def discard_recording(self):
        if self.busy or self.session is None:
            return
        self.busy = True
        self.pending_operation = "discard"
        self.status_var.set("正在丢弃")
        self.target_highlight.clear(force=True)
        self.status_overlay.show(
            "discarding",
            self.label_var.get().strip(),
        )
        self.operations.submit(
            f"{self.operation_prefix}capture",
            self.session.discard,
            context="discard",
        )

    def _schedule_poll(self):
        if self.closed or self.poll_after_id is not None:
            return
        self.poll_after_id = self.dialog.after(100, self._poll)

    def _poll(self):
        self.poll_after_id = None
        if self.closed:
            return
        try:
            if self.recording and self.session is not None:
                self.event_count_var.set(
                    f"已捕获 {self.session.event_count} 个事件"
                )
                poll_hotkeys(self.hotkey_down, (
                    (VK_F7, self.toggle_pause),
                    (VK_F9, self.capture_observation),
                    (VK_F10, self.finish_recording),
                    (VK_F11, self._discard_hotkey),
                ))
                self._drain_capture_notifications()
            for task in self.operations.drain(key_prefix=self.operation_prefix):
                if task.status in {"cancelled", "superseded"}:
                    continue
                self._handle_task_result(task.context, task.value, task.error)
        finally:
            self._schedule_poll()

    def _discard_hotkey(self):
        if not is_key_down(VK_SHIFT):
            self.status_var.set("为防止误触，请按 Shift+F11 丢弃补录片段")
            return
        self.discard_recording()

    def _handle_task_result(self, operation, value, error):
        self.busy = False
        self.pending_operation = None
        if error is not None:
            self.recording = False
            self._restore_parent()
            self.status_overlay.hide()
            self.dialog.deiconify()
            self.dialog.lift()
            self.start_button.configure(state="normal")
            self.status_var.set(f"失败: {type(error).__name__}: {error}")
            return
        if operation == "start":
            reset_hotkeys(self.hotkey_down)
            self.recording = True
            self.event_count_var.set(
                f"已捕获 {self.session.event_count} 个事件"
            )
            self.status_var.set("录制中")
            self.status_overlay.show(
                "recording",
                self.label_var.get().strip(),
            )
            if self.minimize_window:
                self.window_controller.minimize_for_capture()
            self.dialog.withdraw()
            return
        if operation == "finish":
            self.recording = False
            self._restore_parent()
            try:
                self.on_completed(value)
            finally:
                self._destroy()
            return
        if operation == "discard":
            self.recording = False
            self._restore_parent()
            self._destroy()

    def toggle_pause(self):
        if self.busy or not self.recording or self.session is None:
            return
        try:
            paused = self.session.toggle_pause(note="")
        except Exception as error:
            self.status_var.set(
                f"暂停切换失败: {type(error).__name__}: {error}"
            )
            return
        self.status_var.set("已暂停" if paused else "录制中")
        self.status_overlay.show(
            "paused" if paused else "recording",
            self.label_var.get().strip(),
        )

    def capture_observation(self):
        if (
                self.busy
                or not self.recording
                or self.session is None
                or getattr(self.session, "is_paused", False)
        ):
            return
        try:
            event_id = self.session.record_observation(note="")
        except Exception as error:
            self.status_var.set(
                f"观察采集失败: {type(error).__name__}: {error}"
            )
            return
        self.status_var.set(f"正在分析观察目标: {event_id}")
        self.status_overlay.show_observation_pending(event_id)

    def _drain_capture_notifications(self):
        drain_windows = getattr(
            self.session,
            "drain_window_notifications",
            None,
        )
        windows = drain_windows() if callable(drain_windows) else []
        for window in windows:
            title = window.get("title") or window.get("class_name") or "新窗口"
            self.status_overlay.show_window_notice(
                title,
                provisional=window.get("admission") == "provisional",
            )
        drain_observations = getattr(
            self.session,
            "drain_observation_notifications",
            None,
        )
        receipts = drain_observations() if callable(drain_observations) else []
        for receipt in receipts:
            target = receipt.get("target") or {}
            status = receipt.get("status")
            if status == "failed":
                self.status_var.set(
                    f"观察失败: {receipt.get('message') or '未识别到目标'}"
                )
                self.status_overlay.show_observation_receipt(receipt)
                continue
            prefix = "已观察" if status == "captured" else "已观察，建议复核"
            self.status_var.set(
                f"{prefix}: {target.get('control_type') or '控件'} "
                f"{target.get('name') or '未命名'}"
            )
            self.status_overlay.show_observation_receipt(receipt)

    def close(self):
        if self.closed or self.busy:
            return
        if self.recording:
            if not messagebox.askyesno(
                    "丢弃补录",
                    "补录仍在进行，丢弃当前片段吗？",
                    parent=self.dialog,
            ):
                return
            self.discard_recording()
            return
        self._restore_parent()
        self._destroy()

    def force_close(self):
        if self.closed:
            return
        pending_operation = self.pending_operation
        self.operations.abandon_prefix(self.operation_prefix)
        if self.session is not None:
            try:
                request_close = getattr(self.session, "request_close", None)
                if callable(request_close):
                    request_close()
                elif pending_operation is None:
                    self.session.discard()
            except Exception:
                pass
        self.busy = False
        self.recording = False
        self._restore_parent()
        self._destroy()

    def _restore_parent(self):
        if self.minimize_window:
            self.window_controller.restore_after_capture("timeline")

    def _destroy(self):
        if self.closed:
            return
        self.closed = True
        self.operations.abandon_prefix(self.operation_prefix)
        if self._owns_operations:
            self.operations.shutdown(wait=True)
        self.target_highlight.destroy()
        self.status_overlay.destroy()
        if self.poll_after_id is not None:
            try:
                self.dialog.after_cancel(self.poll_after_id)
            except tk.TclError:
                pass
            self.poll_after_id = None
        self.dialog.destroy()


class _ToplevelCaptureWindowController:
    def __init__(self, widget):
        self.window = widget.winfo_toplevel()

    def minimize_for_capture(self):
        try:
            self.window.iconify()
        except tk.TclError:
            pass

    def restore_after_capture(self, _view):
        try:
            self.window.deiconify()
            self.window.lift()
        except tk.TclError:
            pass


def _load_take_window_selection(take_dir):
    path = Path(take_dir) / "take.json"
    if not path.exists():
        return [], None
    try:
        import json

        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], None
    windows = list(
        value.get("target_windows")
        or [value.get("target_window") or {}]
    )
    return windows, value.get("target_window") or next(iter(windows), None)


def _capture_profile_summary(config, backend):
    video = "视频开" if config.get("with_video", True) else "视频关"
    screenshots = (
        "截图开" if config.get("with_screenshots", True) else "截图关"
    )
    mode = {
        "auto": "自动跟随",
        "strict": "严格窗口",
    }.get(
        str(config.get("window_capture_mode") or "strict").strip().lower(),
        "严格窗口",
    )
    monitor = max(1, int(config.get("monitor_index") or 1))
    return (
        f"沿用原录制 · {str(backend).upper()} · {mode} · "
        f"{screenshots} · {video} · 屏幕 {monitor}"
    )
