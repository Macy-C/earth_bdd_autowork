from __future__ import annotations

import json
import math
import re
from types import SimpleNamespace

from autowork_core.common.winauto_xpath import find_by_xpath
from autowork_core.utils.debug_tools.recorder.models import SCHEMA_VERSION, public_dict
from autowork_core.utils.debug_tools.recorder.identity import (
    locator_candidate_id,
    stable_digest,
)


LOCATOR_PRIORITY = ("child", "xpath", "ocr", "pos")
EXCLUDED_LOCATOR_METHODS = ("pic",)


def derive_actions(events):
    raw_events = [public_dict(event) for event in events]
    actions = []
    mouse_down = {}
    key_events = []
    key_down_events = []

    def flush_keys():
        if not key_down_events:
            key_events.clear()
            return
        first = key_events[0]
        last = key_events[-1]
        target_event = next(
            (
                event
                for event in key_down_events
                if _has_structured_target(event.get("target"))
            ),
            key_down_events[0],
        )
        actions.append({
            "type": "keyboard",
            "event_ids": [event["id"] for event in key_down_events],
            "media_event_ids": [event["id"] for event in key_events],
            "keys": [event.get("key") or {} for event in key_down_events],
            "start_ms": first.get("monotonic_ms", 0),
            "end_ms": last.get("monotonic_ms", first.get("monotonic_ms", 0)),
            "commit_event_id": last.get("id"),
            "target_event_id": target_event.get("id"),
            "target": _target_summary(target_event.get("target")),
        })
        key_events.clear()
        key_down_events.clear()

    for event in raw_events:
        event_type = event.get("event_type")
        if event_type in ("pause_start", "pause_end"):
            flush_keys()
            continue
        if event_type == "key_down":
            key_events.append(event)
            key_down_events.append(event)
            continue
        if event_type == "key_up":
            key_events.append(event)
            continue
        flush_keys()
        if event_type == "mouse_down":
            mouse_down[event.get("button")] = event
            continue
        if event_type == "mouse_up":
            down = mouse_down.pop(event.get("button"), None)
            if down is None:
                continue
            action = _mouse_action(down, event)
            if _is_double_click(actions, action):
                previous = actions[-1]
                previous["type"] = "double_click"
                previous["event_ids"].extend(action["event_ids"])
                previous.setdefault("media_event_ids", []).extend(
                    action.get("media_event_ids") or action["event_ids"]
                )
                previous["commit_event_id"] = action.get("commit_event_id")
                previous["end"] = action["end"]
                previous["end_ms"] = action["end_ms"]
                previous["duration_ms"] = action["end_ms"] - previous["start_ms"]
            else:
                actions.append(action)
            continue
        if event_type == "mouse_wheel":
            delta = event.get("wheel_delta")
            actions.append({
                "type": "scroll",
                "event_ids": [event["id"]],
                "media_event_ids": [event["id"]],
                "target_event_id": event["id"],
                "start_ms": event.get("monotonic_ms", 0),
                "end_ms": event.get("monotonic_ms", 0),
                "commit_event_id": event.get("id"),
                "point": event.get("point"),
                "wheel_delta": delta,
                "direction": "up" if delta and delta > 0 else "down" if delta and delta < 0 else None,
                "steps": max(1, abs(int(delta)) // 120) if delta else None,
                "target": _target_summary(event.get("target")),
            })
            continue
        if event_type == "observation":
            actions.append({
                "type": "observe",
                "event_ids": [event["id"]],
                "media_event_ids": [event["id"]],
                "target_event_id": event["id"],
                "start_ms": event.get("monotonic_ms", 0),
                "end_ms": event.get("monotonic_ms", 0),
                "commit_event_id": event.get("id"),
                "point": event.get("point"),
                "note": (event.get("details") or {}).get("note"),
                "target": _target_summary(event.get("target")),
            })
    flush_keys()
    return _assign_action_ids(actions)


def build_locator_bundle(events, *, tree_snapshots=()):
    events = [public_dict(event) for event in events]
    snapshot_roots = [
        (snapshot.get("window_handle"), root)
        for snapshot in tree_snapshots or ()
        for root in [_snapshot_root(snapshot)]
        if root is not None
    ]
    _validate_deferred_candidates(events, snapshot_roots)
    stability_index = _candidate_stability_index(
        events,
        tree_snapshots,
        snapshot_roots=snapshot_roots,
    )
    roots = {}
    locators = {}
    event_targets = []
    unresolved = []
    used_names = set()
    locator_names = {}

    for event in events:
        target = event.get("target") or {}
        if not target:
            continue
        if target.get("inspection_mode") == "state":
            continue
        root_name = target.get("root_name")
        root_locator = target.get("root_locator") or {}
        if root_name and root_locator:
            roots[root_name] = root_locator

        target_key = _target_observation_key(target)
        for candidate in list(target.get("locator_candidates") or []):
            locator_key = _locator_key(candidate.get("locator") or {})
            stability = stability_index.get((target_key, locator_key))
            if stability:
                candidate["stability"] = stability
        validated_candidates = _validated_locator_candidates(
            target.get("locator_candidates") or [],
        )
        candidate = select_locator_candidate(
            target.get("locator_candidates") or []
        )
        event_target = {
            "event_id": event.get("id"),
            "quality": target.get("target_quality"),
            "inspection_error": target.get("error"),
            "element": target.get("element") or {},
            "selected_candidate": candidate,
            "validated_candidates": validated_candidates,
        }
        if isinstance(target.get("pic_region_candidate"), dict):
            event_target["pic_region_candidate"] = target[
                "pic_region_candidate"
            ]
        event_targets.append(event_target)
        if candidate is not None:
            name = _unique_name(candidate.get("name") or "element", used_names)
            locator = candidate.get("locator") or {}
            if locator.get("by") != "pic":
                locator_key = json.dumps(locator, ensure_ascii=False, sort_keys=True)
                existing_name = locator_names.get(locator_key)
                if existing_name is None:
                    locators[name] = locator
                    locator_names[locator_key] = name
                    existing_name = name
                event_target["locator_name"] = existing_name
        if candidate is None or target.get("target_quality") in (None, "unresolved"):
            unresolved.append(event_target)

    return {
        "schema_version": SCHEMA_VERSION,
        "locator_priority": list(LOCATOR_PRIORITY),
        "excluded_methods": list(EXCLUDED_LOCATOR_METHODS),
        "roots": roots,
        "locators": locators,
        "event_targets": event_targets,
        "unresolved": unresolved,
    }


def _validated_locator_candidates(candidates, limit=4):
    validated = sorted((
        candidate
        for candidate in candidates
        if (candidate.get("locator") or {}).get("by", "child")
        in {"child", "xpath"}
        and (candidate.get("validation") or {}).get("status") == "unique"
        and (candidate.get("validation") or {}).get("target_matches") is True
    ), key=_locator_candidate_rank)
    result = []
    for candidate in validated[:limit]:
        locator = candidate.get("locator") or {}
        candidate_id = locator_candidate_id(
            locator,
            candidate.get("reason"),
        )
        candidate["candidate_id"] = candidate_id
        result.append({
            "candidate_id": candidate_id,
            "name": candidate.get("name"),
            "reason": candidate.get("reason"),
            "locator": dict(locator),
            "validation": dict(candidate.get("validation") or {}),
            "stability": dict(candidate.get("stability") or {}),
            "score": candidate.get("score"),
        })
    return result


def select_locator_candidate(candidates):
    candidates = [
        candidate
        for candidate in candidates
        if (candidate.get("locator") or {}).get("by") != "pic"
    ]
    structured = sorted(
        (
            candidate
            for candidate in candidates
            if (candidate.get("locator") or {}).get("by", "child")
            in {"child", "xpath"}
            and (candidate.get("validation") or {}).get("status")
            == "unique"
            and (candidate.get("validation") or {}).get("target_matches")
            is True
        ),
        key=_locator_candidate_rank,
    )
    if structured:
        return structured[0]
    for prefix in ("ocr", "pos"):
        matching = sorted(
            (
                candidate
                for candidate in candidates
                if (candidate.get("locator") or {}).get("by") == prefix
            ),
            key=_locator_candidate_rank,
        )
        if matching:
            return matching[0]
    return None


def _locator_candidate_rank(candidate):
    locator = candidate.get("locator") or {}
    stability = candidate.get("stability") or {}
    stability_rank = {
        "cross_snapshot_unique": 0,
        "repeated_unique": 1,
        "single_snapshot_unique": 2,
        "single_unique": 3,
        "unavailable": 4,
        "snapshot_unstable": 5,
    }.get(stability.get("status"), 3)
    prefix_rank = 0 if locator.get("by", "child") == "child" else 1
    positional = bool(
        locator.get("by") == "xpath"
        and str(candidate.get("reason") or "").endswith("index fallback")
    )
    return (
        stability_rank,
        prefix_rank,
        positional,
        -int(candidate.get("score") or 0),
        str(candidate.get("name") or ""),
    )


def _candidate_stability_index(
    events,
    tree_snapshots,
    *,
    snapshot_roots=None,
):
    observations = {}
    candidates_by_key = {}
    targets = {}
    window_handles = {}
    for event in events:
        target = event.get("target") or {}
        target_key = _target_observation_key(target)
        if not target_key:
            continue
        targets[target_key] = target.get("element") or {}
        window_handles[target_key] = (event.get("details") or {}).get(
            "window_handle"
        )
        for candidate in target.get("locator_candidates") or []:
            locator = candidate.get("locator") or {}
            validation = candidate.get("validation") or {}
            if (
                locator.get("by", "child") not in {"child", "xpath"}
                or validation.get("status") != "unique"
                or validation.get("target_matches") is not True
            ):
                continue
            key = (target_key, _locator_key(locator))
            observations.setdefault(key, set()).add(str(event.get("id") or ""))
            candidates_by_key.setdefault(key, locator)

    if snapshot_roots is None:
        snapshot_roots = [
            (snapshot.get("window_handle"), root)
            for snapshot in tree_snapshots or ()
            for root in [_snapshot_root(snapshot)]
            if root is not None
        ]
    target_cache = {}
    locator_cache = {}
    result = {}
    for key, locator in candidates_by_key.items():
        target_key, _locator = key
        snapshot = _snapshot_stability(
            _snapshot_roots_for_event(
                snapshot_roots,
                window_handles.get(target_key),
            ),
            targets.get(target_key) or {},
            locator,
            target_cache=target_cache,
            locator_cache=locator_cache,
        )
        observation_count = len(observations.get(key) or ())
        status = snapshot["status"]
        if status == "unavailable":
            status = (
                "repeated_unique"
                if observation_count >= 2
                else "single_unique"
            )
        result[key] = {
            "status": status,
            "observation_count": observation_count,
            "snapshot_target_count": snapshot["target_count"],
            "snapshot_unique_count": snapshot["unique_count"],
        }
    return result


def _snapshot_stability(
        snapshot_roots,
        target,
        locator,
        *,
        target_cache,
        locator_cache,
    ):
    target_count = 0
    unique_count = 0
    unstable = False
    for root in snapshot_roots:
        root_key = id(root)
        target_key = (root_key, _target_element_key(target))
        targets = target_cache.get(target_key)
        if targets is None:
            targets = [
                item
                for item in [root, *root.descendants()]
                if _snapshot_target_matches(item, target)
            ]
            target_cache[target_key] = targets
        if not targets:
            continue
        if len(targets) != 1:
            unstable = True
            continue
        target_count += 1
        locator_key = (root_key, _locator_key(locator))
        try:
            matches = locator_cache.get(locator_key)
            if matches is None:
                matches = _snapshot_locator_matches(root, locator)
                locator_cache[locator_key] = matches
        except Exception:
            unstable = True
            continue
        if (
            len(matches) == 1
            and any(matches[0] is target_item for target_item in targets)
        ):
            unique_count += 1
        else:
            unstable = True
    if unstable:
        status = "snapshot_unstable"
    elif target_count >= 2 and unique_count == target_count:
        status = "cross_snapshot_unique"
    elif target_count == 1 and unique_count == 1:
        status = "single_snapshot_unique"
    else:
        status = "unavailable"
    return {
        "status": status,
        "target_count": target_count,
        "unique_count": unique_count,
    }


def _snapshot_locator_matches(root, locator):
    if str(locator.get("by") or "").casefold() == "xpath":
        return list(find_by_xpath(
            root,
            str(locator.get("value") or ""),
            first_only=False,
        ) or [])
    criteria = {
        key: locator.get(key)
        for key in (
            "control_type",
            "auto_id",
            "name",
            "class_name",
        )
        if locator.get(key) not in (None, "")
    }
    return [
        item
        for item in root.descendants()
        if _snapshot_criteria_match(item, criteria)
    ]


def _snapshot_criteria_match(element, criteria):
    info = element.element_info
    aliases = {"auto_id": "automation_id"}
    return all(
        getattr(info, aliases.get(key, key), None) == value
        for key, value in criteria.items()
    )


def _snapshot_target_matches(element, target):
    info = element.element_info
    target_runtime = tuple(target.get("runtime_id") or ())
    actual_runtime = tuple(getattr(info, "runtime_id", None) or ())
    if target_runtime and actual_runtime:
        return target_runtime == actual_runtime
    target_auto_id = str(target.get("auto_id") or "")
    if target_auto_id:
        return bool(
            target_auto_id == str(getattr(info, "automation_id", "") or "")
            and str(target.get("control_type") or "").casefold()
            == str(getattr(info, "control_type", "") or "").casefold()
        )
    rectangle = tuple(target.get("rectangle") or ())
    return bool(
        str(target.get("control_type") or "").casefold()
        == str(getattr(info, "control_type", "") or "").casefold()
        and str(target.get("name") or "")
        == str(getattr(info, "name", "") or "")
        and (not rectangle or rectangle == tuple(element.rectangle_list))
    )


def _target_observation_key(target):
    element = target.get("element") or {}
    return _target_element_key(element, root_name=target.get("root_name"))


def _target_element_key(element, root_name=None):
    runtime_id = tuple(element.get("runtime_id") or ())
    if runtime_id:
        return "runtime", runtime_id
    return (
        "semantic",
        str(root_name or ""),
        str(element.get("control_type") or ""),
        str(element.get("auto_id") or ""),
        str(element.get("name") or ""),
        tuple(element.get("rectangle") or ()),
    )


def _locator_key(locator):
    return json.dumps(locator, ensure_ascii=False, sort_keys=True)


def _snapshot_root(snapshot):
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("error")
        or snapshot.get("truncated")
    ):
        return None
    max_depth = snapshot.get("max_depth")
    if max_depth is not None and any(
        int(item.get("depth") or 0) >= int(max_depth)
        for item in snapshot.get("nodes") or []
    ):
        return None
    nodes = {
        str(item.get("id")): item
        for item in snapshot.get("nodes") or []
        if item.get("id")
    }
    children = {}
    for node in nodes.values():
        children.setdefault(str(node.get("parent_id") or ""), []).append(
            str(node["id"])
        )
    roots = children.get("") or []
    if len(roots) != 1:
        return None
    if any(
        node.get("parent_id")
        and str(node.get("parent_id")) not in nodes
        for node in nodes.values()
    ):
        return None

    visiting = set()
    visited = set()

    def build(node_id):
        if node_id in visiting:
            raise ValueError("snapshot tree contains a cycle")
        visiting.add(node_id)
        node = nodes[node_id]
        element = _SnapshotElement(
            node,
            [build(child_id) for child_id in children.get(node_id, [])],
        )
        visiting.remove(node_id)
        visited.add(node_id)
        return element

    try:
        root = build(roots[0])
    except (KeyError, ValueError, RecursionError):
        return None
    return root if len(visited) == len(nodes) else None


