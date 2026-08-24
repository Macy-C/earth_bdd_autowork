from dataclasses import dataclass
from math import isfinite
from typing import Literal


WaitState = Literal["none", "exists", "visible", "enabled", "ready"]

_NONE_ALIASES = frozenset({"", "false", "no", "none", "off", "0"})
_VALID_STATES = frozenset({"exists", "visible", "enabled", "ready"})


@dataclass(frozen=True, slots=True)
class WaitPolicy:
    state: WaitState
    timeout: float
    interval: float
    required: bool

    @classmethod
    def from_legacy(
        cls,
        wait_type="ready",
        wait_timeout=5,
        *,
        interval=0.2,
        required=True,
    ):
        state = normalize_wait_state(wait_type)
        timeout = _non_negative_seconds(wait_timeout)
        if state == "none":
            timeout = 0.0
        return cls(
            state=state,
            timeout=timeout,
            interval=_non_negative_seconds(interval),
            required=bool(required),
        )


def normalize_wait_state(wait_type) -> WaitState:
    if wait_type is None:
        return "none"
    state = str(wait_type).strip().lower()
    if state in _NONE_ALIASES:
        return "none"
    if state in _VALID_STATES:
        return state
    raise ValueError(f"未知 wait_type: {wait_type}")


def _non_negative_seconds(value):
    seconds = float(value or 0)
    if not isfinite(seconds):
        raise ValueError("等待时间必须为有限数")
    return max(seconds, 0.0)
