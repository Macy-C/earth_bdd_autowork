import re
import time
import zlib
from pathlib import Path

import cv2
import mss
import numpy as np
from loguru import logger

from config.paths import Paths
from config.settings import settings
from autowork_core.utils.bus import normalize, safe_name, timestamp
from autowork_core.common.wait_coordinator import poll_value


OCR_V5_DET_MODEL_DIR = Paths.MODELS_DIR / "ppocrv5" / "PP-OCRv5_mobile_det_infer"
OCR_V5_REC_MODEL_DIR = Paths.MODELS_DIR / "ppocrv5" / "PP-OCRv5_mobile_rec_infer"
_ocr_engine = None
ocr_backend = None
_cache_key = None
_cache_data = None
_cache_time = 0


class _OcrWaitExpired(TimeoutError):
    pass

def _get_paddleocr_class():
    try:
        from paddleocr import PaddleOCR
        return PaddleOCR
    except Exception as e:
        raise RuntimeError(f"未安装 paddleocr，OCR 不可用: {e}")


def has_ppocrv5_models():
    required_files = ("inference.json", "inference.yml", "inference.pdiparams")
    return all(
        (model_dir / file_name).exists()
        for model_dir in (OCR_V5_DET_MODEL_DIR, OCR_V5_REC_MODEL_DIR)
        for file_name in required_files
    )


def get_ocr_engine():
    global _ocr_engine, ocr_backend

    if _ocr_engine is not None:
        return _ocr_engine

    if not has_ppocrv5_models():
        raise RuntimeError(f"PP-OCRv5 本地模型不完整: {OCR_V5_DET_MODEL_DIR}, {OCR_V5_REC_MODEL_DIR}")

    PaddleOCR = _get_paddleocr_class()
    _ocr_engine = PaddleOCR(
        text_detection_model_name="PP-OCRv5_mobile_det",
        text_detection_model_dir=str(OCR_V5_DET_MODEL_DIR),
        text_recognition_model_name="PP-OCRv5_mobile_rec",
        text_recognition_model_dir=str(OCR_V5_REC_MODEL_DIR),
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        text_det_unclip_ratio=settings.ocr_det_unclip_ratio,
        text_det_limit_side_len=settings.ocr_det_limit_side_len,
        text_det_limit_type=settings.ocr_det_limit_type,
        text_det_box_thresh=settings.ocr_det_box_thresh,
        text_recognition_batch_size=settings.ocr_recognition_batch_size,
        device="cpu",
    )
    ocr_backend = "paddleocr_v5"
    logger.info("OCR backend loaded: PaddleOCR PP-OCRv5 local models")
    return _ocr_engine


def clear_ocr_cache():
    global _cache_key, _cache_data, _cache_time
    _cache_key = None
    _cache_data = None
    _cache_time = 0


def _as_list(value):
    if value is None:
        return []
    if hasattr(value, "tolist"):
        return value.tolist()
    return list(value)


def _first_present(*values):
    for value in values:
        if value is not None:
            return value
    return None


def _to_number(value, default=0):
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


def _split_ocr_text_index(text, default_index=0):
    match = re.search(r"^(.*?)(?:\[(-?\d+)])?$", str(text))
    if not match:
        return str(text), default_index
    return match.group(1), _to_int(match.group(2), default_index) if match.group(2) else default_index


