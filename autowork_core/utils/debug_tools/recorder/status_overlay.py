from __future__ import annotations

import tkinter as tk

import win32api
import win32con
import win32gui


class RecordingStatusOverlay:
    """Topmost recording status that never takes focus from the target app."""

    def __init__(
            self,
            parent,
            *,
            capture_name="录制",
            artifact_name="录制",
        ):
        self.parent = parent
        self.capture_name = str(capture_name or "录制")
        self.artifact_name = str(artifact_name or self.capture_name)
        self.context_text = ""
        self.base_state = "recording"
        self.window = tk.Toplevel(parent)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self._restore_after_id = None

        self.frame = tk.Frame(
            self.window,
            background="#166534",
            highlightthickness=1,
            highlightbackground="#ffffff",
        )
        self.frame.pack(fill="both", expand=True)
        self.state_label = tk.Label(
            self.frame,
            text="",
            background="#166534",
            foreground="#ffffff",
            font=("Microsoft YaHei UI", 13, "bold"),
            anchor="w",
        )
        self.state_label.pack(fill="x", padx=14, pady=(10, 1))
        self.detail_label = tk.Label(
            self.frame,
            text="",
            background="#166534",
            foreground="#ffffff",
            font=("Microsoft YaHei UI", 9),
            anchor="w",
        )
        self.detail_label.pack(fill="x", padx=14, pady=(0, 9))

    def show(self, state, step_text=""):
        self._cancel_restore()
        if state in {"preparing", "recording", "paused", "saving", "discarding"}:
            self.base_state = state
            self.context_text = str(step_text or self.context_text)
        title, detail, color = {
            "preparing": (
                f"正在准备{self.capture_name}",
                "请切换到目标窗口，稍候将自动开始",
                "#92400e",
            ),
            "recording": (
                f"正在{self.capture_name}",
                "F7 暂停  |  F9 标记观察目标  |  F10 保存  |  Shift+F11 放弃",
                "#166534",
            ),
            "paused": (
                f"{self.capture_name}已暂停",
                "准备操作不会写入业务动作  |  按 F7 继续  |  Shift+F11 放弃",
                "#9a3412",
            ),
            "saving": (
                f"正在保存{self.artifact_name}",
                "正在整理视频、截图、事件与时间线，请稍候",
                "#1d4ed8",
            ),
            "discarding": (
                f"正在放弃本次{self.artifact_name}",
                "正在清理本次录制，请稍候",
                "#374151",
            ),
            "window_auto": (
                "已自动纳入新窗口",
                "录制继续进行；窗口证据已开始采集",
                "#0369a1",
            ),
            "window_provisional": (
                "发现待确认窗口",
                "录制继续进行；结束后在审阅中心确认是否属于业务流程",
                "#a16207",
            ),
            "observation_pending": (
                "F9 正在采集目标",
                "正在读取鼠标位置、UIA 目标、祖先范围与定位质量",
                "#1d4ed8",
            ),
        }.get(
            state,
            ("录制器", "", "#374151"),
        )
        compact = state == "recording"
        if compact:
            title = (
                f"{title}  ·  F7 暂停  ·  F9 观察  ·  F10 保存"
                "  ·  Shift+F11 放弃"
            )
        suffix = f"  ·  {step_text}" if step_text and not compact else ""
        self._set_compact(compact)
        self.state_label.configure(text=title + suffix, background=color)
        self.detail_label.configure(text=detail, background=color)
        self.frame.configure(background=color)
        self.window.update_idletasks()
        if compact:
            requested_width = max(
                self.window.winfo_reqwidth(),
                self.state_label.winfo_reqwidth() + 24,
            )
            width = min(
                max(requested_width, 260),
                self._overlay_max_width(),
            )
            height = max(self.window.winfo_reqheight(), 40)
        else:
            width = min(max(self.window.winfo_reqwidth(), 560), 760)
            height = max(self.window.winfo_reqheight(), 68)
        self._show_non_activating(width, height)

    def show_window_notice(self, title, provisional=False):
        self._show_message(
            "发现待确认窗口" if provisional else "已自动纳入新窗口",
            (
                "录制继续进行；结束后在审阅中心确认是否属于业务流程"
                if provisional
                else f"录制继续进行；窗口证据已开始采集：{title}"
            ),
            "#a16207" if provisional else "#0369a1",
        )
        self._restore_after_id = self.window.after(
            2600,
            self._restore_recording_status,
        )

    def show_observation_pending(self, event_id):
        self._show_message(
            "F9 正在采集目标",
            f"正在读取鼠标位置、UIA 目标、祖先范围与定位质量：{event_id}",
            "#1d4ed8",
        )
        self._restore_after_id = self.window.after(
            5000,
            self._restore_recording_status,
        )

    def show_observation_receipt(self, receipt):
        status = receipt.get("status")
        target = receipt.get("target") or {}
        scope = receipt.get("scope") or {}
        locator = receipt.get("locator") or {}
        title = {
            "captured": "F9 已采集目标",
            "warning": "F9 已采集，建议复核",
            "failed": "F9 目标采集失败",
        }.get(status, "F9 采集结果")
        color = {
            "captured": "#166534",
            "warning": "#a16207",
            "failed": "#b91c1c",
        }.get(status, "#374151")
        if status == "failed":
            detail = str(receipt.get("message") or "未识别到目标")
        else:
            detail = (
                f"{target.get('control_type') or '控件'}："
                f"{target.get('name') or '未命名'}  |  "
                f"范围：{scope.get('name') or '当前窗口'}  |  "
                f"定位：{locator.get('validation') or '未验证'}"
            )
        self._show_message(title, detail, color)
        self._restore_after_id = self.window.after(
            3200,
            self._restore_recording_status,
        )

    def hide(self):
        self._cancel_restore()
        try:
            self.window.withdraw()
        except tk.TclError:
            pass

    def destroy(self):
        self._cancel_restore()
        try:
            self.window.destroy()
        except tk.TclError:
            pass

    def _show_non_activating(self, width, height):
        x, y = self._overlay_position(width, height)
        self.window.geometry(f"{width}x{height}+{x}+{y}")
        self.window.update_idletasks()
        try:
            handle = int(self.window.wm_frame(), 0)
            style = win32gui.GetWindowLong(handle, win32con.GWL_EXSTYLE)
            win32gui.SetWindowLong(
                handle,
                win32con.GWL_EXSTYLE,
                style
                | win32con.WS_EX_NOACTIVATE
                | win32con.WS_EX_TOOLWINDOW
                | win32con.WS_EX_LAYERED
                | win32con.WS_EX_TRANSPARENT,
            )
            win32gui.SetLayeredWindowAttributes(
                handle,
                0,
                255,
                win32con.LWA_ALPHA,
            )
            win32gui.SetWindowPos(
                handle,
                win32con.HWND_TOPMOST,
                x,
                y,
                width,
                height,
                win32con.SWP_NOACTIVATE
                | win32con.SWP_SHOWWINDOW,
            )
        except (AttributeError, OSError, tk.TclError, ValueError):
            self.window.deiconify()
            self.window.lift()

    @staticmethod
    def _overlay_max_width():
        try:
            monitor = win32api.MonitorFromPoint(
                win32api.GetCursorPos(),
                win32con.MONITOR_DEFAULTTONEAREST,
            )
            left, _top, right, _bottom = win32api.GetMonitorInfo(
                monitor
            )["Work"]
            return max(1, int(right) - int(left) - 36)
        except (AttributeError, OSError):
            return max(1, win32api.GetSystemMetrics(win32con.SM_CXSCREEN) - 36)

    @staticmethod
    def _overlay_position(width, height):
        try:
            monitor = win32api.MonitorFromPoint(
                win32api.GetCursorPos(),
                win32con.MONITOR_DEFAULTTONEAREST,
            )
            left, top, right, bottom = win32api.GetMonitorInfo(monitor)["Work"]
        except (AttributeError, OSError):
            left, top = 0, 0
            right = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
            bottom = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)
        x = max(left + 12, right - width - 24)
        y = min(max(top + 20, top + 12), max(top + 12, bottom - height - 12))
        return x, y

    def _show_message(self, title, detail, color):
        self._cancel_restore()
        self._set_compact(False)
        self.state_label.configure(text=title, background=color)
        self.detail_label.configure(text=detail, background=color)
        self.frame.configure(background=color)
        self.window.update_idletasks()
        width = min(max(self.window.winfo_reqwidth(), 560), 920)
        height = max(self.window.winfo_reqheight(), 68)
        self._show_non_activating(width, height)

    def _set_compact(self, compact):
        if compact:
            self.detail_label.pack_forget()
            self.state_label.pack_configure(padx=12, pady=7)
            return
        self.state_label.pack_configure(padx=14, pady=(10, 1))
        if not self.detail_label.winfo_manager():
            self.detail_label.pack(fill="x", padx=14, pady=(0, 9))

    def _restore_recording_status(self):
        self._restore_after_id = None
        self.show(self.base_state, self.context_text)

    def _cancel_restore(self):
        if self._restore_after_id is None:
            return
        try:
            self.window.after_cancel(self._restore_after_id)
        except tk.TclError:
            pass
        self._restore_after_id = None
