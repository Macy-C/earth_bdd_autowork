import re
import hashlib
import json

import win32api
import win32gui
from pywinauto import Desktop
from pywinauto.findwindows import ElementAmbiguousError

from autowork_core.common.element_properties import read_accessible_name
from autowork_core.common.winauto_xpath import find_by_xpath
from autowork_core.utils.debug_tools.common import (
    get_all_element_properties,
    iter_tree_children,
    make_element_key,
    make_xpath_predicate,
    make_xpath_suggestion,
    safe_parent,
)
from autowork_core.utils.debug_tools.locator_tools import validate_child_locator
from autowork_core.utils.debug_tools.recorder.observation_providers import (
    COLLECTION_CONTROL_TYPES,
    ObservationCaptureContext,
    get_structured_observation_provider,
    select_structured_observation_provider,
)


MAX_STRUCTURAL_XPATH_CANDIDATES = 16
MAX_XPATH_ANCESTORS = 4
MAX_TARGET_CONTAINER_DEPTH = 3
MAX_ANCESTOR_CHAIN_DEPTH = 3
MAX_ANCHOR_DESCENDANT_DEPTH = 2
MAX_ANCHOR_DESCENDANTS = 16
MAX_EVENT_CHAIN_DEPTH = 12
MAX_EVENT_CHAIN_SIBLINGS = 64
LABEL_LIKE_CONTROL_TYPES = frozenset({"label", "static", "text"})
SCROLL_CONTAINER_CONTROL_TYPES = frozenset({
    *COLLECTION_CONTROL_TYPES,
    "document",
})


def _safe_call(func, default=None):
    try:
        return func()
    except Exception:
        return default


def _safe_attr(obj, name, default=None):
    try:
        value = getattr(obj, name, default)
        return default if value is None else value
    except Exception:
        return default


def _rect_to_list(rect):
    if rect is None:
        return None
    return [int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)]


def _snake_name(text, fallback="element"):
    text = str(text or "").strip()
    text = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", text)
    text = text.strip("_").lower()
    return text or fallback


def _element_info(wrapper):
    info = _safe_attr(wrapper, "element_info")
    rect = _safe_call(wrapper.rectangle)
    name = _safe_call(lambda: read_accessible_name(
        wrapper,
        element_info=info,
    ), "")
    auto_id = _safe_attr(info, "automation_id", "") or _safe_attr(info, "auto_id", "")
    control_type = _safe_attr(info, "control_type", "")
    class_name = _safe_attr(info, "class_name", "")

    value = _safe_call(getattr(wrapper, "get_value", lambda: None))
    if value is None:
        value = _safe_attr(_safe_attr(wrapper, "iface_value"), "CurrentValue")
    if value is None:
        legacy = _safe_call(getattr(wrapper, "legacy_properties", lambda: {}), {}) or {}
        value = legacy.get("Value") or legacy.get("value")

    return {
        "name": name,
        "auto_id": auto_id,
        "control_type": control_type,
        "class_name": class_name,
        "framework_id": _safe_attr(info, "framework_id", ""),
        "handle": _safe_attr(info, "handle"),
        "process_id": _safe_attr(info, "process_id"),
        "runtime_id": _safe_attr(info, "runtime_id"),
        "enabled": _safe_call(wrapper.is_enabled),
        "visible": _safe_call(wrapper.is_visible),
        "value": value,
        "rectangle": _rect_to_list(rect),
    }


def _suggest_root_name(window, backend):
    info = _element_info(window) if window is not None else {}
    return _suggest_root_name_from_info(info, backend)