def normalize_ocr_criteria(criteria, default_index=0):
    if isinstance(criteria, dict):
        data = _normalized_mapping(criteria)
        text = _first_value(data, "value", "ocr", "text", "title", "name")
        if text is None:
            raise ValueError(f"ocr locator 缺少 value/ocr/text: {criteria}")

        index_value = _first_value(data, "index", "found_index", default=default_index)
        text, parsed_index = _split_ocr_text_index(text, _to_int(index_value, default_index))
        match_mode = _first_value(data, "match_mode", "matchmode", "mode")

        return {
            "text": text,
            "index": parsed_index,
            "match_mode": str(match_mode).strip().lower() if match_mode is not None else None,
            "timeout": _to_number(_first_value(data, "timeout", "wait_timeout", default=settings.ocr_timeout), settings.ocr_timeout),
            "interval": _to_number(_first_value(data, "interval", "retry_interval", default=settings.ocr_interval), settings.ocr_interval),
            "debug_on_fail": _as_bool(_first_value(data, "debug_on_fail", "debug", default=settings.ocr_debug_on_fail), settings.ocr_debug_on_fail),
            "use_cache": _as_bool(_first_value(data, "use_cache", "cache", default=settings.ocr_cache_ttl > 0), settings.ocr_cache_ttl > 0),
            "cache_ttl": _to_number(_first_value(data, "cache_ttl", "cache_ttl_seconds", default=settings.ocr_cache_ttl), settings.ocr_cache_ttl),
            "det_unclip_ratio": _to_number(
                _first_value(data, "det_unclip_ratio", "text_det_unclip_ratio", default=settings.ocr_det_unclip_ratio),
                settings.ocr_det_unclip_ratio,
            ),
            "det_limit_side_len": _to_int(
                _first_value(data, "det_limit_side_len", "text_det_limit_side_len", default=settings.ocr_det_limit_side_len),
                settings.ocr_det_limit_side_len,
            ),
            "det_limit_type": str(
                _first_value(data, "det_limit_type", "text_det_limit_type", default=settings.ocr_det_limit_type)
            ).strip().lower(),
            "det_box_thresh": _to_number(
                _first_value(data, "det_box_thresh", "text_det_box_thresh", default=settings.ocr_det_box_thresh),
                settings.ocr_det_box_thresh,
            ),
        }

    text, parsed_index = _split_ocr_text_index(criteria, default_index)
    return {
        "text": text,
        "index": parsed_index,
        "match_mode": None,
        "timeout": settings.ocr_timeout,
        "interval": settings.ocr_interval,
        "debug_on_fail": settings.ocr_debug_on_fail,
        "use_cache": settings.ocr_cache_ttl > 0,
        "cache_ttl": settings.ocr_cache_ttl,
        "det_unclip_ratio": settings.ocr_det_unclip_ratio,
        "det_limit_side_len": settings.ocr_det_limit_side_len,
        "det_limit_type": settings.ocr_det_limit_type,
        "det_box_thresh": settings.ocr_det_box_thresh,
    }


def _normalize_text(value):
    return "" if value is None else str(value).strip().lower()


def _normalize_poly(poly):
    if hasattr(poly, "tolist"):
        poly = poly.tolist()

    if not poly:
        return []

    if len(poly) == 4 and all(isinstance(item, (int, float)) for item in poly):
        left, top, right, bottom = poly
        return [[left, top], [right, top], [right, bottom], [left, bottom]]

    return poly


def _get_result_payload(result_item):
    if isinstance(result_item, dict):
        payload = result_item
    else:
        payload = getattr(result_item, "json", None)
        if callable(payload):
            payload = payload()
        if not isinstance(payload, dict):
            payload = {}

    return payload.get("res", payload)


def _normalize_paddleocr_results(results):
    data = []

    for result_item in results or []:
        payload = _get_result_payload(result_item)
        texts = _as_list(payload.get("rec_texts"))
        scores = _as_list(payload.get("rec_scores"))
        polys = _as_list(_first_present(payload.get("rec_polys"), payload.get("dt_polys")))
        boxes = _as_list(payload.get("rec_boxes"))

        for index, text in enumerate(texts):
            if text in (None, ""):
                continue

            poly = polys[index] if index < len(polys) else None
            if poly is None and index < len(boxes):
                poly = boxes[index]

            data.append({
                "text": str(text),
                "confidence": _to_number(scores[index]) if index < len(scores) else 0,
                "text_box_position": _normalize_poly(poly),
            })

    return data


