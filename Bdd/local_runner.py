import sys
from autowork_core.runtime.local_runner import main

if __name__ == "__main__":
    feature_path = r'Bdd\test_features\...'
    debug_settings_overrides = {
        # 应用启动方式
        "app_launch_mode": "attach",  # auto / attach
        "app_path" : '', 
        "backend" : "uia",
        "app_process_track_mode": "snapshot",  # snapshot / root / none

        # 日志和现场
        "log_level": "DEBUG",  # DEBUG / INFO / WARNING / ERROR
        "draw_outline": True,  # 找到元素后画蓝框
        "record_mode": "failed",  # off / failed / all
        "record_buffer_seconds": 10.0 ,

        # OCR 调试
        "ocr_warmup": True,  # 调试时可关，启动更快
        "ocr_cache_ttl": 0.0,  # 关闭 OCR 缓存，避免界面变化后复用旧结果
        "ocr_timeout": 10,
        "ocr_interval": 0.3,
        "ocr_match_mode": "smart",  # smart / contains / exact
        "ocr_debug_on_fail": True,

        # 图片识别调试
        "pic_threshold": 0.7,
        "pic_method": "",  # "" / tpl / mstpl / gmstpl / kaze / brisk / akaze / orb 等
        "pic_timeout": 10,
        "pic_interval": 0.2,
        "pic_debug_on_fail": True,
        "pic_scale_max": 1000,
        "pic_scale_step": 0.005,
    }

    raise SystemExit(main(
        feature_path=sys.argv[1] if len(sys.argv) > 1 else feature_path,
        settings_overrides=debug_settings_overrides,
        verbose=True,
        formatter="progress2",  # progress2 / pretty
    ))
