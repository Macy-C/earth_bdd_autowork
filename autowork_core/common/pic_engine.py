import time
import logging
from collections import OrderedDict
from pathlib import Path

import cv2
import mss
import numpy as np
from airtest.aircv.keypoint_matching import AKAZEMatching, BRISKMatching, KAZEMatching, ORBMatching
from airtest.aircv.keypoint_matching_contrib import BRIEFMatching, SIFTMatching, SURFMatching
from airtest.aircv.multiscale_template_matching import MultiScaleTemplateMatching, MultiScaleTemplateMatchingPre
from airtest.aircv.template_matching import TemplateMatching
from airtest.core.api import G, ST, Template, logwrap
from airtest.core.error import InvalidMatchingMethodError
from airtest.utils.transform import TargetPos
from loguru import logger

from config.paths import Paths
from config.settings import settings
from autowork_core.common.wait_coordinator import poll_value
from autowork_core.utils.bus import normalize, safe_name, timestamp

logging.getLogger("airtest").setLevel(logging.ERROR)

MATCHING_METHODS = {
    "tpl": TemplateMatching,
    "mstpl": MultiScaleTemplateMatchingPre,
    "gmstpl": MultiScaleTemplateMatching,
    "kaze": KAZEMatching,
    "brisk": BRISKMatching,
    "akaze": AKAZEMatching,
    "orb": ORBMatching,
    "sift": SIFTMatching,
    "surf": SURFMatching,
    "brief": BRIEFMatching,
}

MAX_TEMPLATE_IMAGE_CACHE_SIZE = 32
_template_image_cache = OrderedDict()


class _PicWaitExpired(TimeoutError):
    pass


class AirtestTemplate(Template):
    def __init__(self, ori_image, screen, filename="", threshold=None, target_pos=TargetPos.MID, record_pos=None,
                 resolution=(), rgb=True, scale_max=800, scale_step=0.005, method=None):
        super().__init__(filename, threshold, target_pos, record_pos, resolution, rgb, scale_max, scale_step)
        self.screen = screen
        self.ori_image = ori_image
        self.method = method

    @logwrap
    def _cv_match(self):
        image = self._resize_image(self.ori_image, self.screen, ST.RESIZE_METHOD)
        ret = None
        strategies = [self.method] if self.method else ST.CVSTRATEGY
        for method in strategies:
            func = MATCHING_METHODS.get(method)
            if func is None:
                raise InvalidMatchingMethodError(
                    "Undefined method in CVSTRATEGY: '%s', try 'kaze'/'brisk'/'akaze'/'orb'/'surf'/'sift'/'brief' instead." % method
                )

            if method in ["mstpl", "gmstpl"]:
                ret = self._try_match(
                    func,
                    self.ori_image,
                    self.screen,
                    threshold=self.threshold,
                    rgb=self.rgb,
                    record_pos=self.record_pos,
                    resolution=self.resolution,
                    scale_max=self.scale_max,
                    scale_step=self.scale_step,
                )
            else:
                ret = self._try_match(func, image, self.screen, threshold=self.threshold, rgb=self.rgb)

            if ret:
                ret["method"] = method
                break
        return ret

    @staticmethod
    def get_target_position(match_result, target_pos=TargetPos.MID):
        return TargetPos().getXY(match_result, target_pos)

    def match_result(self):
        return self._cv_match()

    def match_in(self):
        match_result = self._cv_match()
        G.LOGGING.debug("match result: %s", match_result)
        if not match_result:
            return None
        return TargetPos().getXY(match_result, self.target_pos)

def _to_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def _to_int(value, default=0):
    try:
        return int(float(value))
    except Exception:
        return default


def _as_bool(value, default=True):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("0", "false", "no", "n", "否")


def _normalized_mapping(data):
    return {normalize(str(key)): value for key, value in dict(data).items()}


def _first_value(data, *keys, default=None):
    for key in keys:
        normalized_key = normalize(str(key))
        if normalized_key in data:
            return data[normalized_key]
    return default


def read_image(path):
    data = np.fromfile(str(path), dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError(f"模板图片读取失败: {path}")
    return image


def clear_pic_template_cache():
    _template_image_cache.clear()


def resolve_pic_path(file_name):
    file_name = str(file_name).strip()
    path = Path(file_name)

    if not path.suffix:
        path = path.with_suffix(".png")

    if path.is_absolute():
        return path

    data_path = Paths.DATA_DIR / path
    if data_path.exists():
        return data_path

    return data_path


def get_template_image(file_name):
    path = resolve_pic_path(file_name)
    stat = path.stat()
    key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)

    image = _template_image_cache.get(key)
    if image is not None:
        _template_image_cache.move_to_end(key)
        return image

    image = read_image(path)
    _template_image_cache[key] = image
    _template_image_cache.move_to_end(key)

    while len(_template_image_cache) > MAX_TEMPLATE_IMAGE_CACHE_SIZE:
        _template_image_cache.popitem(last=False)

    return image


