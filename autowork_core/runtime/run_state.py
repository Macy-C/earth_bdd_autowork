"""管理进程级 Step scope 环境变量及其上下文恢复。

Manages the process-wide step-scope environment variable and restores it
after scoped execution.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path


STEP_SCOPE_ENV = "AUTOWORK_BDD_STEP_SCOPE"
REPORT_SOURCE_MAP_ENV = "AUTOWORK_BDD_REPORT_SOURCE_MAP"


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


def report_source_path(path):
    raw_mapping = os.environ.get(REPORT_SOURCE_MAP_ENV)
    if not raw_mapping:
        return None
    try:
        mapping = json.loads(raw_mapping)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid {REPORT_SOURCE_MAP_ENV}: {raw_mapping}") from exc
    normalized = _normalized_report_path(path)
    return mapping.get(normalized)


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


@contextmanager
def activated_report_source_map(mapping):
    old_mapping = os.environ.get(REPORT_SOURCE_MAP_ENV)
    normalized = {
        _normalized_report_path(key): _normalized_report_path(value)
        for key, value in dict(mapping or {}).items()
    }
    if normalized:
        os.environ[REPORT_SOURCE_MAP_ENV] = json.dumps(normalized, ensure_ascii=False)
    else:
        os.environ.pop(REPORT_SOURCE_MAP_ENV, None)
    try:
        yield
    finally:
        if old_mapping is None:
            os.environ.pop(REPORT_SOURCE_MAP_ENV, None)
        else:
            os.environ[REPORT_SOURCE_MAP_ENV] = old_mapping


def _normalized_report_path(path):
    try:
        return os.path.abspath(os.fspath(Path(path).resolve()))
    except OSError:
        return os.path.abspath(os.fspath(path))
