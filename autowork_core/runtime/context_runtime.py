"""构造 Test Run 状态，并管理公共 data、日志和运行级服务。

Builds Test Run state and manages shared resources, logging, and run services.
"""

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from autowork_core.common.variables import Stratify
from autowork_core.runtime.reporting import SparkReporter
from autowork_core.runtime.run_indicator import AutomationRunIndicator
from autowork_core.runtime.run_state import STEP_SCOPE_ENV
from autowork_core.utils.bus import get_yaml_data
from autowork_core.utils.screenshot import ScreenRecorder
from config.paths import Paths
from config.settings import settings


@dataclass
class AutoworkRunState:
    public_data: Stratify
    recorder: ScreenRecorder
    reporter: SparkReporter | None
    run_indicator: AutomationRunIndicator
    run_indicator_visible: bool = False


def create_run_state():
    return AutoworkRunState(
        public_data=Stratify(
            initial=get_yaml_data(Paths.DATA_DIR / "public_data.yaml")
        ),
        recorder=ScreenRecorder(
            fps=settings.record_fps,
            monitor_index=settings.record_monitor_index,
            buffer_seconds=settings.record_buffer_seconds,
            segment_seconds=settings.record_segment_seconds,
            max_width=settings.record_max_width,
            ffmpeg_path=settings.record_ffmpeg_path,
            ffmpeg_preset=settings.record_ffmpeg_preset,
            ffmpeg_crf=settings.record_ffmpeg_crf,
            ffmpeg_draw_mouse=settings.record_ffmpeg_draw_mouse,
        ),
        reporter=SparkReporter() if settings.spark_report else None,
        run_indicator=AutomationRunIndicator(
            max_scenario_chars=settings.run_indicator_max_scenario_chars,
        ),
    )


def configure_logging():
    logger.remove()
    Paths.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    file_to = Paths.LOGS_DIR / settings.log_file
    logger.add(sys.stderr, colorize=True, level=settings.log_level)
    logger.add(
        file_to,
        level=settings.log_level,
        rotation=settings.log_rotation,
        retention=settings.log_retention,
    )


def current_step_scope_label():
    raw_scope = os.environ.get(STEP_SCOPE_ENV)
    if not raw_scope:
        return "<all steps>"
    try:
        scope = json.loads(raw_scope)
    except json.JSONDecodeError:
        return f"<invalid scope: {raw_scope}>"
    files = scope.get("files") or []
    return ", ".join(files) if files else "<all steps>"


def feature_display_path(feature):
    filename = getattr(feature, "filename", "")
    try:
        return str(Paths.BASE_DIR.joinpath(filename).resolve().relative_to(Paths.BASE_DIR))
    except Exception:
        try:
            return str(Path(filename).resolve().relative_to(Paths.BASE_DIR))
        except Exception:
            return str(filename)