def normalize_pic_criteria(criteria):
    if isinstance(criteria, dict) and criteria.get("_pic_normalized"):
        return criteria

    if isinstance(criteria, dict):
        data = _normalized_mapping(criteria)
        file_name = _first_value(data, "file", "value", "image", "pic")
        if not file_name:
            raise ValueError(f"pic locator 缺少 file/value: {criteria}")

        has_index = any(key in data for key in ("index", "found_index"))

        return {
            "_pic_normalized": True,
            "file": file_name,
            "pos": _to_int(_first_value(data, "pos", "position", "target_pos", default=settings.pic_pos), settings.pic_pos),
            "threshold": _to_float(_first_value(data, "threshold", "confidence", default=settings.pic_threshold), settings.pic_threshold),
            "rgb": _as_bool(_first_value(data, "rgb", default=settings.pic_rgb), settings.pic_rgb),
            "method": str(_first_value(data, "method", default=settings.pic_method)).strip().lower(),
            "index": _to_int(_first_value(data, "index", "found_index", default=0), 0),
            "index_explicit": has_index,
            "timeout": _to_float(_first_value(data, "timeout", "wait_timeout", default=settings.pic_timeout), settings.pic_timeout),
            "interval": _to_float(_first_value(data, "interval", "retry_interval", default=settings.pic_interval), settings.pic_interval),
            "debug_on_fail": _as_bool(_first_value(data, "debug_on_fail", "debug", default=settings.pic_debug_on_fail), settings.pic_debug_on_fail),
            "scale_max": _to_int(_first_value(data, "scale_max", default=settings.pic_scale_max), settings.pic_scale_max),
            "scale_step": _to_float(_first_value(data, "scale_step", default=settings.pic_scale_step), settings.pic_scale_step),
        }

    text = str(criteria).strip()
    parts = [part.strip() for part in text.split(",")]
    return {
        "_pic_normalized": True,
        "file": parts[0],
        "pos": _to_int(parts[1], settings.pic_pos) if len(parts) > 1 else settings.pic_pos,
        "threshold": _to_float(parts[2], settings.pic_threshold) if len(parts) > 2 else settings.pic_threshold,
        "rgb": settings.pic_rgb,
        "method": settings.pic_method,
        "index": 0,
        "index_explicit": False,
        "timeout": settings.pic_timeout,
        "interval": settings.pic_interval,
        "debug_on_fail": settings.pic_debug_on_fail,
        "scale_max": settings.pic_scale_max,
        "scale_step": settings.pic_scale_step,
    }


def _screenshot_to_bgr(screenshot):
    return cv2.cvtColor(np.asarray(screenshot), cv2.COLOR_BGRA2BGR)


def _clip_monitor(monitor, screen_monitor):
    if monitor is None:
        return screen_monitor

    left = max(screen_monitor["left"], _to_int(monitor.get("left"), 0))
    top = max(screen_monitor["top"], _to_int(monitor.get("top"), 0))
    right = min(screen_monitor["left"] + screen_monitor["width"], left + _to_int(monitor.get("width"), 1))
    bottom = min(screen_monitor["top"] + screen_monitor["height"], top + _to_int(monitor.get("height"), 1))
    return {
        "left": left,
        "top": top,
        "width": max(1, right - left),
        "height": max(1, bottom - top),
    }


def screenshot(monitor=None):
    with mss.mss() as sct:
        monitor = _clip_monitor(monitor, sct.monitors[1])
        shot = sct.grab(monitor)
        return _screenshot_to_bgr(shot), monitor


def _get_bounds(match_result):
    rectangle = match_result.get("rectangle") or []
    if len(rectangle) >= 4:
        xs = [point[0] for point in rectangle[:4]]
        ys = [point[1] for point in rectangle[:4]]
        return min(xs), min(ys), max(xs), max(ys)

    result = match_result.get("result")
    if result:
        x, y = result
        return x, y, x, y

    return None


