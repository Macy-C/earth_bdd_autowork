"""短暂标记 OCR、图片和坐标定位命中的屏幕位置。"""

import time
from types import SimpleNamespace

from loguru import logger

from autowork_core.utils.overlay import OverlayManager


_OVERLAY_MANAGER = None
_MARK_DURATION = 0.2
_POINT_SIZE = 18
_BORDER_THICKNESS = 4


def _get_overlay_manager():
    global _OVERLAY_MANAGER
    if _OVERLAY_MANAGER is None:
        _OVERLAY_MANAGER = OverlayManager()
    return _OVERLAY_MANAGER


def _rect(left, top, right, bottom):
    return SimpleNamespace(
        left=int(left),
        top=int(top),
        right=int(right),
        bottom=int(bottom),
    )


def _rect_from_bounds(bounds):
    if not bounds or len(bounds) < 4:
        return None
    left, top, right, bottom = bounds[:4]
    if right <= left or bottom <= top:
        return None
    return _rect(left, top, right, bottom)


def _candidate_rect(candidate):
    if not isinstance(candidate, dict):
        return None
    return _rect_from_bounds(candidate.get("abs_bounds"))


def _show_outline(manager, rect, alpha=185, thickness=_BORDER_THICKNESS):
    left = int(rect.left)
    top = int(rect.top)
    right = int(rect.right)
    bottom = int(rect.bottom)
    width = right - left
    height = bottom - top
    if width <= 0 or height <= 0:
        return False

    thickness = max(1, min(int(thickness), max(width, height)))

    manager.show_rect(_rect(left, top, right, min(bottom, top + thickness)), alpha=alpha)
    manager.show_rect(_rect(left, max(top, bottom - thickness), right, bottom), alpha=alpha)
    manager.show_rect(_rect(left, top, min(right, left + thickness), bottom), alpha=alpha)
    manager.show_rect(_rect(max(left, right - thickness), top, right, bottom), alpha=alpha)
    return True


def _show_crosshair(manager, point, alpha=210, size=_POINT_SIZE, thickness=3):
    if not point or len(point) < 2:
        return False
    x, y = [int(value) for value in point[:2]]
    half = max(2, int(size / 2))
    thickness = max(1, int(thickness))
    manager.show_rect(
        _rect(
            x - half,
            y - int(thickness / 2),
            x + half,
            y + int((thickness + 1) / 2),
        ),
        alpha=alpha,
    )
    manager.show_rect(
        _rect(
            x - int(thickness / 2),
            y - half,
            x + int((thickness + 1) / 2),
            y + half,
        ),
        alpha=alpha,
    )
    return True


def mark_visual_target(target, duration=_MARK_DURATION):
    try:
        if not isinstance(target, tuple) or not target:
            return False

        point = target[0]
        candidate = target[2] if len(target) >= 3 else None

        manager = _get_overlay_manager()
        manager.clear()

        rect = _candidate_rect(candidate)
        marked = False

        if rect is not None:
            marked = _show_outline(manager, rect) or marked
        marked = _show_crosshair(manager, point) or marked

        if not marked:
            return False

        time.sleep(max(0, float(duration)))
        manager.clear()
        return True
    except Exception as error:
        logger.debug(f"视觉命中标记失败: {error}")
        return False