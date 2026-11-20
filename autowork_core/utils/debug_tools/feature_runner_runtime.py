from __future__ import annotations

import argparse
import json
import os
import pprint
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from autowork_core.runtime.temporary_features import cleanup_temporary_features
from autowork_core.utils.debug_tools.feature_runner_catalog import FeatureTreeNode
from config.paths import Paths
from config.settings import OCR_MODE_PRESETS, PIC_MODE_PRESETS, Settings


_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_STATE_FILE_NAME = ".debug_feature_runner_state.json"

FORMATTER_CHOICES = ("progress2", "pretty", "plain")
APP_LAUNCH_MODES = ("attach", "auto")
BACKEND_CHOICES = ("uia", "win32")
PROCESS_TRACK_MODES = ("snapshot", "root", "none")
LOG_LEVEL_CHOICES = ("DEBUG", "INFO", "WARNING", "ERROR")
RECORD_MODE_CHOICES = ("off", "failed", "all")
FFMPEG_PRESET_CHOICES = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium")
OCR_MODE_CHOICES = ("fast", "balanced", "accurate")
OCR_DET_LIMIT_TYPE_CHOICES = ("max", "min")
OCR_MATCH_MODES = ("smart", "contains", "exact")
PIC_MODE_CHOICES = ("fast", "balanced", "accurate")
PIC_POS_CHOICES = ("1", "2", "3", "4", "5", "6", "7", "8", "9")
PIC_METHOD_CHOICES = ("", "tpl", "mstpl", "gmstpl", "kaze", "brisk", "akaze", "orb", "sift", "surf", "brief")
SINGLE_STEP_MODES = ("previous", "background", "none")


@dataclass(frozen=True)
class FeatureRunTarget:
    kind: str
    feature_path: str
    rule_name: str | None = None
    rule_line: int | None = None
    scenario_name: str | None = None
    scenario_line: int | None = None
    example_id: str | None = None
    single_step_index: int | None = None
    single_step_mode: str = "previous"

    def supports_tag_filter(self) -> bool:
        return (
            self.single_step_index is None
            and not self.scenario_name
            and self.kind in {"directory", "feature", "rule"}
        )