class _SnapshotElement:
    def __init__(self, node, children):
        self._node = dict(node)
        self._children = list(children)
        self._descendants = None
        self.rectangle_list = list(node.get("rectangle") or ())
        self.element_info = SimpleNamespace(
            control_type=node.get("control_type") or "",
            name=node.get("name") or "",
            automation_id=node.get("auto_id") or "",
            auto_id=node.get("auto_id") or "",
            class_name=node.get("class_name") or "",
            framework_id=node.get("framework_id") or "",
            handle=node.get("handle"),
            process_id=node.get("process_id"),
            runtime_id=list(node.get("runtime_id") or ()),
            is_offscreen=not bool(node.get("visible", True)),
            is_keyboard_focusable=None,
            has_keyboard_focus=None,
            rich_text="",
        )
        self.iface_value = SimpleNamespace(
            CurrentValue=node.get("value"),
            CurrentIsReadOnly=None,
        )

    def children(self, control_type=None):
        if control_type is None:
            return list(self._children)
        return [
            item
            for item in self._children
            if item.element_info.control_type == control_type
        ]

    def descendants(self, control_type=None, **_criteria):
        if self._descendants is None:
            values = []
            queue = list(self._children)
            while queue:
                current = queue.pop(0)
                queue.extend(current._children)
                values.append(current)
            self._descendants = values
        if control_type is None:
            return list(self._descendants)
        return [
            item
            for item in self._descendants
            if item.element_info.control_type == control_type
        ]

    def window_text(self):
        return self.element_info.name

    def legacy_properties(self):
        return {}

    def is_enabled(self):
        return bool(self._node.get("enabled", True))

    def is_visible(self):
        return bool(self._node.get("visible", True))

    def rectangle(self):
        values = self.rectangle_list or [0, 0, 0, 0]
        return SimpleNamespace(
            left=values[0],
            top=values[1],
            right=values[2],
            bottom=values[3],
        )


