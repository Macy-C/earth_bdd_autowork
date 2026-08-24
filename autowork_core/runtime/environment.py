"""Behave 生命周期 Hook 门面，保持 Hook 顺序并委托具体运行时组件。

Facade for Behave lifecycle hooks that preserves hook ordering and
delegates work to focused runtime components.
"""

from loguru import logger
from autowork_core.common.ocr_engine import warmup_ocr_engine
from autowork_core.runtime.application_lifecycle import (
    finish_application_lifecycle as _finish_application_lifecycle,
)
from autowork_core.runtime.context_runtime import (
    configure_logging as _set_log,
    create_run_state as _create_run_state,
    current_step_scope_label as _current_step_scope_label,
    feature_display_path as _feature_display_path,
)
from autowork_core.runtime.feature_runtime import create_feature_state as _create_feature_state
from autowork_core.runtime.status import should_keep_artifacts
from autowork_core.runtime.reporting import dispatch_report_hook
from autowork_core.runtime.tag_manager import (
    TAG_MANAGER,
    TagOwner,
    scenario_tag_owner,
)
from autowork_core.runtime.scenario_runtime import (
    ScenarioRuntimeState,
    close_app_and_release_resource as _close_app_and_release_resource,
    cleanup_project_scenario as _cleanup_project_scenario,
    finalize_process_tracking_before_step as _finalize_process_tracking_before_step,
    finish_scenario_recording as _finish_scenario_recording,
    initialize_ui_scenario as _initialize_ui_scenario,
    log_scenario_runtime_state as _log_scenario_runtime_state,
)
from config.settings import settings


__all__ = (
    "after_all",
    "after_feature",
    "after_scenario",
    "after_step",
    "before_all",
    "before_feature",
    "before_scenario",
    "before_step",
)


def before_all(context):
    _set_log()
    context.autowork_run = _create_run_state()
    _start_run_indicator(context)
    dispatch_report_hook(context.autowork_run.reporter, "before_all", context)
    logger.opt(colors=True).info(f"<green>{'《》' * 15} Test Start {'《》' * 15}</green>")
    if settings.ocr_warmup:
        try:
            warmup_ocr_engine()
        except Exception as e:
            logger.warning(f"OCR warmup failed: {e}")


def after_all(context):
    try:
        dispatch_report_hook(context.autowork_run.reporter, "after_all", context)
    finally:
        _close_run_indicator(context)
        logger.opt(colors=True).info(f"<green>{'《》' * 15} Test End {'《》' * 15}</green>")


def before_feature(context, feature):
    context.autowork_feature = _create_feature_state(context.autowork_run)
    logger.opt(colors=True).info(fr"<green>{'#' * 34} Feature Start: {feature.name} {'#' * 34}</green>")
    logger.info(f"Feature step file: {_feature_display_path(feature)} -> {_current_step_scope_label()}")
    decision = TAG_MANAGER.resolve_runtime(
        feature.tags,
        TagOwner.FEATURE,
        feature.name,
        effective_tags=feature.effective_tags,
    )
    dispatch_report_hook(context.autowork_run.reporter, "before_feature", context, feature)
    if decision.skip_reason:
        feature.skip()
        logger.opt(colors=True).info(
            f"<cyan>^^^^^^该 feature 标记了 @{decision.skip_reason} ,跳过执行</cyan>"
        )
        return

def after_feature(context, feature):
    dispatch_report_hook(context.autowork_run.reporter, "after_feature", context, feature)
    logger.opt(colors=True).info(fr"<green>{'#' * 34} Feature End: {feature.name} {'#' * 34}</green>")


def before_scenario(context, scenario):
    state = ScenarioRuntimeState()
    context.autowork_scenario = state
    logger.opt(colors=True).info(fr"<green>{'*' * 34} Scenario Start: {scenario.name} {'*' * 34}</green>")
    decision = TAG_MANAGER.resolve_runtime(
        scenario.tags,
        scenario_tag_owner(scenario),
        scenario.name,
        effective_tags=scenario.effective_tags,
    )
    state.tag_decision = decision
    dispatch_report_hook(context.autowork_run.reporter, "before_scenario", context, scenario)
    if decision.skip_reason:
        _hide_run_indicator(context)
        scenario.skip()
        logger.opt(colors=True).info(
            f"<cyan>^^^^^^该 scenario 标记了 @{decision.skip_reason} ,跳过执行</cyan>"
        )
        return

    if decision.api_only:
        _hide_run_indicator(context)
        logger.debug("该 scenario 标记了 @api，跳过应用和录屏初始化")
        return

    _show_run_indicator(context, scenario)
    try:
        _initialize_ui_scenario(context, scenario)
    except Exception:
        _hide_run_indicator(context)
        raise