@dataclass(frozen=True)
class FeatureRunConfig:
    # 1. Behave & Runner behavior
    formatter: str = "progress2"
    verbose: bool = True
    tags: str = ""
    clear_screenshots: bool = True
    minimize_on_run: bool = True

    # 2. App lifecycle
    app_launch_mode: str = "attach"
    app_path: str = ""
    backend: str = "uia"
    app_process_track_mode: str = "snapshot"

    # 3. Log, visual & status indicator
    log_level: str = "DEBUG"
    log_file: str = "logs.logs"
    log_rotation: str = "20 MB"
    log_retention: str = "3 days"
    draw_outline: bool = True
    run_indicator_enabled: bool = True
    run_indicator_max_scenario_chars: int = 28

    # 4. Screen recording (full)
    record_mode: str = "failed"
    record_fps: int = 5
    record_buffer_seconds: float = 10.0
    record_segment_seconds: float = 2.0
    record_max_width: int = 1280
    record_monitor_index: int = 1
    record_ffmpeg_path: str = ""
    record_ffmpeg_preset: str = "ultrafast"
    record_ffmpeg_crf: int = 28
    record_ffmpeg_draw_mouse: bool = True

    # 5. OCR recognition (full)
    ocr_mode: str = "accurate"
    ocr_warmup: bool = True
    ocr_cache_ttl: float = 0.0
    ocr_det_limit_side_len: int = 1600
    ocr_det_limit_type: str = "max"
    ocr_det_box_thresh: float = 0.6
    ocr_det_unclip_ratio: float = 1.6
    ocr_recognition_batch_size: int = 4
    ocr_timeout: float = 10.0
    ocr_interval: float = 0.3
    ocr_match_mode: str = "smart"
    ocr_debug_on_fail: bool = True

    # 6. Picture recognition PIC (full)
    pic_mode: str = "accurate"
    pic_threshold: float = 0.7
    pic_pos: int = 5
    pic_rgb: bool = True
    pic_method: str = ""
    pic_timeout: float = 10.0
    pic_interval: float = 0.2
    pic_debug_on_fail: bool = True
    pic_scale_max: int = 1000
    pic_scale_step: float = 0.005

    # 7. Spark report, LLM diagnosis & Knowledge capture
    spark_report: bool = True
    report_diagnosis_enabled: bool = False
    report_diagnosis_include_images: bool = True
    report_diagnosis_max_items: int = 3
    report_diagnosis_max_input_tokens: int = 60000
    report_diagnosis_max_output_tokens: int = 4096
    knowledge_capture_enabled: bool = False
    llm_base_url: str = ""
    llm_model: str = ""
    llm_connect_timeout_seconds: float = 5.0
    llm_read_timeout_seconds: float = 45.0
    llm_allow_insecure_http: bool = True

    def to_settings_overrides(self) -> dict[str, Any]:
        return {
            "app_launch_mode": str(self.app_launch_mode).strip().lower() or "attach",
            "app_path": str(self.app_path or ""),
            "backend": str(self.backend).strip().lower() or "uia",
            "app_process_track_mode": str(self.app_process_track_mode).strip().lower() or "snapshot",
            "log_level": str(self.log_level).strip().upper() or "INFO",
            "log_file": str(self.log_file).strip() or "logs.logs",
            "log_rotation": str(self.log_rotation).strip() or "20 MB",
            "log_retention": str(self.log_retention).strip() or "3 days",
            "draw_outline": bool(self.draw_outline),
            "run_indicator_enabled": bool(self.run_indicator_enabled),
            "run_indicator_max_scenario_chars": max(8, int(self.run_indicator_max_scenario_chars)),
            "record_mode": str(self.record_mode).strip().lower() or "off",
            "record_fps": max(1, int(self.record_fps)),
            "record_buffer_seconds": max(1.0, float(self.record_buffer_seconds)),
            "record_segment_seconds": max(1.0, float(self.record_segment_seconds)),
            "record_max_width": max(0, int(self.record_max_width)),
            "record_monitor_index": max(1, int(self.record_monitor_index)),
            "record_ffmpeg_path": str(self.record_ffmpeg_path or "").strip(),
            "record_ffmpeg_preset": str(self.record_ffmpeg_preset or "ultrafast").strip(),
            "record_ffmpeg_crf": max(0, min(51, int(self.record_ffmpeg_crf))),
            "record_ffmpeg_draw_mouse": bool(self.record_ffmpeg_draw_mouse),
            "ocr_mode": str(self.ocr_mode).strip().lower() or "balanced",
            "ocr_warmup": bool(self.ocr_warmup),
            "ocr_cache_ttl": max(0.0, float(self.ocr_cache_ttl)),
            "ocr_det_limit_side_len": max(64, int(self.ocr_det_limit_side_len)),
            "ocr_det_limit_type": str(self.ocr_det_limit_type).strip().lower() or "max",
            "ocr_det_box_thresh": min(1.0, max(0.01, float(self.ocr_det_box_thresh))),
            "ocr_det_unclip_ratio": max(0.1, float(self.ocr_det_unclip_ratio)),
            "ocr_recognition_batch_size": max(1, int(self.ocr_recognition_batch_size)),
            "ocr_timeout": max(0.1, float(self.ocr_timeout)),
            "ocr_interval": max(0.05, float(self.ocr_interval)),
            "ocr_match_mode": str(self.ocr_match_mode).strip().lower() or "smart",
            "ocr_debug_on_fail": bool(self.ocr_debug_on_fail),
            "pic_mode": str(self.pic_mode).strip().lower() or "balanced",
            "pic_threshold": min(1.0, max(0.01, float(self.pic_threshold))),
            "pic_pos": min(9, max(1, int(self.pic_pos))),
            "pic_rgb": bool(self.pic_rgb),
            "pic_method": str(self.pic_method or "").strip().lower(),
            "pic_timeout": max(0.1, float(self.pic_timeout)),
            "pic_interval": max(0.05, float(self.pic_interval)),
            "pic_debug_on_fail": bool(self.pic_debug_on_fail),
            "pic_scale_max": max(0, int(self.pic_scale_max)),
            "pic_scale_step": max(0.0005, float(self.pic_scale_step)),
            "spark_report": bool(self.spark_report),
            "report_diagnosis_enabled": bool(self.report_diagnosis_enabled),
            "report_diagnosis_include_images": bool(self.report_diagnosis_include_images),
            "report_diagnosis_max_items": max(0, int(self.report_diagnosis_max_items)),
            "report_diagnosis_max_input_tokens": max(256, int(self.report_diagnosis_max_input_tokens)),
            "report_diagnosis_max_output_tokens": max(64, int(self.report_diagnosis_max_output_tokens)),
            "knowledge_capture_enabled": bool(self.knowledge_capture_enabled),
            "llm_base_url": str(self.llm_base_url or "").strip().rstrip("/"),
            "llm_model": str(self.llm_model or "").strip(),
            "llm_connect_timeout_seconds": max(0.5, float(self.llm_connect_timeout_seconds)),
            "llm_read_timeout_seconds": max(1.0, float(self.llm_read_timeout_seconds)),
            "llm_allow_insecure_http": bool(self.llm_allow_insecure_http),
        }

    def compact_summary(self) -> str:
        parts = [
            str(self.app_launch_mode or "attach"),
            str(self.log_level or "INFO").upper(),
            f"录屏:{self.record_mode}",
        ]
        if self.draw_outline:
            parts.append("蓝框")
        if self.knowledge_capture_enabled:
            parts.append("知识采集")
        return " · ".join(parts)


