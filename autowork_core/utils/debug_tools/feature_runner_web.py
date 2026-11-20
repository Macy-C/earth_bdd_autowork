"""Modern Web UI host (D1 architecture) for BDD Autowork Feature Runner.

Provides a local ThreadingHTTPServer + Server-Sent Events (SSE) console stream,
renders `templates/feature_runner.html` with the bundled `lucide.min.js` SVG
icon library, launches a chromeless Edge `--app` desktop window, and coordinates
Win32 window minimization/restoration during test execution.
"""

from __future__ import annotations

from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
import tkinter as tk
from tkinter import filedialog
from typing import Any
from urllib.parse import urlparse
import webbrowser

try:
    import win32con  # type: ignore[import-untyped]
    import win32gui  # type: ignore[import-untyped]
    import win32process  # type: ignore[import-untyped]
except ImportError:
    win32con = None  # type: ignore[assignment]
    win32gui = None  # type: ignore[assignment]
    win32process = None  # type: ignore[assignment]

from autowork_core.utils.debug_tools.feature_runner_catalog import (
    FeatureCatalog,
    discover_failed_targets,
    discover_feature_catalog,
)
from autowork_core.utils.debug_tools.feature_runner_runtime import (
    ConsoleEvent,
    FeatureRunConfig,
    FeatureRunTarget,
    SubprocessFeatureRunner,
    apply_ocr_mode_preset,
    apply_pic_mode_preset,
    config_from_dict,
    config_from_project_settings,
    describe_run_target,
    load_runner_ui_state,
    local_debug_preset_config,
    render_local_runner_snippet,
    save_runner_ui_state,
)
from config.paths import Paths


WINDOW_TITLE = "BDD Autowork 用例运行器"
_TEMPLATE_PATH = Path(__file__).with_name("templates") / "feature_runner.html"
_LUCIDE_JS_PATH = (
    Paths.CORE_DIR / "runtime" / "reporting" / "templates" / "lucide.min.js"
)


def render_runner_workspace_html() -> str:
    html = _TEMPLATE_PATH.read_text(encoding="utf-8")
    marker = "<!-- runner-resource:lucide.min.js -->"
    if _LUCIDE_JS_PATH.is_file():
        lucide_code = _LUCIDE_JS_PATH.read_text(encoding="utf-8-sig")
        lucide_code = lucide_code.replace("</script", "<\\/script")
        html = html.replace(marker, f"<script>\n{lucide_code}\n</script>")
    return html


def _format_display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(Paths.BASE_DIR.resolve()).as_posix()
    except Exception:
        return str(path)


_CACHED_HICON: int | None = None


def _apply_window_icon(hwnd: int | None) -> None:
    """Set a clean blue BDD Autowork icon on the native Win32 app window."""
    if not hwnd or os.name != "nt":
        return
    global _CACHED_HICON
    try:
        import ctypes
        import struct

        user32 = ctypes.windll.user32
        if _CACHED_HICON is None:
            ico_path = Paths.ARTIFACTS_DIR / ".autowork_app_icon.ico"
            if not ico_path.is_file():
                ico_path.parent.mkdir(parents=True, exist_ok=True)
                w, h = 32, 32
                xor_mask = bytearray()
                for y in range(h - 1, -1, -1):
                    for x in range(w):
                        dx = max(0, max(5 - x, x - 26))
                        dy = max(0, max(5 - y, y - 26))
                        if (dx * dx + dy * dy) ** 0.5 > 5.5:
                            xor_mask.extend((0, 0, 0, 0))
                        elif (11 <= x <= 22) and (abs(y - 15.5) <= (22 - x) * 0.65):
                            xor_mask.extend((255, 255, 255, 255))
                        else:
                            xor_mask.extend((235, 99, 37, 255))
                and_mask = bytes(4 * h)
                bmp_hdr = struct.pack(
                    "<IIIHHIIIIII",
                    40, w, h * 2, 1, 32, 0, len(xor_mask) + len(and_mask), 0, 0, 0, 0
                )
                img_data = bmp_hdr + bytes(xor_mask) + and_mask
                ico_hdr = struct.pack("<HHH", 0, 1, 1) + struct.pack(
                    "<BBBBHHII", w, h, 0, 0, 1, 32, len(img_data), 22
                )
                ico_path.write_bytes(ico_hdr + img_data)
            _CACHED_HICON = int(
                user32.LoadImageW(0, str(ico_path), 1, 32, 32, 0x00000010) or 0
            )
        if _CACHED_HICON:
            user32.SendMessageW(hwnd, 0x0080, 0, _CACHED_HICON)
            user32.SendMessageW(hwnd, 0x0080, 1, _CACHED_HICON)
    except Exception:
        pass


