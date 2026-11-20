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
    recorder: ScreenRecorder | None
    reporter: SparkReporter | None
    run_indicator: AutomationRunIndicator
    run_indicator_visible: bool = False
    knowledge_capture: object | None = None
    knowledge_capture_error: object | None = None
    collector: object | None = None


def create_run_state(collector=None):
    state = AutoworkRunState(
        public_data=Stratify(
            initial=get_yaml_data(Paths.DATA_DIR / "public_data.yaml")
        ),
        recorder=collector.recorder if collector is not None else ScreenRecorder(
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
        reporter=SparkReporter(collector=collector) if settings.spark_report else None,
        run_indicator=AutomationRunIndicator(
            max_scenario_chars=settings.run_indicator_max_scenario_chars,
        ),
        collector=collector,
    )
    return state


def finish_run_state(context):
    from autowork_core.runtime.knowledge_capture import dispatch_knowledge_hook

    state = getattr(context, "autowork_run", None)
    if state is None:
        return
    collector = getattr(state, "collector", None)
    deliveries = collector.deliveries if collector is not None else {}
    for name, callback in (
        ("report", lambda: state.reporter.after_all(context)),
        ("knowledge", lambda: dispatch_knowledge_hook(context, "after_all")),
    ):
        if name in deliveries:
            continue
        if name == "report" and state.reporter is None or name == "knowledge" and getattr(state, "knowledge_capture", None) is None:
            deliveries[name] = "not_requested"
            continue
        deliveries[name] = "started"
        if collector is not None:
            collector.delivery_name = name
        try:
            callback()
        except Exception as error:
            deliveries[name] = "failed"
            logger.warning("运行交付失败，不改变执行结果: {}: {}", name, type(error).__name__)
        else:
            deliveries[name] = "completed"
            if name == "knowledge":
                status = state.knowledge_capture.document.get("markdown_export", {}).get("status")
                if status == "failed":
                    deliveries[name] = "failed"
        finally:
            if collector is not None:
                collector.delivery_name = None


_LOG_SINKS = []


def configure_logging():
    from autowork_core.runtime.evidence.logging import standalone_log_record
    if not _LOG_SINKS:
        try:
            logger.remove(0)
        except ValueError:
            pass
    for sink in _LOG_SINKS:
        try:
            logger.remove(sink)
        except ValueError:
            pass
    _LOG_SINKS.clear()
    Paths.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    file_to = Paths.LOGS_DIR / settings.log_file
    _LOG_SINKS.append(logger.add(sys.stderr, colorize=True, level=settings.log_level, filter=standalone_log_record))
    _LOG_SINKS.append(logger.add(
        file_to,
        level=settings.log_level,
        rotation=settings.log_rotation,
        retention=settings.log_retention,
        filter=standalone_log_record,
    ))


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