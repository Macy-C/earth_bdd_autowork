import json
import os
import shutil
import tempfile
from pathlib import Path

import yaml
from PIL import Image, ImageDraw, ImageOps

from autowork_core.utils.debug_tools.recorder.models import SCHEMA_VERSION, public_dict


ATOMIC_TEMP_PREFIX = ".tmp-"
ATOMIC_TEMP_SUFFIX = ".tmp"
ATOMIC_TEMP_TOKEN_LENGTH = 8


def atomic_write_path_probe(path):
    path = Path(path)
    return path.parent / (
        f"{ATOMIC_TEMP_PREFIX}"
        f"{'0' * ATOMIC_TEMP_TOKEN_LENGTH}"
        f"{ATOMIC_TEMP_SUFFIX}"
    )


def dump_yaml(data):
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)


class RecordingSessionWriter:
    def __init__(self, session_dir):
        self.session_dir = Path(session_dir)
        self.steps_dir = self.session_dir / "steps"
        self.ai_dir = self.session_dir / "ai"
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.steps_dir.mkdir(parents=True, exist_ok=True)
        self.ai_dir.mkdir(parents=True, exist_ok=True)

    def initialize(self, feature_plan, scenario_plan, selected_steps, config):
        shutil.copy2(feature_plan.source_path, self.session_dir / "source.feature")
        self.write_json("feature-plan.json", {
            "schema_version": SCHEMA_VERSION,
            "feature": feature_plan,
        })
        self.write_json("scenario.json", {
            "schema_version": SCHEMA_VERSION,
            "scenario": scenario_plan,
            "selected_step_ids": [step.id for step in selected_steps],
            "capture_config": config,
        })

    def write_manifest(self, manifest):
        return self.write_json("manifest.json", manifest)

    def write_take(self, take_dir, metadata, events, actions, before_tree, after_tree, tree_diff, locator_bundle):
        take_dir = Path(take_dir)
        ui_dir = take_dir / "ui"
        ui_dir.mkdir(parents=True, exist_ok=True)
        self._write_json_path(take_dir / "take.json", metadata)
        self._write_jsonl_path(take_dir / "events.jsonl", events)
        self._write_json_path(ui_dir / "before-tree.json", before_tree)
        self._write_json_path(ui_dir / "after-tree.json", after_tree)
        self._write_json_path(ui_dir / "tree-diff.json", tree_diff)
        self._write_yaml_path(take_dir / "locator-candidates.auto.yaml", locator_bundle)
        self.write_take_summary(
            take_dir,
            metadata,
            actions,
            tree_diff,
            locator_bundle,
        )
        contact_sheet = None
        try:
            contact_sheet = self._write_contact_sheet(take_dir)
        except Exception as error:
            (take_dir / "contact-sheet-error.txt").write_text(
                f"{type(error).__name__}: {error}\n",
                encoding="utf-8",
            )
        self._write_json_path(
            take_dir / "media-index.json",
            self._media_index(take_dir, metadata, events, contact_sheet),
        )

    def write_ai_bundle(self, context, target_index, generation_contract, unresolved, locator_drafts, summary):
        self._write_json_path(self.ai_dir / "context.json", context)
        self._write_json_path(self.ai_dir / "target-index.json", target_index)
        self._write_json_path(self.ai_dir / "generation-contract.json", generation_contract)
        self._write_json_path(self.ai_dir / "unresolved-events.json", {
            "schema_version": SCHEMA_VERSION,
            "events": unresolved,
        })
        self._write_yaml_path(self.ai_dir / "locator-drafts.yaml", locator_drafts)
        _atomic_write_text(self.ai_dir / "generation-summary.md", summary)

    def write_readiness(self, readiness):
        self._write_json_path(self.ai_dir / "readiness.json", readiness)

    def update_action_media_index(self, take_dir, action_media):
        take_dir = Path(take_dir)
        media_path = take_dir / "media-index.json"
        media = (
            json.loads(media_path.read_text(encoding="utf-8"))
            if media_path.exists()
            else {"schema_version": SCHEMA_VERSION}
        )
        media["action_media"] = "action-media.json"
        media["actions"] = action_media.get("actions") or []
        media["action_contact_sheet"] = action_media.get("contact_sheet")
        self._write_json_path(media_path, media)
        return media_path

    def write_take_summary(self, take_dir, metadata, actions, tree_diff, locator_bundle):
        _atomic_write_text(
            Path(take_dir) / "capture-summary.md",
            self._take_markdown(metadata, actions, tree_diff, locator_bundle),
        )

    def write_json(self, relative_path, value):
        path = self.session_dir / relative_path
        self._write_json_path(path, value)
        return path

    @staticmethod
    def _write_json_path(path, value):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(
            path,
            json.dumps(public_dict(value), ensure_ascii=False, indent=2),
        )

    @staticmethod
    def _write_jsonl_path(path, values):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(public_dict(value), ensure_ascii=False) for value in values]
        _atomic_write_text(path, "\n".join(lines) + ("\n" if lines else ""))

    @staticmethod
    def _write_yaml_path(path, value):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(path, dump_yaml(public_dict(value)))

    @staticmethod
    def _take_markdown(metadata, actions, tree_diff, locator_bundle):
        metadata = public_dict(metadata)
        step = metadata.get("step") or {}
        summary = tree_diff.get("summary") or {}
        lines = [
            f"# Take {metadata.get('take_number')}: {step.get('keyword', '')} {step.get('text', '')}",
            "",
            f"- Status: `{metadata.get('status', '')}`",
            f"- Events: `{metadata.get('event_count', 0)}`",
            f"- Actions: `{len(actions)}`",
            f"- Tree diff: added={summary.get('added_count', 0)}, removed={summary.get('removed_count', 0)}, changed={summary.get('changed_count', 0)}",
            f"- Unresolved targets: `{len(locator_bundle.get('unresolved') or [])}`",
            "",
            "## Derived actions",
            "",
            "```json",
            json.dumps(actions, ensure_ascii=False, indent=2),
            "```",
            "",
        ]
        return "\n".join(lines)

    @staticmethod
    def _write_contact_sheet(take_dir, max_frames=16, columns=3, cell_size=(320, 180)):
        take_dir = Path(take_dir)
        frames = []
        before = take_dir / "screenshots" / "before.png"
        after = take_dir / "screenshots" / "after.png"
        if before.exists():
            frames.append(before)
        frames.extend(sorted((take_dir / "screenshots" / "events").glob("*.png")))
        if after.exists():
            frames.append(after)
        frames = _sample_paths(frames, max_frames)
        if not frames:
            return None

        label_height = 24
        rows = (len(frames) + columns - 1) // columns
        sheet = Image.new(
            "RGB",
            (columns * cell_size[0], rows * (cell_size[1] + label_height)),
            "white",
        )
        draw = ImageDraw.Draw(sheet)
        for index, path in enumerate(frames):
            row, column = divmod(index, columns)
            left = column * cell_size[0]
            top = row * (cell_size[1] + label_height)
            with Image.open(path) as source:
                frame = ImageOps.contain(source.convert("RGB"), cell_size)
            offset_x = left + (cell_size[0] - frame.width) // 2
            offset_y = top + (cell_size[1] - frame.height) // 2
            sheet.paste(frame, (offset_x, offset_y))
            draw.text((left + 6, top + cell_size[1] + 5), path.stem, fill="black")
        output = take_dir / "contact-sheet.png"
        sheet.save(output)
        return {
            "path": output.relative_to(take_dir).as_posix(),
            "columns": columns,
            "rows": rows,
            "cell_size": [cell_size[0], cell_size[1]],
            "label_height": label_height,
            "frames": [
                {
                    "cell_index": index,
                    "row": index // columns,
                    "column": index % columns,
                    "source": path.relative_to(take_dir).as_posix(),
                    "label": path.stem,
                    "event_id": path.stem if path.stem.startswith("event-") else None,
                }
                for index, path in enumerate(frames)
            ],
        }

    @staticmethod
    def _media_index(take_dir, metadata, events, contact_sheet):
        take_dir = Path(take_dir)
        metadata = public_dict(metadata)
        events = [public_dict(event) for event in events]
        timeline = metadata.get("timeline") or {}
        offset = timeline.get("video_to_event_offset_ms")
        has_video = (take_dir / "step.mp4").exists()
        linked_events = []
        for event in events:
            event_ms = event.get("monotonic_ms")
            screenshot_ms = (event.get("details") or {}).get("screenshot_monotonic_ms")
            linked_events.append({
                "event_id": event.get("id"),
                "event_type": event.get("event_type"),
                "event_ms": event_ms,
                "video_ms": (
                    int(offset + event_ms)
                    if has_video and offset is not None and event_ms is not None
                    else None
                ),
                "screenshot": event.get("screenshot"),
                "screenshot_ms": screenshot_ms,
                "screenshot_video_ms": (
                    int(offset + screenshot_ms)
                    if has_video and offset is not None and screenshot_ms is not None
                    else None
                ),
                "evidence_latency_ms": (event.get("details") or {}).get("evidence_latency_ms"),
                "screenshot_latency_ms": (event.get("details") or {}).get("screenshot_latency_ms"),
                "wheel_delta": event.get("wheel_delta"),
                "point": event.get("point"),
                "frames": (event.get("details") or {}).get("frames") or [],
            })
        return {
            "schema_version": SCHEMA_VERSION,
            "timebase": "milliseconds",
            "sync": {
                "method": timeline.get("sync_method"),
                "accuracy": timeline.get("sync_accuracy"),
                "event_time_origin": timeline.get("event_time_origin"),
                "event_zero_video_ms_estimate": offset if has_video else None,
                "formula": "video_ms = event_zero_video_ms_estimate + event_ms",
            },
            "video": {
                "path": "step.mp4" if has_video else None,
            },
            "screenshots": {
                "before": "screenshots/before.png" if (take_dir / "screenshots" / "before.png").exists() else None,
                "after": "screenshots/after.png" if (take_dir / "screenshots" / "after.png").exists() else None,
                "before_relation": "captured before video and input capture start",
                "after_relation": "captured after input capture and video stop",
                "scope": "step_boundary_only",
            },
            "action_media": None,
            "action_media_rule": (
                "Use action-media.json before/after for individual operations; "
                "Step before/after are not action result frames."
            ),
            "pauses": metadata.get("pauses") or [],
            "events": linked_events,
            "contact_sheet": contact_sheet,
        }


def _sample_paths(paths, limit):
    paths = list(paths)
    if len(paths) <= limit:
        return paths
    indexes = {
        round(index * (len(paths) - 1) / (limit - 1))
        for index in range(limit)
    }
    return [paths[index] for index in sorted(indexes)]


def _atomic_write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temp_name = tempfile.mkstemp(
        prefix=ATOMIC_TEMP_PREFIX,
        suffix=ATOMIC_TEMP_SUFFIX,
        dir=path.parent,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def write_json_atomic(path, value, *, compact=False):
    options = (
        {"separators": (",", ":")}
        if compact
        else {"indent": 2}
    )
    _atomic_write_text(
        path,
        json.dumps(
            public_dict(value),
            ensure_ascii=False,
            **options,
        ),
    )