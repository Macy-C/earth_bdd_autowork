"""Windows-native, non-activating indicator for an active Behave run."""

from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes

import win32api
import win32con
import win32gui


class AutomationRunIndicator:
    TITLE = "自动化测试运行中"
    WIDTH = 360
    HEIGHT = 56
    _STATE_MESSAGE = win32con.WM_APP + 41
    _TIMER_ID = 1

    def __init__(
            self,
            title=TITLE,
            *,
            max_scenario_chars=28,
            clock=None,
    ):
        self.title = str(title or self.TITLE)
        self.max_scenario_chars = max(8, int(max_scenario_chars or 28))
        self._clock = clock or time.monotonic
        self._scenario_name = ""
        self._started_at = None
        self._visible = False
        self._handle = None
        self._thread = None
        self._ready = threading.Event()
        self._state_applied = threading.Event()
        self._lock = threading.RLock()
        self._class_name = f"AutoworkRunIndicator_{id(self):x}"
        self._brush = None

    @property
    def handle(self):
        with self._lock:
            return self._handle

    @property
    def display_lines(self):
        with self._lock:
            scenario_name = self._scenario_name
            started_at = self._started_at
        return self.title, self._detail_text(
            scenario_name,
            started_at,
            self._clock(),
        )

    def start(self):
        with self._lock:
            if self._started_at is None:
                self._started_at = self._clock()

    def show(self, scenario_name=""):
        with self._lock:
            self._scenario_name = str(scenario_name or "当前场景")
            if self._started_at is None:
                self._started_at = self._clock()
            self._visible = True
        self._ensure_thread()
        self._apply_state()
        handle = self.handle
        return bool(
            handle
            and win32gui.IsWindow(handle)
            and win32gui.IsWindowVisible(handle)
        )

    def hide(self):
        with self._lock:
            self._visible = False
        self._apply_state()

    def close(self):
        handle = self.handle
        thread = self._thread
        if handle and win32gui.IsWindow(handle):
            try:
                win32gui.PostMessage(handle, win32con.WM_CLOSE, 0, 0)
            except win32gui.error:
                pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=3)
        with self._lock:
            self._visible = False
            self._scenario_name = ""
            self._started_at = None
            self._handle = None
            self._thread = None

    def _ensure_thread(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._ready.clear()
            self._thread = threading.Thread(
                target=self._run_window,
                name="autowork-run-indicator",
                daemon=True,
            )
            self._thread.start()
        if not self._ready.wait(timeout=3):
            raise TimeoutError("自动化运行提示窗口启动超时")
        if not self.handle:
            raise RuntimeError("自动化运行提示窗口创建失败")

    def _apply_state(self):
        handle = self.handle
        if not handle or not win32gui.IsWindow(handle):
            return
        self._state_applied.clear()
        try:
            win32gui.PostMessage(handle, self._STATE_MESSAGE, 0, 0)
        except win32gui.error:
            return
        self._state_applied.wait(timeout=2)

    def _run_window(self):
        instance = win32api.GetModuleHandle(None)
        try:
            self._brush = win32gui.CreateSolidBrush(
                win32api.RGB(15, 118, 110)
            )
            window_class = win32gui.WNDCLASS()
            window_class.hInstance = instance
            window_class.lpszClassName = self._class_name
            window_class.hbrBackground = self._brush
            window_class.lpfnWndProc = self._window_proc
            win32gui.RegisterClass(window_class)
            handle = win32gui.CreateWindowEx(
                (
                    win32con.WS_EX_TOPMOST
                    | win32con.WS_EX_TOOLWINDOW
                    | win32con.WS_EX_NOACTIVATE
                    | win32con.WS_EX_LAYERED
                    | win32con.WS_EX_TRANSPARENT
                ),
                self._class_name,
                self.title,
                win32con.WS_POPUP,
                0,
                0,
                self.WIDTH,
                self.HEIGHT,
                0,
                0,
                instance,
                None,
            )
            win32gui.SetLayeredWindowAttributes(
                handle,
                0,
                255,
                win32con.LWA_ALPHA,
            )
            with self._lock:
                self._handle = handle
            self._ready.set()
            win32gui.PumpMessages()
        finally:
            self._ready.set()
            with self._lock:
                self._handle = None
            try:
                win32gui.UnregisterClass(self._class_name, instance)
            except win32gui.error:
                pass
            if self._brush is not None:
                try:
                    win32gui.DeleteObject(self._brush)
                except (AttributeError, win32gui.error):
                    pass
                self._brush = None

    def _window_proc(self, handle, message, wparam, lparam):
        if message == win32con.WM_NCHITTEST:
            return win32con.HTTRANSPARENT
        if message == win32con.WM_MOUSEACTIVATE:
            return win32con.MA_NOACTIVATE
        if message == self._STATE_MESSAGE:
            self._show_or_hide(handle)
            self._state_applied.set()
            return 0
        if message == win32con.WM_TIMER:
            win32gui.InvalidateRect(handle, None, True)
            return 0
        if message == win32con.WM_ERASEBKGND:
            return 1
        if message == win32con.WM_PAINT:
            self._paint(handle)
            return 0
        if message == win32con.WM_CLOSE:
            win32gui.DestroyWindow(handle)
            return 0
        if message == win32con.WM_DESTROY:
            try:
                ctypes.windll.user32.KillTimer(handle, self._TIMER_ID)
            except (AttributeError, OSError):
                pass
            win32gui.PostQuitMessage(0)
            return 0
        return win32gui.DefWindowProc(handle, message, wparam, lparam)

    def _show_or_hide(self, handle):
        with self._lock:
            visible = self._visible
        if not visible:
            ctypes.windll.user32.KillTimer(handle, self._TIMER_ID)
            win32gui.ShowWindow(handle, win32con.SW_HIDE)
            return
        left, top = self._position()
        win32gui.SetWindowText(handle, self.title)
        ctypes.windll.user32.SetTimer(
            handle,
            self._TIMER_ID,
            1000,
            None,
        )
        win32gui.SetWindowPos(
            handle,
            win32con.HWND_TOPMOST,
            left,
            top,
            self.WIDTH,
            self.HEIGHT,
            win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW,
        )
        win32gui.InvalidateRect(handle, None, True)
        win32gui.UpdateWindow(handle)

    def _paint(self, handle):
        device, paint = win32gui.BeginPaint(handle)
        try:
            rect = win32gui.GetClientRect(handle)
            win32gui.FillRect(device, rect, self._brush)
            win32gui.SetBkMode(device, win32con.TRANSPARENT)
            win32gui.SetTextColor(device, win32api.RGB(255, 255, 255))
            try:
                font = win32gui.GetStockObject(win32con.DEFAULT_GUI_FONT)
                previous = win32gui.SelectObject(device, font)
            except (AttributeError, win32gui.error):
                previous = None
            with self._lock:
                scenario_name = self._scenario_name
                started_at = self._started_at
            scenario, duration = self._detail_parts(
                scenario_name,
                started_at,
                self._clock(),
            )
            self._draw_text(
                device,
                self.title,
                (12, 4, self.WIDTH - 12, 27),
                win32con.DT_LEFT,
            )
            win32gui.SetTextColor(device, win32api.RGB(204, 251, 241))
            self._draw_text(
                device,
                scenario,
                (12, 27, self.WIDTH - 74, self.HEIGHT - 4),
                win32con.DT_LEFT | win32con.DT_END_ELLIPSIS,
            )
            self._draw_text(
                device,
                duration,
                (self.WIDTH - 70, 27, self.WIDTH - 12, self.HEIGHT - 4),
                win32con.DT_RIGHT,
            )
            if previous is not None:
                win32gui.SelectObject(device, previous)
        finally:
            win32gui.EndPaint(handle, paint)

    def _position(self):
        try:
            monitor = win32api.MonitorFromPoint(
                win32api.GetCursorPos(),
                win32con.MONITOR_DEFAULTTONEAREST,
            )
            left, top, right, _bottom = win32api.GetMonitorInfo(monitor)[
                "Work"
            ]
        except (AttributeError, OSError, win32gui.error):
            left, top = 0, 0
            right = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
        return max(left + 12, right - self.WIDTH - 16), top + 16

    def _detail_text(self, scenario_name, started_at, now):
        scenario, duration = self._detail_parts(
            scenario_name,
            started_at,
            now,
        )
        return f"{scenario}  |  {duration}"

    def _detail_parts(self, scenario_name, started_at, now):
        scenario = self._truncate(scenario_name or "当前场景")
        elapsed = max(0, int(now - started_at)) if started_at is not None else 0
        hours, remainder = divmod(elapsed, 3600)
        minutes, seconds = divmod(remainder, 60)
        duration = (
            f"{hours:d}:{minutes:02d}:{seconds:02d}"
            if hours
            else f"{minutes:02d}:{seconds:02d}"
        )
        return scenario, duration

    @staticmethod
    def _draw_text(device, text, rect, alignment):
        bounds = wintypes.RECT(*rect)
        flags = (
            alignment
            | win32con.DT_SINGLELINE
            | win32con.DT_VCENTER
            | win32con.DT_NOPREFIX
        )
        ctypes.windll.user32.DrawTextW(
            device,
            str(text),
            -1,
            ctypes.byref(bounds),
            flags,
        )

    def _truncate(self, value):
        value = str(value or "")
        if len(value) <= self.max_scenario_chars:
            return value
        return value[: self.max_scenario_chars - 1].rstrip() + "…"