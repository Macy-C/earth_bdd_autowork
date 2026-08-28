from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from autowork_core.utils.debug_tools.recorder.projection_store import (
    resolve_take_artifact,
)
from config.paths import Paths


def extract_video_frame(
        take_dir,
        *,
        event_id=None,
        video_ms=None,
        video_path=None,
        output_path=None,
    ):
    take_dir = Path(take_dir).resolve()
    media_index_path = resolve_take_artifact(
        take_dir,
        "media_index",
    )
    if media_index_path is None:
        raise ValueError(
            "Take 缺少有效 Projection 5.7 media_index"
        )
    if not media_index_path.exists():
        raise FileNotFoundError(f"media-index.json 不存在: {media_index_path}")
    media_index = json.loads(media_index_path.read_text(encoding="utf-8"))

    if event_id is not None:
        event = next(
            (
                item
                for item in media_index.get("events", [])
                if item.get("event_id") == event_id
            ),
            None,
        )
        if event is None:
            raise KeyError(f"media-index.json 中不存在事件: {event_id}")
        video_ms = event.get("video_ms")
        if video_ms is None:
            raise ValueError(f"事件没有可用视频时间: {event_id}")
    if video_ms is None:
        raise ValueError("必须提供 event_id 或 video_ms")

    video_path = video_path or (media_index.get("video") or {}).get("path")
    if not video_path:
        raise ValueError("当前 Take 没有视频")
    video_path = (take_dir / video_path).resolve()
    try:
        video_path.relative_to(take_dir)
    except ValueError as error:
        raise ValueError(f"视频路径越界: {video_path}") from error
    if not video_path.exists():
        raise FileNotFoundError(f"视频不存在: {video_path}")

    ffmpeg_path = Paths.FFMPEG_EXE.resolve()
    if not ffmpeg_path.exists():
        raise FileNotFoundError(f"ffmpeg 不存在: {ffmpeg_path}")

    if output_path is None:
        stem = event_id or f"video-{int(video_ms):08d}ms"
        output_path = take_dir / "extracted_frames" / f"{stem}.png"
    else:
        output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    command = [
        str(ffmpeg_path),
        "-y",
        "-ss",
        f"{max(0, float(video_ms)) / 1000:.3f}",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        str(output_path),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0 or not output_path.exists():
        detail = (result.stderr or result.stdout or "").strip()[-2000:]
        raise RuntimeError(f"视频帧提取失败: code={result.returncode}, detail={detail}")
    return output_path


def build_parser():
    parser = argparse.ArgumentParser(description="Extract a linked frame from a recorder Take")
    parser.add_argument("take_dir", help="Path to a take-XXX directory")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--event", dest="event_id", help="Event id from media-index.json")
    target.add_argument("--video-ms", type=float, help="Video timestamp in milliseconds")
    parser.add_argument("--output", default=None, help="Optional output PNG path")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    output = extract_video_frame(
        args.take_dir,
        event_id=args.event_id,
        video_ms=args.video_ms,
        output_path=args.output,
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())