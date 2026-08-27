from __future__ import annotations

import hashlib
import os

import psutil
import win32gui
import win32process


MIN_RECORDABLE_WINDOW_EDGE = 16


def is_recordable_window_handle(handle):
    try:
        handle = int(handle)
    except (TypeError, ValueError):
        return False
    if not handle or not win32gui.IsWindow(handle):
        return False
    if not win32gui.IsWindowVisible(handle):
        return False
    try:
        left, top, right, bottom = win32gui.GetWindowRect(handle)
    except Exception:
        return False
    return (
        int(right) - int(left) >= MIN_RECORDABLE_WINDOW_EDGE
        and int(bottom) - int(top) >= MIN_RECORDABLE_WINDOW_EDGE
    )


def freeze_window_identity(window):
    value = {
        key: window.get(key)
        for key in (
            "handle",
            "process_id",
            "title",
            "class_name",
            "process_name",
            "process_executable_fingerprint",
        )
        if window.get(key) is not None
    }
    process_id = value.get("process_id")
    if process_id is not None:
        for key, item in _process_identity(process_id).items():
            value.setdefault(key, item)
    return value


def window_identity_for_handle(handle):
    handle = int(handle)
    _, process_id = win32process.GetWindowThreadProcessId(handle)
    return freeze_window_identity({
        "handle": handle,
        "process_id": int(process_id),
        "title": str(win32gui.GetWindowText(handle) or ""),
        "class_name": str(win32gui.GetClassName(handle) or ""),
    })


def list_top_level_windows(backend=None):
    del backend
    windows = []

    def collect(handle, _value):
        if not win32gui.IsWindowVisible(handle):
            return True
        try:
            windows.append(window_identity_for_handle(handle))
        except Exception:
            pass
        return True

    win32gui.EnumWindows(collect, None)
    return windows


def restore_window_handles(recorded_windows, current_windows):
    current_by_handle = {
        int(window["handle"]): freeze_window_identity(window)
        for window in current_windows
        if window.get("handle") is not None
    }
    restored = [None] * len(recorded_windows)
    used_handles = set()
    for index, recorded in enumerate(recorded_windows):
        handle = recorded.get("handle")
        handle = int(handle) if handle is not None else None
        if (
                handle in current_by_handle
                and handle not in used_handles
                and window_identity_score(
                    recorded,
                    current_by_handle[handle],
                ) >= 30
        ):
            restored[index] = handle
            used_handles.add(handle)

    for index, recorded in enumerate(recorded_windows):
        if restored[index] is not None:
            continue
        candidates = []
        for handle, current in current_by_handle.items():
            if handle in used_handles:
                continue
            score = window_identity_score(recorded, current)
            if score:
                candidates.append((score, handle))
        candidates.sort(reverse=True)
        if not candidates or candidates[0][0] < 30:
            continue
        if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
            continue
        restored[index] = candidates[0][1]
        used_handles.add(candidates[0][1])
    return tuple(handle for handle in restored if handle is not None)


def window_identity_score(recorded, current):
    recorded_image = _identity_text(
        recorded.get("process_executable_fingerprint")
    )
    current_image = _identity_text(
        current.get("process_executable_fingerprint")
    )
    recorded_process = _identity_text(recorded.get("process_name"))
    current_process = _identity_text(current.get("process_name"))
    if recorded_image and recorded_image != current_image:
        return 0
    if not recorded_image and recorded_process and (
            recorded_process != current_process
    ):
        return 0

    recorded_class = _identity_text(recorded.get("class_name"))
    current_class = _identity_text(current.get("class_name"))
    recorded_title = _identity_text(recorded.get("title"))
    current_title = _identity_text(current.get("title"))
    score = 0
    if recorded_image and recorded_image == current_image:
        score += 15
    elif recorded_process and recorded_process == current_process:
        score += 10
    if (
            recorded.get("process_id") is not None
            and recorded.get("process_id") == current.get("process_id")
    ):
        score += 15
    if recorded_class and recorded_class == current_class:
        score += 20
    if recorded_title and recorded_title == current_title:
        score += 20
    elif (
            recorded_title
            and current_title
            and _title_application(recorded_title)
            == _title_application(current_title)
    ):
        score += 10
    return score


def _process_identity(process_id):
    try:
        process = psutil.Process(int(process_id))
        result = {"process_name": str(process.name() or "")}
        try:
            executable = os.path.normcase(os.path.realpath(process.exe()))
        except (OSError, psutil.Error):
            executable = ""
        if executable:
            result["process_executable_fingerprint"] = hashlib.sha256(
                executable.casefold().encode("utf-8")
            ).hexdigest()
        return result
    except (OSError, psutil.Error, TypeError, ValueError):
        return {}


def _identity_text(value):
    return " ".join(str(value or "").casefold().split()).lstrip("*")


def _title_application(title):
    for separator in (" - ", " – ", " — "):
        if separator in title:
            return title.rsplit(separator, 1)[-1].strip()
    return title