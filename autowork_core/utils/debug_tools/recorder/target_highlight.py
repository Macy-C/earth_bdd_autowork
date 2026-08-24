from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

import win32api
import win32con
import win32gui


WDA_EXCLUDEFROMCAPTURE = 0x00000011
PREVIEW_COLOR = (0, 194, 215)
_CLASS_NAME = "AutoworkRecorderTargetHighlight"
_UPDATE_MESSAGE = win32con.WM_APP + 0x5A1
_WINDOW_COLORS = {}
_WINDOW_OWNERS = {}
_CLASS_BRUSH = None
_CLASS_REGISTERED = False


def _window_proc(hwnd, message, wparam, lparam):
    if message == _UPDATE_MESSAGE:
        owner = _WINDOW_OWNERS.get(int(hwnd))
        if owner is not None:
            owner._apply_pending_notification()
        return 0
    if message == win32con.WM_NCHITTEST:
        return win32con.HTTRANSPARENT
    if message == win32con.WM_ERASEBKGND:
        color = _WINDOW_COLORS.get(int(hwnd), PREVIEW_COLOR)
        brush = win32gui.CreateSolidBrush(win32api.RGB(*color))
        try:
            win32gui.FillRect(wparam, win32gui.GetClientRect(hwnd), brush)
        finally:
            win32gui.DeleteObject(brush)
        return 1
    if message == win32con.WM_DESTROY:
        _WINDOW_COLORS.pop(int(hwnd), None)
        _WINDOW_OWNERS.pop(int(hwnd), None)
        return 0
    return win32gui.DefWindowProc(hwnd, message, wparam, lparam)


def _register_window_class():
    global _CLASS_BRUSH, _CLASS_REGISTERED
    if _CLASS_REGISTERED:
        return True
    window_class = win32gui.WNDCLASS()
    window_class.hInstance = win32api.GetModuleHandle(None)
    window_class.lpszClassName = _CLASS_NAME
    window_class.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
    _CLASS_BRUSH = win32gui.CreateSolidBrush(win32api.RGB(*PREVIEW_COLOR))
    window_class.hbrBackground = _CLASS_BRUSH
    window_class.lpfnWndProc = _window_proc
    try:
        win32gui.RegisterClass(window_class)
    except win32gui.error as error:
        if getattr(error, "winerror", None) != 1410:
            return False
    _CLASS_REGISTERED = True
    return True


def _exclude_from_capture(hwnd):
    set_affinity = ctypes.windll.user32.SetWindowDisplayAffinity
    set_affinity.argtypes = [wintypes.HWND, wintypes.DWORD]
    set_affinity.restype = wintypes.BOOL
    get_affinity = ctypes.windll.user32.GetWindowDisplayAffinity
    get_affinity.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    get_affinity.restype = wintypes.BOOL
    try:
        if not set_affinity(int(hwnd), WDA_EXCLUDEFROMCAPTURE):
            return False
        affinity = wintypes.DWORD()
        return bool(get_affinity(int(hwnd), ctypes.byref(affinity))) and (
            int(affinity.value) == WDA_EXCLUDEFROMCAPTURE
        )
    except (AttributeError, OSError, ValueError):
        return False


