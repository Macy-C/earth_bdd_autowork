from __future__ import annotations

import ast
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from autowork_core.common.runtime_diagnostics import (
    runtime_diagnostic_from_exception,
    runtime_diagnostic_payload,
    runtime_diagnostic_summary,
)
from autowork_core.page import get_page
from autowork_core.runtime.context_runtime import (
    configure_logging,
    create_run_state,
)
from autowork_core.runtime.feature_runtime import create_feature_state
from autowork_core.runtime.scenario_runtime import ScenarioRuntimeState
from autowork_core.utils.debug_tools.action_runner_catalog import (
    ActionDescriptor,
    PageViewDescriptor,
    coerce_action_argument,
    render_call_source_snippet,
    render_action_snippet,
)


@dataclass(frozen=True)
class ActionRunRequest:
    target: PageViewDescriptor
    action: ActionDescriptor
    values: dict[str, str]
    call_source: str | None = None


@dataclass(frozen=True)
class ActionRunSequence:
    requests: tuple[ActionRunRequest, ...] = ()


@dataclass(frozen=True)
class ActionRunResult:
    success: bool
    return_value: Any = None
    stage: str | None = None
    target_label: str | None = None
    action_name: str | None = None
    call_source: str = ""
    error_type: str | None = None
    error_message: str | None = None
    cleanup_error: str | None = None
    diagnostic_summary: str | None = None
    diagnostic: dict | None = None
    snippet: str = ""


def append_to_sequence(
        sequence: ActionRunSequence,
        request: ActionRunRequest,
) -> ActionRunSequence:
    return ActionRunSequence(requests=(*sequence.requests, request))


def run_action(request: ActionRunRequest) -> ActionRunResult:
    snippet = _request_snippet(request)
    call_source = _request_call_source(request)
    try:
        args, kwargs = _request_arguments(request)
    except Exception as error:
        return _failure_result(error, request, stage="parse_call", snippet=snippet, call_source=call_source)

    run_context = None
    try:
        run_context = create_action_runner_context(request)
        context = run_context.context
    except Exception as error:
        result = _failure_result(
            error,
            request,
            stage="init_context",
            snippet=snippet,
            call_source=call_source,
        )
    else:
        try:
            receiver = _receiver(context, request.target)
        except Exception as error:
            result = _failure_result(
                error,
                request,
                stage="resolve_receiver",
                snippet=snippet,
                call_source=call_source,
            )
        else:
            try:
                return_value = getattr(receiver, request.action.name)(*args, **kwargs)
                result = ActionRunResult(
                    success=True,
                    return_value=return_value,
                    stage="execute_action",
                    target_label=request.target.label,
                    action_name=request.action.name,
                    call_source=call_source,
                    snippet=snippet,
                )
            except Exception as error:
                result = _failure_result(
                    error,
                    request,
                    stage="execute_action",
                    snippet=snippet,
                    call_source=call_source,
                )
    finally:
        cleanup_error = None
        if run_context is not None:
            cleanup_error = finish_action_runner_context(run_context)
    if cleanup_error:
        return _with_cleanup_error(result, cleanup_error)
    return result


@dataclass(frozen=True)
class ActionRunnerContext:
    context: Any


def create_action_runner_context(_request: ActionRunRequest):
    configure_logging()
    context = SimpleNamespace()
    context.autowork_run = create_run_state()
    context.autowork_run.reporter = None
    context.autowork_feature = create_feature_state(context.autowork_run)
    context.autowork_scenario = ScenarioRuntimeState()
    return ActionRunnerContext(
        context=context,
    )


def finish_action_runner_context(run_context: ActionRunnerContext):
    context = run_context.context
    errors = []
    run_state = getattr(context, "autowork_run", None)
    recorder = getattr(run_state, "recorder", None)
    if recorder is not None:
        for method_name in ("clear", "delete"):
            method = getattr(recorder, method_name, None)
            if not callable(method):
                continue
            try:
                method()
            except Exception as error:
                errors.append(error)
            break
    indicator = getattr(run_state, "run_indicator", None)
    close = getattr(indicator, "close", None)
    if callable(close):
        try:
            close()
        except Exception as error:
            errors.append(error)
    if errors:
        if len(errors) == 1:
            return f"{type(errors[0]).__name__}: {errors[0]}"
        return "; ".join(
            f"{type(error).__name__}: {error}"
            for error in errors
        )
    return None


