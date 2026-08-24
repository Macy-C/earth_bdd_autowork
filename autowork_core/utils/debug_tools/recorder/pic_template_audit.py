from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image

from autowork_core.common.pic_engine import (
    match_pic_candidates_in_image,
    read_image,
)
from autowork_core.utils.debug_tools.recorder.models import SCHEMA_VERSION
from autowork_core.utils.debug_tools.recorder.writer import write_json_atomic


PIC_TEMPLATE_AUDIT_VERSION = "1.0"
PIC_TEMPLATE_DIRECTORY = "pic"


def build_pic_template_audit(
        take_dir,
        semantic_pack,
        action_media,
        *,
        output_dir,
        threshold=0.95,
    ):
    take_dir = Path(take_dir).resolve()
    output_dir = Path(output_dir).resolve()
    media_by_action = {
        str(item.get("action_id")): item
        for item in (action_media or {}).get("actions") or ()
        if item.get("action_id")
    }
    audits = []
    for fallback in semantic_pack.get("locator_fallback_candidates") or ():
        candidate = fallback.get("pic_candidate")
        if not candidate:
            continue
        audits.append(_audit_candidate(
            take_dir,
            output_dir,
            str(fallback.get("action_id") or ""),
            candidate,
            media_by_action.get(str(fallback.get("action_id"))) or {},
            threshold=float(threshold),
        ))
    value = {
        "schema_version": SCHEMA_VERSION,
        "pic_template_audit_version": PIC_TEMPLATE_AUDIT_VERSION,
        "take_id": semantic_pack.get("take_id"),
        "audits": audits,
        "summary": {
            "candidate_count": len(audits),
            "passed": sum(item.get("status") == "passed" for item in audits),
            "failed": sum(item.get("status") != "passed" for item in audits),
        },
    }
    value["audit_fingerprint"] = _hash({
        key: item
        for key, item in value.items()
        if key != "audit_fingerprint"
    })
    write_json_atomic(output_dir / "pic-template-audit.json", value)
    return value


def apply_pic_template_audit(semantic_pack, audit):
    value = json.loads(json.dumps(semantic_pack, ensure_ascii=False))
    by_candidate = {
        str(item.get("candidate_id")): item
        for item in audit.get("audits") or ()
        if item.get("candidate_id")
    }
    unresolved = list(value.get("unresolved_decisions") or [])
    for fallback in value.get("locator_fallback_candidates") or ():
        candidate = fallback.get("pic_candidate")
        if not candidate:
            continue
        result = by_candidate.get(str(candidate.get("candidate_id"))) or {}
        candidate.update({
            "audit_id": result.get("audit_id"),
            "audit_status": result.get("status") or "failed",
            "audit_errors": (
                list(result.get("errors") or [])
                if result
                else ["PIC template audit missing"]
            ),
            "template_artifact": (
                (result.get("template") or {}).get("path")
            ),
            "template_sha256": (
                (result.get("template") or {}).get("sha256")
            ),
            "region": result.get("region"),
            "cross_frame_validation": result.get("validation") or {},
        })
        if (
            candidate["audit_status"] != "passed"
            and not fallback.get("pos_candidate")
        ):
            unresolved.append({
                "code": "pic_template_audit_failed",
                "action_id": fallback.get("action_id"),
                "candidate_id": candidate.get("candidate_id"),
                "blocking": True,
                "errors": candidate["audit_errors"],
            })
    value["unresolved_decisions"] = _dedupe_unresolved(unresolved)
    value["pic_template_audit"] = {
        "version": audit.get("pic_template_audit_version"),
        "fingerprint": audit.get("audit_fingerprint"),
        "summary": audit.get("summary") or {},
    }
    value["semantic_fingerprint"] = _hash({
        key: item
        for key, item in value.items()
        if key != "semantic_fingerprint"
    })
    return value


def template_artifacts(audit):
    return {
        f"pic_template:{item['candidate_id']}": item["template"]["path"]
        for item in audit.get("audits") or ()
        if (item.get("template") or {}).get("path")
    }