def _candidate_from_match(match_result, criteria, monitor, template_shape, screen_shape):
    focus_pos = tuple(AirtestTemplate.get_target_position(match_result, criteria["pos"]))
    bounds = _get_bounds(match_result)

    if bounds:
        left, top, right, bottom = bounds
    else:
        height, width = template_shape[:2]
        left = focus_pos[0] - width / 2
        top = focus_pos[1] - height / 2
        right = focus_pos[0] + width / 2
        bottom = focus_pos[1] + height / 2

    abs_bounds = (
        left + monitor["left"],
        top + monitor["top"],
        right + monitor["left"],
        bottom + monitor["top"],
    )
    center = (focus_pos[0] + monitor["left"], focus_pos[1] + monitor["top"])

    return {
        "file": str(criteria["file"]),
        "path": str(resolve_pic_path(criteria["file"])),
        "pos": center,
        "center": center,
        "bounds": (left, top, right, bottom),
        "abs_bounds": abs_bounds,
        "confidence": _to_float(match_result.get("confidence"), 0),
        "method": criteria.get("method") or match_result.get("method") or "auto",
        "threshold": criteria["threshold"],
        "target_pos": criteria["pos"],
        "screen_shape": tuple(screen_shape),
        "template_shape": tuple(template_shape),
        "raw": match_result,
    }


def _pic_match_strategies(criteria):
    method = criteria.get("method") or None
    return [method] if method else ST.CVSTRATEGY


def _make_airtest_matcher(method, criteria, image, template_image, monitor):
    func = MATCHING_METHODS.get(method)
    if func is None:
        raise InvalidMatchingMethodError(
            "Undefined method in CVSTRATEGY: '%s', try 'kaze'/'brisk'/'akaze'/'orb'/'surf'/'sift'/'brief' instead." % method
        )

    if method in ["mstpl", "gmstpl"]:
        return func(
            template_image,
            image,
            threshold=criteria["threshold"],
            rgb=criteria["rgb"],
            record_pos=None,
            resolution=(monitor["width"], monitor["height"]),
            scale_max=criteria["scale_max"],
            scale_step=criteria["scale_step"],
        )

    search_image = AirtestTemplate(ori_image=template_image, screen=image)._resize_image(template_image, image, ST.RESIZE_METHOD)
    return func(search_image, image, threshold=criteria["threshold"], rgb=criteria["rgb"])


def _sort_pic_candidates(candidates):
    return sorted(candidates, key=lambda item: (item["abs_bounds"][1], item["abs_bounds"][0], -item.get("confidence", 0)))


def select_pic_candidate(candidates, index=0):
    if not candidates:
        return None
    try:
        return candidates[_to_int(index, 0)]
    except IndexError:
        return None


def match_pic_in_image(criteria, image, monitor=None, template_image=None):
    criteria = normalize_pic_criteria(criteria)
    monitor = monitor or {"left": 0, "top": 0, "width": image.shape[1], "height": image.shape[0]}
    template_image = template_image if template_image is not None else get_template_image(criteria["file"])

    template = AirtestTemplate(
        ori_image=template_image,
        screen=image,
        resolution=(monitor["width"], monitor["height"]),
        threshold=criteria["threshold"],
        target_pos=criteria["pos"],
        rgb=criteria["rgb"],
        scale_max=criteria["scale_max"],
        scale_step=criteria["scale_step"],
        method=criteria.get("method") or None,
    )
    match_result = template.match_result()
    if not match_result:
        return None
    return _candidate_from_match(match_result, criteria, monitor, template_image.shape, image.shape)


def match_pic_candidates_in_image(criteria, image, monitor=None, template_image=None):
    criteria = normalize_pic_criteria(criteria)
    monitor = monitor or {"left": 0, "top": 0, "width": image.shape[1], "height": image.shape[0]}
    template_image = template_image if template_image is not None else get_template_image(criteria["file"])

    for method in _pic_match_strategies(criteria):
        try:
            matcher = _make_airtest_matcher(method, criteria, image, template_image, monitor)
            match_results = matcher.find_all_results()
        except NotImplementedError:
            logger.debug("Pic index 多候选不支持当前匹配方法: {}", method)
            continue

        if match_results:
            candidates = [
                _candidate_from_match(match_result, criteria, monitor, template_image.shape, image.shape)
                for match_result in match_results
            ]
            for candidate in candidates:
                candidate["method"] = method
            return _sort_pic_candidates(candidates)

    candidate = match_pic_in_image(criteria, image, monitor=monitor, template_image=template_image)
    return [candidate] if candidate else []


def scan_pic(criteria, monitor=None):
    criteria = normalize_pic_criteria(criteria)
    image, monitor = screenshot(monitor=monitor)
    if criteria.get("index_explicit"):
        return image, monitor, match_pic_candidates_in_image(criteria, image, monitor=monitor)
    candidate = match_pic_in_image(criteria, image, monitor=monitor)
    return image, monitor, [candidate] if candidate else []


