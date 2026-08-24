from __future__ import annotations

import hashlib
import json
from pathlib import Path


EXECUTION_PROFILE_VERSION = "1.0"
EXECUTION_MODES = {
    "not_configured",
    "attach_existing",
    "launch",
    "external_manual",
}
PROCESS_TRACK_MODES = {"snapshot", "root", "none"}


class GenerationExecutionUnavailable(RuntimeError):
    pass


def normalize_execution_profile(value=None):
    value = dict(value or {})
    allowed = {
        "execution_profile_version",
        "mode",
        "runtime_policy",
        "app_path",
        "process_track_mode",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(
            "Execution Profile包含未知字段: " + ", ".join(unknown)
        )
    version = str(
        value.get("execution_profile_version")
        or EXECUTION_PROFILE_VERSION
    )
    if version != EXECUTION_PROFILE_VERSION:
        raise ValueError(f"Execution Profile版本不支持: {version}")
    mode = str(value.get("mode") or "not_configured").strip().casefold()
    if mode not in EXECUTION_MODES:
        raise ValueError(f"Execution Profile mode无效: {mode}")

    result = {
        "execution_profile_version": EXECUTION_PROFILE_VERSION,
        "mode": mode,
        "runtime_policy": (
            "allowed"
            if mode in {"attach_existing", "launch"}
            else "manual_only"
            if mode == "external_manual"
            else "static_only"
        ),
    }
    declared_policy = str(value.get("runtime_policy") or "")
    if declared_policy and declared_policy != result["runtime_policy"]:
        raise ValueError(
            "Execution Profile runtime_policy与mode不一致: "
            f"{declared_policy}/{mode}"
        )
    if mode == "launch":
        app_path = str(value.get("app_path") or "").strip()
        if not app_path:
            raise ValueError("launch Execution Profile必须提供app_path")
        if app_path.casefold() == "runtime":
            raise ValueError(
                "launch Execution Profile不能使用全局runtime Hook；"
                "请提供明确应用命令"
            )
        process_track_mode = str(
            value.get("process_track_mode") or "root"
        ).strip().casefold()
        if process_track_mode not in PROCESS_TRACK_MODES:
            raise ValueError(
                "Execution Profile process_track_mode无效: "
                f"{process_track_mode}"
            )
        result.update({
            "app_path": app_path,
            "process_track_mode": process_track_mode,
        })
    elif any(
            value.get(key) not in (None, "")
            for key in ("app_path", "process_track_mode")
    ):
        raise ValueError(f"{mode} Execution Profile不能声明启动参数")
    return result


def execution_profile_fingerprint(profile):
    normalized = normalize_execution_profile(profile)
    payload = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def execution_settings(profile):
    normalized = normalize_execution_profile(profile)
    mode = normalized["mode"]
    if mode == "attach_existing":
        return {
            "app_launch_mode": "attach",
            "app_process_track_mode": "none",
        }
    if mode == "launch":
        return {
            "app_launch_mode": "auto",
            "app_path": normalized["app_path"],
            "app_process_track_mode": normalized["process_track_mode"],
        }
    raise GenerationExecutionUnavailable(
        "Generation Request未授权自动运行: "
        f"mode={mode}; 静态事务可完成，但不得猜测启动或attach方式"
    )


def execution_settings_for_request(request_path, transaction_report):
    from autowork_core.runtime.reporting.run_result_bridge import (
        load_generation_provenance,
    )
    from autowork_core.utils.debug_tools.recorder.request_repository import (
        request_identity_is_valid,
    )

    request_path = Path(request_path).resolve()
    transaction_report = Path(transaction_report).resolve()
    request = json.loads(request_path.read_text(encoding="utf-8"))
    report = json.loads(transaction_report.read_text(encoding="utf-8"))
    if not request_identity_is_valid(request):
        raise GenerationExecutionUnavailable(
            f"Generation Request身份无效: {request_path}"
        )
    try:
        provenance = load_generation_provenance(
            transaction_report,
            project_root=report.get("project_root"),
        )
    except (KeyError, OSError, TypeError, ValueError) as error:
        raise GenerationExecutionUnavailable(
            f"Generation Transaction验证失败: {error}"
        ) from error
    if any((
        provenance.get("request_id") != request.get("request_id"),
        provenance.get("request_fingerprint")
        != request.get("request_fingerprint"),
        Path(str(report.get("request_path") or "")).resolve()
        != request_path,
    )):
        raise GenerationExecutionUnavailable(
            "Execution Request与Generation Transaction身份不一致"
        )
    return execution_settings(request.get("execution"))


__all__ = [
    "EXECUTION_PROFILE_VERSION",
    "GenerationExecutionUnavailable",
    "execution_profile_fingerprint",
    "execution_settings",
    "execution_settings_for_request",
    "normalize_execution_profile",
]