@dataclass(frozen=True)
class ConsoleEvent:
    kind: str  # "line" | "finished" | "error"
    text: str = ""
    level_tag: str = "normal"
    exit_code: int | None = None
    stopped: bool = False
    duration_seconds: float = 0.0


def config_from_project_settings(project_settings: Settings | None = None) -> FeatureRunConfig:
    cfg = project_settings or Settings()
    return FeatureRunConfig(
        formatter="progress2",
        verbose=True,
        tags="",
        clear_screenshots=True,
        minimize_on_run=True,
        app_launch_mode=str(cfg.app_launch_mode or "attach"),
        app_path=str(cfg.app_path or ""),
        backend=str(cfg.backend or "uia"),
        app_process_track_mode=str(cfg.app_process_track_mode or "snapshot"),
        log_level=str(cfg.log_level or "INFO").upper(),
        log_file=str(cfg.log_file or "logs.logs"),
        log_rotation=str(cfg.log_rotation or "20 MB"),
        log_retention=str(cfg.log_retention or "3 days"),
        draw_outline=bool(cfg.draw_outline),
        run_indicator_enabled=bool(cfg.run_indicator_enabled),
        run_indicator_max_scenario_chars=int(cfg.run_indicator_max_scenario_chars or 28),
        record_mode=str(cfg.record_mode or "failed"),
        record_fps=int(cfg.record_fps or 5),
        record_buffer_seconds=float(cfg.record_buffer_seconds or 8.0),
        record_segment_seconds=float(cfg.record_segment_seconds or 2.0),
        record_max_width=int(cfg.record_max_width if cfg.record_max_width is not None else 1280),
        record_monitor_index=int(cfg.record_monitor_index or 1),
        record_ffmpeg_path=str(cfg.record_ffmpeg_path or ""),
        record_ffmpeg_preset=str(cfg.record_ffmpeg_preset or "ultrafast"),
        record_ffmpeg_crf=int(cfg.record_ffmpeg_crf if cfg.record_ffmpeg_crf is not None else 28),
        record_ffmpeg_draw_mouse=bool(cfg.record_ffmpeg_draw_mouse),
        ocr_mode=str(cfg.ocr_mode or "balanced"),
        ocr_warmup=bool(cfg.ocr_warmup),
        ocr_cache_ttl=float(cfg.ocr_cache_ttl),
        ocr_det_limit_side_len=int(cfg.ocr_det_limit_side_len),
        ocr_det_limit_type=str(cfg.ocr_det_limit_type or "max"),
        ocr_det_box_thresh=float(cfg.ocr_det_box_thresh),
        ocr_det_unclip_ratio=float(cfg.ocr_det_unclip_ratio),
        ocr_recognition_batch_size=int(cfg.ocr_recognition_batch_size),
        ocr_timeout=float(cfg.ocr_timeout),
        ocr_interval=float(cfg.ocr_interval),
        ocr_match_mode=str(cfg.ocr_match_mode or "smart"),
        ocr_debug_on_fail=bool(cfg.ocr_debug_on_fail),
        pic_mode=str(cfg.pic_mode or "balanced"),
        pic_threshold=float(cfg.pic_threshold),
        pic_pos=int(cfg.pic_pos or 5),
        pic_rgb=bool(cfg.pic_rgb),
        pic_method=str(cfg.pic_method or ""),
        pic_timeout=float(cfg.pic_timeout),
        pic_interval=float(cfg.pic_interval),
        pic_debug_on_fail=bool(cfg.pic_debug_on_fail),
        pic_scale_max=int(cfg.pic_scale_max),
        pic_scale_step=float(cfg.pic_scale_step),
        spark_report=bool(cfg.spark_report),
        report_diagnosis_enabled=bool(cfg.report_diagnosis_enabled),
        report_diagnosis_include_images=bool(cfg.report_diagnosis_include_images),
        report_diagnosis_max_items=int(cfg.report_diagnosis_max_items),
        report_diagnosis_max_input_tokens=int(cfg.report_diagnosis_max_input_tokens),
        report_diagnosis_max_output_tokens=int(cfg.report_diagnosis_max_output_tokens),
        knowledge_capture_enabled=bool(cfg.knowledge_capture_enabled),
        llm_base_url=str(cfg.llm_base_url or ""),
        llm_model=str(cfg.llm_model or ""),
        llm_connect_timeout_seconds=float(cfg.llm_connect_timeout_seconds or 5.0),
        llm_read_timeout_seconds=float(cfg.llm_read_timeout_seconds or 30.0),
        llm_allow_insecure_http=bool(cfg.llm_allow_insecure_http),
    )