def _image_fingerprint(image):
    height, width = image.shape[:2]
    step_y = max(1, height // 96)
    step_x = max(1, width // 96)
    sample = np.ascontiguousarray(image[::step_y, ::step_x])
    return image.shape, zlib.crc32(sample.tobytes())


def _make_cache_key(cache_key, image, det_unclip_ratio, det_limit_side_len, det_limit_type, det_box_thresh):
    return (
        cache_key,
        _image_fingerprint(image),
        det_unclip_ratio,
        det_limit_side_len,
        det_limit_type,
        det_box_thresh,
        settings.ocr_recognition_batch_size,
    )


def _get_cache(key, cache_ttl):
    if cache_ttl <= 0:
        return None

    global _cache_key, _cache_data, _cache_time
    if _cache_key == key and _cache_data is not None:
        age = time.monotonic() - _cache_time
        if age <= cache_ttl:
            return list(_cache_data)

    return None


def _set_cache(key, data, cache_ttl):
    if cache_ttl <= 0:
        return

    global _cache_key, _cache_data, _cache_time
    _cache_key = key
    _cache_data = list(data)
    _cache_time = time.monotonic()


def recognize_text_by_ocr(image, det_unclip_ratio=None, det_limit_side_len=None, det_limit_type=None,
                          det_box_thresh=None, cache_ttl=None, cache_key=None, use_cache=True):
    det_unclip_ratio = settings.ocr_det_unclip_ratio if det_unclip_ratio is None else det_unclip_ratio
    det_limit_side_len = settings.ocr_det_limit_side_len if det_limit_side_len is None else det_limit_side_len
    det_limit_type = settings.ocr_det_limit_type if det_limit_type is None else det_limit_type
    det_box_thresh = settings.ocr_det_box_thresh if det_box_thresh is None else det_box_thresh
    cache_ttl = settings.ocr_cache_ttl if cache_ttl is None else cache_ttl

    key = _make_cache_key(
        cache_key,
        image,
        det_unclip_ratio,
        det_limit_side_len,
        det_limit_type,
        det_box_thresh,
    ) if use_cache and cache_ttl > 0 else None
    if use_cache:
        cached = _get_cache(key, cache_ttl)
        if cached is not None:
            logger.debug("OCR cache hit | key={}", cache_key)
            return cached

    engine = get_ocr_engine()

    try:
        results = engine.predict(
            image,
            text_det_unclip_ratio=det_unclip_ratio,
            text_det_limit_side_len=det_limit_side_len,
            text_det_limit_type=det_limit_type,
            text_det_box_thresh=det_box_thresh,
        )
    except TypeError:
        results = engine.predict(image)
    data = _normalize_paddleocr_results(results)
    if key is not None:
        _set_cache(key, data, cache_ttl)
    return data


def _mss_screenshot_to_bgr(screenshot):
    return np.ascontiguousarray(np.asarray(screenshot)[:, :, :3])


def _normalize_monitor(monitor):
    if monitor is None:
        return None
    return {
        "left": _to_int(monitor.get("left"), 0),
        "top": _to_int(monitor.get("top"), 0),
        "width": _to_int(monitor.get("width"), 0),
        "height": _to_int(monitor.get("height"), 0),
    }


def _clip_monitor(monitor, screen_monitor):
    if monitor is None:
        return screen_monitor

    monitor = _normalize_monitor(monitor)
    left = max(screen_monitor["left"], monitor["left"])
    top = max(screen_monitor["top"], monitor["top"])
    right = min(screen_monitor["left"] + screen_monitor["width"], monitor["left"] + monitor["width"])
    bottom = min(screen_monitor["top"] + screen_monitor["height"], monitor["top"] + monitor["height"])
    return {
        "left": left,
        "top": top,
        "width": max(1, right - left),
        "height": max(1, bottom - top),
    }


def _get_box_points(candidate):
    points = candidate.get("text_box_position") or candidate.get("box") or []
    if hasattr(points, "tolist"):
        points = points.tolist()
    if len(points) < 4:
        return []
    return [[_to_number(point[0]), _to_number(point[1])] for point in points[:4]]


def _bounds_from_points(points):
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _candidate_with_screen_box(candidate, monitor):
    points = _get_box_points(candidate)
    if not points:
        return None

    left, top, right, bottom = _bounds_from_points(points)
    abs_points = [[point[0] + monitor["left"], point[1] + monitor["top"]] for point in points]
    abs_left, abs_top, abs_right, abs_bottom = _bounds_from_points(abs_points)

    item = dict(candidate)
    item["box"] = points
    item["bounds"] = (left, top, right, bottom)
    item["abs_box"] = abs_points
    item["abs_bounds"] = (abs_left, abs_top, abs_right, abs_bottom)
    item["center"] = ((abs_left + abs_right) / 2, (abs_top + abs_bottom) / 2)
    return item


def _sort_candidates(candidates):
    return sorted(candidates, key=lambda item: (item["abs_bounds"][1], item["abs_bounds"][0]))


def enrich_ocr_candidates(candidates, monitor):
    result = []
    for candidate in candidates or []:
        item = _candidate_with_screen_box(candidate, monitor)
        if item is not None:
            result.append(item)
    return _sort_candidates(result)


def match_ocr_candidates(candidates, text, mode="contains"):
    expected = _normalize_text(text)
    if not expected:
        return []

    exact = []
    contains = []
    for candidate in candidates or []:
        actual = _normalize_text(candidate.get("text"))
        if actual == expected:
            exact.append(candidate)
        if expected in actual:
            contains.append(candidate)

    if mode == "exact":
        return exact
    if mode == "smart":
        return exact or contains
    return contains


def select_ocr_candidate(candidates, index=0):
    if not candidates:
        return None
    try:
        return candidates[_to_int(index, 0)]
    except IndexError:
        return None


def scan_ocr(monitor=None, det_unclip_ratio=None, det_limit_side_len=None, det_limit_type=None,
             det_box_thresh=None, cache_ttl=None, use_cache=False):
    with mss.mss() as sct:
        monitor = _clip_monitor(monitor, sct.monitors[1])
        screenshot = sct.grab(monitor)
        image = _mss_screenshot_to_bgr(screenshot)

    cache_key = (monitor["left"], monitor["top"], monitor["width"], monitor["height"])
    raw_candidates = recognize_text_by_ocr(
        image,
        det_unclip_ratio=det_unclip_ratio,
        det_limit_side_len=det_limit_side_len,
        det_limit_type=det_limit_type,
        det_box_thresh=det_box_thresh,
        cache_ttl=cache_ttl,
        cache_key=cache_key,
        use_cache=use_cache,
    )
    return image, monitor, enrich_ocr_candidates(raw_candidates, monitor)


def _load_debug_font(size=16):
    try:
        from PIL import ImageFont

        font_paths = (
            Path("C:/Windows/Fonts/msyh.ttc"),
            Path("C:/Windows/Fonts/simhei.ttf"),
            Path("C:/Windows/Fonts/arial.ttf"),
        )
        for font_path in font_paths:
            if font_path.exists():
                return ImageFont.truetype(str(font_path), size=size)
    except Exception:
        pass
    return None


def _draw_debug_labels(image, labels):
    try:
        from PIL import Image, ImageDraw

        font = _load_debug_font()
        pil_image = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
        draw = ImageDraw.Draw(pil_image)
        for x, y, label, color in labels:
            color_rgb = (color[2], color[1], color[0])
            draw.text((x, max(0, y - 18)), label, fill=color_rgb, font=font)
        return cv2.cvtColor(np.asarray(pil_image), cv2.COLOR_RGB2BGR)
    except Exception:
        for x, y, label, color in labels:
            label = label.encode("ascii", errors="ignore").decode("ascii") or "ocr"
            cv2.putText(image, label, (x, max(14, y - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)
        return image


def save_ocr_debug_image(image=None, monitor=None, candidates=None, target_text=None, reason="ocr_debug"):
    if image is None or monitor is None or candidates is None:
        image, monitor, candidates = scan_ocr(monitor=monitor, use_cache=False)

    debug_image = image.copy()
    labels = []
    for index, candidate in enumerate(candidates or []):
        points = np.array(candidate.get("box") or _get_box_points(candidate), dtype=np.int32)
        if len(points) < 4:
            continue
        text = str(candidate.get("text", ""))
        confidence = _to_number(candidate.get("confidence"), 0)
        color = (0, 180, 0)
        if target_text and _normalize_text(target_text) in _normalize_text(text):
            color = (0, 0, 255)
        cv2.polylines(debug_image, [points], True, color, 2)
        left, top, _, _ = candidate.get("bounds") or _bounds_from_points(points.tolist())
        labels.append((int(left), int(top), f"{index}: {text} ({confidence:.2f})", color))

    debug_image = _draw_debug_labels(debug_image, labels)
    Paths.SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    unique_suffix = time.perf_counter_ns() % 1000000
    file_name = f"{safe_name(reason)}_{safe_name(str(target_text or 'all'))}_{timestamp()}_{unique_suffix}.png"
    file_path = Paths.SCREENSHOTS_DIR / file_name
    cv2.imwrite(str(file_path), debug_image)
    logger.error("OCR debug image saved: {}", file_path)
    return str(file_path)


def find_ocr_text(text, timeout=None, interval=None, monitor=None, index=None, match_mode=None,
                  debug_on_fail=None, use_cache=None, default_index=0, default_match_mode=None,
                  det_unclip_ratio=None, det_limit_side_len=None, det_limit_type=None,
                  det_box_thresh=None, cache_ttl=None):
    criteria = normalize_ocr_criteria(text, default_index=default_index)
    text = criteria["text"]
    timeout = _to_number(_first_present(timeout, criteria.get("timeout"), settings.ocr_timeout), settings.ocr_timeout)
    interval = _to_number(_first_present(interval, criteria.get("interval"), settings.ocr_interval), settings.ocr_interval)
    index = _to_int(_first_present(index, criteria.get("index"), default_index), default_index)
    match_mode = str(_first_present(match_mode, criteria.get("match_mode"), default_match_mode, settings.ocr_match_mode)).strip().lower()
    debug_on_fail = _as_bool(_first_present(debug_on_fail, criteria.get("debug_on_fail"), settings.ocr_debug_on_fail), settings.ocr_debug_on_fail)
    use_cache = _as_bool(_first_present(use_cache, criteria.get("use_cache"), settings.ocr_cache_ttl > 0), settings.ocr_cache_ttl > 0)
    cache_ttl = _to_number(_first_present(cache_ttl, criteria.get("cache_ttl"), settings.ocr_cache_ttl), settings.ocr_cache_ttl)
    det_unclip_ratio = _to_number(_first_present(det_unclip_ratio, criteria.get("det_unclip_ratio"), settings.ocr_det_unclip_ratio), settings.ocr_det_unclip_ratio)
    det_limit_side_len = _to_int(_first_present(det_limit_side_len, criteria.get("det_limit_side_len"), settings.ocr_det_limit_side_len), settings.ocr_det_limit_side_len)
    det_limit_type = str(_first_present(det_limit_type, criteria.get("det_limit_type"), settings.ocr_det_limit_type)).strip().lower()
    det_box_thresh = _to_number(_first_present(det_box_thresh, criteria.get("det_box_thresh"), settings.ocr_det_box_thresh), settings.ocr_det_box_thresh)

    def probe():
        image, resolved_monitor, candidates = scan_ocr(
            monitor=monitor,
            det_unclip_ratio=det_unclip_ratio,
            det_limit_side_len=det_limit_side_len,
            det_limit_type=det_limit_type,
            det_box_thresh=det_box_thresh,
            cache_ttl=cache_ttl,
            use_cache=use_cache,
        )
        matches = match_ocr_candidates(candidates, text, mode=match_mode)
        candidate = select_ocr_candidate(matches, index=index)
        return candidate, image, resolved_monitor, candidates

    try:
        result = poll_value(
            probe,
            lambda current: current[0] is not None,
            timeout=float(timeout),
            interval=interval,
            timeout_message=f"OCR 未找到文本: {text}",
            timeout_error_type=_OcrWaitExpired,
            fatal_errors=(Exception,),
            monotonic=time.monotonic,
            sleep=lambda seconds: time.sleep(float(seconds)),
        )
    except _OcrWaitExpired as wait_error:
        _, last_image, last_monitor, last_candidates = wait_error.last_value
        if debug_on_fail:
            save_ocr_debug_image(
                image=last_image,
                monitor=last_monitor,
                candidates=last_candidates,
                target_text=text,
                reason="ocr_find_failed",
            )
        return None

    return result[0]


def wait_ocr_text_present(text, timeout=None, interval=None, monitor=None, debug_on_fail=None):
    return find_ocr_text(
        text,
        timeout=timeout,
        interval=interval,
        monitor=monitor,
        default_index=0,
        default_match_mode="contains",
        debug_on_fail=debug_on_fail,
    ) is not None


def wait_ocr_text_absent(text, timeout=None, interval=None, monitor=None, debug_on_fail=None):
    criteria = normalize_ocr_criteria(text)
    text = criteria["text"]
    timeout = _to_number(_first_present(timeout, criteria.get("timeout"), settings.ocr_timeout), settings.ocr_timeout)
    interval = _to_number(_first_present(interval, criteria.get("interval"), settings.ocr_interval), settings.ocr_interval)
    match_mode = criteria.get("match_mode") or "contains"
    debug_on_fail = _as_bool(_first_present(debug_on_fail, criteria.get("debug_on_fail"), settings.ocr_debug_on_fail), settings.ocr_debug_on_fail)
    use_cache = False
    cache_ttl = _to_number(criteria.get("cache_ttl"), settings.ocr_cache_ttl)

    def probe():
        image, resolved_monitor, candidates = scan_ocr(
            monitor=monitor,
            det_unclip_ratio=criteria.get("det_unclip_ratio"),
            det_limit_side_len=criteria.get("det_limit_side_len"),
            det_limit_type=criteria.get("det_limit_type"),
            det_box_thresh=criteria.get("det_box_thresh"),
            cache_ttl=cache_ttl,
            use_cache=use_cache,
        )
        matches = match_ocr_candidates(candidates, text, mode=match_mode)
        return not matches, image, resolved_monitor, candidates

    try:
        poll_value(
            probe,
            lambda current: current[0],
            timeout=float(timeout),
            interval=interval,
            timeout_message=f"OCR 文本仍存在: {text}",
            timeout_error_type=_OcrWaitExpired,
            fatal_errors=(Exception,),
            monotonic=time.monotonic,
            sleep=lambda seconds: time.sleep(float(seconds)),
        )
    except _OcrWaitExpired as wait_error:
        _, last_image, last_monitor, last_candidates = wait_error.last_value
        if debug_on_fail:
            save_ocr_debug_image(
                image=last_image,
                monitor=last_monitor,
                candidates=last_candidates,
                target_text=text,
                reason="ocr_still_present",
            )
        return False

    return True


def warmup_ocr_engine():
    start = time.perf_counter()
    image = np.full((90, 240, 3), 255, dtype=np.uint8)
    cv2.putText(image, "File", (20, 60), cv2.FONT_HERSHEY_SIMPLEX, 1.7, (0, 0, 0), 3, cv2.LINE_AA)
    data = recognize_text_by_ocr(image, use_cache=False)
    clear_ocr_cache()
    logger.info("OCR warmup finished | cost={:.2f}s | candidates={}", time.perf_counter() - start, len(data))

