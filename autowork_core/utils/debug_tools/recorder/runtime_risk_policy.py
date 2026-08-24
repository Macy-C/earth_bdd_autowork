from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

from autowork_core.runtime.reporting.oracle_registry import (
    oracle_registry_projection,
)


RUNTIME_RISK_POLICY_VERSION = "1.2"
_FRAMEWORK_RUNTIME_ROOTS = frozenset({"autowork_core"})
_RISK_REQUIREMENTS = {
    "plugin": (
        ("plugin_alternate", ("business_passed", "fail_closed")),
    ),
    "dynamic_dispatch": (
        ("dispatch_alternate", ("business_passed", "fail_closed")),
    ),
    "external_mutable_state": (
        ("external_state_alternate", ("business_passed", "fail_closed")),
    ),
    "concurrent_execution": (
        ("concurrent_schedule", ("business_passed", "fail_closed")),
    ),
    "concurrent_toctou": (
        ("concurrent_mutation", ("business_passed", "fail_closed")),
        ("partial_state", ("business_passed", "fail_closed")),
    ),
}


def derive_runtime_risk_policy(project_root, manifest):
    project_root = Path(project_root).resolve()
    oracle_registry = oracle_registry_projection(project_root)
    roots = _manifest_python_paths(project_root, manifest)
    sources = _reachable_python_sources(project_root, roots)
    signals = []
    source_records = []
    classes = set()
    for path in sources:
        content = path.read_bytes()
        relative = path.relative_to(project_root).as_posix()
        source_records.append({
            "path": relative,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
        })
        try:
            tree = ast.parse(content.decode("utf-8"), filename=str(path))
        except (SyntaxError, UnicodeError):
            continue
        file_signals = _source_risk_signals(tree, relative)
        for item in file_signals:
            classes.add(item["risk_class"])
            signals.append(item)
    if _has_concurrency_signal(signals):
        if "external_mutable_state" in classes:
            classes.add("concurrent_toctou")
            derived_class = "concurrent_toctou"
            derived_signal = "external_state_plus_concurrency"
        else:
            classes.add("concurrent_execution")
            derived_class = "concurrent_execution"
            derived_signal = "concurrency_primitive"
        signals.append({
            "risk_class": derived_class,
            "path": "<derived>",
            "signal": derived_signal,
            "line": 0,
        })
    classes.discard("concurrency_primitive")
    required = {
        "baseline": {"business_passed"},
        "duplicate_target": {"fail_closed"},
    } if classes else {}
    for risk_class in sorted(classes):
        for role, outcomes in _RISK_REQUIREMENTS.get(risk_class, ()):
            required.setdefault(role, set()).update(outcomes)
    policy = {
        "runtime_risk_policy_version": RUNTIME_RISK_POLICY_VERSION,
        "risk_level": "high" if classes else "standard",
        "risk_classes": sorted(classes),
        "signals": sorted(
            signals,
            key=lambda item: (
                item["risk_class"],
                item["path"],
                item["line"],
                item["signal"],
            ),
        ),
        "source_snapshot": sorted(
            source_records,
            key=lambda item: item["path"],
        ),
        "requires_runtime_matrix": bool(classes),
        "requires_independent_oracle": bool(classes),
        "oracle_registry_fingerprint": oracle_registry["fingerprint"],
        "required_matrix": [
            {
                "role": role,
                "allowed_outcomes": sorted(outcomes),
            }
            for role, outcomes in sorted(required.items())
        ],
        "fail_closed": True,
    }
    policy["fingerprint"] = runtime_risk_policy_fingerprint(policy)
    return policy


def runtime_risk_policy_fingerprint(policy):
    payload = {
        key: value for key, value in dict(policy or {}).items()
        if key != "fingerprint"
    }
    return _fingerprint(payload)


def runtime_risk_policy_identity_is_valid(policy):
    if not isinstance(policy, dict):
        return False
    return bool(
        policy.get("runtime_risk_policy_version")
        == RUNTIME_RISK_POLICY_VERSION
        and policy.get("risk_level") in {"standard", "high"}
        and isinstance(policy.get("risk_classes"), list)
        and isinstance(policy.get("required_matrix"), list)
        and policy.get("fingerprint")
        == runtime_risk_policy_fingerprint(policy)
    )


def _manifest_python_paths(project_root, manifest):
    values = {
        str(item.get("path") or "")
        for item in (manifest or {}).get("files") or ()
        if isinstance(item, dict)
        and str(item.get("path") or "").endswith(".py")
    }
    paths = []
    for value in sorted(values):
        path = (project_root / value).resolve()
        try:
            path.relative_to(project_root)
        except ValueError:
            continue
        if path.is_file() and not path.is_symlink():
            paths.append(path)
    return paths


