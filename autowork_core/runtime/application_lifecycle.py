"""注册并分发当前 Step scope 的应用启停生命周期回调。"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from autowork_core.runtime.tag_manager import normalize_tag


LifecycleCallback = Callable[[Any, Any], Any]


class ApplicationLifecyclePhase(str, Enum):
    BEFORE_APP_START = "before_app_start"
    AFTER_APP_START = "after_app_start"
    AFTER_APP_STOP = "after_app_stop"


class ApplicationLifecycleConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class RegisteredLifecycleCallback:
    callback: LifecycleCallback
    required_tags: frozenset[str]
    source_file: Path


@dataclass(frozen=True)
class ApplicationLifecycleState:
    callbacks: dict[ApplicationLifecyclePhase, tuple[RegisteredLifecycleCallback, ...]]


_CALLBACKS = {
    phase: []
    for phase in ApplicationLifecyclePhase
}


def before_app_start(callback=None, *, tags=None):
    """在框架启动当前 Scenario 的应用之前运行回调。"""
    return _lifecycle_decorator(
        ApplicationLifecyclePhase.BEFORE_APP_START,
        callback,
        tags=tags,
    )


def after_app_start(callback=None, *, tags=None):
    """在当前 Scenario 的应用启动命令成功返回后运行回调。"""
    return _lifecycle_decorator(
        ApplicationLifecyclePhase.AFTER_APP_START,
        callback,
        tags=tags,
    )


def after_app_stop(callback=None, *, tags=None):
    """在框架完成当前 Scenario 的应用清理之后运行回调。"""
    return _lifecycle_decorator(
        ApplicationLifecyclePhase.AFTER_APP_STOP,
        callback,
        tags=tags,
    )


def run_application_lifecycle(phase, context, scenario):
    phase = ApplicationLifecyclePhase(phase)
    for registration in callbacks_for(phase, scenario):
        registration.callback(context, scenario)


def callbacks_for(phase, scenario):
    phase = ApplicationLifecyclePhase(phase)
    return _callbacks_for_registrations(_CALLBACKS[phase], scenario)


def _callbacks_for_registrations(registrations, scenario):
    effective_tags = {
        normalize_tag(tag)
        for tag in getattr(scenario, "effective_tags", ())
    }
    return tuple(
        registration
        for registration in registrations
        if registration.required_tags.issubset(effective_tags)
    )


def has_application_lifecycle_callbacks(scenario):
    return any(
        callbacks_for(phase, scenario)
        for phase in ApplicationLifecyclePhase
    )


def state_has_application_lifecycle_callbacks(state, scenario):
    return any(
        _callbacks_for_registrations(state.callbacks.get(phase, ()), scenario)
        for phase in ApplicationLifecyclePhase
    )


def validate_application_lifecycle_sources(step_files):
    allowed_files = {Path(path).resolve() for path in step_files}
    invalid = [
        (phase, registration)
        for phase, registrations in _CALLBACKS.items()
        for registration in registrations
        if registration.source_file not in allowed_files
    ]
    if not invalid:
        return

    details = ", ".join(
        f"{registration.callback.__qualname__} ({phase.value}, "
        f"{registration.source_file})"
        for phase, registration in invalid
    )
    raise ApplicationLifecycleConfigurationError(
        "Application lifecycle callbacks must be defined directly in the "
        f"current scoped Step file: {details}"
    )


def prepare_application_lifecycle(context, scenario, *, launch_mode):
    if not has_application_lifecycle_callbacks(scenario):
        return
    if str(launch_mode).strip().lower() == "attach":
        raise ApplicationLifecycleConfigurationError(
            "Application lifecycle callbacks require app_launch_mode='auto'; "
            "attach mode has no framework-owned application start/stop boundary"
        )

    context.autowork_scenario.application_lifecycle_active = True
    run_application_lifecycle(
        ApplicationLifecyclePhase.BEFORE_APP_START,
        context,
        scenario,
    )


def notify_application_started(context, scenario):
    if context.autowork_scenario.application_lifecycle_active:
        run_application_lifecycle(
            ApplicationLifecyclePhase.AFTER_APP_START,
            context,
            scenario,
        )


def finish_application_lifecycle(context, scenario):
    state = context.autowork_scenario
    if not state.application_lifecycle_active:
        return
    errors = []
    try:
        for registration in callbacks_for(
                ApplicationLifecyclePhase.AFTER_APP_STOP,
                scenario,
        ):
            try:
                registration.callback(context, scenario)
            except Exception as error:
                errors.append(error)
    finally:
        state.application_lifecycle_active = False
    _raise_collected_errors("after_app_stop callbacks failed", errors)


def snapshot_application_lifecycle():
    return ApplicationLifecycleState(
        callbacks={
            phase: tuple(registrations)
            for phase, registrations in _CALLBACKS.items()
        }
    )


def restore_application_lifecycle(state):
    reset_application_lifecycle()
    for phase in ApplicationLifecyclePhase:
        _CALLBACKS[phase].extend(state.callbacks.get(phase, ()))


def reset_application_lifecycle():
    for registrations in _CALLBACKS.values():
        registrations.clear()


def _lifecycle_decorator(phase, callback, *, tags):
    required_tags = _normalize_required_tags(tags)

    def register(func):
        source_file = Path(inspect.getsourcefile(func) or func.__code__.co_filename).resolve()
        registration = RegisteredLifecycleCallback(func, required_tags, source_file)
        if registration not in _CALLBACKS[phase]:
            _CALLBACKS[phase].append(registration)
        return func

    if callback is None:
        return register
    return register(callback)


def _normalize_required_tags(tags):
    if tags is None:
        return frozenset()
    values: Iterable[str] = (tags,) if isinstance(tags, str) else tags
    normalized = frozenset(normalize_tag(tag) for tag in values)
    if "" in normalized:
        raise ValueError("Application lifecycle tags cannot be empty")
    return normalized


def _raise_collected_errors(message, errors):
    if not errors:
        return
    if len(errors) == 1:
        raise errors[0]
    raise ExceptionGroup(message, errors)


__all__ = (
    "ApplicationLifecycleConfigurationError",
    "ApplicationLifecyclePhase",
    "after_app_start",
    "after_app_stop",
    "before_app_start",
)