def apply_ocr_mode_preset(cfg: FeatureRunConfig, mode: str) -> FeatureRunConfig:
    normalized = str(mode or "balanced").strip().lower()
    preset = OCR_MODE_PRESETS.get(normalized)
    if not preset:
        return cfg
    data = asdict(cfg)
    data.update({
        "ocr_mode": normalized,
        "ocr_cache_ttl": float(preset["CACHE_TTL"]),
        "ocr_det_limit_side_len": int(preset["DET_LIMIT_SIDE_LEN"]),
        "ocr_det_limit_type": str(preset["DET_LIMIT_TYPE"]).lower(),
        "ocr_det_box_thresh": float(preset["DET_BOX_THRESH"]),
        "ocr_det_unclip_ratio": float(preset["DET_UNCLIP_RATIO"]),
        "ocr_recognition_batch_size": int(preset["RECOGNITION_BATCH_SIZE"]),
        "ocr_timeout": float(preset["TIMEOUT"]),
        "ocr_interval": float(preset["INTERVAL"]),
        "ocr_match_mode": str(preset["MATCH_MODE"]).lower(),
    })
    return FeatureRunConfig(**data)


def apply_pic_mode_preset(cfg: FeatureRunConfig, mode: str) -> FeatureRunConfig:
    normalized = str(mode or "balanced").strip().lower()
    preset = PIC_MODE_PRESETS.get(normalized)
    if not preset:
        return cfg
    data = asdict(cfg)
    data.update({
        "pic_mode": normalized,
        "pic_threshold": float(preset["THRESHOLD"]),
        "pic_pos": int(preset["POS"]),
        "pic_rgb": bool(preset["RGB"]),
        "pic_method": str(preset["METHOD"]).lower(),
        "pic_timeout": float(preset["TIMEOUT"]),
        "pic_interval": float(preset["INTERVAL"]),
        "pic_debug_on_fail": bool(preset["DEBUG_ON_FAIL"]),
        "pic_scale_max": int(preset["SCALE_MAX"]),
        "pic_scale_step": float(preset["SCALE_STEP"]),
    })
    return FeatureRunConfig(**data)


def local_debug_preset_config(project_settings: Settings | None = None) -> FeatureRunConfig:
    base = config_from_project_settings(project_settings)
    data = asdict(base)
    data.update({
        "formatter": "progress2",
        "verbose": True,
        "clear_screenshots": True,
        "minimize_on_run": True,
        "log_level": "DEBUG",
        "draw_outline": True,
        "record_mode": "failed",
        "record_buffer_seconds": 10.0,
        "report_diagnosis_enabled": False,
        "ocr_cache_ttl": 0.0,
        "ocr_timeout": 10.0,
        "ocr_interval": 0.3,
        "ocr_match_mode": "smart",
        "ocr_debug_on_fail": True,
        "pic_threshold": 0.7,
        "pic_timeout": 10.0,
        "pic_interval": 0.2,
        "pic_debug_on_fail": True,
        "pic_scale_max": 1000,
        "pic_scale_step": 0.005,
    })
    return FeatureRunConfig(**data)


