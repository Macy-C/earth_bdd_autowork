from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol

from autowork_core.utils.debug_tools.common import (
    iter_tree_children,
    safe_parent,
)


MAX_COLLECTION_ITEMS = 200
COLLECTION_CONTROL_TYPES = frozenset({"list", "table", "tree", "datagrid"})
COLLECTION_CHROME_CONTROL_TYPES = frozenset({"scrollbar"})

ElementInfoReader = Callable[[object], dict]


@dataclass(frozen=True)
class ObservationCaptureContext:
    target_info: dict
    window_info: dict
    root_name: str
    point: tuple[int, int] | None
    root_locator: dict = field(default_factory=dict)


class StructuredObservationProvider(Protocol):
    name: str
    version: str
    auto_select: bool
    default_focus: str
    default_expected_source: str

    def matches(self, element, *, element_info: ElementInfoReader) -> bool:
        ...

    def resolve_target(
            self,
            element,
            *,
            element_info: ElementInfoReader,
        ):
        ...

    def capture(
            self,
            element,
            *,
            context: ObservationCaptureContext | None = None,
            element_info: ElementInfoReader,
        ) -> dict:
        ...


class UIACollectionObservationProvider:
    name = "uia_collection"
    version = "1.0"
    auto_select = False
    default_focus = "collection"
    default_expected_source = "observed_state"

    def matches(self, element, *, element_info):
        return str(
            element_info(element).get("control_type") or ""
        ).casefold() in COLLECTION_CONTROL_TYPES

    def resolve_target(self, element, *, element_info, limit=6):
        current = element
        for _index in range(limit + 1):
            control_type = str(
                element_info(current).get("control_type") or ""
            ).casefold()
            if control_type in COLLECTION_CONTROL_TYPES:
                return current
            parent = safe_parent(current)
            if parent is None:
                break
            current = parent
        return element

    def capture(self, element, *, context=None, element_info):
        try:
            children = [
                child
                for child in iter_tree_children(element)
                if str(
                    element_info(child).get("control_type") or ""
                ).casefold() not in COLLECTION_CHROME_CONTROL_TYPES
            ]
        except Exception as error:
            return {
                "provider": self.name,
                "provider_version": self.version,
                "status": "failed",
                "items": [],
                "truncated": False,
                "error": f"{type(error).__name__}: {error}",
            }
        items = []
        for index, child in enumerate(children[:MAX_COLLECTION_ITEMS]):
            info = element_info(child)
            items.append({
                "index": index,
                "name": str(info.get("name") or ""),
                "value": info.get("value"),
                "control_type": str(info.get("control_type") or ""),
                "auto_id": str(info.get("auto_id") or ""),
                "enabled": info.get("enabled"),
                "visible": info.get("visible"),
            })
        return {
            "provider": self.name,
            "provider_version": self.version,
            "status": "captured",
            "items": items,
            "item_count": len(children),
            "truncated": len(children) > len(items),
        }


