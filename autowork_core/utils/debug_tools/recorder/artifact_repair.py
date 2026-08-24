from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import yaml

from autowork_core.utils.debug_tools.recorder.models import SCHEMA_VERSION
from autowork_core.utils.debug_tools.recorder.session_projection import (
    rebuild_session_projections,
)
from autowork_core.utils.debug_tools.recorder.timeline import TimelineStore
from autowork_core.utils.debug_tools.recorder.tree_snapshot import diff_tree_snapshots
from autowork_core.utils.debug_tools.recorder.writer import RecordingSessionWriter


def repair_derived_artifacts(session_dir):
    """Rebuild only deterministic derivatives; never modify raw events or media."""
    session_dir = Path(session_dir).resolve()
    manifest_path = session_dir / "manifest.json"
    if not manifest_path.exists():
        return _report(session_dir, [], ["manifest.json 不存在，无法识别会话"])
    try:
        manifest = _read_json(manifest_path)
    except Exception as error:
        return _report(
            session_dir,
            [],
            [f"manifest.json 无法解析: {type(error).__name__}: {error}"],
        )

    repaired = []
    unrecoverable = []
    writer = RecordingSessionWriter(session_dir)
    for step_entry in manifest.get("steps") or []:
        for take in step_entry.get("takes") or []:
            if take.get("status") != "completed":
                continue
            take_dir = session_dir / str(take.get("path") or "")
            if not take_dir.exists():
                unrecoverable.append(f"selected Take 目录不存在: {take_dir}")
                continue
            _repair_take(take_dir, writer, repaired, unrecoverable)

    try:
        rebuild_session_projections(session_dir, manifest=manifest)
        repaired.extend([
            "ai/context.json",
            "ai/target-index.json",
            "ai/generation-contract.json",
            "ai/readiness.json",
            "catalog.json entry",
        ])
    except Exception as error:
        unrecoverable.append(
            f"AI 索引重建失败: {type(error).__name__}: {error}"
        )
    return _report(session_dir, repaired, unrecoverable)


def _repair_take(take_dir, writer, repaired, unrecoverable):
    timeline = TimelineStore(take_dir)
    before_path = take_dir / "ui" / "before-tree.json"
    after_path = take_dir / "ui" / "after-tree.json"
    diff_path = take_dir / "ui" / "tree-diff.json"
    diff_valid = _valid_json(diff_path)
    if not diff_valid and before_path.exists() and after_path.exists():
        try:
            tree_diff = diff_tree_snapshots(
                _read_json(before_path),
                _read_json(after_path),
            )
            writer._write_json_path(diff_path, tree_diff)
            repaired.append(_relative(take_dir, diff_path))
        except Exception as error:
            unrecoverable.append(
                f"tree diff 无法重建: {take_dir}: {type(error).__name__}: {error}"
            )
    elif not diff_valid:
        unrecoverable.append(
            f"缺少 before/after tree，无法重建 tree-diff: {take_dir}"
        )

    metadata_path = take_dir / "take.json"
    events_path = take_dir / "events.jsonl"
    media_path = take_dir / "media-index.json"
    media_valid = _valid_json(media_path)
    if not media_valid and metadata_path.exists() and events_path.exists():
        try:
            metadata = _read_json(metadata_path)
            events = _read_jsonl(events_path)
            contact_sheet = writer._write_contact_sheet(take_dir)
            writer._write_json_path(
                media_path,
                writer._media_index(take_dir, metadata, events, contact_sheet),
            )
            repaired.append(_relative(take_dir, media_path))
        except Exception as error:
            unrecoverable.append(
                f"media-index 无法重建: {take_dir}: {type(error).__name__}: {error}"
            )
    elif not media_valid:
        unrecoverable.append(
            f"缺少 take.json/events.jsonl，无法重建 media-index: {take_dir}"
        )

    if timeline.auto_path.exists():
        try:
            timeline.materialize()
            repaired.extend([
                _relative(take_dir, timeline.auto_path),
                _relative(take_dir, timeline.effective_path),
                _relative(take_dir, timeline.state_path),
                _relative(take_dir, timeline.locator_effective_path),
                _relative(
                    take_dir,
                    timeline.projections.current().path("action_media"),
                ),
                _relative(
                    take_dir,
                    timeline.projections.current().path("evidence_graph"),
                ),
            ])
        except Exception as error:
            unrecoverable.append(
                f"时间线无法重建: {take_dir}: {type(error).__name__}: {error}"
            )
    else:
        unrecoverable.append(
            f"动作源不存在: {take_dir}/actions.auto.json"
        )

    summary_path = take_dir / "capture-summary.md"
    try:
        metadata = _read_json(metadata_path)
        actions = timeline.effective_actions()
        tree_diff = _read_json(diff_path) if diff_path.exists() else {}
        locator_path = (
            timeline.locator_effective_path
            if timeline.locator_effective_path.exists()
            else timeline.locator_auto_path
        )
        locator = (
            yaml.safe_load(locator_path.read_text(encoding="utf-8")) or {}
            if locator_path.exists()
            else {}
        )
        writer.write_take_summary(
            take_dir,
            metadata,
            actions,
            tree_diff,
            locator,
        )
        repaired.append(_relative(take_dir, summary_path))
    except Exception as error:
        unrecoverable.append(
            f"capture-summary 无法重建: {take_dir}: {type(error).__name__}: {error}"
        )

def _report(session_dir, repaired, unrecoverable):
    report = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "session_dir": str(session_dir),
        "repaired": list(dict.fromkeys(repaired)),
        "unrecoverable": list(dict.fromkeys(unrecoverable)),
        "raw_evidence_modified": False,
    }
    output = session_dir / "ai" / "repairs" / "latest.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_path"] = str(output)
    return report


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _read_jsonl(path):
    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _valid_json(path):
    path = Path(path)
    if not path.exists():
        return False
    try:
        return isinstance(json.loads(path.read_text(encoding="utf-8")), dict)
    except Exception:
        return False


def _relative(base, path):
    try:
        return Path(path).relative_to(base).as_posix()
    except ValueError:
        return str(path)
