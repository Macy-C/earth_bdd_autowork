from __future__ import annotations

from pathlib import Path

from autowork_core.utils.screenshot import ScreenRecorder
from config.settings import settings


class StepVideoRecorder:
    def __init__(self, output_dir):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.recorder = ScreenRecorder(
            fps=settings.record_fps,
            monitor_index=settings.record_monitor_index,
            buffer_seconds=settings.record_buffer_seconds,
            segment_seconds=settings.record_segment_seconds,
            max_width=settings.record_max_width,
            ffmpeg_path=settings.record_ffmpeg_path,
            ffmpeg_preset=settings.record_ffmpeg_preset,
            ffmpeg_crf=settings.record_ffmpeg_crf,
            ffmpeg_draw_mouse=settings.record_ffmpeg_draw_mouse,
        )
        self.recorder.output_dir = self.output_dir
        self.recorder.screenshot_dir = self.output_dir / "screenshots"
        self.path = None

    @property
    def is_running(self):
        return self.recorder._running

    def start(self, feature_name, step_name):
        self.path = self.recorder.start(feature_name, step_name, mode="all")
        return self.path

    def stop(self):
        path = self.recorder.stop()
        if path is None or not Path(path).exists():
            return None
        destination = self.output_dir / "step.mp4"
        source = Path(path)
        if source != destination:
            destination.unlink(missing_ok=True)
            source.replace(destination)
        self.path = destination
        return destination

    def abort(self):
        self.recorder.delete()
        self.path = None