def after_scenario(context, scenario):
    record_path = None
    errors = []
    try:
        record_path = _finish_scenario_recording(context, scenario)
    except Exception as error:
        errors.append(error)
    for cleanup in (
            lambda: _log_scenario_runtime_state(context),
            lambda: _cleanup_project_scenario(context, scenario),
    ):
        try:
            cleanup()
        except Exception as error:
            errors.append(error)
    state = getattr(context, "autowork_scenario", None)
    if state is not None and state.application_cleanup_required:
        try:
            _close_app_and_release_resource(context)
        except Exception as error:
            errors.append(error)
    if state is not None:
        try:
            _finish_application_lifecycle(context, scenario)
        except Exception as error:
            errors.append(error)
    dispatch_report_hook(context.autowork_run.reporter,"after_scenario",context,scenario,record_path=record_path,)
    logger.opt(colors=True).info(fr"<green>{'*' * 34} Scenario End: {scenario.name} {'*' * 34}</green>")
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise ExceptionGroup("Scenario cleanup failed", errors)


def before_step(context, step):
    _finalize_process_tracking_before_step(context)
    logger.opt(colors=True).info(fr"<green>proceed step ------ {step.name}</green>")
    dispatch_report_hook(context.autowork_run.reporter, "before_step", context, step)


def after_step(context, step):
    screenshot_path = None
    screenshot_error = None
    if should_keep_artifacts(step.status):
        screenshot_name = f"{context.feature.name}_{context.scenario.name}_{step.name}"
        try:
            screenshot_path = context.autowork_run.recorder.save_screenshot(screenshot_name)
            logger.error(f"[步骤执行失败-截图已保存] {screenshot_path}")
        except Exception as e:
            screenshot_error = f"{type(e).__name__}: {e}"
            logger.warning(f"[步骤执行失败-截图保存失败] name={screenshot_name}, err={screenshot_error}")
    dispatch_report_hook(context.autowork_run.reporter,"after_step",context,step,screenshot_path=screenshot_path,screenshot_error=screenshot_error,)








def _start_run_indicator(context):
    if not settings.run_indicator_enabled:
        return
    indicator = getattr(context.autowork_run, "run_indicator", None)
    if indicator is None:
        return
    try:
        indicator.start()
    except Exception as error:
        logger.warning(
            "自动化运行提示计时启动失败，不影响用例执行: "
            f"{type(error).__name__}: {error}"
        )


def _show_run_indicator(context, scenario):
    if not settings.run_indicator_enabled:
        return False
    indicator = getattr(context.autowork_run, "run_indicator", None)
    if indicator is None:
        return False
    try:
        visible = bool(indicator.show(scenario.name))
        context.autowork_run.run_indicator_visible = visible
        return visible
    except Exception as error:
        logger.warning(
            "自动化运行提示显示失败，不影响用例执行: "
            f"{type(error).__name__}: {error}"
        )
        return False


def _hide_run_indicator(context):
    if not getattr(context.autowork_run, "run_indicator_visible", False):
        return
    indicator = getattr(context.autowork_run, "run_indicator", None)
    if indicator is None:
        return
    try:
        indicator.hide()
        context.autowork_run.run_indicator_visible = False
    except Exception as error:
        logger.warning(
            "自动化运行提示隐藏失败，不影响用例执行: "
            f"{type(error).__name__}: {error}"
        )


def _close_run_indicator(context):
    indicator = getattr(context.autowork_run, "run_indicator", None)
    if indicator is None:
        return
    try:
        indicator.close()
        context.autowork_run.run_indicator_visible = False
    except Exception as error:
        logger.warning(
            "自动化运行提示关闭失败，不影响用例执行: "
            f"{type(error).__name__}: {error}"
        )