def find_pic(criteria, timeout=None, interval=None, monitor=None, debug_on_fail=None):
    criteria = normalize_pic_criteria(criteria)
    timeout = criteria["timeout"] if timeout is None else timeout
    interval = criteria["interval"] if interval is None else interval
    debug_on_fail = criteria["debug_on_fail"] if debug_on_fail is None else debug_on_fail

    def probe():
        image, resolved_monitor, candidates = scan_pic(
            criteria,
            monitor=monitor,
        )
        candidate = select_pic_candidate(
            candidates,
            criteria["index"] if criteria.get("index_explicit") else 0,
        )
        return candidate, image, resolved_monitor, candidates

    try:
        result = poll_value(
            probe,
            lambda current: bool(current[0]),
            timeout=float(timeout),
            interval=interval,
            timeout_message=f"PIC 未找到图片: {criteria['file']}",
            timeout_error_type=_PicWaitExpired,
            fatal_errors=(Exception,),
            monotonic=time.monotonic,
            sleep=lambda seconds: time.sleep(float(seconds)),
        )
    except _PicWaitExpired as wait_error:
        _, last_image, last_monitor, last_candidates = wait_error.last_value
        if debug_on_fail:
            save_pic_debug_image(
                image=last_image,
                monitor=last_monitor,
                candidates=last_candidates,
                criteria=criteria,
                reason="pic_find_failed",
            )
        return None

    return result[0]


def wait_pic_present(criteria, timeout=None, interval=None, monitor=None, debug_on_fail=None):
    return find_pic(criteria, timeout=timeout, interval=interval, monitor=monitor, debug_on_fail=debug_on_fail) is not None


def wait_pic_absent(criteria, timeout=None, interval=None, monitor=None, debug_on_fail=None):
    criteria = normalize_pic_criteria(criteria)
    timeout = criteria["timeout"] if timeout is None else timeout
    interval = criteria["interval"] if interval is None else interval
    debug_on_fail = criteria["debug_on_fail"] if debug_on_fail is None else debug_on_fail

    def probe():
        image, resolved_monitor, candidates = scan_pic(
            criteria,
            monitor=monitor,
        )
        return not candidates, image, resolved_monitor, candidates

    try:
        poll_value(
            probe,
            lambda current: current[0],
            timeout=float(timeout),
            interval=interval,
            timeout_message=f"PIC 图片仍存在: {criteria['file']}",
            timeout_error_type=_PicWaitExpired,
            fatal_errors=(Exception,),
            monotonic=time.monotonic,
            sleep=lambda seconds: time.sleep(float(seconds)),
        )
    except _PicWaitExpired as wait_error:
        _, last_image, last_monitor, last_candidates = wait_error.last_value
        if debug_on_fail:
            save_pic_debug_image(
                image=last_image,
                monitor=last_monitor,
                candidates=last_candidates,
                criteria=criteria,
                reason="pic_still_present",
            )
        return False

    return True


def get_pic_region(criteria, timeout=None, monitor=None, padding=0):
    candidate = find_pic(criteria, timeout=timeout, monitor=monitor, debug_on_fail=None)
    if not candidate:
        raise LookupError(f"图片未找到，无法获取区域: {criteria}")

    left, top, right, bottom = candidate["abs_bounds"]
    padding = _to_int(padding, 0)
    return {
        "left": int(left) - padding,
        "top": int(top) - padding,
        "width": int(right - left) + padding * 2,
        "height": int(bottom - top) + padding * 2,
    }


def _draw_candidates(image, candidates=None):
    output = image.copy()
    for index, candidate in enumerate(candidates or []):
        bounds = candidate.get("bounds")
        if not bounds:
            continue
        left, top, right, bottom = [int(value) for value in bounds]
        cv2.rectangle(output, (left, top), (right, bottom), (0, 0, 255), 2)
        label = f"{index}: {candidate.get('confidence', 0):.3f}"
        cv2.putText(output, label, (left, max(16, top - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 255), 2, cv2.LINE_AA)
    return output


def save_pic_debug_image(image=None, monitor=None, candidates=None, criteria=None, reason="pic_debug"):
    if image is None or monitor is None or candidates is None:
        image, monitor, candidates = scan_pic(criteria, monitor=monitor)

    debug_image = _draw_candidates(image, candidates)
    Paths.SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    file_text = "pic"
    try:
        file_text = normalize_pic_criteria(criteria).get("file", "pic")
    except Exception:
        pass
    unique_suffix = time.perf_counter_ns() % 1000000
    file_name = f"{safe_name(reason)}_{safe_name(str(file_text))}_{timestamp()}_{unique_suffix}.png"
    file_path = Paths.SCREENSHOTS_DIR / file_name
    cv2.imwrite(str(file_path), debug_image)
    logger.error("Pic debug image saved: {}", file_path)
    return str(file_path)