def compute_adaptive_window_geometry(role: str = "runner") -> dict[str, int | float]:
    """Compute compact, screen-workarea-bounded bounds and Chromium scale factor.

    Uses `GetDeviceCaps(DESKTOPHORZRES)` to normalize Chromium's device scale factor
    (`1.0` on 1080p screens so Windows 125%/150% DPI scaling does not inflate the
    debug window or its CSS elements, and proportional scaling on 2K/4K screens),
    while keeping `SetWindowPos` coordinates in the calling process's work-area space.
    """
    work_left, work_top, work_right, work_bottom = 0, 0, 1920, 1020
    phys_screen_w = 1920
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            gdi32 = ctypes.windll.gdi32
            rect = wintypes.RECT()
            if user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
                work_left = int(rect.left)
                work_top = int(rect.top)
                work_right = int(rect.right)
                work_bottom = int(rect.bottom)
            dc = user32.GetDC(0)
            if dc:
                try:
                    horz = int(gdi32.GetDeviceCaps(dc, 118) or 0)
                    if horz > 0:
                        phys_screen_w = horz
                finally:
                    user32.ReleaseDC(0, dc)
        except Exception:
            pass

    work_w = max(800, work_right - work_left)
    work_h = max(560, work_bottom - work_top)

    if role == "locator":
        ratio_w, ratio_h = 0.54, 0.65
        max_w, max_h = 1040, 660
        offset_x, offset_y = 24, 24
    elif role == "action":
        ratio_w, ratio_h = 0.46, 0.58
        max_w, max_h = 880, 580
        offset_x, offset_y = 64, 48
    else:
        ratio_w, ratio_h = 0.52, 0.63
        max_w, max_h = 1000, 640
        offset_x, offset_y = 44, 36

    win_w = min(max_w, int(work_w * ratio_w), work_w - 24)
    win_h = min(max_h, int(work_h * ratio_h), work_h - 24)
    x = max(work_left + 8, min(work_left + offset_x, work_right - win_w - 8))
    y = max(work_top + 8, min(work_top + offset_y, work_bottom - win_h - 8))

    scale_factor = 1.0 if phys_screen_w <= 2048 else round(min(2.0, phys_screen_w / 1920.0), 2)
    phys_ratio = max(1.0, phys_screen_w / float(max(800, work_right - work_left)))
    phys_win_w = int(round(win_w * phys_ratio))
    phys_win_h = int(round(win_h * phys_ratio))
    dip_w = max(720, int(round(phys_win_w / scale_factor)))
    dip_h = max(480, int(round(phys_win_h / scale_factor)))
    dip_x = int(round((x * phys_ratio) / scale_factor))
    dip_y = int(round((y * phys_ratio) / scale_factor))

    return {
        "x": x,
        "y": y,
        "width": win_w,
        "height": win_h,
        "dip_x": dip_x,
        "dip_y": dip_y,
        "dip_width": dip_w,
        "dip_height": dip_h,
        "scale_factor": scale_factor,
    }


