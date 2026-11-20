"""Modern Web UI host (D1 architecture) for BDD Autowork Locator Workbench.

Synchronizes `XPathDebuggerApp` (Target Window, Control Tree, XPath Locator,
Property Inspector, Child/Coordinate/OCR/Image Validation Tools) with a chromeless
browser window (`templates/locator_workbench.html`), while keeping the underlying
Tkinter event loop, global hotkeys (F2/F4/F8/F9/Esc), screen overlay rectangles,
and Recorder Workbench bridge intact.
"""

from __future__ import annotations

import base64
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import queue
import subprocess
import threading
import time
from typing import Any
from urllib.parse import urlparse
import webbrowser

import cv2

try:
    import win32con  # type: ignore[import-untyped]
    import win32gui  # type: ignore[import-untyped]
except ImportError:
    win32con = None  # type: ignore[assignment]
    win32gui = None  # type: ignore[assignment]

from autowork_core.utils.debug_tools.common import (
    draw_ocr_preview,
    draw_pic_preview,
    make_element_key,
    make_xpath_suggestion,
    safe_to_string,
)
from autowork_core.utils.debug_tools.feature_runner_runtime import (
    load_runner_ui_state,
    save_runner_ui_state,
)
from autowork_core.utils.debug_tools.feature_runner_web import (
    apply_initial_webview_window_placement,
    build_webview_launch_command,
    find_browser_executable,
)
from config.paths import Paths


LOCATOR_WINDOW_TITLE = "BDD Autowork Locator 工作台"
_LOCATOR_TEMPLATE_PATH = Path(__file__).with_name("templates") / "locator_workbench.html"
_LUCIDE_JS_PATH = (
    Paths.CORE_DIR / "runtime" / "reporting" / "templates" / "lucide.min.js"
)


def render_locator_workbench_html() -> str:
    html = _LOCATOR_TEMPLATE_PATH.read_text(encoding="utf-8")
    marker = "<!-- runner-resource:lucide.min.js -->"
    if _LUCIDE_JS_PATH.is_file():
        lucide_code = _LUCIDE_JS_PATH.read_text(encoding="utf-8-sig")
        lucide_code = lucide_code.replace("</script", "<\\/script")
        html = html.replace(marker, f"<script>\n{lucide_code}\n</script>")
    return html


def _bgr_to_data_url(bgr_image: Any) -> str:
    if bgr_image is None:
        return ""
    try:
        ok, encoded = cv2.imencode(".png", bgr_image)
        if not ok:
            return ""
        b64 = base64.b64encode(encoded.tobytes()).decode("ascii")
        return f"data:image/png;base64,{b64}"
    except Exception:
        return ""


