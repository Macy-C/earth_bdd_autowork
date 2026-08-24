from __future__ import annotations

import ctypes


VK_F7 = 0x76
VK_F9 = 0x78
VK_F10 = 0x79
VK_F11 = 0x7A
VK_SHIFT = 0x10
RECORDER_HOTKEYS = (VK_F7, VK_F9, VK_F10, VK_F11)
user32 = ctypes.windll.user32


def is_key_down(virtual_key):
    return bool(user32.GetAsyncKeyState(int(virtual_key)) & 0x8000)


def reset_hotkeys(state):
    for virtual_key in tuple(state):
        current = user32.GetAsyncKeyState(int(virtual_key))
        state[virtual_key] = bool(current & 0x8000)


def poll_hotkeys(state, bindings):
    for virtual_key, callback in bindings:
        current = user32.GetAsyncKeyState(int(virtual_key))
        pressed = bool(current & 0x8000)
        pressed_since_poll = bool(current & 0x0001)
        previous = bool(state.get(virtual_key, False))
        state[virtual_key] = pressed
        if pressed_since_poll or (pressed and not previous):
            callback()