def _receiver(context, target: PageViewDescriptor):
    if target.kind == "view":
        if target.parent_class_object is None or not target.view_property:
            raise RuntimeError(
                f"WindowView 缺少可运行父 Page: {target.label}"
            )
        page = get_page(context, target.parent_class_object)
        return getattr(page, target.view_property)
    return get_page(context, target.class_object)


def _call_arguments(action: ActionDescriptor, values: dict[str, str]):
    args = []
    kwargs = {}
    keyword_mode = False
    for parameter in action.parameters:
        raw_value = str(values.get(parameter.name, "")).strip()
        if not raw_value:
            if parameter.required:
                raise ValueError(f"缺少必填参数: {parameter.name}")
            keyword_mode = True
            continue
        value = coerce_action_argument(raw_value, parameter)
        if parameter.kind == "KEYWORD_ONLY" or keyword_mode or not parameter.required:
            kwargs[parameter.name] = value
        else:
            args.append(value)
    return args, kwargs


def _request_snippet(request: ActionRunRequest):
    call_source = (request.call_source or "").strip()
    if call_source:
        return render_call_source_snippet(request.target, call_source)
    return render_action_snippet(
        request.target,
        request.action,
        request.values,
    )


def _request_call_source(request: ActionRunRequest):
    call_source = (request.call_source or "").strip()
    if call_source:
        return call_source
    snippet = render_action_snippet(
        request.target,
        request.action,
        request.values,
    )
    lines = [line.strip() for line in snippet.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _request_arguments(request: ActionRunRequest):
    call_source = (request.call_source or "").strip()
    if call_source:
        return parse_action_call_source(call_source, request.action)
    return _call_arguments(request.action, request.values)


def parse_action_call_source(call_source: str, action: ActionDescriptor):
    try:
        tree = ast.parse(call_source.strip(), mode="eval")
    except SyntaxError as error:
        raise ValueError(f"调用代码不是有效 Python 表达式: {error.msg}") from error
    call = tree.body
    if not isinstance(call, ast.Call):
        raise ValueError("调用代码必须是一个 action 调用表达式")
    if not isinstance(call.func, ast.Attribute):
        raise ValueError("调用代码必须形如 page.action(...) 或 view.action(...)")
    if call.func.attr != action.name:
        raise ValueError(
            f"调用代码 action 与当前选择不一致: {call.func.attr} != {action.name}"
        )
    if not isinstance(call.func.value, ast.Name) or call.func.value.id not in {"page", "view"}:
        raise ValueError("调用代码只能调用当前 page/view 对象")
    args = [_literal_argument(argument) for argument in call.args]
    kwargs = {}
    for keyword in call.keywords:
        if keyword.arg is None:
            raise ValueError("调用代码不支持 **kwargs")
        kwargs[keyword.arg] = _literal_argument(keyword.value)
    return args, kwargs


def _literal_argument(node: ast.AST):
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError) as error:
        raise ValueError("调用参数只支持 Python literal") from error


def _failure_result(error, request: ActionRunRequest, *, stage, snippet, call_source):
    diagnostic = runtime_diagnostic_from_exception(error)
    return ActionRunResult(
        success=False,
        stage=stage,
        target_label=request.target.label,
        action_name=request.action.name,
        call_source=call_source,
        error_type=type(error).__name__,
        error_message=str(error),
        diagnostic_summary=(
            runtime_diagnostic_summary(diagnostic)
            if diagnostic is not None
            else None
        ),
        diagnostic=runtime_diagnostic_payload(diagnostic),
        snippet=snippet,
    )


def _with_cleanup_error(result: ActionRunResult, cleanup_error: str):
    return ActionRunResult(
        success=result.success,
        return_value=result.return_value,
        stage=("cleanup" if not result.stage else result.stage),
        target_label=result.target_label,
        action_name=result.action_name,
        call_source=result.call_source,
        error_type=result.error_type,
        error_message=result.error_message,
        cleanup_error=cleanup_error,
        diagnostic_summary=result.diagnostic_summary,
        diagnostic=result.diagnostic,
        snippet=result.snippet,
    )