class CanvasOCRObservationProvider:
    name = "canvas_ocr"
    version = "1.0"
    auto_select = True
    default_focus = "region_text"
    default_expected_source = "auto"

    def __init__(self, *, scanner=None, max_items=200):
        self.scanner = scanner or _scan_canvas_region
        self.max_items = max(1, int(max_items))

    def matches(self, element, *, element_info):
        return _is_canvas_target(element_info(element))

    def resolve_target(self, element, *, element_info, limit=6):
        current = element
        for _index in range(limit + 1):
            control_type = str(
                element_info(current).get("control_type") or ""
            ).casefold()
            if control_type == "canvas":
                return current
            parent = safe_parent(current)
            if parent is None:
                break
            current = parent
        return element

    def capture(self, element, *, context=None, element_info):
        context = context or ObservationCaptureContext(
            target_info=element_info(element),
            window_info={},
            root_name="",
            point=None,
        )
        target_info = dict(context.target_info or {})
        if not _is_canvas_target(target_info):
            return self._failure("target_not_canvas")
        rectangle = _rectangle(target_info.get("rectangle"))
        if rectangle is None:
            return self._failure("target_region_missing")
        if not context.root_name or not (context.window_info or {}).get("handle"):
            return self._failure("target_owner_missing")
        if context.point is None or not _contains(rectangle, context.point):
            return self._failure("target_point_outside_region")
        monitor = {
            "left": rectangle[0],
            "top": rectangle[1],
            "width": rectangle[2] - rectangle[0],
            "height": rectangle[3] - rectangle[1],
        }
        try:
            candidates = list(self.scanner(monitor) or ())
        except Exception as error:
            return self._failure(f"{type(error).__name__}: {error}")
        if not candidates:
            return self._failure("text_not_observed")
        if len(candidates) > self.max_items:
            return self._failure(
                "provider_output_truncated",
                truncated=True,
                item_count=len(candidates),
            )
        items = []
        for index, candidate in enumerate(candidates):
            text = str((candidate or {}).get("text") or "").strip()
            if not text:
                return self._failure("text_candidate_invalid")
            items.append({
                "index": index,
                "name": text,
                "confidence": (candidate or {}).get("confidence"),
                "rectangle": list(
                    (candidate or {}).get("screen_box")
                    or (candidate or {}).get("abs_bounds")
                    or ()
                ) or None,
            })
        return {
            "provider": self.name,
            "provider_version": self.version,
            "status": "captured",
            "items": items,
            "item_count": len(items),
            "truncated": False,
            "region": monitor,
            "target": {
                "control_type": "Canvas",
                "rectangle": rectangle,
                "window_handle": context.window_info.get("handle"),
                "root_name": context.root_name,
                "root_locator": dict(context.root_locator or {}),
            },
        }

    def _failure(self, error, *, truncated=False, item_count=0):
        return {
            "provider": self.name,
            "provider_version": self.version,
            "status": "failed",
            "items": [],
            "item_count": int(item_count),
            "truncated": bool(truncated),
            "error": str(error),
        }


def _scan_canvas_region(monitor):
    from autowork_core.common.ocr_engine import scan_ocr

    _image, _resolved_monitor, candidates = scan_ocr(
        monitor=monitor,
        use_cache=False,
    )
    return candidates


def _rectangle(value):
    try:
        left, top, right, bottom = [int(item) for item in value]
    except (TypeError, ValueError):
        return None
    if right <= left or bottom <= top:
        return None
    return [left, top, right, bottom]


def _contains(rectangle, point):
    try:
        x, y = int(point[0]), int(point[1])
    except (TypeError, ValueError, IndexError):
        return False
    left, top, right, bottom = rectangle
    return left <= x < right and top <= y < bottom


def _is_canvas_target(info):
    info = dict(info or {})
    control_type = str(info.get("control_type") or "").casefold()
    class_name = str(info.get("class_name") or "").casefold()
    return bool(
        control_type == "canvas"
        or (
            "canvas" in class_name
            and control_type in {"custom", "pane"}
        )
    )


_PROVIDERS: dict[str, StructuredObservationProvider] = {
    UIACollectionObservationProvider.name: UIACollectionObservationProvider(),
    CanvasOCRObservationProvider.name: CanvasOCRObservationProvider(),
}


def get_structured_observation_provider(name):
    name = str(name or "").strip()
    if not name:
        return None
    provider = _PROVIDERS.get(name)
    if provider is None:
        raise ValueError(f"未知结构化 Observation Provider: {name}")
    return provider


def select_structured_observation_provider(element, *, element_info):
    matches = [
        provider
        for provider in _PROVIDERS.values()
        if provider.auto_select
        and provider.matches(element, element_info=element_info)
    ]
    if len(matches) > 1:
        raise ValueError("目标匹配多个结构化 Observation Provider")
    return matches[0] if matches else None


def default_observation_intent(provider_name):
    provider = get_structured_observation_provider(provider_name)
    if provider is None:
        return {
            "focus": "auto",
            "relation": "auto",
            "expected_source": {"kind": "auto"},
        }
    return {
        "focus": provider.default_focus,
        "relation": "auto",
        "expected_source": {"kind": provider.default_expected_source},
    }