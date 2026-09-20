from __future__ import annotations

import multiprocessing
import queue
from datetime import datetime

import win32gui
from pywinauto import Desktop

from autowork_core.utils.debug_tools.common import iter_tree_children, try_to_wrapper
from autowork_core.utils.debug_tools.recorder.models import SCHEMA_VERSION


def capture_tree_snapshot(
        backend="uia",
        window_handle=None,
        max_depth=8,
        max_nodes=1200,
        timeout_ms=None,
):
    timeout_ms = _tree_timeout_ms(timeout_ms)
    if timeout_ms is not None:
        return _capture_tree_snapshot_with_process_timeout(
            backend,
            window_handle,
            max_depth,
            max_nodes,
            timeout_ms,
        )
    return _capture_tree_snapshot_sync(
        backend=backend,
        window_handle=window_handle,
        max_depth=max_depth,
        max_nodes=max_nodes,
    )


def _capture_tree_snapshot_sync(backend="uia", window_handle=None, max_depth=8, max_nodes=1200):
    captured_at = datetime.now().isoformat(timespec="milliseconds")
    result = {
        "schema_version": SCHEMA_VERSION,
        "captured_at": captured_at,
        "backend": backend,
        "window_handle": int(window_handle) if window_handle else None,
        "max_depth": int(max_depth),
        "max_nodes": int(max_nodes),
        "truncated": False,
        "nodes": [],
        "error": None,
    }
    try:
        handle = int(window_handle or win32gui.GetForegroundWindow())
        result["window_handle"] = handle
        root = Desktop(backend=backend).window(handle=handle).wrapper_object()
        _append_tree(result, root, max_depth=max_depth, max_nodes=max_nodes)
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
    return result


def _capture_tree_snapshot_with_process_timeout(
        backend,
        window_handle,
        max_depth,
        max_nodes,
        timeout_ms,
):
    context = multiprocessing.get_context("spawn")
    output = context.Queue(maxsize=1)
    process = context.Process(
        target=_capture_tree_snapshot_process,
        args=(output, backend, window_handle, max_depth, max_nodes),
    )
    process.start()
    process.join(timeout_ms / 1000)
    if process.is_alive():
        process.terminate()
        process.join(1)
        if process.is_alive():
            process.kill()
            process.join(1)
        return _tree_timeout_snapshot(
            backend,
            window_handle,
            max_depth,
            max_nodes,
            timeout_ms,
        )
    try:
        return output.get_nowait()
    except queue.Empty:
        return _tree_timeout_snapshot(
            backend,
            window_handle,
            max_depth,
            max_nodes,
            timeout_ms,
            reason="tree_capture_process_returned_no_result",
        )


def _capture_tree_snapshot_process(output, backend, window_handle, max_depth, max_nodes):
    output.put(_capture_tree_snapshot_sync(
        backend=backend,
        window_handle=window_handle,
        max_depth=max_depth,
        max_nodes=max_nodes,
    ))


def _tree_timeout_ms(value):
    if value in (None, ""):
        return None
    try:
        timeout_ms = int(value)
    except (TypeError, ValueError):
        return None
    return timeout_ms if timeout_ms > 0 else None


def _tree_timeout_snapshot(
        backend,
        window_handle,
        max_depth,
        max_nodes,
        timeout_ms,
        *,
        reason=None,
):
    reason = reason or f"tree_capture_timeout: exceeded {int(timeout_ms)} ms"
    return {
        "schema_version": SCHEMA_VERSION,
        "captured_at": datetime.now().isoformat(timespec="milliseconds"),
        "backend": backend,
        "window_handle": _safe_window_handle(window_handle),
        "max_depth": int(max_depth),
        "max_nodes": int(max_nodes),
        "truncated": False,
        "nodes": [],
        "error": reason,
    }


def _safe_window_handle(value):
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, int):
        return int(value)
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


def diff_tree_snapshots(before, after):
    before_nodes = {_stable_key(node): node for node in before.get("nodes", [])}
    after_nodes = {_stable_key(node): node for node in after.get("nodes", [])}
    before_keys = set(before_nodes)
    after_keys = set(after_nodes)
    before_handle = before.get("window_handle")
    after_handle = after.get("window_handle")
    comparable = bool(
        before_handle
        and after_handle
        and int(before_handle) == int(after_handle)
        and not before.get("error")
        and not after.get("error")
    )
    changed = []
    for key in sorted(before_keys & after_keys):
        previous = _comparable(before_nodes[key])
        current = _comparable(after_nodes[key])
        if previous != current:
            changed.append({"key": key, "before": previous, "after": current})
    return {
        "schema_version": SCHEMA_VERSION,
        "comparable": comparable,
        "comparison_reason": (
            "same_target_window"
            if comparable
            else _tree_comparison_reason(
                before,
                after,
                before_handle,
                after_handle,
            )
        ),
        "added": [after_nodes[key] for key in sorted(after_keys - before_keys)],
        "removed": [before_nodes[key] for key in sorted(before_keys - after_keys)],
        "changed": changed,
        "summary": {
            "before_count": len(before_nodes),
            "after_count": len(after_nodes),
            "added_count": len(after_keys - before_keys),
            "removed_count": len(before_keys - after_keys),
            "changed_count": len(changed),
        },
    }


