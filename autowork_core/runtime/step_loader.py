"""按当前 Step scope 动态导入 Step 定义，并重置 Behave 模块加载缓存。

Dynamically imports step definitions for the active step scope and
resets Behave-related module-loading caches.
"""

from __future__ import annotations

import builtins
import hashlib
import importlib.util
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from autowork_core.runtime.application_lifecycle import (
    ApplicationLifecycleState,
    reset_application_lifecycle,
    restore_application_lifecycle,
    snapshot_application_lifecycle,
    validate_application_lifecycle_sources,
)
from autowork_core.runtime.run_state import active_step_scope, is_step_scope_empty
from autowork_core.runtime.step_scope import resolve_scoped_step_path


MODULE_PREFIX = "bdd_dynamic_steps"
SKIPPED_STEP_FILE_NAMES = {"__init__.py"}


@dataclass(frozen=True)
class StepLoadingState:
    loaded_files: tuple[str, ...]
    modules: dict[str, object]
    application_lifecycle: ApplicationLifecycleState


def load_step_modules(steps_dir):
    steps_dir = Path(steps_dir).resolve()
    _ensure_loaded_file_store()

    scope = active_step_scope()
    if is_step_scope_empty(scope):
        step_files = tuple(_iter_all_step_files(steps_dir))
    else:
        step_files = tuple(_iter_scoped_step_files(steps_dir, scope))

    for py_file in step_files:
        _load_step_file(steps_dir, py_file)
    validate_application_lifecycle_sources(step_files)


def reset_step_loading_state():
    _ensure_loaded_file_store()
    builtins._BDD_LOADED_STEP_FILES.clear()
    reset_application_lifecycle()
    for module_name in list(sys.modules):
        if _is_step_module(module_name):
            del sys.modules[module_name]


def snapshot_step_loading_state():
    _ensure_loaded_file_store()
    return StepLoadingState(
        loaded_files=tuple(sorted(builtins._BDD_LOADED_STEP_FILES)),
        modules={
            module_name: module
            for module_name, module in sys.modules.items()
            if _is_step_module(module_name)
        },
        application_lifecycle=snapshot_application_lifecycle(),
    )


def restore_step_loading_state(state):
    _ensure_loaded_file_store()
    builtins._BDD_LOADED_STEP_FILES.update(state.loaded_files)
    sys.modules.update(state.modules)
    restore_application_lifecycle(state.application_lifecycle)


def _iter_all_step_files(steps_dir):
    for py_file in sorted(steps_dir.rglob("*.py")):
        if py_file.name in SKIPPED_STEP_FILE_NAMES:
            continue
        yield py_file.resolve()


def _iter_scoped_step_files(steps_dir, scope):
    seen = set()
    for file_value in scope.get("files") or []:
        py_file = _resolve_scoped_path(steps_dir, file_value)
        if not py_file.is_file():
            raise FileNotFoundError(f"Scoped step file does not exist: {py_file}")
        if py_file.name in SKIPPED_STEP_FILE_NAMES:
            continue
        if str(py_file) not in seen:
            seen.add(str(py_file))
            yield py_file


def _load_step_file(steps_dir, py_file):
    py_file = py_file.resolve()
    if str(py_file) in builtins._BDD_LOADED_STEP_FILES:
        return

    module_name = _module_name_for_step_file(steps_dir, py_file)
    if module_name in sys.modules:
        return

    spec = importlib.util.spec_from_file_location(module_name, str(py_file))
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot import step file: {py_file}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    builtins._BDD_LOADED_STEP_FILES.add(str(py_file))


def _resolve_scoped_path(steps_dir, value):
    return resolve_scoped_step_path(steps_dir, value)


def _module_name_for_step_file(steps_dir, py_file):
    try:
        relative_path = py_file.relative_to(steps_dir).with_suffix("")
    except ValueError:
        relative_path = Path(py_file.stem)
    readable_name = ".".join(_safe_module_part(part) for part in relative_path.parts)
    digest = hashlib.md5(str(py_file).encode("utf-8")).hexdigest()[:12]
    return f"{MODULE_PREFIX}.{digest}.{readable_name}"


def _safe_module_part(value):
    safe_value = re.sub(r"\W+", "_", str(value)).strip("_")
    return safe_value or "step"


def _ensure_loaded_file_store():
    if not hasattr(builtins, "_BDD_LOADED_STEP_FILES"):
        builtins._BDD_LOADED_STEP_FILES = set()


def _is_step_module(module_name):
    return (
        module_name == MODULE_PREFIX
        or module_name.startswith(f"{MODULE_PREFIX}.")
        or module_name.startswith("Bdd.steps.")
    )