def _mouse_action(down, up):
    start = down.get("point") or [0, 0]
    end = up.get("point") or start
    distance = math.dist(start, end)
    button = down.get("button")
    if distance >= 8:
        action_type = "drag"
    elif button == "right":
        action_type = "right_click"
    elif button == "middle":
        action_type = "middle_click"
    else:
        action_type = "click"
    return {
        "type": action_type,
        "button": button,
        "event_ids": [down["id"], up["id"]],
        "media_event_ids": [down["id"], up["id"]],
        "target_event_id": down["id"],
        "commit_event_id": up["id"],
        "start": start,
        "end": end,
        "distance": round(distance, 2),
        "start_ms": down.get("monotonic_ms", 0),
        "end_ms": up.get("monotonic_ms", 0),
        "duration_ms": up.get("monotonic_ms", 0) - down.get("monotonic_ms", 0),
        "target": _target_summary(down.get("target")),
    }


def _is_double_click(actions, current):
    if current.get("type") != "click" or not actions:
        return False
    previous = actions[-1]
    if previous.get("type") != "click" or previous.get("button") != current.get("button"):
        return False
    if current.get("start_ms", 0) - previous.get("end_ms", 0) > 500:
        return False
    return math.dist(previous.get("end") or [0, 0], current.get("start") or [0, 0]) <= 6