def _suggest_root_name_from_info(info, backend):
    root_locator = _root_locator(info, backend)
    base = info.get("auto_id") or info.get("class_name") or info.get("name") or "window"
    digest = hashlib.sha1(
        json.dumps(root_locator, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:8]
    return f"{_snake_name(base, fallback='window')}_window_{digest}"


def event_target_from_binding(binding, raw, backend):
    if not isinstance(binding, dict) or binding.get("status") != "captured":
        return None
    captured_element = dict(binding.get("element") or {})
    if not captured_element:
        return None
    ancestors = [
        dict(item)
        for item in binding.get("ancestors") or ()
        if isinstance(item, dict)
    ]
    selected_index = -1
    element_info = captured_element
    if raw.get("event_type") == "mouse_wheel":
        for index, candidate in enumerate(
                [captured_element, *ancestors],
        ):
            if str(candidate.get("control_type") or "").casefold() in (
                    SCROLL_CONTAINER_CONTROL_TYPES
            ):
                element_info = candidate
                selected_index = index - 1
                break
    window_info = {
        "name": str(raw.get("window_title") or ""),
        "auto_id": "",
        "control_type": "Window",
        "class_name": str(raw.get("window_class") or ""),
        "framework_id": "Win32",
        "handle": raw.get("window_handle"),
        "process_id": raw.get("process_id"),
        "runtime_id": None,
        "enabled": None,
        "visible": None,
        "value": None,
        "rectangle": None,
    }
    root_name = _suggest_root_name_from_info(window_info, backend)
    point = tuple(raw.get("point") or ())
    candidates = _locator_candidates(
        element_info,
        root_name,
        point or (0, 0),
    )
    _defer_structured_candidates(candidates)
    first_structured = next((
        candidate
        for candidate in candidates
        if (candidate.get("locator") or {}).get("by", "child")
        in {"child", "xpath"}
    ), None)
    preferred = first_structured or _preferred_candidate(candidates)
    first_name = preferred["name"] if preferred else "element"
    pic_region_candidate = _event_bound_pic_region_candidate(
        captured_element,
        ancestors,
        root_name,
    )
    return {
        "backend": backend,
        "point": list(point) if point else None,
        "window": window_info,
        "element": element_info,
        "element_properties": {},
        "ancestors": list(reversed(ancestors[selected_index + 1:])),
        "local_context": {},
        "pic_region_candidate": pic_region_candidate,
        "point_in_element": (
            _point_in_element(point, element_info.get("rectangle"))
            if point else None
        ),
        "structural_role": _structural_role(element_info),
        "target_quality": _target_quality(element_info),
        "root_name": root_name,
        "root_locator": _root_locator(window_info, backend),
        "locator_candidates": candidates,
        "suggested_action": _suggested_action(
            _inspection_event_type_from_raw(raw),
            first_name,
        ),
        "inspection_mode": "event_bound",
        "error": None,
    }


def _event_bound_pic_region_candidate(element, ancestors, root_name):
    for ancestor in ancestors:
        event_locator = ancestor.get("event_locator") or {}
        locator = event_locator.get("locator") or {}
        rectangle = ancestor.get("rectangle")
        if not (
            event_locator.get("name")
            and isinstance(locator, dict)
            and _strictly_contains(rectangle, element.get("rectangle"))
        ):
            continue
        return {
            "name": f"{event_locator['name']}_pic_region",
            "locator": {"root": root_name, **locator},
            "rectangle": list(rectangle),
            "element": {
                key: value
                for key, value in ancestor.items()
                if key != "event_locator"
            },
            "validation": {
                "status": "deferred",
                "count": None,
                "target_matches": None,
                "reason": "validate against complete post-capture tree",
                "source": "event_native_parent",
            },
        }
    return None


def _inspection_event_type_from_raw(raw):
    if raw.get("event_type") == "mouse_wheel":
        return "scroll"
    if raw.get("button") == "right":
        return "right_click"
    if raw.get("button") == "middle":
        return "middle_click"
    return "click"


def _root_locator(window_info, backend):
    locator = {"top_level": True, "backend": backend}
    if window_info.get("control_type"):
        locator["control_type"] = window_info["control_type"]
    if window_info.get("auto_id"):
        locator["auto_id"] = window_info["auto_id"]
    if (
        window_info.get("name")
        and not window_info.get("auto_id")
        and not window_info.get("class_name")
    ):
        locator["title"] = window_info["name"]
    if window_info.get("class_name"):
        locator["class_name"] = window_info["class_name"]
    return locator


def _candidate_name(element_info, suffix=""):
    base = (
        element_info.get("auto_id")
        or element_info.get("name")
        or element_info.get("class_name")
        or element_info.get("control_type")
        or "element"
    )
    name = _snake_name(base, fallback="element")
    return f"{name}_{suffix}" if suffix else name


def _locator_candidates(
    element_info,
    root_name,
    point,
    xpath=None,
    structural_xpaths=(),
):
    candidates = []
    control_type = element_info.get("control_type")
    auto_id = element_info.get("auto_id")
    name = element_info.get("name")
    class_name = element_info.get("class_name")

    if auto_id and control_type:
        candidates.append({
            "score": 100,
            "reason": "auto_id + control_type",
            "name": _candidate_name(element_info),
            "locator": {
                "root": root_name,
                "control_type": control_type,
                "auto_id": auto_id,
            },
        })

    if name and control_type:
        candidates.append({
            "score": 80,
            "reason": "name + control_type",
            "name": _candidate_name(element_info, "by_name"),
            "locator": {
                "root": root_name,
                "control_type": control_type,
                "name": name,
            },
        })

    if class_name:
        locator = {
            "root": root_name,
            "class_name": class_name,
        }
        if control_type:
            locator["control_type"] = control_type
        candidates.append({
            "score": 60 if control_type else 45,
            "reason": "class_name + control_type" if control_type else "class_name",
            "name": _candidate_name(element_info, "by_class"),
            "locator": locator,
        })

    candidates.extend(_structural_locator_candidates(
        element_info,
        root_name,
        structural_xpaths,
    ))

    if xpath and xpath != "//*":
        candidates.append({
            "score": 50,
            "reason": "generated XPath; validate uniqueness before use",
            "name": _candidate_name(element_info, "xpath"),
            "locator": {
                "root": root_name,
                "by": "xpath",
                "value": xpath,
            },
        })

    if name and element_info.get("visible") is True:
        candidates.append({
            "score": 30,
            "reason": "visible text OCR fallback; verify the region before use",
            "name": _candidate_name(element_info, "ocr"),
            "locator": {
                "by": "ocr",
                "value": name,
                "region": root_name,
            },
            "validation": {
                "status": "unverified",
                "count": None,
                "target_matches": None,
            },
        })

    screen_width = int(win32api.GetSystemMetrics(0))
    screen_height = int(win32api.GetSystemMetrics(1))
    candidates.append({
        "score": 10,
        "reason": "coordinate fallback",
        "name": _candidate_name(element_info, "pos"),
        "locator": {
            "by": "pos",
            "coords": [int(point[0]), int(point[1]), screen_width, screen_height],
        },
    })
    return candidates


def _structural_locator_candidates(
        element_info,
        root_name,
        structural_xpaths,
    ):
    return [{
        "score": structural["score"],
        "reason": structural["reason"],
        "name": _candidate_name(
            element_info,
            structural["suffix"],
        ),
        "locator": {
            "root": root_name,
            "by": "xpath",
            "value": structural["value"],
        },
    } for structural in structural_xpaths]


def _suggested_action(event_type, candidate_name):
    strict_candidate = f"${candidate_name}"
    if event_type == "observation":
        return {
            "python": None,
            "flow": {"action": "observe", "target": candidate_name},
        }
    if event_type == "keyboard":
        return {
            "python": None,
            "flow": {"action": "keyboard", "target": candidate_name},
        }
    if event_type == "right_click":
        return {
            "python": f'self.right_click("{strict_candidate}")',
            "flow": {"action": "right_click", "target": candidate_name},
        }
    if event_type in ("middle_click", "scroll"):
        return {
            "python": None,
            "flow": {"action": event_type, "target": candidate_name},
        }
    return {
        "python": f'self.click("{strict_candidate}")',
        "flow": {"action": "click", "target": candidate_name},
    }


def _uia_wrapper_from_point(x, y):
    from comtypes.gen.UIAutomationClient import tagPOINT
    from pywinauto.controls.uiawrapper import UIAWrapper
    from pywinauto.uia_defines import IUIA
    from pywinauto.uia_element_info import UIAElementInfo

    raw_element = IUIA().iuia.ElementFromPoint(tagPOINT(int(x), int(y)))
    return UIAWrapper(UIAElementInfo(raw_element))


def _win32_wrapper_from_point(x, y):
    import win32gui
    from pywinauto.controls.hwndwrapper import HwndWrapper

    handle = win32gui.WindowFromPoint((int(x), int(y)))
    return HwndWrapper(handle)


def _wrapper_from_point(x, y, backend):
    if backend == "win32":
        return _win32_wrapper_from_point(x, y)
    return _uia_wrapper_from_point(x, y)


def _top_level_window(element, backend, limit=32):
    if backend != "uia":
        return _safe_call(element.top_level_parent)

    desktop_handle = _safe_call(win32gui.GetDesktopWindow)
    current = element
    seen = set()
    for _ in range(limit):
        current_info = _element_info(current)
        current_key = (
            current_info.get("handle"),
            tuple(current_info.get("runtime_id") or ()),
            current_info.get("control_type"),
            current_info.get("name"),
            id(current),
        )
        if current_key in seen:
            return current
        seen.add(current_key)
        parent = safe_parent(current)
        if parent is None:
            return current
        parent_info = _element_info(parent)
        if _is_uia_desktop(parent_info, desktop_handle):
            return current
        current = parent
    return current


def _is_uia_desktop(element_info, desktop_handle):
    handle = element_info.get("handle")
    if desktop_handle and handle:
        try:
            if int(handle) == int(desktop_handle):
                return True
        except (TypeError, ValueError):
            pass
    return str(element_info.get("class_name") or "").casefold() == "#32769"


def _focused_wrapper(backend):
    if backend == "uia":
        from pywinauto.controls.uiawrapper import UIAWrapper
        from pywinauto.uia_defines import IUIA
        from pywinauto.uia_element_info import UIAElementInfo

        raw_element = IUIA().iuia.GetFocusedElement()
        return UIAWrapper(UIAElementInfo(raw_element))

    handle = win32gui.GetForegroundWindow()
    window = Desktop(backend="win32").window(handle=handle).wrapper_object()
    return _safe_call(window.get_focus, window)


def _ancestor_path(element, limit=12):
    ancestors = []
    current = element
    seen = set()
    for _ in range(limit):
        current = safe_parent(current)
        if current is None:
            break
        info = _element_info(current)
        key = (
            info.get("handle"),
            tuple(info.get("runtime_id") or ()),
            info.get("control_type"),
            info.get("name"),
        )
        if key in seen:
            break
        seen.add(key)
        ancestors.append(info)
    ancestors.reverse()
    return ancestors


def _point_in_element(point, rectangle):
    if not rectangle:
        return None
    left, top, right, bottom = rectangle
    width = right - left
    height = bottom - top
    if width <= 0 or height <= 0:
        return None
    x, y = point
    return {
        "offset": [int(x - left), int(y - top)],
        "ratio": [round((x - left) / width, 4), round((y - top) / height, 4)],
    }


def _structural_role(element_info):
    control_type = str(element_info.get("control_type") or "").casefold()
    class_name = str(element_info.get("class_name") or "").casefold()
    if not control_type and not class_name:
        return "unresolved"
    if control_type in {"window", "pane", "group", "custom", "document"}:
        return "container_like"
    if class_name in {"desktop", "shell_traywnd", "progman", "workerw"}:
        return "container_like"
    return "leaf_like"


def _target_quality(element_info, preferred_candidate=None):
    validation = (preferred_candidate or {}).get("validation") or {}
    if (
        validation.get("status") == "unique"
        and validation.get("target_matches") is True
    ):
        return "exact"
    structural_role = _structural_role(element_info)
    if structural_role == "unresolved":
        return "unresolved"
    if structural_role == "container_like":
        return "container"
    return "exact"


def _capture_element_properties(element, backend):
    return get_all_element_properties(
        element,
        excluded_wrapper_methods=("is_active",) if backend == "uia" else (),
    )


def _local_context(element, sibling_radius=3, child_limit=30):
    target_key = _safe_element_key(element)
    parent = safe_parent(element)
    siblings = []
    if parent is not None:
        try:
            all_siblings = list(iter_tree_children(parent))
        except Exception:
            all_siblings = []
        target_index = next(
            (index for index, sibling in enumerate(all_siblings) if _safe_element_key(sibling) == target_key),
            None,
        )
        if target_index is not None:
            start = max(0, target_index - sibling_radius)
            end = min(len(all_siblings), target_index + sibling_radius + 1)
            siblings = [
                {
                    "relative_index": index - target_index,
                    **_element_info(all_siblings[index]),
                }
                for index in range(start, end)
            ]
    try:
        children = list(iter_tree_children(element))
    except Exception:
        children = []
    return {
        "parent": _element_info(parent) if parent is not None else None,
        "siblings": siblings,
        "children": [_element_info(child) for child in children[:child_limit]],
        "children_truncated": len(children) > child_limit,
    }


def _xpath_for_element(element, element_info):
    xpath = make_xpath_suggestion(element)
    if xpath != "//*":
        return xpath
    class_name = element_info.get("class_name")
    predicate = make_xpath_predicate("class_name", class_name) if class_name else None
    return f"//*[{predicate}]" if predicate else xpath


def _contextual_xpath(element_info, local_context):
    """Build the historical same-row value XPath for legacy readers."""
    target_type = str(element_info.get("control_type") or "")
    target_name = str(element_info.get("name") or "")
    parent_type = str(
        ((local_context or {}).get("parent") or {}).get("control_type") or ""
    )
    if not target_type or not target_name or not parent_type:
        return None
    anchor = next((
        item
        for item in (local_context or {}).get("siblings") or ()
        if item.get("relative_index") != 0
        and item.get("control_type")
        and item.get("value") not in (None, "")
    ), None)
    if anchor is None:
        return None
    anchor_predicate = make_xpath_predicate(
        "Value.Value",
        anchor["value"],
    )
    target_predicate = make_xpath_predicate("name", target_name)
    if not anchor_predicate or not target_predicate:
        return None
    return (
        f"//{anchor['control_type']}[{anchor_predicate}]/"
        f"parent::{parent_type}/{target_type}[{target_predicate}]"
    )


def _structural_xpath_candidates(element, element_info, window):
    values = []
    for context in _target_container_contexts(
            element,
            element_info,
            window,
            limit=MAX_TARGET_CONTAINER_DEPTH,
    ):
        container = context["container"]
        container_info = context["container_info"]
        target_path = context["target_path"]
        scopes = _ancestor_scopes(
            container,
            window,
            limit=MAX_XPATH_ANCESTORS,
        )
        try:
            siblings = list(iter_tree_children(container))
        except Exception:
            siblings = []
        branch_key = _safe_element_key(context["target_branch"])
        target_index = next(
            (
                index
                for index, sibling in enumerate(siblings)
                if _safe_element_key(sibling) == branch_key
            ),
            None,
        )
        for anchor_index, sibling in enumerate(siblings):
            if _safe_element_key(sibling) == branch_key:
                continue
            if context["depth"] == 0 and target_index is not None:
                values.extend(_sibling_axis_candidates(
                    sibling,
                    anchor_index,
                    target_index,
                    target_path,
                    scopes,
                ))
            values.extend(_anchor_xpath_candidates(
                sibling,
                container_info,
                target_path,
                scopes,
            ))
            if len(values) >= MAX_STRUCTURAL_XPATH_CANDIDATES:
                break

        for scope in scopes:
            values.append({
                "score": scope["score"] - 8,
                "reason": scope.get("reason") or "stable ancestor context",
                "suffix": "by_ancestor_chain" if scope.get("chain") else "by_ancestor",
                "value": f"{scope['value']}//{target_path}",
            })
        if len(values) >= MAX_STRUCTURAL_XPATH_CANDIDATES:
            break

    return _dedupe_structural_xpaths(values)[
        :MAX_STRUCTURAL_XPATH_CANDIDATES
    ]


def _target_container_contexts(element, element_info, window, limit):
    contexts = []
    window_key = _safe_element_key(window) if window is not None else None
    target_branch = element
    container = safe_parent(element)
    target_path = _target_xpath_step(
        element_info,
        _element_info(container) if container is not None else {},
    )
    for depth in range(limit):
        if container is None or not target_path:
            break
        if _safe_element_key(container) == window_key:
            break
        container_info = _element_info(container)
        if not container_info.get("control_type"):
            break
        contexts.append({
            "container": container,
            "container_info": container_info,
            "target_branch": target_branch,
            "target_path": target_path,
            "depth": depth,
        })
        target_branch = container
        container = safe_parent(container)
        if container is None:
            break
        branch_step = _target_xpath_step(
            _element_info(target_branch),
            _element_info(container),
        )
        if not branch_step:
            break
        target_path = f"{branch_step}/{target_path}"
    return contexts


def _target_xpath_step(info, parent_info):
    selectors = _stable_target_selectors(info)
    if (
        str(info.get("control_type") or "").casefold() == "text"
        and str(parent_info.get("control_type") or "").casefold()
        in {"dataitem", "edit"}
        and _structural_selectors(parent_info)
    ):
        selectors = [
            item for item in selectors if item[0] == "auto_id"
        ]
    return _xpath_step(info, selectors=selectors)


def _sibling_axis_candidates(
        anchor,
        anchor_index,
        target_index,
        target_path,
        scopes,
    ):
    info = _element_info(anchor)
    control_type = str(info.get("control_type") or "")
    if (
        control_type.casefold() not in LABEL_LIKE_CONTROL_TYPES
        or anchor_index == target_index
    ):
        return []
    axis = (
        "following-sibling"
        if anchor_index < target_index
        else "preceding-sibling"
    )
    values = []
    for attribute, value, selector_score in _anchor_selectors(
        info,
        depth=0,
        target_step=target_path,
    ):
        predicate = make_xpath_predicate(attribute, value)
        if not predicate:
            continue
        relative = (
            f"//{control_type}[{predicate}]/"
            f"{axis}::{target_path}[0]"
        )
        values.append({
            "score": 96 + selector_score,
            "reason": "sibling label context",
            "suffix": "by_sibling",
            "value": relative,
        })
        for scope in scopes[:2]:
            values.append({
                "score": min(99, scope["score"] + selector_score),
                "reason": "stable ancestor + sibling context",
                "suffix": "by_scoped_sibling",
                "value": f"{scope['value']}{relative}",
            })
    return values


def _anchor_xpath_candidates(anchor, parent_info, target_step, scopes):
    values = []
    for wrapper, parent_steps, depth in _anchor_nodes(
            anchor,
            str(parent_info.get("control_type") or ""),
    ):
        info = _element_info(wrapper)
        control_type = str(info.get("control_type") or "")
        if not control_type:
            continue
        selectors = _anchor_selectors(
            info,
            depth=depth,
            target_step=target_step,
        )
        for attribute, value, selector_score in selectors:
            predicate = make_xpath_predicate(attribute, value)
            if not predicate:
                continue
            anchor_step = f"{control_type}[{predicate}]"
            parent_path = "".join(
                f"/parent::{step}"
                for step in parent_steps
            )
            relative = f"//{anchor_step}{parent_path}/{target_step}"
            descendant_bonus = (
                2
                if depth > 0 and attribute in {"name", "Value.Value"}
                else 0
            )
            values.append({
                "score": 94 - depth + selector_score + descendant_bonus,
                "reason": "same-row structural context",
                "suffix": "by_row_context",
                "value": relative,
            })
            for scope in scopes[:2]:
                values.append({
                    "score": min(
                        99,
                        scope["score"]
                        - depth
                        + max(0, selector_score - 1)
                        + descendant_bonus,
                    ),
                    "reason": "stable ancestor + same-row context",
                    "suffix": "by_scoped_row_context",
                    "value": f"{scope['value']}{relative}",
                })
    return values


def _anchor_nodes(anchor, row_parent_type):
    anchor_info = _element_info(anchor)
    anchor_type = str(anchor_info.get("control_type") or "")
    if not anchor_type or not row_parent_type:
        return []
    values = [(anchor, [row_parent_type], 0)]
    queue = [(anchor, [row_parent_type], 0)]
    seen = {_safe_element_key(anchor)}
    while queue and len(values) < MAX_ANCHOR_DESCENDANTS:
        current, parent_steps, depth = queue.pop(0)
        if depth >= MAX_ANCHOR_DESCENDANT_DEPTH:
            continue
        try:
            children = list(iter_tree_children(current))
        except Exception:
            children = []
        for child in children:
            key = _safe_element_key(child)
            if key in seen:
                continue
            seen.add(key)
            child_info = _element_info(child)
            child_type = str(child_info.get("control_type") or "")
            if not child_type:
                continue
            current_type = str(
                _element_info(current).get("control_type") or ""
            )
            if not current_type:
                continue
            child_steps = [current_type, *parent_steps]
            values.append((child, child_steps, depth + 1))
            queue.append((child, child_steps, depth + 1))
            if len(values) >= MAX_ANCHOR_DESCENDANTS:
                break
    return values


def _ancestor_scopes(parent, window, limit):
    entries = []
    current = parent
    window_key = _safe_element_key(window) if window is not None else None
    seen = set()
    for depth in range(limit):
        if current is None:
            break
        key = _safe_element_key(current)
        if key in seen or key == window_key:
            break
        seen.add(key)
        info = _element_info(current)
        selectors = _stable_ancestor_selectors(info)
        selectors.sort(key=lambda item: item[0] != "auto_id")
        step = _xpath_step(info, selectors=selectors)
        if step and selectors:
            selector_score = selectors[0][2] if selectors else -1
            entries.append({
                "score": 96 + selector_score - depth,
                "step": step,
            })
        current = safe_parent(current)

    scopes = [{
        "score": entry["score"],
        "value": f"//{entry['step']}",
        "reason": "stable ancestor context",
        "chain": False,
    } for entry in entries]
    for length in range(
            2,
            min(MAX_ANCESTOR_CHAIN_DEPTH, len(entries)) + 1,
    ):
        chain = list(reversed(entries[:length]))
        scopes.append({
            "score": min(
                99,
                max(item["score"] for item in chain) + length,
            ),
            "value": "//" + "//".join(
                item["step"] for item in chain
            ),
            "reason": "stable ancestor chain",
            "chain": True,
        })
    return sorted(
        scopes,
        key=lambda item: (-int(item["score"]), item["value"]),
    )


def _xpath_step(info, prefer_auto_id=False, selectors=None):
    control_type = str(info.get("control_type") or "")
    if not control_type:
        return None
    selectors = list(
        _structural_selectors(info)
        if selectors is None
        else selectors
    )
    if prefer_auto_id:
        selectors.sort(key=lambda item: item[0] != "auto_id")
    if not selectors:
        return control_type
    attribute, value, _score = selectors[0]
    predicate = make_xpath_predicate(attribute, value)
    return f"{control_type}[{predicate}]" if predicate else control_type


def _stable_selectors(info):
    values = []
    for attribute, key, score in (
        ("auto_id", "auto_id", 3),
        ("Value.Value", "value", 2),
        ("name", "name", 1),
        ("class_name", "class_name", 0),
    ):
        value = info.get(key)
        if value not in (None, ""):
            values.append((attribute, value, score))
    return values


def _structural_selectors(info):
    return [
        item
        for item in _stable_selectors(info)
        if item[0] != "Value.Value"
    ]


def _stable_target_selectors(info):
    values = _structural_selectors(info)
    if _dynamic_row_name(info.get("name")):
        values = [item for item in values if item[0] != "name"]
    return values


def _stable_ancestor_selectors(info):
    values = _structural_selectors(info)
    name = str(info.get("name") or "").strip()
    control_type = str(info.get("control_type") or "").casefold()
    if _dynamic_row_name(name):
        values = [item for item in values if item[0] != "name"]
    if control_type in {"dataitem"}:
        values = [item for item in values if item[0] != "name"]
    return values


def _anchor_selectors(info, *, depth, target_step):
    selectors = _stable_selectors(info)
    if _dynamic_row_name(info.get("name")):
        selectors = [item for item in selectors if item[0] != "name"]
    if (
        str(info.get("control_type") or "").casefold() == "dataitem"
        and str(target_step or "").split("/")[-1].casefold().startswith(
            "dataitem"
        )
    ):
        return [
            item
            for item in selectors
            if item[0] in {"auto_id", "Value.Value"}
        ]
    return selectors


def _dynamic_row_name(value):
    return bool(re.fullmatch(
        r"row\s*\d+",
        str(value or "").strip(),
        flags=re.IGNORECASE,
    ))


def _dedupe_structural_xpaths(values):
    result = []
    seen = set()
    for value in sorted(
            values,
            key=lambda item: (-int(item.get("score", 0)), item.get("value", "")),
    ):
        xpath = value.get("value")
        if not xpath or xpath in seen:
            continue
        seen.add(xpath)
        result.append(value)
    return result


class UIAInspector:
    def __init__(self, backend="uia"):
        self.backend = backend

    def inspect_point(self, x, y, event_type="click"):
        return self._inspect(
            lambda: _wrapper_from_point(x, y, self.backend),
            point=(int(x), int(y)),
            event_type=event_type,
        )

    def inspect_point_capture(
            self,
            x,
            y,
            event_type="click",
            provider=None,
        ):
        return self._inspect_capture(
            lambda: _wrapper_from_point(x, y, self.backend),
            point=(int(x), int(y)),
            event_type=event_type,
            provider=provider,
        )

    def inspect_point_state(self, x, y):
        return self._inspect_state(
            lambda: _wrapper_from_point(x, y, self.backend),
            point=(int(x), int(y)),
        )

    def inspect_focus(self, event_type="keyboard"):
        return self._inspect(
            lambda: _focused_wrapper(self.backend),
            point=None,
            event_type=event_type,
        )

    def inspect_focus_capture(self):
        return self._inspect_capture(
            lambda: _focused_wrapper(self.backend),
            point=None,
            event_type="keyboard",
        )

    def inspect_focus_state(self):
        return self._inspect_state(
            lambda: _focused_wrapper(self.backend),
            point=None,
        )

    def _inspect_state(self, element_getter, point):
        result = {
            "backend": self.backend,
            "point": list(point) if point is not None else None,
            "window": {},
            "element": {},
            "element_properties": {},
            "inspection_mode": "state",
            "error": None,
        }
        try:
            element = element_getter()
            window = _top_level_window(element, self.backend)
            element_info = _element_info(element)
            result.update({
                "window": _element_info(window) if window is not None else {},
                "element": element_info,
                "element_properties": _state_properties(
                    element,
                    element_info,
                ),
            })
        except Exception as error:
            result["error"] = repr(error)
        return result

    def _inspect_capture(
            self,
            element_getter,
            point,
            event_type,
            provider=None,
        ):
        result = {
            "backend": self.backend,
            "point": list(point) if point is not None else None,
            "window": {},
            "element": {},
            "element_properties": {},
            "ancestors": [],
            "local_context": {},
            "pic_region_candidate": None,
            "point_in_element": None,
            "structural_role": "unresolved",
            "target_quality": "unresolved",
            "root_name": "window",
            "root_locator": {},
            "locator_candidates": [],
            "suggested_action": {},
            "inspection_mode": "capture",
            "structured_observation": None,
            "error": None,
        }
        try:
            element = element_getter()
            observation_provider = get_structured_observation_provider(
                provider
            )
            if observation_provider is None and event_type == "observation":
                observation_provider = select_structured_observation_provider(
                    element,
                    element_info=_element_info,
                )
            if observation_provider is not None:
                element = observation_provider.resolve_target(
                    element,
                    element_info=_element_info,
                )
            elif event_type == "scroll":
                element = _scroll_container_element(element)
            window = _top_level_window(element, self.backend)
            element_info = _element_info(element)
            window_info = _element_info(window) if window is not None else {}
            root_name = _suggest_root_name(window, self.backend)
            root_locator = _root_locator(window_info, self.backend)
            local_context = _local_context(element)
            fallback_point = point or _rectangle_center(
                element_info.get("rectangle")
            )
            candidates = _locator_candidates(
                element_info,
                root_name,
                fallback_point,
                xpath=_xpath_for_element(element, element_info),
                structural_xpaths=_structural_xpath_candidates(
                    element,
                    element_info,
                    window,
                ),
            )
            direct_chain = _event_direct_chain_candidate(
                element,
                element_info,
                window,
                root_name,
            )
            if direct_chain is not None:
                candidates.append(direct_chain)
                candidates.sort(
                    key=lambda item: -int(item.get("score", 0))
                )
            _defer_structured_candidates(candidates)
            if direct_chain is not None:
                direct_chain["validation"] = {
                    "status": "unique",
                    "count": 1,
                    "target_matches": True,
                    "source": "event_direct_chain",
                }
            first_structured = next(
                (
                    candidate
                    for candidate in candidates
                    if (candidate.get("locator") or {}).get(
                        "by",
                        "child",
                    ) in {"child", "xpath"}
                ),
                None,
            )
            preferred = first_structured or _preferred_candidate(candidates)
            first_name = preferred["name"] if preferred else "element"
            pic_region_candidate = _capture_pic_region_candidate(
                window,
                element,
                root_name,
            )
            result.update({
                "window": window_info,
                "element": element_info,
                "element_properties": (
                    _capture_element_properties(element, self.backend)
                    if event_type == "observation"
                    else _state_properties(element, element_info)
                ),
                "ancestors": _ancestor_path(element),
                "local_context": local_context,
                "pic_region_candidate": pic_region_candidate,
                "point_in_element": (
                    _point_in_element(point, element_info.get("rectangle"))
                    if point
                    else None
                ),
                "structural_role": _structural_role(element_info),
                "target_quality": _target_quality(element_info),
                "root_name": root_name,
                "root_locator": _root_locator(window_info, self.backend),
                "locator_candidates": candidates,
                "suggested_action": _suggested_action(
                    event_type,
                    first_name,
                ),
                "structured_observation": (
                    observation_provider.capture(
                        element,
                        context=ObservationCaptureContext(
                            target_info=element_info,
                            window_info=window_info,
                            root_name=root_name,
                            root_locator=root_locator,
                            point=point,
                        ),
                        element_info=_element_info,
                    )
                    if observation_provider is not None
                    else None
                ),
            })
        except Exception as error:
            result["error"] = repr(error)
            fallback_point = point or (0, 0)
            result["locator_candidates"] = _locator_candidates(
                {},
                "window",
                fallback_point,
            )
            result["suggested_action"] = _suggested_action(
                event_type,
                result["locator_candidates"][0]["name"],
            )
        return result


    def _inspect(self, element_getter, point, event_type):
        result = {
            "backend": self.backend,
            "point": list(point) if point is not None else None,
            "window": {},
            "element": {},
            "element_properties": {},
            "ancestors": [],
            "local_context": {},
            "pic_region_candidate": None,
            "point_in_element": None,
            "structural_role": "unresolved",
            "target_quality": "unresolved",
            "root_name": "window",
            "root_locator": {},
            "locator_candidates": [],
            "suggested_action": {},
            "error": None,
        }

        try:
            element = element_getter()
            window = _top_level_window(element, self.backend)

            element_info = _element_info(element)
            window_info = _element_info(window) if window is not None else {}
            root_name = _suggest_root_name(window, self.backend)
            root_locator = _root_locator(window_info, self.backend)
            xpath = _xpath_for_element(element, element_info)
            local_context = _local_context(element)
            fallback_point = point or _rectangle_center(element_info.get("rectangle"))
            candidates = _locator_candidates(
                element_info,
                root_name,
                fallback_point,
                xpath=xpath,
            )
            direct_ambiguous_xpaths = _validate_locator_candidates(
                window,
                element,
                candidates,
                allow_index_fallback=False,
            )
            if _preferred_structured_candidate(candidates) is None:
                candidates.extend(_structural_locator_candidates(
                    element_info,
                    root_name,
                    _structural_xpath_candidates(
                        element,
                        element_info,
                        window,
                    ),
                ))
                _validate_locator_candidates(
                    window,
                    element,
                    candidates,
                    allow_index_fallback=True,
                    prior_ambiguous_xpaths=direct_ambiguous_xpaths,
                )
            preferred = _preferred_candidate(candidates)
            pic_region_candidate = _pic_region_candidate(
                window,
                element,
                root_name,
            )
            first_name = preferred["name"] if preferred else "element"

            result.update({
                "window": window_info,
                "element": element_info,
                "element_properties": _capture_element_properties(
                    element,
                    self.backend,
                ),
                "ancestors": _ancestor_path(element),
                "local_context": local_context,
                "pic_region_candidate": pic_region_candidate,
                "point_in_element": _point_in_element(point, element_info.get("rectangle")) if point else None,
                "structural_role": _structural_role(element_info),
                "target_quality": _target_quality(element_info, preferred),
                "root_name": root_name,
                "root_locator": root_locator,
                "locator_candidates": candidates,
                "suggested_action": _suggested_action(event_type, first_name),
            })
        except Exception as exc:
            result["error"] = repr(exc)
            fallback_point = point or (0, 0)
            result["locator_candidates"] = _locator_candidates({}, "window", fallback_point)
            result["suggested_action"] = _suggested_action(event_type, result["locator_candidates"][0]["name"])

        return result


def _scroll_container_element(element, limit=6):
    current = element
    for _index in range(limit + 1):
        control_type = str(
            _element_info(current).get("control_type") or ""
        ).casefold()
        if control_type in SCROLL_CONTAINER_CONTROL_TYPES:
            return current
        parent = safe_parent(current)
        if parent is None:
            break
        current = parent
    return element


def _state_properties(element, element_info):
    result = {}
    value = element_info.get("value")
    if value is not None:
        result["Value.Value"] = value
    readonly = _safe_attr(
        _safe_attr(element, "iface_value"),
        "CurrentIsReadOnly",
    )
    if readonly is not None:
        result["Value.IsReadOnly"] = readonly
    return result


def _defer_structured_candidates(candidates):
    for candidate in candidates:
        locator = candidate.get("locator") or {}
        if locator.get("by", "child") not in {"child", "xpath"}:
            continue
        candidate["validation"] = {
            "status": "deferred",
            "count": None,
            "target_matches": None,
            "reason": "validate against complete post-capture tree",
        }


def _event_direct_chain_candidate(
        element,
        element_info,
        window,
        root_name,
):
    window_key = _safe_element_key(window) if window is not None else None
    current = element
    current_info = element_info
    steps = []
    seen = set()
    for _depth in range(MAX_EVENT_CHAIN_DEPTH):
        parent = safe_parent(current)
        if parent is None:
            return None
        parent_key = _safe_element_key(parent)
        if parent_key in seen:
            return None
        seen.add(parent_key)
        try:
            siblings = list(iter_tree_children(parent))
        except Exception:
            return None
        if len(siblings) > MAX_EVENT_CHAIN_SIBLINGS:
            return None
        step = _unique_direct_child_step(
            current,
            current_info,
            siblings,
        )
        if step is None:
            return None
        steps.append(step)
        if parent_key == window_key:
            value = "/".join(reversed(steps))
            return {
                "score": 99,
                "reason": "event direct child chain",
                "name": _candidate_name(
                    element_info,
                    "by_event_chain",
                ),
                "locator": {
                    "root": root_name,
                    "by": "xpath",
                    "value": value,
                },
            }
        current = parent
        current_info = _element_info(current)
    return None


def _unique_direct_child_step(target, info, siblings):
    target_key = _safe_element_key(target)
    control_type = str(info.get("control_type") or "")
    if not control_type:
        return None
    for attribute, value, _score in _stable_target_selectors(info):
        predicate = make_xpath_predicate(attribute, value)
        if not predicate:
            continue
        matches = [
            sibling
            for sibling in siblings
            if _direct_selector_matches(
                _element_info(sibling),
                control_type,
                attribute,
                value,
            )
        ]
        if (
            len(matches) == 1
            and _safe_element_key(matches[0]) == target_key
        ):
            return f"child::{control_type}[{predicate}]"
    return None


def _direct_selector_matches(info, control_type, attribute, value):
    if str(info.get("control_type") or "").casefold() != control_type.casefold():
        return False
    key = {
        "auto_id": "auto_id",
        "name": "name",
        "class_name": "class_name",
    }.get(attribute)
    if key is None:
        return False
    return str(info.get(key) or "") == str(value)


def _rectangle_center(rectangle):
    if not rectangle:
        return 0, 0
    left, top, right, bottom = rectangle
    return int((left + right) / 2), int((top + bottom) / 2)


def _validate_locator_candidates(
        window,
        target,
        candidates,
        *,
        allow_index_fallback=True,
        prior_ambiguous_xpaths=(),
    ):
    if window is None:
        return []
    target_key = _safe_element_key(target)
    ambiguous_xpaths = list(prior_ambiguous_xpaths)
    ordered = sorted(
        candidates,
        key=lambda item: -int(item.get("score", 0)),
    )
    stable_match = False
    for candidate in ordered:
        locator = candidate.get("locator") or {}
        prefix = locator.get("by", "child")
        existing_validation = candidate.get("validation") or {}
        if prefix in {"child", "xpath"} and existing_validation:
            if (
                existing_validation.get("status") == "unique"
                and existing_validation.get("target_matches") is True
            ):
                stable_match = True
                break
            continue
        if prefix == "ocr":
            continue
        if prefix == "pos":
            candidate["validation"] = {
                "status": "fallback",
                "count": 1,
                "target_matches": True,
            }
            continue
        try:
            if prefix == "xpath":
                matches = find_by_xpath(window, locator.get("value", ""), first_only=False)
                matches = list(matches or [])
            else:
                matched, _ = validate_child_locator(
                    window,
                    locator,
                    timeout=0,
                    wait_type="none",
                )
                matches = [matched]
            count = len(matches)
            candidate["validation"] = {
                "status": "unique" if count == 1 else "ambiguous",
                "count": count,
                "target_matches": any(_safe_element_key(match) == target_key for match in matches),
            }
            if (
                count == 1
                and candidate["validation"]["target_matches"] is True
            ):
                stable_match = True
                break
            if prefix == "xpath" and count > 1:
                target_index = next(
                    (index for index, match in enumerate(matches) if _safe_element_key(match) == target_key),
                    None,
                )
                if target_index is not None:
                    ambiguous_xpaths.append((candidate, target_index))
        except ElementAmbiguousError as error:
            matches = list(getattr(error, "elements", ()) or ())
            candidate["validation"] = {
                "status": "ambiguous",
                "count": len(matches),
                "target_matches": any(_safe_element_key(match) == target_key for match in matches),
            }
        except LookupError as error:
            candidate["validation"] = {
                "status": "not_found",
                "count": 0,
                "target_matches": False,
                "error": str(error),
            }
        except Exception as error:
            candidate["validation"] = {
                "status": "error",
                "count": None,
                "target_matches": False,
                "error": f"{type(error).__name__}: {error}",
            }
    if allow_index_fallback and not stable_match and ambiguous_xpaths:
        for candidate, target_index in sorted(
            ambiguous_xpaths,
            key=_index_fallback_rank,
        ):
            locator = candidate.get("locator") or {}
            indexed_xpath = (
                f"{locator.get('value', '')}[{target_index}]"
            )
            try:
                indexed_matches = list(find_by_xpath(
                    window,
                    indexed_xpath,
                    first_only=False,
                ) or [])
            except Exception:
                continue
            if not (
                len(indexed_matches) == 1
                and _safe_element_key(indexed_matches[0]) == target_key
            ):
                continue
            candidates.append({
                "score": max(1, int(candidate.get("score", 50)) - 1),
                "reason": "generated zero-based XPath index fallback",
                "name": f"{candidate.get('name', 'element')}_index",
                "locator": {
                    **locator,
                    "value": indexed_xpath,
                },
                "validation": {
                    "status": "unique",
                    "count": 1,
                    "target_matches": True,
                },
            })
            break
    candidates.sort(key=lambda item: -int(item.get("score", 0)))
    return ambiguous_xpaths


def _index_fallback_rank(item):
    candidate, _target_index = item
    validation = candidate.get("validation") or {}
    locator = candidate.get("locator") or {}
    xpath = str(locator.get("value") or "")
    reason = str(candidate.get("reason") or "")
    return (
        int(validation.get("count") or 10**9),
        0 if "stable ancestor" in reason else 1,
        0 if reason == "stable ancestor context" else 1,
        -int(candidate.get("score", 0)),
        len(xpath),
    )


def _safe_element_key(element):
    try:
        return make_element_key(element)
    except Exception:
        return None


def _preferred_candidate(candidates):
    for candidate in sorted(
            candidates,
            key=lambda item: -int(item.get("score", 0)),
    ):
        locator = candidate.get("locator") or {}
        validation = candidate.get("validation") or {}
        if (
            locator.get("by", "child") in {"child", "xpath"}
            and validation.get("status") == "unique"
            and validation.get("target_matches") is True
        ):
            return candidate
    for prefix in ("ocr", "pos"):
        for candidate in candidates:
            if (candidate.get("locator") or {}).get("by") == prefix:
                return candidate
    return candidates[0] if candidates else None


def _pic_region_candidate(window, target, root_name, limit=8):
    if window is None or target is None:
        return None
    window_key = _safe_element_key(window)
    target_rectangle = _element_info(target).get("rectangle")
    current = safe_parent(target)
    seen = set()
    for _ in range(limit):
        if current is None:
            break
        current_key = _safe_element_key(current)
        if current_key == window_key or current_key in seen:
            break
        seen.add(current_key)
        info = _element_info(current)
        rectangle = info.get("rectangle")
        if _strictly_contains(rectangle, target_rectangle):
            candidates = _locator_candidates(
                info,
                root_name,
                _rectangle_center(rectangle),
                xpath=_xpath_for_element(current, info),
            )
            _validate_locator_candidates(window, current, candidates)
            candidate = _preferred_structured_candidate(candidates)
            if candidate is not None:
                return {
                    "name": f"{candidate.get('name')}_pic_region",
                    "locator": candidate.get("locator") or {},
                    "rectangle": rectangle,
                    "validation": candidate.get("validation") or {},
                }
        current = safe_parent(current)
    return None


def _capture_pic_region_candidate(window, target, root_name, limit=8):
    if window is None or target is None:
        return None
    window_key = _safe_element_key(window)
    target_rectangle = _element_info(target).get("rectangle")
    current = safe_parent(target)
    seen = set()
    for _ in range(limit):
        if current is None:
            break
        current_key = _safe_element_key(current)
        if current_key == window_key or current_key in seen:
            break
        seen.add(current_key)
        info = _element_info(current)
        rectangle = info.get("rectangle")
        if _strictly_contains(rectangle, target_rectangle):
            candidate = _event_direct_chain_candidate(
                current,
                info,
                window,
                root_name,
            )
            if candidate is not None:
                validation = {
                    "status": "unique",
                    "count": 1,
                    "target_matches": True,
                    "source": "event_direct_chain",
                }
                return {
                    "name": f"{candidate.get('name')}_pic_region",
                    "locator": candidate.get("locator") or {},
                    "rectangle": rectangle,
                    "element": info,
                    "validation": validation,
                }
            candidates = _locator_candidates(
                info,
                root_name,
                _rectangle_center(rectangle),
            )
            candidate = next((
                item
                for item in sorted(
                    candidates,
                    key=lambda value: -int(value.get("score", 0)),
                )
                if (item.get("locator") or {}).get("by", "child")
                in {"child", "xpath"}
            ), None)
            if candidate is not None:
                return {
                    "name": f"{candidate.get('name')}_pic_region",
                    "locator": candidate.get("locator") or {},
                    "rectangle": rectangle,
                    "element": info,
                    "validation": {
                        "status": "deferred",
                        "count": None,
                        "target_matches": None,
                        "reason": "validate against complete post-capture tree",
                    },
                }
        current = safe_parent(current)
    return None


def _preferred_structured_candidate(candidates):
    for prefix in ("child", "xpath"):
        for candidate in candidates:
            locator = candidate.get("locator") or {}
            validation = candidate.get("validation") or {}
            if (
                locator.get("by", "child") == prefix
                and validation.get("status") == "unique"
                and validation.get("target_matches") is True
            ):
                return candidate
    return None


def _strictly_contains(outer, inner):
    if not outer or not inner or len(outer) != 4 or len(inner) != 4:
        return False
    return bool(
        outer[0] <= inner[0]
        and outer[1] <= inner[1]
        and outer[2] >= inner[2]
        and outer[3] >= inner[3]
        and (outer[2] - outer[0]) * (outer[3] - outer[1])
        > (inner[2] - inner[0]) * (inner[3] - inner[1])
    )