class LocatorWorkbenchWebHost:
    """D1 Web UI host for `XPathDebuggerApp`."""

    def __init__(self, app_owner: Any, *, open_browser: bool = True):
        self.owner = app_owner
        self.open_browser = bool(open_browser)

        saved_state = load_runner_ui_state()
        self.theme_mode = str(saved_state.get("theme_mode") or "light")
        self.always_on_top = bool(saved_state.get("locator_always_on_top", True))
        self.active_right_tab = "properties"

        self._lock = threading.RLock()
        self._closed = False
        self._subscribers: list[queue.Queue[dict[str, Any]]] = []
        self._task_queue: queue.Queue[tuple[Any, dict[str, Any], threading.Event]] = queue.Queue()
        self._poll_after_id: str | None = None
        self._browser_process: subprocess.Popen | None = None
        self._hwnd_ever_seen = False
        self._last_hwnd: int | None = None
        self._minimized_for_pick = False

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self._server.daemon_threads = True
        self.port: int = int(self._server.server_port)
        self.url: str = f"http://127.0.0.1:{self.port}/"

        self._server_thread = threading.Thread(
            target=self._server.serve_forever,
            name="autowork-locator-workbench-web",
            daemon=True,
        )
        self._server_thread.start()
        self._schedule_task_poll()

        if self.open_browser:
            self._launch_app_window()

    def focus(self, *, right_tab: str | None = None) -> None:
        if right_tab:
            self.active_right_tab = right_tab
            self.notify_state_changed(switch_tab=right_tab)
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
            cmd, _geom = build_webview_launch_command(browser_exe, self.url, role="locator")
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
                self._hwnd_ever_seen = True
                self._last_hwnd = hwnd
                apply_initial_webview_window_placement(
                    hwnd,
                    "locator",
                    always_on_top=self.always_on_top,
                )
                return
            time.sleep(0.15)

    def _find_app_hwnd(self) -> int | None:
        if win32gui is None:
            return None
        if self._last_hwnd and win32gui.IsWindow(self._last_hwnd):
            return self._last_hwnd
        tk_hwnd = None
        try:
            tk_hwnd = int(self.owner.app.winfo_id())
        except Exception:
            pass
        matches: list[int] = []

        def _enum_cb(hwnd: int, _extra: Any) -> bool:
            if not win32gui.IsWindowVisible(hwnd) or hwnd == tk_hwnd:
                return True
            title = win32gui.GetWindowText(hwnd) or ""
            if LOCATOR_WINDOW_TITLE in title:
                matches.append(hwnd)
            return True

        try:
            win32gui.EnumWindows(_enum_cb, None)
        except Exception:
            return None
        if matches:
            self._hwnd_ever_seen = True
            self._last_hwnd = matches[0]
            return matches[0]
        return None

    def is_browser_window_closed(self) -> bool:
        if not self._hwnd_ever_seen:
            return False
        if self._last_hwnd and win32gui is not None:
            try:
                if win32gui.IsWindow(self._last_hwnd):
                    return False
            except Exception:
                pass
        return self._find_app_hwnd() is None

    def minimize_for_pick(self) -> None:
        hwnd = self._find_app_hwnd()
        if hwnd and win32gui is not None and win32con is not None:
            try:
                if not win32gui.IsIconic(hwnd):
                    win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
                    self._minimized_for_pick = True
            except Exception:
                pass

    def restore_after_pick(self) -> None:
        if not self._minimized_for_pick:
            return
        self._minimized_for_pick = False
        hwnd = self._find_app_hwnd()
        if hwnd and win32gui is not None and win32con is not None:
            try:
                if win32gui.IsWindow(hwnd):
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                    win32gui.SetForegroundWindow(hwnd)
                    if self.always_on_top:
                        self._apply_window_topmost(hwnd, True)
            except Exception:
                pass

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

    def notify_state_changed(self, *, include_tree: bool = False, switch_tab: str | None = None) -> None:
        if self._closed:
            return
        try:
            snap = self._snapshot_state(include_tree=include_tree)
            if switch_tab:
                snap["switch_tab"] = switch_tab
            self._broadcast({"type": "sync", "state": snap})
        except Exception:
            pass

    def _broadcast(self, message: dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(message)
            except Exception:
                pass

    def _schedule_task_poll(self) -> None:
        if self._closed:
            return
        try:
            self._poll_after_id = self.owner.app.after(25, self._poll_web_tasks)
        except Exception:
            self._poll_after_id = None

    def drain_pending_tasks(self) -> None:
        while True:
            try:
                fn, box, done = self._task_queue.get_nowait()
            except queue.Empty:
                break
            try:
                box["val"] = fn()
            except Exception as exc:
                box["err"] = exc
            finally:
                done.set()

    def _poll_web_tasks(self) -> None:
        self._poll_after_id = None
        if self._closed:
            return
        self.drain_pending_tasks()
        self._schedule_task_poll()

    def _run_on_tk_thread(self, fn):
        if threading.current_thread() is threading.main_thread():
            return fn()
        box: dict[str, Any] = {"val": None, "err": None}
        done = threading.Event()
        self._task_queue.put((fn, box, done))
        done.wait(timeout=45.0)
        if box["err"] is not None:
            raise box["err"]
        return box["val"]

    # =====================================================
    # State Serialization Helpers
    # =====================================================
    def _serialize_tree(self) -> list[dict[str, Any]]:
        tree = self.owner.tree

        def _walk(item_id: str) -> dict[str, Any]:
            vals = tree.item(item_id, "values") or ()
            tags = list(tree.item(item_id, "tags") or ())
            is_open = bool(tree.item(item_id, "open"))
            children = [_walk(child_id) for child_id in tree.get_children(item_id)]
            return {
                "id": item_id,
                "text": str(tree.item(item_id, "text") or ""),
                "type": str(vals[0]) if len(vals) > 0 else "",
                "name": str(vals[1]) if len(vals) > 1 else "",
                "auto_id": str(vals[2]) if len(vals) > 2 else "",
                "class_name": str(vals[3]) if len(vals) > 3 else "",
                "rect": str(vals[4]) if len(vals) > 4 else "",
                "open": is_open,
                "tags": tags,
                "children": children,
            }

        return [_walk(root_id) for root_id in tree.get_children("")]

    def _serialize_properties(self) -> list[dict[str, Any]]:
        prop_tree = self.owner.property_tree
        if prop_tree is None:
            return []
        rows: list[dict[str, Any]] = []
        for row_id in prop_tree.get_children(""):
            vals = prop_tree.item(row_id, "values") or ()
            rows.append({
                "id": row_id,
                "key": str(prop_tree.item(row_id, "text") or ""),
                "value": str(vals[1]) if len(vals) > 1 else "",
                "source": str(vals[3]) if len(vals) > 3 else "",
            })
        return rows

    def _serialize_child_state(self) -> dict[str, Any]:
        extra_yaml = (
            "# 可选: title_re, auto_id_re, class_name_re, process, handle, depth\n"
            "# framework_id, visible, enabled"
        )
        if self.owner.child_extra_text is not None:
            try:
                extra_yaml = self.owner.child_extra_text.get("1.0", "end").rstrip()
            except Exception:
                pass
        elif hasattr(self.owner, "_web_child_extra_yaml"):
            extra_yaml = str(getattr(self.owner, "_web_child_extra_yaml"))
        return {
            "root_mode": self.owner.child_root_mode_var.get(),
            "control_type": self.owner.child_control_type_var.get(),
            "auto_id": self.owner.child_auto_id_var.get(),
            "title": self.owner.child_title_var.get(),
            "class_name": self.owner.child_class_name_var.get(),
            "wait_type": self.owner.child_wait_type_var.get(),
            "timeout": self.owner.child_timeout_var.get(),
            "extra_yaml": extra_yaml,
            "result": self.owner.child_result_var.get()
            or "验证会先统计全部匹配；只有恰好 1 个且满足等待状态时才通过。",
        }

    def _serialize_pos_state(self) -> dict[str, Any]:
        return {
            "x": self.owner.pos_x_var.get(),
            "y": self.owner.pos_y_var.get(),
            "source_width": self.owner.pos_source_width_var.get(),
            "source_height": self.owner.pos_source_height_var.get(),
            "result": self.owner.pos_result_var.get()
            or "移动鼠标到目标位置后按 F4，或点击“读取鼠标 (F4)”。",
        }

    def _serialize_ocr_state(self) -> dict[str, Any]:
        preview_url = ""
        if self.owner.ocr_image is not None:
            preview = draw_ocr_preview(
                self.owner.ocr_image,
                candidates=self.owner.ocr_candidates,
                matches=self.owner.ocr_matches,
            )
            preview_url = _bgr_to_data_url(preview)

        match_ids = {id(item) for item in (self.owner.ocr_matches or ())}
        rows: list[dict[str, Any]] = []
        for idx, cand in enumerate(self.owner.ocr_candidates or ()):
            bounds = cand.get("abs_bounds") or cand.get("bounds") or ""
            center = cand.get("center") or ""
            conf = cand.get("confidence", 0)
            if isinstance(bounds, tuple):
                bounds = ",".join(str(int(v)) for v in bounds)
            if isinstance(center, tuple):
                center = ",".join(str(int(v)) for v in center)
            try:
                conf_str = f"{float(conf):.3f}"
            except Exception:
                conf_str = safe_to_string(conf)
            rows.append({
                "index": idx,
                "matched": id(cand) in match_ids,
                "text": safe_to_string(cand.get("text", ""), max_len=500),
                "confidence": conf_str,
                "bounds": str(bounds),
                "center": str(center),
            })

        self.owner.update_ocr_params_summary()
        return {
            "target_text": self.owner.ocr_target_var.get(),
            "match_mode": self.owner.ocr_match_mode_var.get(),
            "det_limit_type": self.owner.ocr_det_limit_type_var.get(),
            "det_limit_side_len": self.owner.ocr_det_limit_side_len_var.get(),
            "det_box_thresh": self.owner.ocr_det_box_thresh_var.get(),
            "det_unclip_ratio": self.owner.ocr_det_unclip_ratio_var.get(),
            "cache_ttl": self.owner.ocr_cache_ttl_var.get(),
            "use_cache": bool(self.owner.ocr_use_cache_var.get()),
            "params_summary": self.owner.ocr_params_summary_var.get(),
            "source_label": self.owner.ocr_source_label_var.get(),
            "status": self.owner.ocr_status_var.get(),
            "preview_data_url": preview_url,
            "rows": rows,
        }

    def _serialize_pic_state(self) -> dict[str, Any]:
        preview_url = ""
        if self.owner.pic_source_image is not None:
            preview = draw_pic_preview(
                self.owner.pic_source_image,
                self.owner.pic_candidate,
            )
            preview_url = _bgr_to_data_url(preview)
        return {
            "template_path": self.owner.pic_template_path_var.get(),
            "source_label": self.owner.pic_source_label_var.get(),
            "threshold": self.owner.pic_threshold_var.get(),
            "method": self.owner.pic_method_var.get(),
            "pos": self.owner.pic_pos_var.get(),
            "result_value": self.owner.pic_result_value.get(),
            "status": self.owner.pic_status_var.get(),
            "preview_data_url": preview_url,
        }

    def _snapshot_state(self, *, include_tree: bool = True) -> dict[str, Any]:
        win_idx = -1
        try:
            win_idx = int(self.owner.window_combo.current())
        except Exception:
            pass

        payload: dict[str, Any] = {
            "theme_mode": self.theme_mode,
            "always_on_top": self.always_on_top,
            "active_right_tab": self.active_right_tab,
            "windows": [
                {
                    "index": idx,
                    "title": str(w.get("title") or ""),
                    "display": str(w.get("display") or ""),
                }
                for idx, w in enumerate(self.owner.windows or ())
            ],
            "selected_window_index": win_idx,
            "selected_tree_ids": list(self.owner.tree.selection() or ()),
            "captured_tree_id": self.owner.captured_tree_id,
            "tree_expanded": bool(self.owner.tree_expanded),
            "xpath": str(self.owner.entry.get() or ""),
            "selected_cell_value": str(self.owner.selected_cell_value.get() or ""),
            "properties": self._serialize_properties(),
            "hotkey_capture_mode": bool(self.owner.hotkey_capture_mode),
            "pick_mode": bool(self.owner.pick_mode),
            "status_text": str(self.owner.status_label.cget("text") or ""),
            "child_state": self._serialize_child_state(),
            "pos_state": self._serialize_pos_state(),
        }
        if include_tree:
            payload["tree"] = self._serialize_tree()
        return payload

    def close(self) -> None:
        self._closed = True
        if self._poll_after_id is not None:
            try:
                self.owner.app.after_cancel(self._poll_after_id)
            except Exception:
                pass
            self._poll_after_id = None
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:
            pass

    # =====================================================
    # HTTP & SSE Request Handler
    # =====================================================
    def _make_handler(self):
        host = self
        owner = self.owner

        class _LocatorRequestHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path in {"/", "/index.html"}:
                    body = render_locator_workbench_html().encode("utf-8")
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if parsed.path == "/api/init":
                    def _init():
                        snap = host._snapshot_state(include_tree=True)
                        snap["ocr_state"] = host._serialize_ocr_state()
                        snap["pic_state"] = host._serialize_pic_state()
                        return snap

                    self._send_json(host._run_on_tk_thread(_init))
                    return

                if parsed.path == "/api/stream":
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "keep-alive")
                    self.end_headers()

                    sub_q: queue.Queue[dict[str, Any]] = queue.Queue()
                    with host._lock:
                        host._subscribers.append(sub_q)
                    try:
                        while not host._closed:
                            try:
                                ev = sub_q.get(timeout=10.0)
                                data = json.dumps(ev, ensure_ascii=False)
                                self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                                self.wfile.flush()
                            except queue.Empty:
                                self.wfile.write(b": keepalive\n\n")
                                self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass
                    finally:
                        with host._lock:
                            if sub_q in host._subscribers:
                                host._subscribers.remove(sub_q)
                    return

                self.send_error(HTTPStatus.NOT_FOUND)

            def do_POST(self) -> None:
                parsed = urlparse(self.path)
                body = self._read_json()

                if parsed.path == "/api/windows/refresh":
                    def _refresh_wins():
                        owner.refresh_window_list()
                        return host._snapshot_state(include_tree=False)

                    self._send_json(host._run_on_tk_thread(_refresh_wins))
                    return

                if parsed.path == "/api/windows/switch":
                    def _switch_win():
                        idx = int(body.get("index", 0))
                        if 0 <= idx < len(owner.windows):
                            owner.window_combo.current(idx)
                        owner.switch_to_selected_window()
                        return host._snapshot_state(include_tree=True)

                    self._send_json(host._run_on_tk_thread(_switch_win))
                    return

                if parsed.path == "/api/tree/refresh":
                    def _refresh_tree():
                        owner.refresh_tree()
                        return host._snapshot_state(include_tree=True)

                    self._send_json(host._run_on_tk_thread(_refresh_tree))
                    return

                if parsed.path == "/api/tree/select":
                    def _select_node():
                        item_id = str(body.get("item_id") or "")
                        cell_val = body.get("cell_value")
                        if item_id and owner.tree.exists(item_id):
                            owner.tree.selection_set(item_id)
                            owner.tree.focus(item_id)
                            owner.on_tree_select()
                        if cell_val is not None:
                            owner.selected_cell_value.set(str(cell_val))
                        return host._snapshot_state(include_tree=False)

                    self._send_json(host._run_on_tk_thread(_select_node))
                    return

                if parsed.path == "/api/tree/dblclick":
                    def _dblclick_node():
                        item_id = str(body.get("item_id") or "")
                        element = owner.tree_id_to_element.get(item_id)
                        if element is None:
                            owner.set_status("当前行不是控件节点，无法生成 XPath。")
                            return host._snapshot_state(include_tree=False)
                        xpath, xpath_matches = owner.make_unique_xpath_match(
                            element,
                            make_xpath_suggestion(element),
                        )
                        owner.tree.selection_set(item_id)
                        owner.tree.focus(item_id)
                        owner.entry.delete(0, "end")
                        owner.entry.insert(0, xpath)
                        owner.app.clipboard_clear()
                        owner.app.clipboard_append(xpath)
                        owner.last_xpath = None
                        owner.show_element_properties(element)
                        owner.run_xpath(force=True, precomputed_elements=xpath_matches)
                        owner.set_status(f"已生成并复制 XPath: {xpath}")
                        return host._snapshot_state(include_tree=False)

                    self._send_json(host._run_on_tk_thread(_dblclick_node))
                    return

                if parsed.path == "/api/tree/pick":
                    def _start_pick():
                        owner.start_pick_mode()
                        return host._snapshot_state(include_tree=False)

                    self._send_json(host._run_on_tk_thread(_start_pick))
                    return

                if parsed.path == "/api/tree/capture-toggle":
                    def _toggle_cap():
                        owner.toggle_hotkey_capture_mode()
                        return host._snapshot_state(include_tree=False)

                    self._send_json(host._run_on_tk_thread(_toggle_cap))
                    return

                if parsed.path == "/api/tree/clear-overlay":
                    def _clear_ov():
                        owner.clear_overlay()
                        return host._snapshot_state(include_tree=False)

                    self._send_json(host._run_on_tk_thread(_clear_ov))
                    return

                if parsed.path == "/api/xpath/evaluate":
                    def _eval_xpath():
                        xp = str(body.get("xpath") or "")
                        owner.entry.delete(0, "end")
                        owner.entry.insert(0, xp)
                        owner.run_xpath(force=True)
                        return host._snapshot_state(include_tree=False)

                    self._send_json(host._run_on_tk_thread(_eval_xpath))
                    return

                if parsed.path == "/api/xpath/sibling":
                    def _gen_sibling():
                        direction = str(body.get("direction") or "prev")
                        wildcard = bool(body.get("wildcard", False))
                        owner.generate_sibling_xpath(direction, wildcard=wildcard)
                        return host._snapshot_state(include_tree=False)

                    self._send_json(host._run_on_tk_thread(_gen_sibling))
                    return

                if parsed.path == "/api/xpath/clear":
                    def _clear_xp():
                        owner.clear_input()
                        return host._snapshot_state(include_tree=False)

                    self._send_json(host._run_on_tk_thread(_clear_xp))
                    return

                if parsed.path == "/api/property/select":
                    def _sel_prop():
                        val = str(body.get("value") or "")
                        owner.selected_cell_value.set(val)
                        return {"ok": True, "selected_cell_value": val}

                    self._send_json(host._run_on_tk_thread(_sel_prop))
                    return

                if parsed.path in {"/api/child/fill", "/api/child/validate", "/api/child/copy"}:
                    def _do_child():
                        if "root_mode" in body:
                            owner.child_root_mode_var.set(str(body.get("root_mode") or "当前窗口"))
                            owner.child_control_type_var.set(str(body.get("control_type") or ""))
                            owner.child_auto_id_var.set(str(body.get("auto_id") or ""))
                            owner.child_title_var.set(str(body.get("title") or ""))
                            owner.child_class_name_var.set(str(body.get("class_name") or ""))
                            owner.child_wait_type_var.set(str(body.get("wait_type") or "exists"))
                            owner.child_timeout_var.set(str(body.get("timeout") or "1.0"))
                            extra_yaml = str(body.get("extra_yaml") or "")
                            setattr(owner, "_web_child_extra_yaml", extra_yaml)
                            if owner.child_extra_text is not None:
                                try:
                                    owner.child_extra_text.delete("1.0", "end")
                                    owner.child_extra_text.insert("1.0", extra_yaml)
                                except Exception:
                                    pass
                        if parsed.path == "/api/child/fill":
                            owner.fill_child_from_selection()
                        elif parsed.path == "/api/child/validate":
                            owner.validate_child_from_gui()
                        else:
                            owner.copy_child_locator()
                        return {
                            "child_state": host._serialize_child_state(),
                            "selected_tree_ids": list(owner.tree.selection() or ()),
                        }

                    self._send_json(host._run_on_tk_thread(_do_child))
                    return

                if parsed.path in {"/api/pos/capture", "/api/pos/validate", "/api/pos/copy"}:
                    def _do_pos():
                        if "x" in body:
                            owner.pos_x_var.set(str(body.get("x") or ""))
                            owner.pos_y_var.set(str(body.get("y") or ""))
                            owner.pos_source_width_var.set(str(body.get("source_width") or ""))
                            owner.pos_source_height_var.set(str(body.get("source_height") or ""))
                        if parsed.path == "/api/pos/capture":
                            owner.capture_pos_from_cursor()
                        elif parsed.path == "/api/pos/validate":
                            owner.validate_pos_from_gui()
                        else:
                            owner.copy_pos_locator()
                        return {"pos_state": host._serialize_pos_state()}

                    self._send_json(host._run_on_tk_thread(_do_pos))
                    return

                if parsed.path in {
                    "/api/ocr/choose-image",
                    "/api/ocr/capture-window",
                    "/api/ocr/run",
                    "/api/ocr/match",
                    "/api/ocr/reset",
                }:
                    def _do_ocr():
                        if "target_text" in body:
                            owner.ocr_target_var.set(str(body.get("target_text") or ""))
                        if "match_mode" in body:
                            owner.ocr_match_mode_var.set(str(body.get("match_mode") or "smart"))
                        if "det_limit_type" in body:
                            owner.ocr_det_limit_type_var.set(str(body.get("det_limit_type") or "max"))
                            owner.ocr_det_limit_side_len_var.set(str(body.get("det_limit_side_len") or "960"))
                            owner.ocr_det_box_thresh_var.set(str(body.get("det_box_thresh") or "0.5"))
                            owner.ocr_det_unclip_ratio_var.set(str(body.get("det_unclip_ratio") or "1.6"))
                            owner.ocr_cache_ttl_var.set(str(body.get("cache_ttl") or "0.0"))
                            owner.ocr_use_cache_var.set(bool(body.get("use_cache", False)))

                        if parsed.path == "/api/ocr/choose-image":
                            owner.choose_ocr_image()
                        elif parsed.path == "/api/ocr/capture-window":
                            hwnd = host._find_app_hwnd()
                            if hwnd and win32gui is not None and win32con is not None:
                                try:
                                    win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
                                    time.sleep(0.2)
                                except Exception:
                                    pass
                            try:
                                owner.capture_ocr_current_window()
                            finally:
                                if hwnd and win32gui is not None and win32con is not None:
                                    try:
                                        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                                        win32gui.SetForegroundWindow(hwnd)
                                    except Exception:
                                        pass
                        elif parsed.path == "/api/ocr/run":
                            owner.run_ocr_debug()
                        elif parsed.path == "/api/ocr/match":
                            owner.refresh_ocr_matches()
                        elif parsed.path == "/api/ocr/reset":
                            owner.reset_ocr_params()
                        return {"ocr_state": host._serialize_ocr_state()}

                    self._send_json(host._run_on_tk_thread(_do_ocr))
                    return

                if parsed.path in {
                    "/api/pic/choose-template",
                    "/api/pic/choose-source",
                    "/api/pic/capture-window",
                    "/api/pic/run",
                }:
                    def _do_pic():
                        if "threshold" in body:
                            owner.pic_threshold_var.set(str(body.get("threshold") or "0.6"))
                            owner.pic_method_var.set(str(body.get("method") or "auto"))
                            owner.pic_pos_var.set(str(body.get("pos") or "5"))

                        if parsed.path == "/api/pic/choose-template":
                            owner.choose_pic_template()
                        elif parsed.path == "/api/pic/choose-source":
                            owner.choose_pic_source_image()
                        elif parsed.path == "/api/pic/capture-window":
                            hwnd = host._find_app_hwnd()
                            if hwnd and win32gui is not None and win32con is not None:
                                try:
                                    win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
                                    time.sleep(0.2)
                                except Exception:
                                    pass
                            try:
                                owner.capture_pic_current_window()
                            finally:
                                if hwnd and win32gui is not None and win32con is not None:
                                    try:
                                        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                                        win32gui.SetForegroundWindow(hwnd)
                                    except Exception:
                                        pass
                        elif parsed.path == "/api/pic/run":
                            owner.run_pic_debug()
                        return {"pic_state": host._serialize_pic_state()}

                    self._send_json(host._run_on_tk_thread(_do_pic))
                    return

                if parsed.path == "/api/nav/open":
                    target_tool = str(body.get("tool") or "")

                    def _open_tool():
                        if target_tool == "feature_runner":
                            owner.open_feature_runner()
                            return {"ok": True, "message": "已打开用例运行器。"}
                        if target_tool == "action_runner":
                            owner.open_action_runner(open_browser=True)
                            return {"ok": True, "message": "已打开动作调试器。"}
                        if target_tool == "recorder":
                            owner.open_recorder_workbench()
                            return {"ok": True, "message": "已打开 Recorder 工作台。"}
                        return {"ok": False, "message": f"未知工具: {target_tool}"}

                    self._send_json(host._run_on_tk_thread(_open_tool))
                    return

                if parsed.path == "/api/window/topmost":
                    enabled = bool(body.get("always_on_top", False))
                    host.always_on_top = enabled
                    hwnd = host._find_app_hwnd()
                    host._apply_window_topmost(hwnd, enabled)
                    self._send_json({"ok": True, "always_on_top": enabled})
                    return

                if parsed.path == "/api/theme":
                    mode = str(body.get("theme_mode") or "light")
                    host.theme_mode = "dark" if mode == "dark" else "light"
                    state_dict = load_runner_ui_state()
                    state_dict["theme_mode"] = host.theme_mode
                    save_runner_ui_state(state_dict)
                    self._send_json({"ok": True, "theme_mode": host.theme_mode})
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

        return _LocatorRequestHandler