def target_from_tree_node(
        node: FeatureTreeNode,
        *,
        single_step_index: int | None = None,
        single_step_mode: str = "previous",
) -> FeatureRunTarget:
    step_idx = single_step_index if single_step_index is not None else node.step_index
    kind = "step" if step_idx is not None else node.kind
    mode = str(single_step_mode or "previous").strip().lower()
    if mode not in SINGLE_STEP_MODES:
        mode = "previous"
    return FeatureRunTarget(
        kind=kind,
        feature_path=str(node.target_path),
        rule_name=node.rule_name,
        rule_line=node.rule_line,
        scenario_name=node.scenario_name,
        scenario_line=node.scenario_line,
        example_id=node.example_id,
        single_step_index=step_idx,
        single_step_mode=mode,
    )


def describe_run_target(target: FeatureRunTarget, *, base_dir: Path | None = None) -> str:
    root = Path(base_dir or Paths.BASE_DIR).resolve()
    raw_path = Path(target.feature_path)
    try:
        rel = raw_path.resolve().relative_to(root).as_posix()
    except ValueError:
        rel = raw_path.as_posix()

    parts = [rel]
    if target.rule_name:
        parts.append(f"Rule: {target.rule_name}")
    if target.scenario_name:
        parts.append(f"Scenario: {target.scenario_name}")
    if target.example_id:
        if str(target.example_id).endswith(".*"):
            parts.append(f"Examples 组: {target.example_id}")
        else:
            parts.append(f"Example: @{target.example_id}")
    if target.single_step_index is not None:
        parts.append(f"Step {target.single_step_index} ({target.single_step_mode})")
    return "  ›  ".join(parts)


def render_local_runner_snippet(
        target: FeatureRunTarget | None,
        config: FeatureRunConfig,
        *,
        diff_only: bool = True,
        project_settings: Settings | None = None,
) -> str:
    all_overrides = config.to_settings_overrides()
    if diff_only:
        baseline = config_from_project_settings(project_settings).to_settings_overrides()
        overrides = {
            key: value
            for key, value in all_overrides.items()
            if baseline.get(key) != value
        }
    else:
        overrides = all_overrides

    overrides_str = pprint.pformat(
        overrides,
        indent=4,
        width=88,
        sort_dicts=False,
    )
    indented_overrides = "\n    ".join(overrides_str.splitlines())
    feature_path_text = target.feature_path if target is not None else str(Paths.TEST_FEATURES_DIR)
    call_lines = [
        "from autowork_core.runtime.local_runner import main",
        "",
        f"debug_settings_overrides = {indented_overrides}",
        "",
        "raise SystemExit(main(",
        f"    feature_path=r{feature_path_text!r},",
        "    settings_overrides=debug_settings_overrides,",
        f"    verbose={config.verbose!r},",
        f"    formatter={config.formatter or None!r},",
    ]
    if target is not None:
        if target.scenario_name:
            call_lines.append(f"    scenario_name={target.scenario_name!r},")
        if target.example_id:
            call_lines.append(f"    example_id={target.example_id!r},")
        if target.rule_name:
            call_lines.append(f"    rule_name={target.rule_name!r},")
        if target.rule_line is not None and not target.scenario_name:
            call_lines.append(f"    rule_line={target.rule_line!r},")
        if target.scenario_line is not None and target.scenario_name:
            call_lines.append(f"    scenario_line={target.scenario_line!r},")
        if target.single_step_index is not None:
            call_lines.append(f"    single_step_index={target.single_step_index!r},")
            call_lines.append(f"    single_step_mode={target.single_step_mode!r},")
        if target.supports_tag_filter() and config.tags.strip():
            call_lines.append(f"    tags={config.tags.strip()!r},")
    elif config.tags.strip():
        call_lines.append(f"    tags={config.tags.strip()!r},")
    if not config.clear_screenshots:
        call_lines.append("    clear_screenshots=False,")
    call_lines.append("))")
    return "\n".join(call_lines)


def strip_ansi(text: str) -> str:
    cleaned = _ANSI_ESCAPE_RE.sub("", str(text or ""))
    if "\r" in cleaned:
        cleaned = cleaned.replace("\r\n", "\n")
        segments = cleaned.split("\r")
        cleaned = segments[-1]
    return cleaned


