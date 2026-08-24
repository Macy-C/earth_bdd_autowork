"""管理进程级 Step scope 环境变量及其上下文恢复。

Manages the process-wide step-scope environment variable and restores it
after scoped execution.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager


STEP_SCOPE_ENV = "AUTOWORK_BDD_STEP_SCOPE"


def is_step_scope_empty(scope):
    return not scope or not scope.get("files")


def active_step_scope():
    raw_scope = os.environ.get(STEP_SCOPE_ENV)
    if not raw_scope:
        return None
    try:
        scope = json.loads(raw_scope)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid {STEP_SCOPE_ENV}: {raw_scope}") from exc
    return {
        key: value
        for key, value in scope.items()
        if key in {
            "files",
            "entry_file",
            "origin",
            "declarations",
            "fingerprint",
        }
    } | {"files": list(scope.get("files") or [])}


@contextmanager
def activated_step_scope(scope):
    old_scope = os.environ.get(STEP_SCOPE_ENV)
    if is_step_scope_empty(scope):
        os.environ.pop(STEP_SCOPE_ENV, None)
    else:
        os.environ[STEP_SCOPE_ENV] = json.dumps(scope, ensure_ascii=False)
    try:
        yield
    finally:
        if old_scope is None:
            os.environ.pop(STEP_SCOPE_ENV, None)
        else:
            os.environ[STEP_SCOPE_ENV] = old_scope
