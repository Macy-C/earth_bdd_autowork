from pathlib import Path
from config.paths import Paths
import yaml
from autowork_core.utils.bus import get_screen_size


OCR_MODE_PRESETS = {
    "fast": {
        "CACHE_TTL": 1.0,
        "DET_LIMIT_SIDE_LEN": 960,
        "DET_LIMIT_TYPE": "max",
        "DET_BOX_THRESH": 0.8,
        "DET_UNCLIP_RATIO": 1.5,
        "RECOGNITION_BATCH_SIZE": 8,
        "TIMEOUT": 5,
        "INTERVAL": 0.3,
        "MATCH_MODE": "smart",
    },
    "balanced": {
        "CACHE_TTL": 2.0,
        "DET_LIMIT_SIDE_LEN": 960,
        "DET_LIMIT_TYPE": "max",
        "DET_BOX_THRESH": 0.8,
        "DET_UNCLIP_RATIO": 1.5,
        "RECOGNITION_BATCH_SIZE": 8,
        "TIMEOUT": 5,
        "INTERVAL": 0.5,
        "MATCH_MODE": "smart",
    },
    "accurate": {
        "CACHE_TTL": 0.0,
        "DET_LIMIT_SIDE_LEN": 1600,
        "DET_LIMIT_TYPE": "max",
        "DET_BOX_THRESH": 0.6,
        "DET_UNCLIP_RATIO": 1.6,
        "RECOGNITION_BATCH_SIZE": 4,
        "TIMEOUT": 10,
        "INTERVAL": 0.5,
        "MATCH_MODE": "smart",
    },
}

PIC_MODE_PRESETS = {
    "fast": {
        "THRESHOLD": 0.6,
        "POS": 5,
        "RGB": True,
        "METHOD": "tpl",
        "TIMEOUT": 5,
        "INTERVAL": 0.2,
        "DEBUG_ON_FAIL": True,
        "SCALE_MAX": 600,
        "SCALE_STEP": 0.01,
    },
    "balanced": {
        "THRESHOLD": 0.6,
        "POS": 5,
        "RGB": True,
        "METHOD": "",
        "TIMEOUT": 5,
        "INTERVAL": 0.2,
        "DEBUG_ON_FAIL": True,
        "SCALE_MAX": 800,
        "SCALE_STEP": 0.005,
    },
    "accurate": {
        "THRESHOLD": 0.7,
        "POS": 5,
        "RGB": True,
        "METHOD": "mstpl",
        "TIMEOUT": 10,
        "INTERVAL": 0.3,
        "DEBUG_ON_FAIL": True,
        "SCALE_MAX": 1000,
        "SCALE_STEP": 0.005,
    },
}


def _as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("0", "false", "no", "n", "否")


def _as_int(value, default=0):
    try:
        return int(float(value))
    except Exception:
        return default


def _as_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def _normalize_record_mode(value, default="off"):
    if isinstance(value, bool):
        return "all" if value else "off"

    mode = str(value if value is not None else default).strip().lower()
    aliases = {
        "1": "all",
        "true": "all",
        "yes": "all",
        "y": "all",
        "on": "all",
        "always": "all",
        "all": "all",
        "failed": "failed",
        "fail": "failed",
        "failure": "failed",
        "on_fail": "failed",
        "on-fail": "failed",
        "0": "off",
        "false": "off",
        "no": "off",
        "n": "off",
        "off": "off",
        "none": "off",
    }
    return aliases.get(mode, default)


def _normalize_config_keys(config):
    return {str(key).strip().upper(): value for key, value in (config or {}).items()}


def _apply_mode_defaults(config, presets, default_mode="balanced"):
    config = _normalize_config_keys(config)
    mode = str(config.get("MODE", default_mode)).strip().lower()
    if mode not in presets:
        mode = default_mode
    merged = dict(presets[mode])
    merged.update(config)
    merged["MODE"] = mode
    return merged