def _unique_name(name, used_names):
    name = str(name or "element")
    candidate = name
    index = 2
    while candidate in used_names:
        candidate = f"{name}_{index}"
        index += 1
    used_names.add(candidate)
    return candidate


def _target_summary(target):
    if not target:
        return None
    return {
        "quality": target.get("target_quality"),
        "root_name": target.get("root_name"),
        "element": target.get("element") or {},
        "suggested_action": target.get("suggested_action") or {},
    }


def _has_structured_target(target):
    return any(
        (candidate.get("locator") or {}).get("by", "child")
        in {"child", "xpath"}
        for candidate in (target or {}).get("locator_candidates") or ()
    )


def _assign_action_ids(actions):
    result = []
    seen = set()
    for ordinal, action in enumerate(actions, start=1):
        action = dict(action)
        event_ids = tuple(action.get("event_ids") or ())
        digest = stable_digest(
            "action",
            action.get("type"),
            *event_ids,
            ordinal if not event_ids else "",
            length=12,
        )
        action_id = f"action-{digest}"
        suffix = 2
        while action_id in seen:
            action_id = f"action-{digest}-{suffix}"
            suffix += 1
        seen.add(action_id)
        action["id"] = action_id
        action["ordinal"] = ordinal
        action.setdefault("role", "business")
        result.append(action)
    return result