def classify_console_line(line: str) -> str:
    stripped = line.strip()
    if not stripped:
        return "normal"
    lower = stripped.lower()
    upper = stripped.upper()
    if (
        ("0 failed" in lower and "passed" in lower and "hook-error" not in lower)
        or stripped.startswith("状态: 通过")
        or "运行结束: 通过" in stripped
    ):
        return "success"
    if (
        "| ERROR" in upper
        or "| CRITICAL" in upper
        or "运行结束: 失败" in stripped
        or stripped.startswith(("Traceback (most recent call last):", "AssertionError", "RuntimeError", "LookupError", "ValueError", "FileNotFoundError"))
        or (
            "FAILED" in upper
            and not re.search(r"\b0\s+FAILED\b", upper)
            and ("SCENARIO" in upper or "STEP" in upper or "HOOK-ERROR" in upper or "ERROR:" in upper)
        )
    ):
        return "error"
    if "| WARNING" in upper or stripped.startswith("WARN") or "运行已手动停止" in stripped:
        return "warning"
    if "| DEBUG" in upper:
        return "debug"
    if (
        stripped.startswith(("Feature:", "Rule:", "Scenario:", "Scenario Outline:", "Single-step debug", "Scenario rerun", "Rule rerun", "--- Generated", "=== "))
    ):
        return "heading"
    return "normal"


def state_file_path(artifacts_dir: Path | None = None) -> Path:
    return Path(artifacts_dir or Paths.ARTIFACTS_DIR) / _STATE_FILE_NAME


