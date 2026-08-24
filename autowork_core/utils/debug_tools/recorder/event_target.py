from __future__ import annotations

from dataclasses import dataclass, field
import queue
import threading
import time


EVENT_TARGET_BINDING_VERSION = "1.0"
DEFAULT_EVENT_TARGET_TIMEOUT_MS = 15
_STOP = object()


@dataclass
class _TargetRequest:
    point: tuple[int, int]
    process_id: int | None
    window_handle: int | None
    event_type: str
    deadline: float
    done: threading.Event = field(default_factory=threading.Event)
    element: dict | None = None
    ancestors: list[dict] = field(default_factory=list)
    error: str | None = None


class EventTargetResolver:
    def __init__(self, backend="uia"):
        self.backend = str(backend or "uia").strip().lower()
        self._queue = queue.Queue()
        self._thread = None
        self._ready = threading.Event()
        self._running = False
        self._startup_error = None
        self._diagnostics = {
            "captured": 0,
            "timed_out": 0,
            "errors": 0,
            "unavailable": 0,
        }

    @property
    def diagnostics(self):
        return dict(self._diagnostics)

    def start(self, timeout=1.0):
        if self._running:
            return self._ready.is_set() and self._startup_error is None
        self._queue = queue.Queue()
        self._ready.clear()
        self._startup_error = None
        self._running = True
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="recorder-event-target",
        )
        self._thread.start()
        return (
            self._ready.wait(max(0.0, float(timeout)))
            and self._startup_error is None
        )

    def stop(self, timeout=1.0):
        self._running = False
        self._queue.put(_STOP)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout)))
        self._thread = None

    def capture(
            self,
            point,
            *,
            process_id,
            window_handle,
            timeout_ms,
            event_type="mouse_down",
        ):
        timeout_ms = max(0, int(timeout_ms))
        started = time.perf_counter()
        base = {
            "target_binding_version": EVENT_TARGET_BINDING_VERSION,
            "phase": "pre_dispatch",
            "budget_ms": timeout_ms,
            "process_id": process_id,
            "window_handle": window_handle,
        }
        if any((
            self.backend != "uia",
            not self._running,
            not self._ready.is_set(),
            self._startup_error is not None,
        )):
            self._diagnostics["unavailable"] += 1
            return {
                **base,
                "status": "unavailable",
                "latency_ms": 0,
                "element": None,
            }
        request = _TargetRequest(
            point=(int(point[0]), int(point[1])),
            process_id=(int(process_id) if process_id is not None else None),
            window_handle=(
                int(window_handle) if window_handle is not None else None
            ),
            event_type=str(event_type or "mouse_down"),
            deadline=started + timeout_ms / 1000,
        )
        self._queue.put(request)
        completed = request.done.wait(timeout_ms / 1000)
        latency_ms = int(round((time.perf_counter() - started) * 1000))
        if not completed:
            if request.element is None:
                self._diagnostics["timed_out"] += 1
                return {
                    **base,
                    "status": "timeout",
                    "latency_ms": latency_ms,
                    "element": None,
                }
            self._diagnostics["captured"] += 1
            return {
                **base,
                "status": "captured",
                "latency_ms": latency_ms,
                "element": request.element,
                "ancestors": [],
                "partial": "ancestors_timeout",
            }
        if request.error is not None or request.element is None:
            self._diagnostics["errors"] += 1
            return {
                **base,
                "status": "error",
                "latency_ms": latency_ms,
                "element": None,
                "error": request.error or "event target unavailable",
            }
        self._diagnostics["captured"] += 1
        return {
            **base,
            "status": "captured",
            "latency_ms": latency_ms,
            "element": request.element,
            "ancestors": request.ancestors,
        }

    def _run(self):
        import pythoncom

        pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)
        try:
            backend = _UIAEventTargetBackend()
            backend.warm_up()
            self._ready.set()
            while True:
                request = self._queue.get()
                if request is _STOP:
                    return
                if not self._running or time.perf_counter() > request.deadline:
                    request.done.set()
                    continue
                try:
                    element = backend.element_from_point(request.point)
                    _validate_element(
                        element,
                        point=request.point,
                        process_id=request.process_id,
                    )
                    request.element = element
                    if _requires_ancestor_context(
                            element,
                            request.event_type,
                    ):
                        try:
                            ancestors = backend.ancestors(
                                window_handle=request.window_handle,
                                deadline=request.deadline,
                                limit=6,
                            )
                            if isinstance(ancestors, (list, tuple)):
                                request.ancestors = list(ancestors)
                        except Exception:
                            request.ancestors = []
                except Exception as error:
                    request.error = (
                        f"{type(error).__name__}: {error}"
                    )[:300]
                finally:
                    request.done.set()
        except Exception as error:
            self._startup_error = f"{type(error).__name__}: {error}"[:300]
            self._ready.set()
        finally:
            pythoncom.CoUninitialize()


