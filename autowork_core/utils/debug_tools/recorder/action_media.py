from __future__ import annotations

import json
import hashlib
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageOps, ImageStat

from autowork_core.utils.debug_tools.recorder.models import SCHEMA_VERSION, public_dict
from autowork_core.utils.debug_tools.recorder.writer import write_json_atomic


ACTION_MEDIA_VERSION = "1.0"


def build_action_media(
        take_dir,
        actions,
        events,
        metadata=None,
        *,
        output_dir=None,
        artifact_prefix=None,
    ):
    take_dir = Path(take_dir)
    output_dir = Path(output_dir or take_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    actions = [public_dict(action) for action in actions]
    events = [public_dict(event) for event in events]
    event_map = {event.get("id"): event for event in events}
    event_order = {
        event.get("id"): index
        for index, event in enumerate(events)
    }
    default_video_offset = ((metadata or {}).get("timeline") or {}).get(
        "video_to_event_offset_ms"
    )
    entries = []
    for index, action in enumerate(actions):
        source = action.get("source") or {}
        if source.get("kind") == "supplement" and source.get("path"):
            video_path = (Path(source["path"]) / "step.mp4").as_posix()
            video_offset = source.get("video_to_event_offset_ms")
        else:
            video_path = "step.mp4"
            video_offset = default_video_offset
        has_video = (take_dir / video_path).exists()
        media_ids = action.get("media_event_ids") or action.get("event_ids") or []
        action_events = [event_map[event_id] for event_id in media_ids if event_id in event_map]
        before = _select_before_frame(take_dir, action_events)
        after_immediate = _select_stage_frame(take_dir, action_events, "after_immediate")
        after_probe = _select_stage_frame(take_dir, action_events, "after_probe")
        after_settled = _select_stage_frame(take_dir, action_events, "after_settled")
        after = after_settled or after_probe or after_immediate
        if before is None:
            before = _fallback_event_frame(take_dir, action_events, prefer_first=True)
        if after is None:
            after = _fallback_event_frame(take_dir, action_events, prefer_first=False)

        next_before = None
        if index + 1 < len(actions):
            next_ids = (
                actions[index + 1].get("media_event_ids")
                or actions[index + 1].get("event_ids")
                or []
            )
            next_events = [event_map[event_id] for event_id in next_ids if event_id in event_map]
            next_before = _select_before_frame(take_dir, next_events)
        context = next_before or after
        stability = _stability(take_dir, after_probe, after_settled)
        outcome = _action_outcome(action, metadata, index == len(actions) - 1)
        commit_event_id = action.get("commit_event_id") or (
            media_ids[-1] if media_ids else None
        )
        start_ms = _first_number(
            action.get("start_ms"),
            *(event.get("monotonic_ms") for event in action_events),
        )
        end_ms = _last_number(
            action.get("end_ms"),
            *(event.get("monotonic_ms") for event in action_events),
        )
        commit_ms = (
            (event_map.get(commit_event_id) or {}).get("monotonic_ms")
            if commit_event_id
            else end_ms
        )
        entry = {
            "action_id": action.get("id"),
            "ordinal": action.get("ordinal"),
            "type": action.get("type"),
            "role": action.get("role", "business"),
            "event_ids": action.get("event_ids") or [],
            "media_event_ids": media_ids,
            "event_indexes": [
                event_order[event_id]
                for event_id in media_ids
                if event_id in event_order
            ],
            "before": _with_video(before, video_offset, has_video),
            "commit": {
                "event_id": commit_event_id,
                "event_ms": commit_ms,
                "video_ms": _video_ms(commit_ms, video_offset, has_video),
            },
            "after_immediate": _with_video(after_immediate, video_offset, has_video),
            "after": _with_video(after, video_offset, has_video),
            "context": _with_video(context, video_offset, has_video),
            "stability": stability,
            "outcome": outcome,
            "video": {
                "path": video_path,
                "offset_ms": video_offset,
            } if has_video else None,
            "start_ms": start_ms,
            "end_ms": end_ms,
        }
        entries.append(entry)

    result = {
        "schema_version": SCHEMA_VERSION,
        "action_media_version": ACTION_MEDIA_VERSION,
        "timebase": "milliseconds",
        "actions": entries,
    }
    write_json_atomic(output_dir / "action-media.json", result)
    result["contact_sheet"] = _write_action_contact_sheet(
        take_dir,
        entries,
        output_dir=output_dir,
        artifact_prefix=artifact_prefix,
    )
    write_json_atomic(output_dir / "action-media.json", result)
    return result


def load_action_media(take_dir):
    from autowork_core.utils.debug_tools.recorder.projection_store import (
        resolve_take_artifact,
    )

    path = resolve_take_artifact(
        take_dir,
        "action_media",
        "action-media.json",
    )
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _frames(event):
    return list((event.get("details") or {}).get("frames") or ())


def _select_before_frame(take_dir, events):
    for event in events:
        frame = next(
            (item for item in _frames(event) if item.get("stage") == "before"),
            None,
        )
        if frame:
            return _frame(take_dir, frame, source="boundary_capture")
    return None


def _select_stage_frame(take_dir, events, stage):
    for event in reversed(events):
        frame = next(
            (item for item in reversed(_frames(event)) if item.get("stage") == stage),
            None,
        )
        if frame:
            return _frame(take_dir, frame, source="boundary_capture")
    return None


def _fallback_event_frame(take_dir, events, prefer_first):
    ordered = events if prefer_first else list(reversed(events))
    for event in ordered:
        screenshot = event.get("screenshot")
        if screenshot and (take_dir / screenshot).exists():
            details = event.get("details") or {}
            return {
                "path": screenshot,
                **_frame_integrity(take_dir / screenshot),
                "captured_ms": details.get("screenshot_monotonic_ms"),
                "event_ms": event.get("monotonic_ms"),
                "latency_ms": details.get("screenshot_latency_ms"),
                "stage": "event_fallback",
                "source": "event_screenshot_fallback",
                "monitor": details.get("screenshot_monitor"),
            }
    return None


def _frame(take_dir, frame, source):
    path = frame.get("path")
    if not path or not (take_dir / path).exists():
        return None
    return {
        "path": path,
        **_frame_integrity(take_dir / path),
        "captured_ms": frame.get("captured_ms"),
        "event_ms": frame.get("event_ms"),
        "latency_ms": frame.get("latency_ms"),
        "stage": frame.get("stage"),
        "source": source,
        "monitor": frame.get("monitor"),
    }


def _frame_integrity(path):
    try:
        content = Path(path).read_bytes()
    except OSError:
        return {}
    return {
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
    }


def _with_video(frame, offset, has_video):
    if frame is None:
        return None
    return {
        **frame,
        "video_ms": _video_ms(frame.get("captured_ms"), offset, has_video),
    }


def _video_ms(value, offset, has_video):
    if not has_video or value is None or offset is None:
        return None
    return int(offset + value)


def _stability(take_dir, probe, settled):
    if probe is None or settled is None:
        return {
            "status": "single_after_candidate",
            "method": "boundary_timing",
            "visual_change_ratio": None,
        }
    ratio = _image_change_ratio(
        take_dir / probe["path"],
        take_dir / settled["path"],
    )
    if ratio is None:
        return {
            "status": "not_measured",
            "method": "image_compare_failed",
            "visual_change_ratio": None,
        }
    return {
        "status": "visual_stable" if ratio <= 0.012 else "visual_still_changing",
        "method": "probe_vs_settled_mean_pixel_delta",
        "visual_change_ratio": round(ratio, 6),
        "threshold": 0.012,
    }


def _image_change_ratio(first_path, second_path):
    try:
        with Image.open(first_path) as first, Image.open(second_path) as second:
            first_image = ImageOps.contain(first.convert("RGB"), (320, 180))
            second_image = ImageOps.contain(second.convert("RGB"), (320, 180))
        width = min(first_image.width, second_image.width)
        height = min(first_image.height, second_image.height)
        first_image = first_image.crop((0, 0, width, height))
        second_image = second_image.crop((0, 0, width, height))
        diff = ImageChops.difference(first_image, second_image)
        means = ImageStat.Stat(diff).mean
        return sum(means) / len(means) / 255
    except Exception:
        return None


def _action_outcome(action, metadata, is_last):
    lifecycle = (metadata or {}).get("window_lifecycle") or []
    target = action.get("target") or {}
    element = target.get("element") or {}
    target_text = " ".join(str(value or "") for value in (
        element.get("name"),
        element.get("auto_id"),
        element.get("control_type"),
    )).casefold()
    close_words = ("close", "关闭", "dismiss", "exit", "退出")
    closed = is_last and any(
        item.get("closed_during_take") for item in lifecycle
    )
    inferred_close = is_last and any(word in target_text for word in close_words)
    return {
        "window_closed": bool(closed),
        "close_action_detected": bool(inferred_close),
        "result": (
            "window_closed"
            if closed
            else "close_action_candidate"
            if inferred_close
            else "visual_state_recorded"
        ),
    }


def _write_action_contact_sheet(
        take_dir,
        entries,
        cell_size=(360, 220),
        *,
        output_dir=None,
        artifact_prefix=None,
    ):
    output_dir = Path(output_dir or take_dir)
    rows = []
    for entry in entries:
        before = (entry.get("before") or {}).get("path")
        after = (entry.get("after") or {}).get("path")
        if before or after:
            rows.append((entry, before, after))
    if not rows:
        return None
    label_height = 36
    sheet = Image.new(
        "RGB",
        (cell_size[0] * 2, (cell_size[1] + label_height) * len(rows)),
        "#f5f5f5",
    )
    draw = ImageDraw.Draw(sheet)
    cells = []
    for row, (entry, before, after) in enumerate(rows):
        top = row * (cell_size[1] + label_height)
        for column, (label, relative) in enumerate((("操作前", before), ("操作后", after))):
            left = column * cell_size[0]
            if relative and (take_dir / relative).exists():
                with Image.open(take_dir / relative) as source:
                    image = ImageOps.contain(source.convert("RGB"), cell_size)
                x = left + (cell_size[0] - image.width) // 2
                y = top + (cell_size[1] - image.height) // 2
                sheet.paste(image, (x, y))
            draw.text(
                (left + 6, top + cell_size[1] + 5),
                f"#{entry.get('ordinal')} {entry.get('type')} {label}",
                fill="black",
            )
            cells.append({
                "action_id": entry.get("action_id"),
                "row": row,
                "column": column,
                "stage": "before" if column == 0 else "after",
                "source": relative,
            })
    output = output_dir / "action-contact-sheet.png"
    sheet.save(output)
    return {
        "path": (
            f"{str(artifact_prefix).strip('/')}/{output.name}"
            if artifact_prefix
            else output.name
        ),
        "rows": len(rows),
        "columns": 2,
        "cell_size": list(cell_size),
        "cells": cells,
    }


def _first_number(*values):
    for value in values:
        if value is not None:
            return value
    return None


def _last_number(*values):
    for value in reversed(values):
        if value is not None:
            return value
    return None
