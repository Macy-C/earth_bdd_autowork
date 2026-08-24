from __future__ import annotations

CANONICAL_ACTION_VERSION = "1.0"

_MODIFIER_NAMES = {
    "alt",
    "lalt",
    "ralt",
    "menu",
    "lmenu",
    "rmenu",
    "control",
    "ctrl",
    "lcontrol",
    "rcontrol",
    "shift",
    "lshift",
    "rshift",
}
_WINDOWS_MODIFIERS = {"win", "lwin", "rwin"}


def build_canonical_action(action, action_events, text_change=None):
    action = dict(action or {})
    action_type = str(action.get("type") or "unknown")
    command = {
        "kind": action_type,
        "parameters": dict(action.get("parameters") or {}),
    }
    if action_type == "keyboard":
        command.update(_keyboard_command(action_events))
    elif action_type == "input_text" and action.get("text") is not None:
        command["text"] = str(action["text"])

    observed_after = {
        "status": "not_observed",
        "authority": "runtime_observed",
    }
    if text_change:
        observed_after.update({
            "status": str(text_change.get("status") or "observed"),
            "text": text_change.get("after_value"),
            "source": "text_change.after_value",
        })
    return {
        "canonical_action_version": CANONICAL_ACTION_VERSION,
        "command": command,
        "observed_after": observed_after,
        "business_expectation": {
            "status": "not_declared",
            "authority": "none",
        },
    }


def _keyboard_command(action_events):
    key_events = [
        {
            "name": str((event.get("key") or {}).get("name") or ""),
            "modifiers": _event_modifiers(event),
            "is_modifier": (
                str((event.get("key") or {}).get("name") or "").casefold()
                in _MODIFIER_NAMES | _WINDOWS_MODIFIERS
            ),
        }
        for event in action_events or ()
        if event.get("event_type") == "key_down"
        and (event.get("key") or {}).get("name")
    ]
    result = {
        "key_events": key_events,
        "sequence_status": (
            "complete"
            if _key_sequence_is_complete(action_events)
            else "incomplete"
        ),
    }
    replacement = replacement_text_candidate(action_events)
    if replacement is not None:
        result["text"] = replacement["value"]
        result["text_operation"] = replacement["operation"]
        result["text_derivation"] = {
            "basis": replacement["basis"],
            "confidence": replacement["confidence"],
        }
    elif result["sequence_status"] == "complete":
        text = _literal_text(action_events)
        if text is not None:
            result["text"] = text
            result["text_operation"] = "append"
            result["text_derivation"] = {
                "basis": "literal_key_sequence",
                "confidence": 1.0,
            }
    return result


def replacement_text_candidate(events):
    if not _key_sequence_is_complete(events):
        return None
    selected_all = False
    cleared = False
    text = []
    modifier_names = _MODIFIER_NAMES | _WINDOWS_MODIFIERS
    control_names = {"control", "ctrl", "lcontrol", "rcontrol"}
    for event in events or ():
        if event.get("event_type") != "key_down":
            continue
        key = event.get("key") or {}
        name = str(key.get("name") or "")
        normalized = name.casefold()
        pressed = {
            str(item).casefold()
            for item in key.get("pressed") or ()
        }
        if normalized in modifier_names:
            continue
        controls = pressed & control_names
        forbidden_modifiers = pressed & (
            modifier_names - control_names - {
                "shift",
                "lshift",
                "rshift",
            }
        )
        if normalized == "a" and controls and not forbidden_modifiers:
            selected_all = True
            continue
        if normalized in {"back", "backspace", "delete"}:
            if not selected_all or controls or forbidden_modifiers:
                return None
            text.clear()
            cleared = True
            continue
        if controls or forbidden_modifiers or len(name) != 1 or not name.isprintable():
            return None
        if not selected_all or not cleared:
            return None
        text.append(name)
    if not selected_all or not cleared or not text:
        return None
    return {
        "value": "".join(text),
        "operation": "replace_all",
        "confidence": 1.0,
        "basis": "key_sequence",
    }


def _event_modifiers(event):
    key = event.get("key") or {}
    current = str(key.get("name") or "").casefold()
    pressed = {
        str(item).casefold()
        for item in key.get("pressed") or ()
    }
    pressed.discard(current)
    return sorted(pressed)


def _literal_text(action_events):
    characters = []
    for event in action_events or ():
        if event.get("event_type") != "key_down":
            continue
        key = event.get("key") or {}
        name = str(key.get("name") or "")
        normalized = name.casefold()
        if normalized in _MODIFIER_NAMES | _WINDOWS_MODIFIERS:
            continue
        if _event_modifiers(event) or len(name) != 1 or not name.isprintable():
            return None
        characters.append(name)
    return "".join(characters) if characters else None


def _key_sequence_is_complete(events):
    pressed = set()
    seen = False
    for event in events or ():
        event_type = event.get("event_type")
        if event_type not in {"key_down", "key_up"}:
            continue
        name = str((event.get("key") or {}).get("name") or "").casefold()
        if not name:
            return False
        seen = True
        if event_type == "key_down":
            if name in pressed:
                return False
            pressed.add(name)
        elif name not in pressed:
            return False
        else:
            pressed.remove(name)
    return seen and not pressed


__all__ = [
    "CANONICAL_ACTION_VERSION",
    "build_canonical_action",
    "replacement_text_candidate",
]