def load_runner_ui_state(artifacts_dir: Path | None = None) -> dict[str, Any]:
    path = state_file_path(artifacts_dir)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def save_runner_ui_state(
        state: dict[str, Any],
        *,
        artifacts_dir: Path | None = None,
) -> None:
    path = state_file_path(artifacts_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(path)
    except OSError:
        pass


def config_from_dict(
        data: dict[str, Any] | None,
        *,
        fallback: FeatureRunConfig | None = None,
) -> FeatureRunConfig:
    base = asdict(fallback or local_debug_preset_config())
    if isinstance(data, dict):
        valid_keys = {f.name for f in fields(FeatureRunConfig)}
        for key, value in data.items():
            if key in valid_keys and value is not None:
                base[key] = value
    try:
        return FeatureRunConfig(**base)
    except Exception:
        return fallback or local_debug_preset_config()


def build_execution_payload(target: FeatureRunTarget, config: FeatureRunConfig) -> dict[str, Any]:
    tags_value = config.tags.strip() if target.supports_tag_filter() else ""
    return {
        "feature_path": target.feature_path,
        "settings_overrides": config.to_settings_overrides(),
        "verbose": bool(config.verbose),
        "formatter": config.formatter.strip() or None,
        "scenario_name": target.scenario_name or None,
        "example_id": target.example_id or None,
        "rule_name": target.rule_name or None,
        "rule_line": target.rule_line,
        "scenario_line": target.scenario_line,
        "single_step_index": target.single_step_index,
        "single_step_mode": target.single_step_mode,
        "tags": tags_value or None,
        "clear_screenshots": bool(config.clear_screenshots),
    }


def execute_payload(payload: dict[str, Any]) -> int:
    from autowork_core.runtime.local_runner import main as local_runner_main

    return int(local_runner_main(
        feature_path=payload.get("feature_path"),
        settings_overrides=payload.get("settings_overrides"),
        verbose=bool(payload.get("verbose", True)),
        formatter=payload.get("formatter"),
        scenario_name=payload.get("scenario_name"),
        example_id=payload.get("example_id"),
        rule_name=payload.get("rule_name"),
        rule_line=payload.get("rule_line"),
        scenario_line=payload.get("scenario_line"),
        single_step_index=payload.get("single_step_index"),
        single_step_mode=payload.get("single_step_mode") or "previous",
        tags=payload.get("tags"),
        clear_screenshots=bool(payload.get("clear_screenshots", True)),
    ) or 0)


class SubprocessFeatureRunner:
    """Executes one BDD run in an isolated child process with live log streaming."""

    def __init__(
            self,
            *,
            python_executable: str | None = None,
            project_root: Path | None = None,
    ):
        self.python_executable = python_executable or sys.executable
        self.project_root = Path(project_root or Paths.BASE_DIR).resolve()
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._payload_path: Path | None = None
        self._current_target: FeatureRunTarget | None = None
        self._stop_requested = False
        self._started_at = 0.0
        self.events: queue.Queue[ConsoleEvent] = queue.Queue()

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None

    @property
    def elapsed_seconds(self) -> float:
        with self._lock:
            if self._started_at <= 0:
                return 0.0
            return max(0.0, time.monotonic() - self._started_at)

    def start(self, target: FeatureRunTarget, config: FeatureRunConfig) -> None:
        with self._lock:
            if self.is_running:
                raise RuntimeError("已有用例正在运行中，请先等待结束或点击停止。")
            while not self.events.empty():
                try:
                    self.events.get_nowait()
                except queue.Empty:
                    break

            payload = build_execution_payload(target, config)
            Paths.ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix=".runner_payload_",
                suffix=".json",
                dir=Paths.ARTIFACTS_DIR,
                delete=False,
            ) as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                payload_path = Path(stream.name)

            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"
            env["PYTHONUNBUFFERED"] = "1"
            existing_pythonpath = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = (
                f"{self.project_root}{os.pathsep}{existing_pythonpath}"
                if existing_pythonpath
                else str(self.project_root)
            )

            cmd = [
                self.python_executable,
                "-B",
                "-u",
                "-m",
                "autowork_core.utils.debug_tools.feature_runner_runtime",
                "--run-payload",
                str(payload_path),
            ]

            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(self.project_root),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=env,
                    creationflags=creationflags,
                )
            except Exception:
                payload_path.unlink(missing_ok=True)
                raise

            self._process = proc
            self._payload_path = payload_path
            self._current_target = target
            self._stop_requested = False
            self._started_at = time.monotonic()

            banner = f"=== 开始运行: {describe_run_target(target, base_dir=self.project_root)} ==="
            self.events.put(ConsoleEvent(kind="line", text=banner, level_tag="heading"))

            reader = threading.Thread(
                target=self._stream_worker,
                args=(proc, payload_path, target, self._started_at),
                name="autowork-feature-runner-reader",
                daemon=True,
            )
            reader.start()

    def stop(self) -> bool:
        with self._lock:
            proc = self._process
            target = self._current_target
            if proc is None or proc.poll() is not None:
                return False
            self._stop_requested = True
            pid = proc.pid

        if os.name == "nt" and pid:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    check=False,
                )
            except Exception:
                pass
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

        if target is not None:
            try:
                cleanup_temporary_features(target.feature_path)
            except Exception:
                pass
        return True

    def _stream_worker(
            self,
            proc: subprocess.Popen,
            payload_path: Path,
            target: FeatureRunTarget,
            started_at: float,
    ) -> None:
        exit_code = -1
        try:
            if proc.stdout is not None:
                for raw_line in proc.stdout:
                    cleaned = strip_ansi(raw_line).rstrip("\r\n")
                    self.events.put(ConsoleEvent(
                        kind="line",
                        text=cleaned,
                        level_tag=classify_console_line(cleaned),
                    ))
            exit_code = proc.wait()
        except Exception as exc:
            self.events.put(ConsoleEvent(
                kind="error",
                text=f"读取运行输出异常: {type(exc).__name__}: {exc}",
                level_tag="error",
            ))
        finally:
            duration = max(0.0, time.monotonic() - started_at)
            payload_path.unlink(missing_ok=True)
            with self._lock:
                stopped = self._stop_requested
                if self._process is proc:
                    self._process = None
                    self._payload_path = None
            if stopped:
                try:
                    cleanup_temporary_features(target.feature_path)
                except Exception:
                    pass
            footer = (
                f"=== 运行已手动停止 (耗时 {duration:.2f}s) ==="
                if stopped
                else f"=== 运行结束: {'通过' if exit_code == 0 else '失败'} "
                     f"(exit_code={exit_code}, 耗时 {duration:.2f}s) ==="
            )
            footer_tag = "warning" if stopped else ("success" if exit_code == 0 else "error")
            self.events.put(ConsoleEvent(kind="line", text=footer, level_tag=footer_tag))
            self.events.put(ConsoleEvent(
                kind="finished",
                exit_code=exit_code,
                stopped=stopped,
                duration_seconds=duration,
            ))


def _cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BDD Autowork Feature Runner subprocess entrypoint")
    parser.add_argument("--run-payload", required=True, help="Path to JSON execution payload")
    args = parser.parse_args(argv)
    payload_file = Path(args.run_payload).resolve()
    payload = json.loads(payload_file.read_text(encoding="utf-8"))
    try:
        return execute_payload(payload)
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else (0 if code is None else 1)
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(_cli_main())
