"""提供运行时和调试 GUI 共用的 Windows 透明覆盖层。"""

import win32api
import win32con
import win32gui


class OverlayManager:
    CLASS_NAME = "AutoworkClickThroughOverlay"

    def __init__(self):
        self.overlays = []
        self.class_registered = False
        self.brush = win32gui.CreateSolidBrush(win32api.RGB(0, 0, 0))
        self._register_class()

    def _register_class(self):
        if self.class_registered:
            return

        try:
            wc = win32gui.WNDCLASS()
            wc.hInstance = win32api.GetModuleHandle(None)
            wc.lpszClassName = self.CLASS_NAME
            wc.hbrBackground = self.brush
            wc.lpfnWndProc = self._wnd_proc
            win32gui.RegisterClass(wc)
        except Exception:
            pass

        self.class_registered = True

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == win32con.WM_NCHITTEST:
            return win32con.HTTRANSPARENT

        if msg == win32con.WM_ERASEBKGND:
            hdc = wparam
            rect = win32gui.GetClientRect(hwnd)
            win32gui.FillRect(hdc, rect, self.brush)
            return 1

        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def clear(self):
        for hwnd in self.overlays:
            try:
                if win32gui.IsWindow(hwnd):
                    win32gui.DestroyWindow(hwnd)
            except Exception:
                pass

        self.overlays.clear()

    def show_rect(self, rect, alpha=105):
        left = int(rect.left)
        top = int(rect.top)
        right = int(rect.right)
        bottom = int(rect.bottom)

        width = right - left
        height = bottom - top

        if width <= 0 or height <= 0:
            return

        ex_style = (
            win32con.WS_EX_LAYERED
            | win32con.WS_EX_TRANSPARENT
            | win32con.WS_EX_NOACTIVATE
            | win32con.WS_EX_TOOLWINDOW
            | win32con.WS_EX_TOPMOST
        )

        hwnd = win32gui.CreateWindowEx(
            ex_style,
            self.CLASS_NAME,
            "",
            win32con.WS_POPUP,
            left,
            top,
            width,
            height,
            0,
            0,
            win32api.GetModuleHandle(None),
            None,
        )

        win32gui.SetLayeredWindowAttributes(
            hwnd,
            0,
            int(alpha),
            win32con.LWA_ALPHA,
        )

        win32gui.SetWindowPos(
            hwnd,
            win32con.HWND_TOPMOST,
            left,
            top,
            width,
            height,
            win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW,
        )

        win32gui.InvalidateRect(hwnd, None, True)
        win32gui.UpdateWindow(hwnd)

        self.overlays.append(hwnd)