def _validate_deferred_candidates(events, snapshot_roots):
    if not snapshot_roots:
        return
    target_cache = {}
    locator_cache = {}
    for event in events:
        target = event.get("target") or {}
        element = target.get("element") or {}
        if not element:
            continue
        event_snapshot_roots = _snapshot_roots_for_event(
            snapshot_roots,
            (event.get("details") or {}).get("window_handle"),
        )
        if not event_snapshot_roots:
            continue
        for candidate in target.get("locator_candidates") or []:
            locator = candidate.get("locator") or {}
            if locator.get("by", "child") not in {"child", "xpath"}:
                continue
            validation = candidate.get("validation") or {}
            if validation.get("status") not in {
                None,
                "deferred",
                "unverified",
            }:
                continue
            snapshot = _snapshot_stability(
                event_snapshot_roots,
                element,
                locator,
                target_cache=target_cache,
                locator_cache=locator_cache,
            )
            status = snapshot["status"]
            if status in {
                "single_snapshot_unique",
                "cross_snapshot_unique",
            }:
                candidate["validation"] = {
                    "status": "unique",
                    "count": 1,
                    "target_matches": True,
                    "source": "complete_tree_snapshot",
                    "snapshot_target_count": snapshot["target_count"],
                    "snapshot_unique_count": snapshot["unique_count"],
                }
            elif status == "snapshot_unstable":
                fallback = _stable_snapshot_index_fallback(
                    event_snapshot_roots,
                    element,
                    candidate,
                    target_cache=target_cache,
                    locator_cache=locator_cache,
                )
                if fallback is not None:
                    target.setdefault("locator_candidates", []).append(
                        fallback
                    )
                    continue
                candidate["validation"] = {
                    "status": "snapshot_unstable",
                    "count": None,
                    "target_matches": False,
                    "source": "complete_tree_snapshot",
                    "snapshot_target_count": snapshot["target_count"],
                    "snapshot_unique_count": snapshot["unique_count"],
                }
        _validate_deferred_pic_region(
            target,
            event_snapshot_roots,
            target_cache=target_cache,
            locator_cache=locator_cache,
        )


