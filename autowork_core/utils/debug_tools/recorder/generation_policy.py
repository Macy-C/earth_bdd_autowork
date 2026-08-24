from __future__ import annotations

import ast
import hashlib
from collections import Counter
from pathlib import Path


GENERATION_POLICY_VERSION = "2.1"
_SCANNED_ROOTS = (Path("Bdd/steps"), Path("Bdd/page_obj"))
_LOCATOR_KEYS = {
    "auto_id",
    "backend",
    "class_name",
    "control_type",
    "coords",
    "name",
    "region",
    "root",
    "title",
    "top_level",
}


def snapshot_generation_policy(project_root):
    project_root = Path(project_root).resolve()
    findings = []
    for root in _SCANNED_ROOTS:
        directory = project_root / root
        if not directory.exists():
            continue
        for path in sorted(directory.rglob("*.py")):
            findings.extend(_scan_file(project_root, path))
    counts = Counter(item["fingerprint"] for item in findings)
    return {
        "generation_policy_version": GENERATION_POLICY_VERSION,
        "finding_counts": dict(sorted(counts.items())),
        "finding_count": len(findings),
    }


def validate_generation_policy(project_root, changed_files, baseline):
    project_root = Path(project_root).resolve()
    baseline_counts = Counter((baseline or {}).get("finding_counts") or {})
    current = []
    for relative in changed_files:
        relative = Path(relative)
        path = (project_root / relative).resolve()
        if path.suffix.casefold() != ".py" or not path.exists():
            continue
        if not any(relative == root or root in relative.parents for root in _SCANNED_ROOTS):
            continue
        current.extend(_scan_file(project_root, path))
    violations = []
    seen = Counter()
    for item in current:
        fingerprint = item["fingerprint"]
        seen[fingerprint] += 1
        if seen[fingerprint] > baseline_counts[fingerprint]:
            violations.append(item)
    errors = [
        (
            f"生成策略禁止新增 {item['kind']}: "
            f"{item['path']}:{item['line']} ({item['message']})"
        )
        for item in violations
    ]
    return errors, {
        "status": "passed" if not errors else "failed",
        "generation_policy_version": GENERATION_POLICY_VERSION,
        "baseline_finding_count": sum(baseline_counts.values()),
        "changed_file_finding_count": len(current),
        "violations": violations,
    }


def _scan_file(project_root, path):
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, SyntaxError, UnicodeError):
        return []
    relative = path.relative_to(project_root).as_posix()
    findings = []
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and _is_step_definition(node)
            and _is_placeholder_function(node)
        ):
            findings.append(_finding(
                relative,
                node,
                "placeholder_step",
                "目标 Step 必须实现业务编排，不能使用空占位",
            ))
        elif isinstance(node, ast.Call) and _call_name(node.func) == "sleep":
            findings.append(_finding(
                relative,
                node,
                "fixed_sleep",
                "生成代码必须使用状态等待，不能新增固定 sleep",
            ))
        elif isinstance(node, (ast.Import, ast.ImportFrom)) and any(
            name.startswith("pywinauto") for name in _imported_modules(node)
        ):
            findings.append(_finding(
                relative,
                node,
                "direct_pywinauto",
                "生成代码必须通过 BasePage API 操作 UI",
            ))
        elif isinstance(node, ast.Call) and _call_name(node.func) == "set_root":
            findings.append(_finding(
                relative,
                node,
                "runtime_set_root",
                "正式生成必须在 locator YAML 声明 top-level Root",
            ))
        elif isinstance(node, ast.Dict) and _is_inline_locator(node):
            findings.append(_finding(
                relative,
                node,
                "inline_locator",
                "长期 locator 必须写入 Bdd/locators YAML 并使用严格引用",
            ))
    return findings


def _is_step_definition(node):
    return any(
        _call_name(
            decorator.func if isinstance(decorator, ast.Call) else decorator
        ) in {"given", "when", "then", "step"}
        for decorator in node.decorator_list
    )


def _is_placeholder_function(node):
    body = list(node.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    if not body:
        return True
    return all(
        isinstance(statement, ast.Pass)
        or (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and statement.value.value is Ellipsis
        )
        or (
            isinstance(statement, ast.Raise)
            and isinstance(statement.exc, ast.Call)
            and _call_name(statement.exc.func) == "NotImplementedError"
        )
        for statement in body
    )


def _imported_modules(node):
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        return [str(node.module or "")]
    return []


def _call_name(value):
    if isinstance(value, ast.Attribute):
        return value.attr
    if isinstance(value, ast.Name):
        return value.id
    return ""


def _is_inline_locator(node):
    keys = {
        key.value
        for key in node.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }
    if "by" in keys:
        return True
    locator_key_count = len(keys & _LOCATOR_KEYS)
    return locator_key_count >= 2


def _finding(path, node, kind, message):
    canonical = ast.dump(node, annotate_fields=True, include_attributes=False)
    fingerprint = hashlib.sha256(
        f"{path}|{kind}|{canonical}".encode("utf-8")
    ).hexdigest()
    return {
        "fingerprint": fingerprint,
        "path": path,
        "line": getattr(node, "lineno", 0),
        "kind": kind,
        "message": message,
    }