class _UIAEventTargetBackend:
    def __init__(self):
        import comtypes
        import comtypes.client

        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient

        self._client = UIAutomationClient
        self._automation = comtypes.CoCreateInstance(
            UIAutomationClient.CUIAutomation().IPersist_GetClassID(),
            interface=UIAutomationClient.IUIAutomation,
            clsctx=comtypes.CLSCTX_INPROC_SERVER,
        )
        self._control_types = {
            int(getattr(UIAutomationClient, name)): name[4:-13]
            for name in dir(UIAutomationClient)
            if name.startswith("UIA_") and name.endswith("ControlTypeId")
        }
        self._last_element = None

    def warm_up(self):
        import win32gui

        self._automation.GetRootElement()
        try:
            self.element_from_point(win32gui.GetCursorPos())
        except Exception:
            pass

    def element_from_point(self, point):
        raw = self._automation.ElementFromPoint(
            self._client.tagPOINT(int(point[0]), int(point[1]))
        )
        self._last_element = raw
        return self._element_info(raw)

    def ancestors(self, *, window_handle, deadline, limit):
        walker = self._automation.ControlViewWalker
        current = self._last_element
        result = []
        for _index in range(max(0, int(limit))):
            if current is None or time.perf_counter() > deadline:
                break
            current = walker.GetParentElement(current)
            if current is None:
                break
            info = self._element_info(current)
            if (
                info.get("control_type") == "Window"
                or (
                    window_handle
                    and info.get("handle")
                    and int(info["handle"]) == int(window_handle)
                )
            ):
                break
            event_locator = _native_parent_locator(
                info,
                window_handle=window_handle,
            )
            if event_locator is not None:
                info["event_locator"] = event_locator
            result.append(info)
        return result

    def _element_info(self, raw):
        rectangle = raw.CurrentBoundingRectangle
        runtime_id = raw.GetRuntimeId()
        native_handle = int(raw.CurrentNativeWindowHandle or 0)
        return {
            "name": str(raw.CurrentName or ""),
            "auto_id": str(raw.CurrentAutomationId or ""),
            "control_type": self._control_types.get(
                int(raw.CurrentControlType),
                "",
            ),
            "class_name": str(raw.CurrentClassName or ""),
            "framework_id": str(raw.CurrentFrameworkId or ""),
            "handle": native_handle or None,
            "process_id": int(raw.CurrentProcessId),
            "runtime_id": [int(value) for value in (runtime_id or ())],
            "enabled": bool(raw.CurrentIsEnabled),
            "visible": not bool(raw.CurrentIsOffscreen),
            "value": None,
            "rectangle": [
                int(rectangle.left),
                int(rectangle.top),
                int(rectangle.right),
                int(rectangle.bottom),
            ],
        }


def _validate_element(element, *, point, process_id):
    if not isinstance(element, dict):
        raise ValueError("event target is not structured")
    if process_id is not None and int(element.get("process_id") or 0) != int(
            process_id
        ):
        raise ValueError("event target process changed")
    rectangle = element.get("rectangle") or ()
    if len(rectangle) != 4:
        raise ValueError("event target rectangle unavailable")
    if not (
        int(rectangle[0]) <= int(point[0]) < int(rectangle[2])
        and int(rectangle[1]) <= int(point[1]) < int(rectangle[3])
    ):
        raise ValueError("event target no longer contains the input point")


def _requires_ancestor_context(element, event_type):
    if str(event_type or "").casefold() == "mouse_wheel":
        return True
    control_type = str(
        (element or {}).get("control_type") or ""
    ).casefold()
    if control_type in {"custom", "image"}:
        return True
    return control_type == "pane" and not str(
        (element or {}).get("auto_id") or ""
    )


def _native_parent_locator(element, *, window_handle):
    handle = int(element.get("handle") or 0)
    window_handle = int(window_handle or 0)
    control_type = str(element.get("control_type") or "")
    if not handle or not window_handle or not control_type:
        return None
    import win32gui

    if not win32gui.IsWindow(handle):
        return None
    control_id = int(win32gui.GetDlgCtrlID(handle) or 0)
    if (
        control_id <= 0
        or int(win32gui.GetDlgItem(window_handle, control_id) or 0) != handle
    ):
        return None
    return {
        "name": f"event_parent_{control_id}",
        "locator": {
            "control_type": control_type,
            "auto_id": str(control_id),
        },
    }