class RecordingTargetHighlight:
    def __init__(self, parent, *, thickness=4):
        self.parent = parent
        self.thickness = max(2, int(thickness))
        self.windows = []
        self.available = False
        self._pending_lock = threading.Lock()
        self._pending_notification = None
        self._create_windows()

    def post_notification(self, notification):
        if not self.available or not self.windows:
            return False
        with self._pending_lock:
            self._pending_notification = dict(notification or {})
        try:
            win32gui.PostMessage(
                self.windows[0],
                _UPDATE_MESSAGE,
                0,
                0,
            )
            return True
        except (OSError, win32gui.error):
            return False

    def _apply_pending_notification(self):
        with self._pending_lock:
            notification = self._pending_notification
            self._pending_notification = None
        if notification is not None:
            self.show_notification(notification)

    def show_notification(self, notification):
        try:
            kind = str((notification or {}).get("kind") or "")
            if kind == "clear":
                self.clear(force=True)
                return
            rectangle = ((notification or {}).get("target") or {}).get(
                "rectangle"
            )
            if kind == "preview":
                self.show_preview(rectangle)
        except Exception:
            self.clear(force=True)

    def show_preview(self, rectangle):
        return self._show(rectangle, PREVIEW_COLOR)

    def clear(self, *, force=False):
        for hwnd in self.windows:
            try:
                if win32gui.IsWindow(hwnd):
                    win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
            except (OSError, win32gui.error):
                pass

    hide = clear

    def destroy(self):
        for hwnd in tuple(self.windows):
            try:
                if win32gui.IsWindow(hwnd):
                    win32gui.DestroyWindow(hwnd)
            except (OSError, win32gui.error):
                pass
        self.windows.clear()
        self.available = False

    def _create_windows(self):
        if not _register_window_class():
            return
        extended_style = (
            win32con.WS_EX_LAYERED
            | win32con.WS_EX_TRANSPARENT
            | win32con.WS_EX_NOACTIVATE
            | win32con.WS_EX_TOOLWINDOW
            | win32con.WS_EX_TOPMOST
        )
        try:
            for _index in range(4):
                hwnd = win32gui.CreateWindowEx(
                    extended_style,
                    _CLASS_NAME,
                    "",
                    win32con.WS_POPUP,
                    0,
                    0,
                    1,
                    1,
                    0,
                    0,
                    win32api.GetModuleHandle(None),
                    None,
                )
                self.windows.append(hwnd)
                _WINDOW_COLORS[int(hwnd)] = PREVIEW_COLOR
                _WINDOW_OWNERS[int(hwnd)] = self
                win32gui.SetLayeredWindowAttributes(
                    hwnd,
                    0,
                    230,
                    win32con.LWA_ALPHA,
                )
                if not _exclude_from_capture(hwnd):
                    raise RuntimeError("高亮窗口无法排除在屏幕捕获之外")
        except Exception:
            self.destroy()
            return
        self.available = len(self.windows) == 4

    def _show(self, rectangle, color):
        if not self.available:
            return False
        bounds = _valid_rectangle(rectangle)
        if bounds is None:
            self.clear(force=True)
            return False
        left, top, right, bottom = bounds
        thickness = min(self.thickness, right - left, bottom - top)
        edges = (
            (left, top, right - left, thickness),
            (left, bottom - thickness, right - left, thickness),
            (
                left,
                top + thickness,
                thickness,
                max(1, bottom - top - thickness * 2),
            ),
            (
                right - thickness,
                top + thickness,
                thickness,
                max(1, bottom - top - thickness * 2),
            ),
        )
        try:
            for hwnd, (x, y, width, height) in zip(self.windows, edges):
                _WINDOW_COLORS[int(hwnd)] = color
                win32gui.SetWindowPos(
                    hwnd,
                    win32con.HWND_TOPMOST,
                    x,
                    y,
                    width,
                    height,
                    win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW,
                )
                win32gui.InvalidateRect(hwnd, None, True)
                win32gui.UpdateWindow(hwnd)
        except (OSError, win32gui.error):
            self.clear(force=True)
            return False
        return True

def _valid_rectangle(value):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        left, top, right, bottom = [int(item) for item in value]
    except (TypeError, ValueError):
        return None
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


class NullRecordingTargetHighlight:
    available = False

    def post_notification(self, _notification):
        return False

    def show_notification(self, _notification):
        return None

    def show_preview(self, _rectangle):
        return False

    def clear(self, *, force=False):
        return None

    hide = clear

    def destroy(self):
        return None


def create_recording_target_highlight(
        parent,
        factory=RecordingTargetHighlight,
    ):
    try:
        return factory(parent)
    except Exception:
        return NullRecordingTargetHighlight()