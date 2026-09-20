from __future__ import annotations

from pywinauto.keyboard import CODES, parse_keys  # type: ignore


_MODIFIER_PREFIXES = {
    "alt": "%",
    "lalt": "%",
    "ralt": "%",
    "menu": "%",
    "lmenu": "%",
    "rmenu": "%",
    "control": "^",
    "ctrl": "^",
    "lcontrol": "^",
    "rcontrol": "^",
    "shift": "+",
    "lshift": "+",
    "rshift": "+",
}
_WINDOWS_MODIFIERS = {"win", "lwin", "rwin"}
_KEY_ALIASES = {
    "back": "BACK",
    "backspace": "BACKSPACE",
    "return": "ENTER",
    "escape": "ESC",
    "delete": "DELETE",
    "page up": "PGUP",
    "page down": "PGDN",
}
_NUMPAD_DIGITS = {
    f"numpad{digit}": digit
    for digit in "0123456789"
}
_SYSTEM_KEYBOARD_NOISE_KEYS = frozenset({"numlock", "f15"})
_LITERAL_ESCAPES = frozenset("+^%~(){}")


def encode_pywinauto_command(command):
    command = dict(command or {})
    if command.get("kind") != "keyboard":
        raise ValueError("send_text_keys只接受keyboard command")
    if command.get("sequence_status") != "complete":
        raise ValueError("录制键序列不完整，不能编译send_text_keys")
    encoded = []
    for event in command.get("key_events") or ():
        name = str(event.get("name") or "")
        normalized = name.casefold()
        if not name:
            raise ValueError("键盘命令缺少key name")
        if _is_system_keyboard_noise_event(event):
            continue
        if event.get("is_modifier"):
            continue
        modifiers = {
            str(item).casefold()
            for item in event.get("modifiers") or ()
        }
        if modifiers & _WINDOWS_MODIFIERS:
            raise ValueError("pywinauto SendKeys不能无损编码Windows组合键")
        unknown = modifiers - set(_MODIFIER_PREFIXES)
        if unknown:
            raise ValueError(
                "键盘命令包含未知修饰键: "
                + ", ".join(sorted(unknown))
            )
        prefixes = "".join(
            prefix
            for prefix in ("^", "%", "+")
            if prefix in {
                _MODIFIER_PREFIXES[item]
                for item in modifiers
            }
        )
        encoded.append(prefixes + _encode_key_name(name))
    if not encoded:
        raise ValueError("键盘命令没有可编码的非修饰键")
    result = "".join(encoded)
    parse_keys(result, with_spaces=True)
    return result


def _is_system_keyboard_noise_event(event):
    normalized = str((event or {}).get("name") or "").casefold()
    if normalized not in _SYSTEM_KEYBOARD_NOISE_KEYS:
        return False
    modifiers = {
        str(item).casefold()
        for item in (event or {}).get("modifiers") or ()
    }
    return not modifiers


def _encode_key_name(name):
    normalized = str(name).casefold()
    if normalized in _NUMPAD_DIGITS:
        return _NUMPAD_DIGITS[normalized]
    code = _KEY_ALIASES.get(normalized, str(name).upper())
    if code in CODES:
        return "{" + code + "}"
    if len(name) != 1 or not name.isprintable():
        raise ValueError(f"pywinauto SendKeys不支持录制键: {name}")
    if name == " ":
        return "{SPACE}"
    if name in _LITERAL_ESCAPES:
        return "{" + name + "}"
    return name


__all__ = ["encode_pywinauto_command"]