def _audit_candidate(
        take_dir,
        output_dir,
        action_id,
        candidate,
        media,
        *,
        threshold,
    ):
    candidate_id = str(candidate.get("candidate_id") or "")
    audit_id = "pic-audit-" + _hash({
        "candidate_id": candidate_id,
        "action_id": action_id,
        "source_frame": candidate.get("source_frame"),
        "crop_rectangle": candidate.get("crop_rectangle"),
        "region_rectangle": candidate.get("region_rectangle"),
    })[:16]
    errors = []
    template = {}
    validations = []
    crop_rectangle = _rectangle(candidate.get("crop_rectangle"))
    region_rectangle = _rectangle(candidate.get("region_rectangle"))
    source_frame = _frame_path(take_dir, candidate.get("source_frame"), errors)
    source_monitor = _monitor(candidate.get("source_monitor"))
    if crop_rectangle is None:
        errors.append("crop_rectangle 缺失或无效")
    if region_rectangle is None:
        errors.append("授权 Region 缺失或无效")
    if source_monitor is None:
        errors.append("source frame 缺少 monitor geometry")
    template_image = None
    if not errors:
        try:
            with Image.open(source_frame) as image:
                image = image.convert("RGB")
                crop_box = _pixel_box(
                    crop_rectangle,
                    source_monitor,
                    image.size,
                )
                region_box = _pixel_box(
                    region_rectangle,
                    source_monitor,
                    image.size,
                )
                _require_contained(crop_box, region_box)
                if crop_box[2] - crop_box[0] < 8 or crop_box[3] - crop_box[1] < 8:
                    raise ValueError("PIC template 小于 8x8")
                template_path = (
                    output_dir
                    / PIC_TEMPLATE_DIRECTORY
                    / f"{candidate_id}.png"
                )
                template_path.parent.mkdir(parents=True, exist_ok=True)
                image.crop(crop_box).save(template_path, format="PNG")
            template = {
                "path": template_path.relative_to(output_dir).as_posix(),
                "sha256": _sha256_file(template_path),
                "width": crop_box[2] - crop_box[0],
                "height": crop_box[3] - crop_box[1],
            }
            template_image = read_image(template_path)
        except Exception as error:
            errors.append(f"模板裁剪失败: {type(error).__name__}: {error}")
    frames = _validation_frames(media)
    if len(frames) < 2:
        errors.append("跨帧验证至少需要 before 与一个独立 after/context frame")
    if template_image is not None and region_rectangle is not None:
        for frame in frames:
            validations.append(_validate_frame(
                take_dir,
                frame,
                template,
                template_image,
                crop_rectangle,
                region_rectangle,
                threshold,
            ))
        frame_errors = [
            error
            for item in validations
            for error in item.get("errors") or ()
        ]
        errors.extend(frame_errors)
    validation = {
        "method": "tpl",
        "threshold": threshold,
        "required_frame_count": 2,
        "validated_frame_count": len(validations),
        "cross_frame_unique_match": bool(
            len(validations) >= 2
            and all(item.get("status") == "passed" for item in validations)
        ),
        "frames": validations,
    }
    if not validation["cross_frame_unique_match"] and not errors:
        errors.append("跨帧唯一匹配未通过")
    return {
        "audit_id": audit_id,
        "candidate_id": candidate_id,
        "action_id": action_id,
        "status": "passed" if not errors else "failed",
        "source_frame": candidate.get("source_frame"),
        "crop_rectangle": crop_rectangle,
        "region": (
            {
                "left": region_rectangle[0],
                "top": region_rectangle[1],
                "width": region_rectangle[2] - region_rectangle[0],
                "height": region_rectangle[3] - region_rectangle[1],
                "source": candidate.get("region_source"),
            }
            if region_rectangle is not None
            else None
        ),
        "template": template,
        "validation": validation,
        "errors": list(dict.fromkeys(errors)),
    }