def _tree_comparison_reason(before, after, before_handle, after_handle):
    before_error = before.get("error")
    after_error = after.get("error")
    if before_error or after_error:
        return f"tree_capture_error: before={before_error}, after={after_error}"
    return f"window_or_capture_mismatch: before={before_handle}, after={after_handle}"


def _append_tree(result, root, max_depth, max_nodes):
    root = try_to_wrapper(root)
    root_signature = _semantic_signature(_tree_node_info(root))
    stack = [(root, None, 0, "0", root_signature)]
    seen = set()
    while stack:
        element, parent_id, depth, path, semantic_path = stack.pop()
        if len(result["nodes"]) >= max_nodes:
            result["truncated"] = True
            break
        info = _tree_node_info(element)
        identity = _identity(info, path)
        if identity in seen:
            continue
        seen.add(identity)
        node_id = f"node-{len(result['nodes']) + 1:05d}"
        node = {
            "id": node_id,
            "parent_id": parent_id,
            "depth": depth,
            "path": path,
            "semantic_path": semantic_path,
            **info,
        }
        result["nodes"].append(node)
        if depth >= max_depth:
            continue
        try:
            children = list(iter_tree_children(element))
        except Exception:
            children = []
        child_entries = []
        signature_counts = {}
        for index, child in enumerate(children):
            signature = _semantic_signature(_tree_node_info(child))
            occurrence = signature_counts.get(signature, 0)
            signature_counts[signature] = occurrence + 1
            child_entries.append((
                child,
                node_id,
                depth + 1,
                f"{path}.{index}",
                f"{semantic_path}/{signature}[{occurrence}]",
            ))
        stack.extend(reversed(child_entries))


def _tree_node_info(element):
    info = _element_info_object(element)
    rect = _rectangle(element, info)
    value = _value(element, info)
    return {
        "name": str(_info_attr(info, "name", "") or ""),
        "auto_id": (
            _info_attr(info, "automation_id", "")
            or _info_attr(info, "auto_id", "")
        ),
        "control_type": str(_info_attr(info, "control_type", "") or ""),
        "class_name": str(_info_attr(info, "class_name", "") or ""),
        "framework_id": _info_attr(info, "framework_id", ""),
        "handle": _info_attr(info, "handle"),
        "process_id": _info_attr(info, "process_id"),
        "runtime_id": _info_attr(info, "runtime_id"),
        "enabled": _enabled(element, info),
        "visible": _visible(element, info),
        "value": value,
        "rectangle": _rect_to_list(rect),
    }


def _element_info_object(element):
    info = _info_attr(element, "element_info")
    return info if info is not None else element


def _info_attr(obj, name, default=None):
    try:
        value = getattr(obj, name, default)
    except Exception:
        return default
    if callable(value):
        try:
            value = value()
        except TypeError:
            return default
        except Exception:
            return default
    return default if value is None else value


def _rectangle(element, info):
    return _info_attr(element, "rectangle") or _info_attr(info, "rectangle")


def _value(element, info):
    value = _info_attr(element, "get_value")
    if value is not None:
        return value
    iface_value = _info_attr(element, "iface_value")
    current_value = _info_attr(iface_value, "CurrentValue")
    if current_value is not None:
        return current_value
    legacy = _info_attr(element, "legacy_properties") or {}
    if isinstance(legacy, dict):
        return legacy.get("Value") or legacy.get("value")
    return _info_attr(info, "value")


def _enabled(element, info):
    value = _info_attr(element, "is_enabled")
    if value is not None:
        return value
    value = _info_attr(info, "is_enabled")
    if value is not None:
        return value
    return _info_attr(info, "enabled")


def _visible(element, info):
    value = _info_attr(element, "is_visible")
    if value is not None:
        return value
    value = _info_attr(info, "is_visible")
    if value is not None:
        return value
    value = _info_attr(info, "visible")
    if value is not None:
        return value
    offscreen = _info_attr(info, "is_offscreen")
    return None if offscreen is None else not bool(offscreen)


def _rect_to_list(rect):
    if rect is None:
        return None
    try:
        return [
            int(rect.left),
            int(rect.top),
            int(rect.right),
            int(rect.bottom),
        ]
    except Exception:
        return None

def _identity(info, path):
    runtime_id = tuple(info.get("runtime_id") or ())
    handle = info.get("handle")
    if runtime_id:
        return "runtime", runtime_id
    if handle:
        return "handle", int(handle)
    return "path", path, info.get("control_type"), info.get("auto_id"), info.get("name")


def _stable_key(node):
    runtime_id = tuple(node.get("runtime_id") or ())
    if runtime_id:
        return "runtime:" + ".".join(str(value) for value in runtime_id)
    if node.get("handle"):
        return f"handle:{node['handle']}"
    return f"semantic:{node.get('semantic_path') or node.get('path') or ''}"


def _semantic_signature(info):
    values = (
        info.get("control_type"),
        info.get("auto_id"),
        info.get("name"),
        info.get("class_name"),
    )
    text = "|".join(str(value or "") for value in values)
    return text.replace("/", "_") or "element"


def _comparable(node):
    return {
        key: node.get(key)
        for key in (
            "name",
            "auto_id",
            "control_type",
            "class_name",
            "framework_id",
            "rectangle",
            "enabled",
            "visible",
            "value",
        )
    }