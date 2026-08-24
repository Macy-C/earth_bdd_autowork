from __future__ import annotations

import threading


class HoverTargetResolver:
    def __init__(self, backend="uia"):
        self.backend = str(backend or "uia").strip().lower()
        if self.backend not in {"uia", "win32"}:
            raise ValueError(f"不支持的悬停解析 backend: {backend}")
        self._running = False
        self._backend = None
        self._com_initialized = False
        self._diagnostics = {
            "queries_submitted": 0,
            "queries_completed": 0,
            "query_errors": 0,
        }

    @property
    def diagnostics(self):
        return dict(self._diagnostics)

    def start(self):
        self._running = True
        return self

    def stop(self):
        self._running = False

    def initialize_thread(self):
        if not self._running or self._backend is not None:
            return
        if self.backend == "uia":
            import pythoncom

            pythoncom.CoInitialize()
            self._com_initialized = True
        try:
            self._backend = _HoverBackend(self.backend)
        except Exception:
            self.shutdown_thread()
            raise

    def shutdown_thread(self):
        self._backend = None
        if not self._com_initialized:
            return
        self._com_initialized = False
        try:
            import pythoncom

            pythoncom.CoUninitialize()
        except Exception:
            pass

    def resolve(self, point, *, process_id, window_handle):
        if not self._running:
            return None
        if self._backend is None:
            self.initialize_thread()
        self._diagnostics["queries_submitted"] += 1
        try:
            target = self._backend.resolve(point[0], point[1])
        except Exception:
            self._diagnostics["query_errors"] += 1
            return None
        if not _valid_target(
                target,
                point=point,
                process_id=process_id,
                window_handle=window_handle,
            ):
            self._diagnostics["query_errors"] += 1
            return None
        self._diagnostics["queries_completed"] += 1
        return target


class CursorHoverController:
    def __init__(
            self,
            backend,
            context_provider,
            on_notification,
            *,
            resolver_factory=HoverTargetResolver,
        ):
        self.context_provider = context_provider
        self.on_notification = on_notification
        self.resolver = resolver_factory(str(backend or "uia"))
        self._condition = threading.Condition()
        self._thread = None
        self._running = False
        self._enabled = True
        self._sequence = 0
        self._latest = None
        self._visible_key = None
        self._visible_rectangle = None

    @property
    def diagnostics(self):
        return self.resolver.diagnostics

    def start(self):
        if self._running:
            return self
        self.resolver.start()
        self._running = True
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="recorder-target-hover",
        )
        self._thread.start()
        return self

    def stop(self):
        with self._condition:
            self._running = False
            self._latest = None
            self._condition.notify_all()
        self._publish_clear()
        self.resolver.stop()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._thread = None

    def submit(self, point, *, force=False):
        if point is None:
            return
        point = (int(point[0]), int(point[1]))
        with self._condition:
            if not self._running or not self._enabled:
                return
            if (
                    not force
                    and self._latest is not None
                    and self._latest[1] == point
                    and self._visible_rectangle is not None
                    and _contains(self._visible_rectangle, point)
                ):
                return
            self._sequence += 1
            self._latest = (self._sequence, point)
            if (
                    self._visible_rectangle is not None
                    and (
                        force
                        or not _contains(self._visible_rectangle, point)
                    )
                ):
                self._publish_clear_locked()
            self._condition.notify()

    def set_enabled(self, enabled):
        with self._condition:
            self._enabled = bool(enabled)
            if not self._enabled:
                self._latest = None
            self._condition.notify_all()
        if not enabled:
            self._publish_clear()

    def _run(self):
        handled_sequence = 0
        try:
            initialize = getattr(self.resolver, "initialize_thread", None)
            if callable(initialize):
                initialize()
            while True:
                with self._condition:
                    while (
                            self._running
                            and (
                                not self._enabled
                                or self._latest is None
                                or self._latest[0] == handled_sequence
                            )
                        ):
                        self._condition.wait()
                    if not self._running:
                        return
                    sequence, point = self._latest
                context = self.context_provider(point) or {}
                if not context.get("eligible"):
                    handled_sequence = sequence
                    with self._condition:
                        if (
                                self._latest is not None
                                and self._latest[0] == sequence
                            ):
                            self._publish_clear_locked()
                    continue
                target = self.resolver.resolve(
                    point,
                    process_id=context.get("process_id"),
                    window_handle=context.get("window_handle"),
                )
                with self._condition:
                    current = self._latest
                    current_sequence = (
                        current[0] if current is not None else None
                    )
                handled_sequence = sequence
                with self._condition:
                    if current_sequence != sequence:
                        continue
                    if target is None:
                        self._publish_clear_locked()
                        continue
                    target_key = _target_key(target)
                    if target_key == self._visible_key:
                        continue
                    if self._notify({
                            "kind": "preview",
                            "point": list(point),
                            "target": target,
                        }):
                        self._visible_key = target_key
                        self._visible_rectangle = list(target["rectangle"])
        finally:
            shutdown = getattr(self.resolver, "shutdown_thread", None)
            if callable(shutdown):
                shutdown()

    def _publish_clear(self):
        with self._condition:
            self._publish_clear_locked()

    def _publish_clear_locked(self):
        if self._visible_key is None and self._visible_rectangle is None:
            return True
        if not self._notify({"kind": "clear"}):
            return False
        self._visible_key = None
        self._visible_rectangle = None
        return True

    def _notify(self, notification):
        try:
            return self.on_notification(notification) is not False
        except Exception:
            return False


