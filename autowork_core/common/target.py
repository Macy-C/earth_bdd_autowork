from dataclasses import dataclass
from typing import Any, Literal

from pywinauto.base_wrapper import BaseWrapper


TargetKind = Literal[
    "missing",
    "coords",
    "wrapper",
    "wrappers",
    "spec",
    "other",
]


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    kind: TargetKind
    value: Any
    coords: tuple[int, int] | None = None
    source: str | None = None
    candidate: dict | None = None

    @classmethod
    def from_legacy(cls, target):
        if isinstance(target, cls):
            return target
        if target is None:
            return cls("missing", target)
        if _is_plain_coords(target):
            x, y = target
            return cls("coords", target, coords=(int(x), int(y)))
        if _is_visual_coords(target):
            x, y = target[0]
            source = str(target[1]) if len(target) > 1 else None
            candidate = (
                target[2]
                if len(target) > 2 and isinstance(target[2], dict)
                else None
            )
            return cls(
                "coords",
                target,
                coords=(int(x), int(y)),
                source=source,
                candidate=candidate,
            )
        if isinstance(target, list):
            return cls("wrappers", target)
        if isinstance(target, BaseWrapper):
            return cls("wrapper", target)
        if _is_spec(target):
            return cls("spec", target)
        return cls("other", target)


def _is_plain_coords(target):
    return (
        isinstance(target, (tuple, list))
        and len(target) == 2
        and all(isinstance(item, (int, float)) for item in target)
    )


def _is_visual_coords(target):
    return (
        isinstance(target, tuple)
        and len(target) >= 1
        and isinstance(target[0], tuple)
    )


def _is_spec(target):
    return (
        hasattr(target, "exists")
        and not isinstance(target, BaseWrapper)
        and not isinstance(target, (tuple, list))
    )