def _reachable_python_sources(project_root, roots):
    pending = list(roots)
    seen = set()
    while pending:
        path = pending.pop(0).resolve()
        if path in seen or not path.is_file() or path.is_symlink():
            continue
        try:
            path.relative_to(project_root)
            content = path.read_text(encoding="utf-8")
            tree = ast.parse(content, filename=str(path))
        except (OSError, UnicodeError, SyntaxError, ValueError):
            continue
        seen.add(path)
        for dependency in _local_import_paths(project_root, path, tree):
            if (
                dependency not in seen
                and not _is_framework_runtime_source(
                    project_root,
                    dependency,
                )
            ):
                pending.append(dependency)
    return sorted(seen)


def _is_framework_runtime_source(project_root, path):
    try:
        relative = Path(path).resolve().relative_to(project_root)
    except ValueError:
        return False
    return bool(
        relative.parts
        and relative.parts[0] in _FRAMEWORK_RUNTIME_ROOTS
    )


def _local_import_paths(project_root, source_path, tree):
    paths = []
    for node in ast.walk(tree):
        module = None
        level = 0
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            level = int(node.level or 0)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                candidate = _module_path(project_root, alias.name)
                if candidate is not None:
                    paths.append(candidate)
            continue
        else:
            continue
        if level:
            base = source_path.parent
            for _ in range(max(0, level - 1)):
                base = base.parent
            candidate = base.joinpath(*module.split(".")) if module else base
        else:
            candidate = project_root.joinpath(*module.split("."))
        resolved = _python_module_file(candidate)
        if resolved is not None:
            paths.append(resolved)
        for alias in node.names:
            if alias.name == "*":
                continue
            child = _python_module_file(
                candidate.joinpath(*alias.name.split("."))
            )
            if child is not None:
                paths.append(child)
    return paths


def _module_path(project_root, module):
    if not module:
        return None
    candidate = project_root.joinpath(*str(module).split("."))
    return _python_module_file(candidate)


def _python_module_file(candidate):
    module_file = candidate.with_suffix(".py")
    if module_file.is_file():
        return module_file.resolve()
    package_file = candidate / "__init__.py"
    return package_file.resolve() if package_file.is_file() else None


def _source_risk_signals(tree, relative):
    signals = []
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    for name in sorted(imported):
        root = name.split(".", 1)[0]
        if root in {"importlib", "pkg_resources"} or "plugin" in name.casefold():
            signals.append(_signal("plugin", relative, f"import:{name}", 0))
        if root in {
            "threading",
            "multiprocessing",
            "concurrent",
            "asyncio",
            "_thread",
        }:
            signals.append(_signal(
                "concurrency_primitive",
                relative,
                f"import:{name}",
                0,
            ))
        if root in {"winreg"}:
            signals.append(_signal(
                "external_mutable_state",
                relative,
                f"import:{name}",
                0,
            ))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in {"__getattribute__", "__get__", "__setattr__"}:
                signals.append(_signal(
                    "dynamic_dispatch",
                    relative,
                    f"method:{node.name}",
                    node.lineno,
                ))
        if isinstance(node, ast.Call):
            name = _call_name(node.func)
            root = name.split(".", 1)[0]
            if name in {"getattr", "setattr", "delattr", "eval", "exec", "__import__"}:
                signals.append(_signal(
                    "dynamic_dispatch",
                    relative,
                    f"call:{name}",
                    node.lineno,
                ))
            if name in {
                "importlib.import_module",
                "pkg_resources.iter_entry_points",
                "importlib.metadata.entry_points",
            } or "plugin" in name.casefold():
                signals.append(_signal(
                    "plugin",
                    relative,
                    f"call:{name}",
                    node.lineno,
                ))
            if (
                name in {"open", "json.load", "json.loads"}
                or name.endswith(".open")
                or name.endswith(".read_text")
                or name == "read_text"
                or name.endswith(".read_bytes")
                or name == "read_bytes"
                or root == "winreg"
                or name in {"os.getenv", "os.environ.get"}
            ):
                signals.append(_signal(
                    "external_mutable_state",
                    relative,
                    f"call:{name}",
                    node.lineno,
                ))
            if root in {
                "threading",
                "multiprocessing",
                "concurrent",
                "asyncio",
                "_thread",
            } or name in {
                "Thread", "Lock", "RLock", "Event", "Executor"
            } or any(
                name.startswith(prefix)
                for prefix in (
                    "threading.",
                    "multiprocessing.",
                    "concurrent.",
                    "asyncio.",
                    "_thread.",
                )
            ):
                signals.append(_signal(
                    "concurrency_primitive",
                    relative,
                    f"call:{name}",
                    node.lineno,
                ))
        if isinstance(node, ast.Subscript):
            name = _call_name(node.value)
            if name == "os.environ":
                signals.append(_signal(
                    "external_mutable_state",
                    relative,
                    "subscript:os.environ",
                    node.lineno,
                ))
    return signals


def _has_concurrency_signal(signals):
    return any(item["risk_class"] == "concurrency_primitive" for item in signals)


def _signal(risk_class, path, signal, line):
    return {
        "risk_class": risk_class,
        "path": path,
        "signal": signal,
        "line": int(line or 0),
    }


def _call_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _fingerprint(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