def build_webview_launch_command(
        browser_exe: str,
        url: str,
        role: str = "runner",
) -> tuple[list[str], dict[str, int | float]]:
    exe_stem = Path(browser_exe).stem.lower()
    user_data_dir = Paths.ARTIFACTS_DIR / f".webview_profile_{role}_{exe_stem}"
    user_data_dir.mkdir(parents=True, exist_ok=True)
    geom = compute_adaptive_window_geometry(role)
    cmd = [
        browser_exe,
        f"--app={url}",
        f"--user-data-dir={user_data_dir}",
        f"--window-size={geom['dip_width']},{geom['dip_height']}",
        f"--window-position={geom['dip_x']},{geom['dip_y']}",
        f"--force-device-scale-factor={geom['scale_factor']}",
        "--high-dpi-support=1",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-sandbox",
        "--test-type",
        "--no-proxy-server",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-translate",
        "--disable-component-update",
        "--disk-cache-size=1048576",
    ]
    return cmd, geom


def apply_initial_webview_window_placement(
        hwnd: int | None,
        role: str = "runner",
        *,
        always_on_top: bool = False,
) -> None:
    if not hwnd:
        return
    _apply_window_icon(hwnd)
    if win32gui is None or win32con is None:
        return
    try:
        geom = compute_adaptive_window_geometry(role)
        insert_after = win32con.HWND_TOPMOST if always_on_top else win32con.HWND_NOTOPMOST
        win32gui.SetWindowPos(
            hwnd,
            insert_after,
            int(geom["x"]),
            int(geom["y"]),
            int(geom["width"]),
            int(geom["height"]),
            win32con.SWP_SHOWWINDOW,
        )
    except Exception:
        pass


def find_bundled_browser_executable(browser_dir: Path | None = None) -> str | None:
    """Return the bundled portable browser executable in `resources/browser/current/` if present."""
    root = browser_dir if browser_dir is not None else Paths.BROWSER_CURRENT_DIR
    rel_candidates = (
        "thorium.exe",
        "chrome.exe",
        "msedge.exe",
        "bin/thorium.exe",
        "bin/chrome.exe",
        "Application/thorium.exe",
        "Application/chrome.exe",
        "Application/msedge.exe",
    )
    for rel in rel_candidates:
        candidate = root / rel
        if candidate.is_file():
            return str(candidate.resolve())
    return None


def find_browser_executable(browser_dir: Path | None = None) -> str | None:
    """Fixed priority browser lookup:
    1. Bundled portable browser under `resources/browser/current/` (highest priority)
    2. System Microsoft Edge or Google Chrome (fallback)
    """
    bundled = find_bundled_browser_executable(browser_dir)
    if bundled:
        return bundled

    system_candidates = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    ]
    for path in system_candidates:
        if path and os.path.isfile(path):
            return path
    return None


def find_edge_executable() -> str | None:
    """Backward-compatible alias for `find_browser_executable()`."""
    return find_browser_executable()


class _WindowLiveProxy:
    """Compatibility proxy so `XPathDebuggerApp` can check `.window.winfo_exists()`."""

    def __init__(self, controller: FeatureRunnerWindow):
        self._controller = controller

    def winfo_exists(self) -> bool:
        return self._controller.is_alive()

    def deiconify(self) -> None:
        self._controller.focus()

    def lift(self) -> None:
        self._controller.focus()

    def focus_force(self) -> None:
        self._controller.focus()

    def destroy(self) -> None:
        self._controller.close(force=True)