def _validate_frame(
        take_dir,
        frame,
        template,
        template_image,
        target_rectangle,
        region_rectangle,
        threshold,
    ):
    errors = []
    relative = frame.get("path")
    path = _frame_path(take_dir, relative, errors)
    monitor = _monitor(frame.get("monitor"))
    if monitor is None:
        errors.append(f"frame 缺少 monitor geometry: {relative}")
    candidates = []
    target_box = None
    if not errors:
        try:
            image = read_image(path)
            height, width = image.shape[:2]
            region_box = _pixel_box(
                region_rectangle,
                monitor,
                (width, height),
            )
            target_box = _pixel_box(
                target_rectangle,
                monitor,
                (width, height),
            )
            region_image = image[
                region_box[1]:region_box[3],
                region_box[0]:region_box[2],
            ]
            candidates = match_pic_candidates_in_image(
                {
                    "file": template.get("path"),
                    "method": "tpl",
                    "threshold": threshold,
                    "rgb": True,
                },
                region_image,
                monitor={
                    "left": region_box[0],
                    "top": region_box[1],
                    "width": region_box[2] - region_box[0],
                    "height": region_box[3] - region_box[1],
                },
                template_image=template_image,
            )
            if len(candidates) != 1:
                errors.append(
                    f"frame {relative} 的 Region 内匹配数为 {len(candidates)}，要求 1"
                )
            elif _iou(candidates[0].get("abs_bounds"), target_box) < 0.5:
                errors.append(f"frame {relative} 唯一匹配未覆盖录制目标")
        except Exception as error:
            errors.append(
                f"frame 验证失败 {relative}: {type(error).__name__}: {error}"
            )
    return {
        "path": relative,
        "stage": frame.get("stage"),
        "status": "passed" if not errors else "failed",
        "match_count": len(candidates),
        "best_confidence": (
            max((item.get("confidence") or 0) for item in candidates)
            if candidates
            else None
        ),
        "target_iou": (
            round(_iou(candidates[0].get("abs_bounds"), target_box), 4)
            if len(candidates) == 1 and target_box is not None
            else None
        ),
        "errors": errors,
    }


def _validation_frames(media):
    result = []
    seen = set()
    for key in ("before", "after_immediate", "after", "context"):
        frame = media.get(key) or {}
        path = frame.get("path")
        if not path or path in seen:
            continue
        seen.add(path)
        result.append(frame)
    return result


def _frame_path(take_dir, relative, errors):
    if not relative:
        errors.append("frame path 缺失")
        return None
    path = (take_dir / str(relative)).resolve()
    try:
        path.relative_to(take_dir)
    except ValueError:
        errors.append(f"frame path 越界: {relative}")
        return None
    if not path.is_file():
        errors.append(f"frame 不存在: {relative}")
        return None
    return path


def _rectangle(value):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        left, top, right, bottom = (int(item) for item in value)
    except (TypeError, ValueError):
        return None
    return [left, top, right, bottom] if right > left and bottom > top else None


def _monitor(value):
    if not isinstance(value, dict):
        return None
    try:
        monitor = {
            key: int(value[key])
            for key in ("left", "top", "width", "height")
        }
    except (KeyError, TypeError, ValueError):
        return None
    return monitor if monitor["width"] > 0 and monitor["height"] > 0 else None


def _pixel_box(rectangle, monitor, image_size):
    width, height = image_size
    scale_x = width / monitor["width"]
    scale_y = height / monitor["height"]
    box = (
        round((rectangle[0] - monitor["left"]) * scale_x),
        round((rectangle[1] - monitor["top"]) * scale_y),
        round((rectangle[2] - monitor["left"]) * scale_x),
        round((rectangle[3] - monitor["top"]) * scale_y),
    )
    if any((box[0] < 0, box[1] < 0, box[2] > width, box[3] > height)):
        raise ValueError(
            f"rectangle 超出 frame monitor: rectangle={rectangle}, monitor={monitor}"
        )
    if box[2] <= box[0] or box[3] <= box[1]:
        raise ValueError(f"rectangle 映射后为空: {rectangle}")
    return box


def _require_contained(inner, outer):
    if not all((
        inner[0] >= outer[0],
        inner[1] >= outer[1],
        inner[2] <= outer[2],
        inner[3] <= outer[3],
    )):
        raise ValueError("PIC template 不在授权 Region 内")


def _iou(first, second):
    if not first or not second:
        return 0.0
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[2], second[2])
    bottom = min(first[3], second[3])
    intersection = max(0, right - left) * max(0, bottom - top)
    first_area = max(0, first[2] - first[0]) * max(0, first[3] - first[1])
    second_area = max(0, second[2] - second[0]) * max(0, second[3] - second[1])
    union = first_area + second_area - intersection
    return intersection / union if union else 0.0


def _dedupe_unresolved(values):
    result = {}
    for item in values:
        key = (
            item.get("code"),
            item.get("action_id"),
            item.get("candidate_id"),
        )
        result[key] = item
    return list(result.values())


def _sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _hash(value):
    return hashlib.sha256(json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")).hexdigest()