class _HoverBackend:
    def __init__(self, backend):
        self.backend = str(backend or "uia")
        from autowork_core.utils.debug_tools.recorder.inspector import (
            _wrapper_from_point,
        )

        self.wrapper_from_point = _wrapper_from_point
        try:
            import win32gui

            point = win32gui.GetCursorPos()
            wrapper = self.wrapper_from_point(
                point[0],
                point[1],
                self.backend,
            )
            wrapper.rectangle()
            wrapper.element_info.runtime_id
        except Exception:
            pass

    def resolve(self, x, y):
        import win32gui

        x, y = int(x), int(y)
        point_handle = win32gui.WindowFromPoint((x, y))
        window_handle = win32gui.GetAncestor(point_handle, 2) or point_handle
        wrapper = self.wrapper_from_point(x, y, self.backend)
        rectangle = wrapper.rectangle()
        info = wrapper.element_info
        runtime_id = getattr(info, "runtime_id", None)
        return {
            "rectangle": [
                int(rectangle.left),
                int(rectangle.top),
                int(rectangle.right),
                int(rectangle.bottom),
            ],
            "runtime_id": list(runtime_id) if runtime_id is not None else None,
            "process_id": _optional_int(getattr(info, "process_id", None)),
            "handle": _optional_int(getattr(info, "handle", None)),
            "window_handle": _optional_int(window_handle),
            "control_type": str(getattr(info, "control_type", "") or ""),
        }


def _valid_target(target, *, point, process_id, window_handle):
    try:
        target_process_id = int(target.get("process_id"))
        target_window_handle = int(target.get("window_handle"))
    except (TypeError, ValueError):
        return False
    return (
        target_process_id == int(process_id)
        and target_window_handle == int(window_handle)
        and _contains(target.get("rectangle"), point)
    )


def _contains(rectangle, point):
    try:
        left, top, right, bottom = [int(value) for value in rectangle]
        x, y = int(point[0]), int(point[1])
    except (TypeError, ValueError, IndexError):
        return False
    return (
        right > left
        and bottom > top
        and left <= x < right
        and top <= y < bottom
    )


def _target_key(target):
    runtime_id = target.get("runtime_id")
    return (
        target.get("process_id"),
        target.get("window_handle"),
        target.get("handle"),
        target.get("control_type"),
        tuple(runtime_id) if isinstance(runtime_id, list) else (),
        tuple(target.get("rectangle") or ()),
    )


def _optional_int(value):
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