def _validate_deferred_pic_region(
        target,
        snapshot_roots,
        *,
        target_cache,
        locator_cache,
    ):
    candidate = target.get("pic_region_candidate")
    if not isinstance(candidate, dict):
        return
    validation = candidate.get("validation") or {}
    if validation.get("status") not in {None, "deferred", "unverified"}:
        return
    element = candidate.get("element") or {}
    locator = candidate.get("locator") or {}
    if not element or locator.get("by", "child") not in {"child", "xpath"}:
        return
    snapshot = _snapshot_stability(
        snapshot_roots,
        element,
        locator,
        target_cache=target_cache,
        locator_cache=locator_cache,
    )
    if snapshot["status"] in {
        "single_snapshot_unique",
        "cross_snapshot_unique",
    }:
        candidate["validation"] = {
            "status": "unique",
            "count": 1,
            "target_matches": True,
            "source": "complete_tree_snapshot",
            "snapshot_target_count": snapshot["target_count"],
            "snapshot_unique_count": snapshot["unique_count"],
        }
    else:
        candidate["validation"] = {
            "status": snapshot["status"],
            "count": None,
            "target_matches": False,
            "source": "complete_tree_snapshot",
            "snapshot_target_count": snapshot["target_count"],
            "snapshot_unique_count": snapshot["unique_count"],
        }


def _stable_snapshot_index_fallback(
        snapshot_roots,
        target,
        candidate,
        *,
        target_cache,
        locator_cache,
):
    locator = candidate.get("locator") or {}
    xpath = str(locator.get("value") or "")
    if (
        locator.get("by") != "xpath"
        or not xpath
        or len(snapshot_roots) < 2
        or re.search(r"\[\s*-?\d+\s*\]\s*$", xpath)
    ):
        return None

    indexes = []
    for root in snapshot_roots:
        root_key = id(root)
        target_key = (root_key, _target_element_key(target))
        targets = target_cache.get(target_key)
        if targets is None:
            targets = [
                item
                for item in [root, *root.descendants()]
                if _snapshot_target_matches(item, target)
            ]
            target_cache[target_key] = targets
        if len(targets) != 1:
            return None
        locator_key = (root_key, _locator_key(locator))
        try:
            matches = locator_cache.get(locator_key)
            if matches is None:
                matches = _snapshot_locator_matches(root, locator)
                locator_cache[locator_key] = matches
        except Exception:
            return None
        target_index = next(
            (
                index
                for index, match in enumerate(matches)
                if match is targets[0]
            ),
            None,
        )
        if target_index is None or len(matches) <= 1:
            return None
        indexes.append(target_index)

    if len(set(indexes)) != 1:
        return None
    target_index = indexes[0]
    indexed_locator = {
        **locator,
        "value": f"{xpath}[{target_index}]",
    }
    for root in snapshot_roots:
        targets = [
            item
            for item in [root, *root.descendants()]
            if _snapshot_target_matches(item, target)
        ]
        try:
            matches = _snapshot_locator_matches(root, indexed_locator)
        except Exception:
            return None
        if not (
            len(targets) == 1
            and len(matches) == 1
            and matches[0] is targets[0]
        ):
            return None

    return {
        "score": max(1, int(candidate.get("score") or 50) - 1),
        "reason": "generated zero-based XPath index fallback",
        "name": f"{candidate.get('name') or 'element'}_index",
        "locator": indexed_locator,
        "validation": {
            "status": "unique",
            "count": 1,
            "target_matches": True,
            "source": "complete_tree_snapshot",
            "snapshot_target_count": len(snapshot_roots),
            "snapshot_unique_count": len(snapshot_roots),
        },
    }


def _snapshot_roots_for_event(snapshot_roots, window_handle):
    handles = {
        int(handle)
        for handle, _root in snapshot_roots
        if handle not in (None, "")
    }
    if window_handle not in (None, ""):
        try:
            expected = int(window_handle)
        except (TypeError, ValueError):
            return []
        return [
            root
            for handle, root in snapshot_roots
            if handle not in (None, "") and int(handle) == expected
        ]
    if len(handles) != 1:
        return []
    expected = next(iter(handles))
    return [
        root
        for handle, root in snapshot_roots
        if handle not in (None, "") and int(handle) == expected
    ]