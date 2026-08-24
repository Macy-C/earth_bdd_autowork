from __future__ import annotations

from datetime import datetime

import win32gui
from pywinauto import Desktop

from autowork_core.utils.debug_tools.common import iter_tree_children, try_to_wrapper
from autowork_core.utils.debug_tools.recorder.inspector import _element_info
from autowork_core.utils.debug_tools.recorder.models import SCHEMA_VERSION


def capture_tree_snapshot(backend="uia", window_handle=None, max_depth=8, max_nodes=1200):
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
            else f"window_or_capture_mismatch: before={before_handle}, after={after_handle}"
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


def _append_tree(result, root, max_depth, max_nodes):
    root = try_to_wrapper(root)
    root_signature = _semantic_signature(_element_info(root))
    stack = [(root, None, 0, "0", root_signature)]
    seen = set()
    while stack:
        element, parent_id, depth, path, semantic_path = stack.pop()
        if len(result["nodes"]) >= max_nodes:
            result["truncated"] = True
            break
        info = _element_info(element)
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
            signature = _semantic_signature(_element_info(child))
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