from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from contextvars import ContextVar
import re


RUNTIME_DIAGNOSTIC_VERSION = "1.0"
_LAST_RUNTIME_DIAGNOSTIC = ContextVar(
    "bdd_autowork_last_runtime_diagnostic",
    default=None,
)


@dataclass(frozen=True, slots=True)
class RuntimeDiagnostic:
    code: str
    category: str
    stage: str
    summary: str
    backend: str | None = None
    entry_point: str | None = None
    locator_name: str | None = None
    locator_kind: str | None = None
    root_name: str | None = None
    root_state: str | None = None
    wait_type: str | None = None
    timeout_seconds: float | None = None
    interval_seconds: float | None = None
    probe_count: int | None = None
    candidate_count: int | None = None
    last_state: str | None = None
    cause_type: str | None = None
    cause_message: str | None = None
    artifacts: tuple[str, ...] = ()
    diagnostic_version: str = RUNTIME_DIAGNOSTIC_VERSION

    def with_cause(self, error):
        if error is None:
            return self
        return replace(
            self,
            cause_type=type(error).__name__,
            cause_message=_safe_cause_message(error),
        )


def attach_runtime_diagnostic(error, diagnostic, *, preserve_cause=False):
    if not isinstance(error, BaseException):
        raise TypeError("runtime diagnostic只能附加到异常")
    if not isinstance(diagnostic, RuntimeDiagnostic):
        raise TypeError("diagnostic必须是RuntimeDiagnostic")
    try:
        error.runtime_diagnostic = (
            diagnostic
            if preserve_cause
            else diagnostic.with_cause(error.__cause__ or error)
        )
    except Exception:
        pass
    return error


def runtime_diagnostic_from_exception(error):
    seen = set()
    current = error
    while isinstance(current, BaseException) and id(current) not in seen:
        seen.add(id(current))
        diagnostic = getattr(current, "runtime_diagnostic", None)
        if isinstance(diagnostic, RuntimeDiagnostic):
            return diagnostic
        current = current.__cause__ or current.__context__
    return None


def runtime_diagnostic_payload(value):
    diagnostic = (
        runtime_diagnostic_from_exception(value)
        if isinstance(value, BaseException)
        else value
    )
    if not isinstance(diagnostic, RuntimeDiagnostic):
        return None
    payload = asdict(diagnostic)
    payload["artifacts"] = list(diagnostic.artifacts)
    return payload


def runtime_diagnostic_summary(value):
    diagnostic = (
        runtime_diagnostic_from_exception(value)
        if isinstance(value, BaseException)
        else value
    )
    if not isinstance(diagnostic, RuntimeDiagnostic):
        return ""
    detail = diagnostic.summary
    if diagnostic.probe_count is not None:
        detail += f"（已检查 {diagnostic.probe_count} 次）"
    return detail


def clear_last_runtime_diagnostic():
    _LAST_RUNTIME_DIAGNOSTIC.set(None)


def remember_runtime_diagnostic(value):
    diagnostic = (
        runtime_diagnostic_from_exception(value)
        if isinstance(value, BaseException)
        else value
    )
    if isinstance(diagnostic, RuntimeDiagnostic):
        _LAST_RUNTIME_DIAGNOSTIC.set(diagnostic)
    return diagnostic


def last_runtime_diagnostic():
    value = _LAST_RUNTIME_DIAGNOSTIC.get()
    return value if isinstance(value, RuntimeDiagnostic) else None


def _safe_cause_message(error):
    text = " ".join(str(error or "").split())
    text = re.sub(
        r"(?i)(password|passwd|token|api[_-]?key|secret)\s*[:=]\s*[^\s,;]+",
        r"\1=<redacted>",
        text,
    )
    text = re.sub(
        r"(?i)(?:[a-z]:[\\/]|\\\\)[^\s'\"]+",
        "<path>",
        text,
    )
    text = re.sub(
        r"(?<![:\w])/(?:[^\s/'\"]+/)+[^\s'\"]*",
        "<path>",
        text,
    )
    return text[:500]