class FeatureRunnerWindow:
    """D1 Web Workspace host for the BDD Feature Runner."""

    def __init__(
            self,
            parent: tk.Misc | None = None,
            *,
            on_close=None,
            minimize_targets_provider=None,
            runner: SubprocessFeatureRunner | None = None,
            initial_root: str | Path | None = None,
            persist_state: bool = True,
            open_browser: bool = True,
    ):
        self.parent = parent
        self.on_close = on_close
        self.minimize_targets_provider = minimize_targets_provider
        self.runner = runner or SubprocessFeatureRunner()
        self.persist_state = bool(persist_state)
        self.open_browser = bool(open_browser)
        self.window = _WindowLiveProxy(self)

        saved_state = load_runner_ui_state() if self.persist_state else {}
        self.theme_mode = str(saved_state.get("theme_mode") or "dark")
        self.always_on_top = bool(saved_state.get("always_on_top", False))
        default_root = (
            Path(initial_root)
            if initial_root is not None
            else Path(saved_state.get("root_path") or Paths.TEST_FEATURES_DIR)
        )
        if not default_root.exists():
            default_root = (
                Paths.TEST_FEATURES_DIR
                if Paths.TEST_FEATURES_DIR.exists()
                else Paths.BASE_DIR
            )

        self.root_path: Path = default_root.resolve()
        self.config: FeatureRunConfig = config_from_dict(saved_state.get("config"))
        self.catalog: FeatureCatalog = discover_feature_catalog(self.root_path)
        self.last_run_target: FeatureRunTarget | None = None

        self._lock = threading.RLock()
        self._closed = False
        self._subscribers: list[queue.Queue[dict[str, Any]]] = []
        self._log_history: list[dict[str, Any]] = []
        self._next_seq: int = 1
        self._minimized_tk_windows: list[tk.Misc] = []
        self._minimized_hwnds: list[int] = []
        self._browser_process: subprocess.Popen | None = None
        self._browser_exe: str | None = None

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self._server.daemon_threads = True
        self.port: int = int(self._server.server_port)
        self.url: str = f"http://127.0.0.1:{self.port}/"

        self._server_thread = threading.Thread(
            target=self._server.serve_forever,
            name="autowork-feature-runner-web",
            daemon=True,
        )
        self._server_thread.start()

        self._pump_thread = threading.Thread(
            target=self._event_pump_loop,
            name="autowork-feature-runner-pump",
            daemon=True,
        )
        self._pump_thread.start()

        if self.open_browser:
            self._launch_app_window()

    # =====================================================
    # Public Lifecycle & Window Management
    # =====================================================
    def is_alive(self) -> bool:
        return not self._closed

    def focus(self) -> None:
        hwnd = self._find_app_hwnd()
        if hwnd and win32gui is not None and win32con is not None:
            try:
                if win32gui.IsIconic(hwnd):
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                win32gui.SetForegroundWindow(hwnd)
                return
            except Exception:
                pass
        if self.open_browser:
            self._launch_app_window()

    def _launch_app_window(self) -> None:
        browser_exe = find_browser_executable()
        if browser_exe:
            self._browser_exe = browser_exe
            cmd, _geom = build_webview_launch_command(browser_exe, self.url, role="runner")
            try:
                self._browser_process = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                threading.Thread(
                    target=self._apply_initial_window_setup,
                    daemon=True,
                ).start()
                return
            except Exception:
                pass
        try:
            webbrowser.open(self.url)
        except Exception:
            pass

    def _apply_initial_window_setup(self) -> None:
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline and not self._closed:
            hwnd = self._find_app_hwnd()
            if hwnd:
                apply_initial_webview_window_placement(
                    hwnd,
                    "runner",
                    always_on_top=self.always_on_top,
                )
                return
            time.sleep(0.15)

    def _find_app_hwnd(self) -> int | None:
        if win32gui is None:
            return None
        pid = self._browser_process.pid if self._browser_process is not None else None
        pid_matches: list[int] = []
        title_matches: list[int] = []

        def _enum_cb(hwnd: int, _extra: Any) -> bool:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = win32gui.GetWindowText(hwnd) or ""
            if WINDOW_TITLE in title:
                title_matches.append(hwnd)
                if pid is not None and win32process is not None:
                    try:
                        _, win_pid = win32process.GetWindowThreadProcessId(hwnd)
                        if win_pid == pid:
                            pid_matches.append(hwnd)
                    except Exception:
                        pass
            return True

        try:
            win32gui.EnumWindows(_enum_cb, None)
        except Exception:
            return None
        if pid_matches:
            return pid_matches[0]
        return title_matches[0] if title_matches else None

    def _apply_window_topmost(self, hwnd: int | None, topmost: bool) -> bool:
        if not hwnd or win32gui is None or win32con is None:
            return False
        try:
            insert_after = win32con.HWND_TOPMOST if topmost else win32con.HWND_NOTOPMOST
            flags = win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW
            win32gui.SetWindowPos(hwnd, insert_after, 0, 0, 0, 0, flags)
            return True
        except Exception:
            return False

    def set_always_on_top(self, enabled: bool) -> bool:
        with self._lock:
            self.always_on_top = bool(enabled)
            self._persist_state()
        hwnd = self._find_app_hwnd()
        self._apply_window_topmost(hwnd, self.always_on_top)
        return self.always_on_top

    def _minimize_debug_windows(self) -> None:
        self._minimized_tk_windows.clear()
        self._minimized_hwnds.clear()

        candidates: list[tk.Misc] = []
        if self.minimize_targets_provider is not None:
            try:
                candidates.extend(self.minimize_targets_provider() or ())
            except Exception:
                pass
        elif self.parent is not None:
            candidates.append(self.parent)

        seen_ids: set[int] = set()
        for win in candidates:
            if win is None or id(win) in seen_ids:
                continue
            seen_ids.add(id(win))
            try:
                if win.winfo_exists() and str(win.state()) not in {"iconic", "withdrawn"}:
                    win.iconify()
                    self._minimized_tk_windows.append(win)
            except Exception:
                continue

        if win32gui is not None and win32con is not None:
            web_titles = (
                WINDOW_TITLE,
                "BDD Autowork Locator 工作台",
                "BDD Autowork 动作调试器",
            )
            matched_hwnds: list[int] = []

            def _enum_web(h: int, _extra: Any) -> bool:
                if not win32gui.IsWindowVisible(h):
                    return True
                t = win32gui.GetWindowText(h) or ""
                if any(wt in t for wt in web_titles):
                    matched_hwnds.append(h)
                return True

            try:
                win32gui.EnumWindows(_enum_web, None)
            except Exception:
                pass

            for hwnd in matched_hwnds:
                try:
                    if not win32gui.IsIconic(hwnd):
                        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
                        self._minimized_hwnds.append(hwnd)
                except Exception:
                    continue

    def _restore_debug_windows(self) -> None:
        for win in reversed(self._minimized_tk_windows):
            try:
                if win.winfo_exists():
                    win.deiconify()
            except Exception:
                continue
        self._minimized_tk_windows.clear()

        if win32gui is not None and win32con is not None:
            for hwnd in reversed(self._minimized_hwnds):
                try:
                    if win32gui.IsWindow(hwnd):
                        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                        win32gui.SetForegroundWindow(hwnd)
                        if self.always_on_top:
                            self._apply_window_topmost(hwnd, True)
                except Exception:
                    continue
        self._minimized_hwnds.clear()

    # =====================================================
    # Runner Actions & Event Pump
    # =====================================================
    def start_run(self, target: FeatureRunTarget) -> tuple[bool, str]:
        with self._lock:
            if self.runner.is_running:
                return False, "当前已有用例正在运行中，请先等待完成或点击停止。"
            cfg = self.config
            self._persist_state()

        if cfg.minimize_on_run:
            self._schedule_on_tk_thread(self._minimize_debug_windows)

        try:
            self.runner.start(target, cfg)
        except Exception as exc:
            self._schedule_on_tk_thread(self._restore_debug_windows)
            err_msg = f"启动运行失败: {type(exc).__name__}: {exc}"
            self._append_and_broadcast_line(err_msg, "error")
            return False, err_msg

        with self._lock:
            self.last_run_target = target
        return True, f"正在运行: {describe_run_target(target)}"

    def rerun_last(self) -> tuple[bool, str]:
        with self._lock:
            target = self.last_run_target
        if target is None:
            return False, "尚无历史运行目标。"
        return self.start_run(target)

    def stop_run(self) -> bool:
        return self.runner.stop()

    def _append_and_broadcast_line(self, text: str, level_tag: str) -> None:
        with self._lock:
            seq = self._next_seq
            self._next_seq += 1
            msg = {
                "type": "line",
                "seq": seq,
                "text": text,
                "level_tag": level_tag,
            }
            self._log_history.append(msg)
            if len(self._log_history) > 5000:
                del self._log_history[: len(self._log_history) - 5000]
        self._broadcast(msg)

    def _event_pump_loop(self) -> None:
        last_tick = 0.0
        while not self._closed:
            drained = False
            while True:
                try:
                    ev: ConsoleEvent = self.runner.events.get_nowait()
                except queue.Empty:
                    break
                drained = True
                if ev.kind in {"line", "error"}:
                    self._append_and_broadcast_line(ev.text, ev.level_tag)
                elif ev.kind == "finished":
                    self._schedule_on_tk_thread(self._restore_debug_windows)
                    self._broadcast({
                        "type": "finished",
                        "exit_code": ev.exit_code,
                        "stopped": ev.stopped,
                        "duration_seconds": ev.duration_seconds,
                    })

            now = time.monotonic()
            if self.runner.is_running and now - last_tick >= 0.2:
                last_tick = now
                self._broadcast({
                    "type": "tick",
                    "elapsed": self.runner.elapsed_seconds,
                })

            if not drained:
                time.sleep(0.04)

    def _broadcast(self, message: dict[str, Any]) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(message)
            except Exception:
                pass

    def _schedule_on_tk_thread(self, callback) -> None:
        if self.parent is not None:
            try:
                if self.parent.winfo_exists():
                    self.parent.after(0, callback)
                    return
            except Exception:
                pass
        try:
            callback()
        except Exception:
            pass

    def _browse_native_dialog(self, kind: str) -> str:
        result_box: dict[str, str] = {"path": ""}
        done = threading.Event()

        def _open_dialog() -> None:
            try:
                initial = str(self.root_path if self.root_path.exists() else Paths.BASE_DIR)
                if kind == "directory":
                    chosen = filedialog.askdirectory(
                        parent=self.parent,
                        title="选择 Feature 目录",
                        initialdir=initial,
                    )
                elif kind == "file":
                    init_dir = str(self.root_path.parent if self.root_path.is_file() else self.root_path)
                    chosen = filedialog.askopenfilename(
                        parent=self.parent,
                        title="选择 Feature 文件",
                        initialdir=init_dir,
                        filetypes=[("Gherkin Feature", "*.feature"), ("All Files", "*.*")],
                    )
                elif kind == "ffmpeg":
                    chosen = filedialog.askopenfilename(
                        parent=self.parent,
                        title="选择 FFmpeg 可执行文件",
                        initialdir=str(Paths.RESOURCES_DIR),
                        filetypes=[("FFmpeg", "ffmpeg.exe"), ("All Files", "*.*")],
                    )
                else:
                    chosen = filedialog.askopenfilename(
                        parent=self.parent,
                        title="选择被测应用启动文件",
                        initialdir=str(Paths.BASE_DIR),
                        filetypes=[("Executable", "*.exe;*.bat;*.cmd"), ("All Files", "*.*")],
                    )
                result_box["path"] = str(chosen or "")
            except Exception:
                result_box["path"] = ""
            finally:
                done.set()

        if self.parent is not None and threading.current_thread() is not threading.main_thread():
            try:
                self.parent.after(0, _open_dialog)
                done.wait(timeout=120)
                return result_box["path"]
            except Exception:
                pass
        _open_dialog()
        return result_box["path"]

    def _open_artifact(self, target_kind: str) -> str:
        if target_kind == "spark_report":
            report_html = Paths.REPORTS_DIR / "autowork-report.html"
            if not report_html.is_file():
                return f"尚未生成报告文件: {report_html}"
            browser_exe = find_bundled_browser_executable()
            if browser_exe:
                try:
                    cmd, _geom = build_webview_launch_command(
                        browser_exe,
                        report_html.resolve().as_uri(),
                        role="report",
                    )
                    subprocess.Popen(
                        cmd,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    return f"已打开 Spark 报告: {report_html.name}"
                except Exception:
                    pass
            self._launch_os_path(report_html)
            return f"已打开 Spark 报告: {report_html.name}"

        folder_map = {
            "screenshots": Paths.SCREENSHOTS_DIR,
            "recordings": Paths.RECORDINGS_DIR,
            "reports": Paths.REPORTS_DIR,
            "logs": Paths.LOGS_DIR,
        }
        folder = folder_map.get(target_kind, Paths.ARTIFACTS_DIR)
        folder.mkdir(parents=True, exist_ok=True)
        self._launch_os_path(folder)
        return f"已打开目录: {folder}"

    @staticmethod
    def _launch_os_path(path: Path) -> None:
        if hasattr(os, "startfile"):
            os.startfile(str(path))
        else:
            webbrowser.open(path.resolve().as_uri())

    def _persist_state(self) -> None:
        if not self.persist_state:
            return
        save_runner_ui_state({
            "root_path": str(self.root_path),
            "theme_mode": self.theme_mode,
            "always_on_top": self.always_on_top,
            "config": asdict(self.config),
        })

    def close(self, *, force: bool = False) -> None:
        if self._closed:
            return
        self._closed = True
        if self.runner.is_running:
            self.runner.stop()
        try:
            self._persist_state()
        except Exception:
            pass
        if self._browser_process is not None:
            try:
                if self._browser_process.poll() is None:
                    self._browser_process.terminate()
            except Exception:
                pass
            self._browser_process = None
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:
            pass
        if self.on_close is not None:
            try:
                self.on_close()
            except Exception:
                pass

    # =====================================================
    # HTTP & SSE Request Handler
    # =====================================================
    def _make_handler(self):
        controller = self

        class _RunnerRequestHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path in {"/", "/index.html"}:
                    body = render_runner_workspace_html().encode("utf-8")
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if parsed.path == "/api/init":
                    with controller._lock:
                        payload = {
                            "root_path": str(controller.root_path),
                            "display_root_path": _format_display_path(controller.root_path),
                            "theme_mode": controller.theme_mode,
                            "always_on_top": controller.always_on_top,
                            "config": asdict(controller.config),
                            "config_summary": controller.config.compact_summary(),
                            "catalog": controller.catalog.as_dict(),
                            "is_running": controller.runner.is_running,
                        }
                    self._send_json(payload)
                    return

                if parsed.path == "/api/failed-cases":
                    failed = [item.as_dict() for item in discover_failed_targets()]
                    self._send_json({"failed_cases": failed})
                    return

                if parsed.path == "/api/stream":
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "keep-alive")
                    self.end_headers()

                    sub_q: queue.Queue[dict[str, Any]] = queue.Queue()
                    with controller._lock:
                        history = list(controller._log_history)
                        controller._subscribers.append(sub_q)
                    try:
                        for item in history:
                            self._write_sse(item)
                        while not controller._closed:
                            try:
                                event = sub_q.get(timeout=10.0)
                                self._write_sse(event)
                            except queue.Empty:
                                self.wfile.write(b": keepalive\n\n")
                                self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass
                    finally:
                        with controller._lock:
                            if sub_q in controller._subscribers:
                                controller._subscribers.remove(sub_q)
                    return

                self.send_error(HTTPStatus.NOT_FOUND)

            def do_POST(self) -> None:
                parsed = urlparse(self.path)
                body = self._read_json()

                if parsed.path == "/api/catalog":
                    raw_root = str(body.get("root_path") or "").strip()
                    with controller._lock:
                        if raw_root:
                            candidate = Path(raw_root)
                            if not candidate.is_absolute():
                                candidate = (Paths.BASE_DIR / candidate).resolve()
                            else:
                                candidate = candidate.resolve()
                            controller.root_path = candidate
                        controller.catalog = discover_feature_catalog(controller.root_path)
                        controller._persist_state()
                        catalog_dict = controller.catalog.as_dict()
                        display_root = _format_display_path(controller.root_path)
                    self._send_json({
                        "ok": True,
                        "catalog": catalog_dict,
                        "display_root_path": display_root,
                    })
                    return

                if parsed.path == "/api/config":
                    raw_cfg = body.get("config")
                    with controller._lock:
                        controller.config = config_from_dict(raw_cfg, fallback=controller.config)
                        controller._persist_state()
                        cfg_dict = asdict(controller.config)
                        summary = controller.config.compact_summary()
                    self._send_json({
                        "ok": True,
                        "config": cfg_dict,
                        "config_summary": summary,
                    })
                    return

                if parsed.path == "/api/config/preset":
                    preset_name = str(body.get("preset") or "local_debug")
                    cfg = (
                        local_debug_preset_config()
                        if preset_name == "local_debug"
                        else config_from_project_settings()
                    )
                    self._send_json({"ok": True, "config": asdict(cfg)})
                    return

                if parsed.path == "/api/config/mode-preset":
                    domain = str(body.get("domain") or "ocr").lower()
                    mode = str(body.get("mode") or "balanced").lower()
                    draft = config_from_dict(body.get("config"), fallback=controller.config)
                    updated = (
                        apply_ocr_mode_preset(draft, mode)
                        if domain == "ocr"
                        else apply_pic_mode_preset(draft, mode)
                    )
                    self._send_json({"ok": True, "config": asdict(updated)})
                    return

                if parsed.path == "/api/config/snippet":
                    draft = config_from_dict(body.get("config"), fallback=controller.config)
                    raw_target = body.get("target")
                    target_obj = _target_from_dict(raw_target) if isinstance(raw_target, dict) else None
                    diff_only = bool(body.get("diff_only", True))
                    snippet = render_local_runner_snippet(
                        target_obj,
                        draft,
                        diff_only=diff_only,
                    )
                    self._send_json({"ok": True, "snippet": snippet})
                    return

                if parsed.path == "/api/run":
                    raw_target = body.get("target")
                    if not isinstance(raw_target, dict) or not raw_target.get("feature_path"):
                        self._send_json({"ok": False, "error": "无效的运行目标"})
                        return
                    target_obj = _target_from_dict(raw_target)
                    ok, msg = controller.start_run(target_obj)
                    self._send_json({"ok": ok, "message": msg, "error": None if ok else msg})
                    return

                if parsed.path == "/api/rerun":
                    ok, msg = controller.rerun_last()
                    self._send_json({"ok": ok, "message": msg, "error": None if ok else msg})
                    return

                if parsed.path == "/api/stop":
                    stopped = controller.stop_run()
                    self._send_json({"ok": stopped})
                    return

                if parsed.path == "/api/browse":
                    kind = str(body.get("kind") or "directory")
                    chosen = controller._browse_native_dialog(kind)
                    self._send_json({"ok": True, "chosen": chosen})
                    return

                if parsed.path == "/api/open-artifact":
                    target_kind = str(body.get("target") or "reports")
                    msg = controller._open_artifact(target_kind)
                    self._send_json({"ok": True, "message": msg})
                    return

                if parsed.path == "/api/theme":
                    mode = str(body.get("theme_mode") or "dark")
                    with controller._lock:
                        controller.theme_mode = "light" if mode == "light" else "dark"
                        controller._persist_state()
                    self._send_json({"ok": True, "theme_mode": controller.theme_mode})
                    return

                if parsed.path == "/api/window/topmost":
                    enabled = bool(body.get("always_on_top", False))
                    actual = controller.set_always_on_top(enabled)
                    self._send_json({"ok": True, "always_on_top": actual})
                    return

                self.send_error(HTTPStatus.NOT_FOUND)

            def _read_json(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0:
                    return {}
                raw = self.rfile.read(length)
                try:
                    data = json.loads(raw.decode("utf-8"))
                    return data if isinstance(data, dict) else {}
                except Exception:
                    return {}

            def _send_json(self, payload: dict[str, Any]) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _write_sse(self, payload: dict[str, Any]) -> None:
                data = json.dumps(payload, ensure_ascii=False)
                self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self.wfile.flush()

        return _RunnerRequestHandler


def _target_from_dict(data: dict[str, Any]) -> FeatureRunTarget:
    step_idx = data.get("single_step_index")
    return FeatureRunTarget(
        kind=str(data.get("kind") or "feature"),
        feature_path=str(data.get("feature_path") or ""),
        rule_name=data.get("rule_name") or None,
        rule_line=int(data["rule_line"]) if data.get("rule_line") is not None else None,
        scenario_name=data.get("scenario_name") or None,
        scenario_line=int(data["scenario_line"]) if data.get("scenario_line") is not None else None,
        example_id=data.get("example_id") or None,
        single_step_index=int(step_idx) if step_idx not in (None, "") else None,
        single_step_mode=str(data.get("single_step_mode") or "previous"),
    )