class Settings:
    def __init__(self, yaml_path=None):
        self.yaml_path = (
            Path(yaml_path)
            if yaml_path
            else Paths.PROJECT_CONFIG_FILE
        )

        # ===================== 默认值 =====================
        # log
        self.log_file = "logs.logs"
        self.log_level = "INFO"
        self.log_rotation = "20 MB"
        self.log_retention = "3 days"

        # run mode
        self.app_launch_mode = "auto"   # auto / attach
        self.draw_outline = False
        self.record_mode = "off"        # off / failed / all
        self.record_fps = 5
        self.record_buffer_seconds = 8.0
        self.record_segment_seconds = 2.0
        self.record_max_width = 1280
        self.record_monitor_index = 1
        self.record_ffmpeg_path = ""
        self.record_ffmpeg_preset = "ultrafast"
        self.record_ffmpeg_crf = 28
        self.record_ffmpeg_draw_mouse = True
        self.run_indicator_enabled = True
        self.run_indicator_max_scenario_chars = 28
        self.spark_report = True
        self.desktop_size = get_screen_size()

        # ocr
        self.ocr_mode = "balanced"
        self.ocr_warmup = True
        self.ocr_cache_ttl = 2.0
        self.ocr_det_limit_side_len = 960
        self.ocr_det_limit_type = "max"
        self.ocr_det_box_thresh = 0.8
        self.ocr_det_unclip_ratio = 1.5
        self.ocr_recognition_batch_size = 8
        self.ocr_timeout = 5.0
        self.ocr_interval = 0.5
        self.ocr_match_mode = "smart"
        self.ocr_debug_on_fail = True

        # picture recognition
        self.pic_mode = "balanced"
        self.pic_threshold = 0.6
        self.pic_pos = 5
        self.pic_rgb = True
        self.pic_method = ""
        self.pic_timeout = 5.0
        self.pic_interval = 0.2
        self.pic_debug_on_fail = True
        self.pic_scale_max = 800
        self.pic_scale_step = 0.005

        # app
        self.app_path = ""
        self.backend = "uia"
        self.app_process_track_mode = "snapshot"

        # db
        self.mysql_conn_config = None
        self.oracle_conn_config = None
        self.postgresql_conn_config = None

        # 原始配置
        self.raw = {}

        # 加载 yaml
        self.load()

    def load(self):
        if not self.yaml_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {self.yaml_path}")

        with open(self.yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        self.raw = data

        # ===================== log =====================
        log_conf = data.get("log", {}) or {}
        self.log_file = log_conf.get("file", self.log_file)
        self.log_level = log_conf.get("level", self.log_level)
        self.log_rotation = log_conf.get("rotation", self.log_rotation)
        self.log_retention = log_conf.get("retention", self.log_retention)

        # ===================== runtime =====================
        self.app_launch_mode = str(data.get("APP_LAUNCH_MODE", self.app_launch_mode)).strip().lower()
        self.draw_outline = _as_bool(data.get("draw_outline_location", self.draw_outline), self.draw_outline)
        record_conf = _normalize_config_keys(data.get("RECORD_SETTING", {}) or {})
        self.record_mode = _normalize_record_mode(record_conf.get("MODE", self.record_mode), self.record_mode)
        self.record_fps = max(1, _as_int(record_conf.get("FPS", self.record_fps), self.record_fps))
        self.record_buffer_seconds = max(1.0, _as_float(record_conf.get("BUFFER_SECONDS", self.record_buffer_seconds), self.record_buffer_seconds))
        self.record_segment_seconds = max(1.0, _as_float(record_conf.get("SEGMENT_SECONDS", self.record_segment_seconds), self.record_segment_seconds))
        self.record_max_width = max(0, _as_int(record_conf.get("MAX_WIDTH", self.record_max_width), self.record_max_width))
        self.record_monitor_index = max(1, _as_int(record_conf.get("MONITOR_INDEX", self.record_monitor_index), self.record_monitor_index))
        self.record_ffmpeg_path = str(record_conf.get("FFMPEG_PATH", self.record_ffmpeg_path) or "").strip()
        self.record_ffmpeg_preset = str(record_conf.get("FFMPEG_PRESET", self.record_ffmpeg_preset) or self.record_ffmpeg_preset).strip()
        self.record_ffmpeg_crf = _as_int(record_conf.get("FFMPEG_CRF", self.record_ffmpeg_crf), self.record_ffmpeg_crf)
        self.record_ffmpeg_draw_mouse = _as_bool(record_conf.get("FFMPEG_DRAW_MOUSE", self.record_ffmpeg_draw_mouse), self.record_ffmpeg_draw_mouse)
        run_indicator_conf = _normalize_config_keys(
            data.get("RUN_INDICATOR_SETTING", {}) or {}
        )
        self.run_indicator_enabled = _as_bool(
            run_indicator_conf.get("ENABLED", self.run_indicator_enabled),
            self.run_indicator_enabled,
        )
        self.run_indicator_max_scenario_chars = max(
            8,
            _as_int(
                run_indicator_conf.get(
                    "MAX_SCENARIO_CHARS",
                    self.run_indicator_max_scenario_chars,
                ),
                self.run_indicator_max_scenario_chars,
            ),
        )
        self.spark_report = _as_bool(data.get("SPARK_REPORT", self.spark_report), self.spark_report)

        # ===================== ocr =====================
        ocr_conf = _apply_mode_defaults(data.get("OCR_SETTING", {}) or {}, OCR_MODE_PRESETS)
        self.ocr_mode = ocr_conf.get("MODE", self.ocr_mode)
        self.ocr_warmup = _as_bool(ocr_conf.get("WARMUP", self.ocr_warmup), self.ocr_warmup)
        self.ocr_cache_ttl = float(ocr_conf.get("CACHE_TTL", self.ocr_cache_ttl))
        self.ocr_det_limit_side_len = int(ocr_conf.get("DET_LIMIT_SIDE_LEN", self.ocr_det_limit_side_len))
        self.ocr_det_limit_type = str(ocr_conf.get("DET_LIMIT_TYPE", self.ocr_det_limit_type)).strip().lower()
        self.ocr_det_box_thresh = float(ocr_conf.get("DET_BOX_THRESH", self.ocr_det_box_thresh))
        self.ocr_det_unclip_ratio = float(ocr_conf.get("DET_UNCLIP_RATIO", self.ocr_det_unclip_ratio))
        self.ocr_recognition_batch_size = int(ocr_conf.get("RECOGNITION_BATCH_SIZE", self.ocr_recognition_batch_size))
        self.ocr_timeout = float(ocr_conf.get("TIMEOUT", self.ocr_timeout))
        self.ocr_interval = float(ocr_conf.get("INTERVAL", self.ocr_interval))
        self.ocr_match_mode = str(ocr_conf.get("MATCH_MODE", self.ocr_match_mode)).strip().lower()
        self.ocr_debug_on_fail = _as_bool(ocr_conf.get("DEBUG_ON_FAIL", self.ocr_debug_on_fail), self.ocr_debug_on_fail)

        # ===================== picture recognition =====================
        pic_conf = _apply_mode_defaults(data.get("PIC_SETTING", {}) or {}, PIC_MODE_PRESETS)
        self.pic_mode = pic_conf.get("MODE", self.pic_mode)
        self.pic_threshold = float(pic_conf.get("THRESHOLD", self.pic_threshold))
        self.pic_pos = int(pic_conf.get("POS", self.pic_pos))
        self.pic_rgb = _as_bool(pic_conf.get("RGB", self.pic_rgb), self.pic_rgb)
        self.pic_method = str(pic_conf.get("METHOD", self.pic_method)).strip().lower()
        self.pic_timeout = float(pic_conf.get("TIMEOUT", self.pic_timeout))
        self.pic_interval = float(pic_conf.get("INTERVAL", self.pic_interval))
        self.pic_debug_on_fail = _as_bool(pic_conf.get("DEBUG_ON_FAIL", self.pic_debug_on_fail), self.pic_debug_on_fail)
        self.pic_scale_max = int(pic_conf.get("SCALE_MAX", self.pic_scale_max))
        self.pic_scale_step = float(pic_conf.get("SCALE_STEP", self.pic_scale_step))

        # ===================== app =====================
        app_conf = data.get("APP_SETTING", {}) or {}
        self.app_path = app_conf.get("APP_PATH", self.app_path)
        self.backend = str(app_conf.get("BACKEND", self.backend)).strip().lower()
        self.app_process_track_mode = str(app_conf.get("PROCESS_TRACK_MODE", self.app_process_track_mode)).strip().lower()

        # ===================== db =====================
        self.mysql_conn_config = data.get("mysql_conn_config", self.mysql_conn_config)
        self.oracle_conn_config = data.get("oracle_conn_config", self.oracle_conn_config)
        self.postgresql_conn_config = data.get("postgresql_conn_config", self.postgresql_conn_config)

    def reload(self):
        self.load()

    def update(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

    @property
    def is_attach_mode(self):
        return self.app_launch_mode == "attach"

    @property
    def is_auto_mode(self):
        return self.app_launch_mode == "auto"

    @property
    def effective_record_mode(self):
        return _normalize_record_mode(self.record_mode, "off")

    @property
    def record_enabled(self):
        return self.effective_record_mode != "off"

    @property
    def record_all(self):
        return self.record_enabled and self.effective_record_mode == "all"

    @property
    def record_failed(self):
        return self.record_enabled and self.effective_record_mode == "failed"

    def as_dict(self):
        return {
            "log_file": self.log_file,
            "log_level": self.log_level,
            "log_rotation": self.log_rotation,
            "log_retention": self.log_retention,
            "app_launch_mode": self.app_launch_mode,
            "draw_outline": self.draw_outline,
            "record_mode": self.record_mode,
            "record_fps": self.record_fps,
            "record_buffer_seconds": self.record_buffer_seconds,
            "record_segment_seconds": self.record_segment_seconds,
            "record_max_width": self.record_max_width,
            "record_monitor_index": self.record_monitor_index,
            "record_ffmpeg_path": self.record_ffmpeg_path,
            "record_ffmpeg_preset": self.record_ffmpeg_preset,
            "record_ffmpeg_crf": self.record_ffmpeg_crf,
            "record_ffmpeg_draw_mouse": self.record_ffmpeg_draw_mouse,
            "run_indicator_enabled": self.run_indicator_enabled,
            "run_indicator_max_scenario_chars": (
                self.run_indicator_max_scenario_chars
            ),
            "spark_report": self.spark_report,
            "desktop_size": self.desktop_size,
            "ocr_mode": self.ocr_mode,
            "ocr_warmup": self.ocr_warmup,
            "ocr_cache_ttl": self.ocr_cache_ttl,
            "ocr_det_limit_side_len": self.ocr_det_limit_side_len,
            "ocr_det_limit_type": self.ocr_det_limit_type,
            "ocr_det_box_thresh": self.ocr_det_box_thresh,
            "ocr_det_unclip_ratio": self.ocr_det_unclip_ratio,
            "ocr_recognition_batch_size": self.ocr_recognition_batch_size,
            "ocr_timeout": self.ocr_timeout,
            "ocr_interval": self.ocr_interval,
            "ocr_match_mode": self.ocr_match_mode,
            "ocr_debug_on_fail": self.ocr_debug_on_fail,
            "pic_mode": self.pic_mode,
            "pic_threshold": self.pic_threshold,
            "pic_pos": self.pic_pos,
            "pic_rgb": self.pic_rgb,
            "pic_method": self.pic_method,
            "pic_timeout": self.pic_timeout,
            "pic_interval": self.pic_interval,
            "pic_debug_on_fail": self.pic_debug_on_fail,
            "pic_scale_max": self.pic_scale_max,
            "pic_scale_step": self.pic_scale_step,
            "app_path": self.app_path,
            "backend": self.backend,
            "app_process_track_mode": self.app_process_track_mode,
            "mysql_conn_config": self.mysql_conn_config,
            "oracle_conn_config": self.oracle_conn_config,
            "postgresql_conn_config": self.postgresql_conn_config,
        }


settings = Settings()

