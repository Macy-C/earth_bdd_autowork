from __future__ import annotations

import ast
import contextlib
import hashlib
import io
import json
import re
from collections import Counter
from pathlib import Path

import yaml

from autowork_core.utils.debug_tools.recorder.code_reuse_index import (
    candidate_step_pattern_contracts,
    step_pattern_contract_matches,
)

from autowork_core.common.compile import (
    compile_locators,
    compile_window_locator_package,
)
from autowork_core.runtime.step_validation import (
    STEP_DECORATORS,
    check_features,
    decorator_name,
    decorator_pattern,
    step_pattern_to_regex,
)
from autowork_core.utils.bus import normalize
from autowork_core.utils.debug_tools.recorder.generation_plan import (
    _brief_candidate_matches,
    _brief_implementation_candidates,
    PLAN_VERSION,
    SUPPORTED_PLAN_VERSIONS,
)
from autowork_core.utils.debug_tools.recorder.ai_capability_registry import (
    capability_by_name,
)


def run_generation_validations(
        project_root,
        changed_files,
        *,
        source_feature=None,
    plan_artifact=None,
        target_steps=None,
        target_scenario=None,
):
    project_root = Path(project_root).resolve()
    changed = [Path(path) for path in changed_files]
    validations = {}

    python_files = [
        _absolute(project_root, path)
        for path in changed
        if path.suffix.casefold() == ".py"
    ]
    if python_files:
        errors = []
        for path in python_files:
            try:
                source = path.read_text(encoding="utf-8")
                compile(source, str(path), "exec")
            except Exception as error:
                errors.append(f"{path}: {type(error).__name__}: {error}")
            page_root = project_root / "Bdd" / "page_obj"
            try:
                relative = path.relative_to(page_root)
            except ValueError:
                relative = None
            if (
                relative is not None
                and len(relative.parts) > 1
            ):
                marker = path.parent / "__init__.py"
                if not marker.is_file():
                    errors.append(
                        f"{path}: 新 Page/View 子包缺少 __init__.py marker，"
                        "Step 无法通过标准 Python import 加载"
                    )
                else:
                    try:
                        marker_tree = ast.parse(
                            marker.read_text(encoding="utf-8"),
                            str(marker),
                        )
                    except Exception as error:
                        errors.append(
                            f"{marker}: {type(error).__name__}: {error}"
                        )
                    else:
                        if any(
                            not (
                                isinstance(node, ast.Expr)
                                and isinstance(node.value, ast.Constant)
                                and isinstance(node.value.value, str)
                            )
                            for node in marker_tree.body
                        ):
                            errors.append(
                                f"{marker}: Page/View package marker 只能为空"
                                "或包含模块说明，不能执行 import/re-export"
                            )
        validations["python_compile"] = _result(errors, python_files)

    locator_files = [
        _absolute(project_root, path)
        for path in changed
        if Path("Bdd/locators") in path.parents
        and path.suffix.casefold() in {".yaml", ".yml"}
    ]
    if locator_files:
        errors = []
        compiled = []
        quality_warnings = []
        for path in locator_files:
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                quality_warnings.extend(_locator_quality_warnings(path, raw))
            except Exception:
                pass
        handled = set()
        for package in _changed_window_locator_packages(
            project_root,
            locator_files,
            plan_artifact,
        ):
            try:
                root_data = yaml.safe_load(
                    package["root"].read_text(encoding="utf-8")
                ) or {}
                view_data = [
                    yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                    for path in package["views"]
                ]
                value = compile_window_locator_package(
                    root_data,
                    view_data,
                    package_name=package["name"],
                )
                compiled.extend(
                    f"{package['name']}:{name}"
                    for name in value.locators
                )
                handled.update(package["files"])
            except Exception as error:
                errors.append(
                    f"{package['name']}: {type(error).__name__}: {error}"
                )
        for path in locator_files:
            if path in handled:
                continue
            try:
                value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                locators = compile_locators(value)
                compiled.extend(
                    f"{path.name}:{name}" for name in locators
                )
            except Exception as error:
                errors.append(f"{path}: {type(error).__name__}: {error}")
        validations["locator_compile"] = _result(
            errors,
            locator_files,
            compiled=compiled,
            warnings=quality_warnings,
        )

    data_files = [
        _absolute(project_root, path)
        for path in changed
        if Path("Bdd/data") in path.parents
        and path.suffix.casefold() in {".yaml", ".yml"}
        and _absolute(project_root, path).exists()
    ]
    if data_files:
        errors = []
        for path in data_files:
            try:
                value = yaml.safe_load(path.read_text(encoding="utf-8"))
                if value in (None, {}, []):
                    errors.append(f"{path}: data YAML 不能为空")
            except Exception as error:
                errors.append(f"{path}: {type(error).__name__}: {error}")
        validations["data_content"] = _result(errors, data_files)

    step_files = [
        path
        for path in changed
        if Path("Bdd/steps") in path.parents
    ]
    if step_files:
        errors = []
        output = ""
        feature_path = (
            _absolute(project_root, Path(source_feature))
            if source_feature
            else None
        )
        if feature_path is None or not feature_path.exists():
            errors.append("目标 source Feature 不存在，无法执行 Step scope 检查")
        else:
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(
                stream
            ):
                code = check_features(
                    project_root / "Bdd" / "steps",
                    feature_path,
                    project_root,
                    cross_type=False,
                    check_undefined=True,
                    required_step_texts=(
                        [
                            str(step.get("text"))
                            for step in target_steps or []
                            if step.get("text")
                        ]
                        if target_steps is not None
                        else None
                    ),
                    required_scenario=target_scenario,
                )
            output = stream.getvalue().strip()
            if code != 0:
                errors.append(output or f"Step scope 检查退出码: {code}")
        validations["step_scope"] = _result(
            errors,
            step_files,
            output=output,
        )

    return validations


def _changed_window_locator_packages(
        project_root,
        changed_locator_files,
        plan_artifact,
):
    plan = (plan_artifact or {}).get("plan") or {}
    changed = {Path(path).resolve() for path in changed_locator_files}
    packages = []
    for owner_id, owner in (plan.get("window_owners") or {}).items():
        if not isinstance(owner, dict) or not owner.get("root_locator_file"):
            continue
        root = _absolute(
            project_root,
            Path(owner["root_locator_file"]),
        )
        views = []
        owned_views = []
        for view_id, view in (owner.get("views") or {}).items():
            if not isinstance(view, dict) or not view.get("locator_file"):
                continue
            view_path = _absolute(
                project_root,
                Path(view["locator_file"]),
            )
            if view.get("root_locator"):
                owned_views.append((str(view_id), view_path))
            else:
                views.append(view_path)
        files = {root, *views}
        if files & changed:
            packages.append({
                "name": str(owner_id),
                "root": root,
                "views": views,
                "files": files,
            })
        for view_id, view_path in owned_views:
            if view_path not in changed:
                continue
            packages.append({
                "name": f"{owner_id}.{view_id}",
                "root": view_path,
                "views": [],
                "files": {view_path},
            })
    return packages


def _locator_quality_warnings(path, value):
    warnings = []
    for name, raw in (value.items() if isinstance(value, dict) else []):
        name = str(name)
        if len(name) > 64:
            warnings.append(
                f"{path.name}:{name} 名称过长，建议改为稳定业务名称"
            )
        if re.search(r"_[0-9a-f]{8}$", name, flags=re.IGNORECASE):
            warnings.append(
                f"{path.name}:{name} 含录制哈希后缀，建议改为稳定业务名称"
            )
        if not isinstance(raw, dict):
            continue
        locator_keys = {
            str(key).casefold()
            for key, item in raw.items()
            if item not in (None, "", False, [], {})
            and str(key).casefold() not in {
                "timeout",
                "wait",
                "wait_state",
                "visible",
                "enabled",
            }
        }
        if (
            not raw.get("top_level")
            and "root" in locator_keys
            and "class_name" in locator_keys
            and locator_keys <= {"root", "class_name"}
        ):
            warnings.append(
                f"{path.name}:{name} 只按 class_name 定位，"
                "新增同类控件时可能歧义"
            )
        if (
            str(raw.get("by") or "").casefold() == "xpath"
            and _has_positional_xpath_fallback(raw.get("value"))
        ):
            warnings.append(
                f"{path.name}:{name} 使用位置 XPath，UI 顺序变化时可能失效"
            )
    return warnings


def _has_positional_xpath_fallback(value):
    xpath = str(value or "")
    for match in re.finditer(r"\[\s*-?\d+\s*\]", xpath):
        step_start = xpath.rfind("/", 0, match.start()) + 1
        step = xpath[step_start:match.start()].casefold()
        if any(axis in step for axis in (
            "following-sibling::",
            "preceding-sibling::",
        )):
            continue
        return True
    return False


def validate_plan_conformance(
    project_root,
    changed_files,
    plan_artifact,
    *,
    request=None,
    brief=None,
    generation_input_snapshot=None,
):
    project_root = Path(project_root).resolve()
    if plan_artifact.get("plan_version") not in SUPPORTED_PLAN_VERSIONS:
        return [f"缺少有效 GenerationPlanV{PLAN_VERSION}"], {
            "status": "failed",
            "reason": f"GenerationPlanV{PLAN_VERSION} is required.",
            "checked_operations": 0,
        }
    plan_value = plan_artifact.get("plan") or {}
    require_explicit_locator_references = (
        plan_artifact.get("plan_version") == PLAN_VERSION
    )
    plan = plan_value.get("steps") or {}
    window_owners = plan_value.get("window_owners") or {}
    structured = [
        (str(step_id), operation)
        for step_id, step in plan.items()
        if isinstance(step, dict)
        for operation in step.get("operations") or []
        if isinstance(operation, dict) and operation.get("op")
    ]
    has_table_usage = any(
        isinstance(step, dict) and step.get("table_usage") is not None
        for step in plan.values()
    )
    has_behavior_reuse = any(
        isinstance(step, dict)
        and (step.get("behavior_resolution") or {}).get("strategy")
        == "reuse"
        for step in plan.values()
    )
    has_behavior_modify = any(
        isinstance(step, dict)
        and (step.get("behavior_resolution") or {}).get("strategy")
        == "modify"
        for step in plan.values()
    )
    has_unresolved_issues = any(
        isinstance(step, dict) and step.get("unresolved_issues")
        for step in plan.values()
    )
    if (
        not structured
        and not has_table_usage
        and not has_behavior_reuse
        and not has_behavior_modify
        and not has_unresolved_issues
    ):
        return [], {
            "status": "not_applicable",
            "reason": f"No structured operations in GenerationPlanV{PLAN_VERSION}.",
            "checked_operations": 0,
        }

    plan_python_files = [
        _absolute(project_root, Path(path))
        for step in plan.values()
        if isinstance(step, dict)
        for path in (
            step.get("behavior_file"),
            step.get("page_object"),
        )
        if path and str(path).endswith(".py")
    ]
    for owner in window_owners.values():
        if not isinstance(owner, dict):
            continue
        owner_paths = [owner.get("page_object")]
        owner_paths.extend(
            view.get("view_object")
            for view in (owner.get("views") or {}).values()
            if isinstance(view, dict)
        )
        plan_python_files.extend(
            _absolute(project_root, Path(path))
            for path in owner_paths
            if path and str(path).endswith(".py")
        )
    python_files = list(dict.fromkeys([
        _absolute(project_root, Path(path))
        for path in changed_files
        if Path(path).suffix.casefold() == ".py"
        and _absolute(project_root, Path(path)).exists()
    ] + [path for path in plan_python_files if path.exists()]))
    calls = []
    call_records_by_path = {}
    constants_by_path = {}
    trees_by_path = {}
    syntax_errors = []
    for path in python_files:
        path_key = str(path)
        call_records_by_path[path_key] = []
        constants_by_path[path_key] = set()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        except Exception as error:
            syntax_errors.append(
                f"{path}: {type(error).__name__}: {error}"
            )
            continue
        trees_by_path[path_key] = tree
        call_scopes = _call_scope_map(tree)
        runtime_variable_aliases = _runtime_variable_aliases(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = _call_name(node.func)
                runtime_variable_signature = (
                    _runtime_variable_call_signature(
                        node,
                        runtime_variable_aliases,
                    )
                )
                if name or runtime_variable_signature is not None:
                    record_name = name or runtime_variable_signature[0]
                    receiver, view_owner = _call_receiver(node.func)
                    calls.append(record_name)
                    call_records_by_path[path_key].append({
                        "name": record_name,
                        "receiver": receiver,
                        "view_owner": view_owner,
                        "target": _literal_arg(node, 0),
                        "value": _literal_arg(node, 1),
                        "args": [
                            _literal_arg(node, index)
                            for index in range(len(node.args))
                        ],
                        "_arg_references": [
                            _argument_reference(argument)
                            for argument in node.args
                        ],
                        "_arg_runtime_bindings": [
                            sorted(_runtime_binding_references(argument))
                            for argument in node.args
                        ],
                        "_arg_table_columns": [
                            sorted(_subscript_columns(argument))
                            for argument in node.args
                        ],
                        "keywords": {
                            keyword.arg: _literal_value(keyword.value)
                            for keyword in node.keywords
                            if keyword.arg
                        },
                        "_keyword_table_columns": {
                            keyword.arg: sorted(
                                _subscript_columns(keyword.value)
                            )
                            for keyword in node.keywords
                            if keyword.arg
                        },
                        "_keyword_references": {
                            keyword.arg: _argument_reference(keyword.value)
                            for keyword in node.keywords
                            if keyword.arg
                        },
                        "_keyword_runtime_bindings": {
                            keyword.arg: sorted(
                                _runtime_binding_references(keyword.value)
                            )
                            for keyword in node.keywords
                            if keyword.arg
                        },
                        "path": str(path),
                        "line": getattr(node, "lineno", 0),
                        "_implementation_method": call_scopes.get(
                            id(node)
                        ),
                        "_runtime_variable_signature": (
                            runtime_variable_signature
                        ),
                    })
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
            ):
                constants_by_path[path_key].add(node.value)
    for records in call_records_by_path.values():
        records.sort(key=lambda item: item["line"])

    locator_keys_by_path = {}
    plan_locator_files = [
        Path(step.get("locator_file"))
        for step in plan.values()
        if isinstance(step, dict) and step.get("locator_file")
    ]
    for owner in window_owners.values():
        if not isinstance(owner, dict):
            continue
        if owner.get("root_locator_file"):
            plan_locator_files.append(Path(owner["root_locator_file"]))
        plan_locator_files.extend(
            Path(view["locator_file"])
            for view in (owner.get("views") or {}).values()
            if isinstance(view, dict) and view.get("locator_file")
        )
    for path_value in list(changed_files) + plan_locator_files:
        path = Path(path_value)
        if (
            Path("Bdd/locators") not in path.parents
            or path.suffix.casefold() not in {".yaml", ".yml"}
        ):
            continue
        absolute = _absolute(project_root, path)
        path_key = str(absolute)
        try:
            value = yaml.safe_load(absolute.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        locator_keys_by_path[path_key] = {str(key) for key in value}

    ownership_errors, owner_scopes = _window_owner_scopes(
        project_root,
        window_owners,
    )
    issue_errors, issue_trace = _validate_issue_placeholders(
        project_root,
        plan,
        request,
        trees_by_path,
    )
    behavior_modify_errors = _validate_modified_step_behaviors(
        project_root,
        plan,
        trees_by_path,
        request,
    )
    resolution_errors, resolution_warnings = (
        validate_owner_resolution_snapshot(
            project_root,
            window_owners,
            brief,
            generation_input_snapshot=generation_input_snapshot,
        )
    )
    implementation_errors, implementation_warnings = (
        validate_implementation_resolution_snapshot(
            project_root,
            plan_artifact,
            brief,
            generation_input_snapshot=generation_input_snapshot,
        )
    )
    implementation_errors.extend(
        _validate_implementation_change_set(
            project_root,
            changed_files,
            plan_artifact,
        )
    )
    errors = [
        *syntax_errors,
        *issue_errors,
        *behavior_modify_errors,
        *ownership_errors,
        *resolution_errors,
        *implementation_errors,
    ]
    checked = []
    implementation_trace = []
    implementation_trace.extend(issue_trace)
    matched_runtime_calls = Counter()
    changed_python_files = [
        _absolute(project_root, Path(path))
        for path in changed_files
        if Path(path).suffix.casefold() == ".py"
        and _absolute(project_root, Path(path)).exists()
    ]
    changed_python_paths = {
        str(path.resolve())
        for path in changed_python_files
    }
    changed_locator_files = [
        _absolute(project_root, Path(path))
        for path in changed_files
        if Path("Bdd/locators") in Path(path).parents
        and Path(path).suffix.casefold() in {".yaml", ".yml"}
    ]
    for step_id, step in plan.items():
        if not isinstance(step, dict):
            continue
        if step.get("unresolved_issues"):
            continue
        declared_python = [
            _absolute(project_root, Path(path))
            for path in (step.get("behavior_file"), step.get("page_object"))
            if path and str(path).endswith(".py")
        ]
        scoped_python = declared_python or changed_python_files
        call_records = sorted(
            (
                record
                for path in scoped_python
                for record in call_records_by_path.get(str(path), [])
            ),
            key=lambda item: (item["path"], item["line"]),
        )
        constants = {
            value
            for path in scoped_python
            for value in constants_by_path.get(str(path), set())
        }
        declared_locator = (
            [_absolute(project_root, Path(step["locator_file"]))]
            if step.get("locator_file")
            else []
        )
        scoped_locators = declared_locator or changed_locator_files
        locator_keys = {
            key
            for path in scoped_locators
            for key in locator_keys_by_path.get(str(path), set())
        }
        step_text = _request_step_text(request, step_id)
        table_errors = _validate_table_usage_code(
            step_id,
            step,
            scoped_python,
            trees_by_path,
            step_text=step_text,
            brief=brief,
        )
        errors.extend(table_errors)
        (
            orchestration_errors,
            runtime_method_parameters,
            table_method_parameters,
            method_calls_by_operation,
        ) = (
            _validate_step_method_orchestration(
            step_id,
            step,
            project_root,
            trees_by_path,
            window_owners=window_owners,
            brief=brief,
            strict_provenance=(
                plan_artifact.get("plan_version") == PLAN_VERSION
            ),
            step_text=step_text,
            )
        )
        errors.extend(orchestration_errors)
        call_cursors = {}
        step_inline_scope = None
        for operation_step_id, operation in structured:
            if operation_step_id != str(step_id):
                continue
            op = str(operation.get("op"))
            target = str(operation.get("target") or "").lstrip("$")
            expected_value = _expected_value(operation)
            implementation_location = str(
                operation.get("implementation_location")
                or "page_method"
            )
            implementation_method = str(
                operation.get("implementation_method") or ""
            )
            if _operation_uses_exact_method_reuse(operation, brief):
                method_call = method_calls_by_operation.get(id(operation))
                if method_call is not None:
                    implementation_trace.append({
                        "step_id": str(step_id),
                        "action_ids": list(operation.get("action_ids") or []),
                        "implementation_location": implementation_location,
                        "implementation_method": implementation_method or None,
                        "path": _project_relative_path(
                            project_root,
                            method_call.get("path"),
                        ),
                        "line": method_call.get("line"),
                        "call": method_call.get("name"),
                    })
                checked.append({
                    "step_id": str(step_id),
                    "op": op,
                    "target": target or None,
                    "value": expected_value,
                    "status": "checked",
                })
                continue
            owner_scope = owner_scopes.get(
                str(operation.get("window_owner") or "")
            )
            if implementation_location == "step_inline_base_api":
                if step_inline_scope is None:
                    step_inline_scope, inline_errors = _step_inline_scope(
                        step_id,
                        step,
                        project_root,
                        trees_by_path,
                        call_records_by_path,
                        window_owners,
                        step_text,
                    )
                    errors.extend(inline_errors)
                operation_records = step_inline_scope["records"]
                operation_constants = step_inline_scope["constants"]
                operation_locators = locator_keys
                if owner_scope is not None:
                    view_owner = str(operation.get("view_owner") or "")
                    operation_locators = owner_scope["root_locator_keys"]
                    if view_owner:
                        view_scope = owner_scope["views"].get(view_owner) or {}
                        operation_locators = (
                            operation_locators
                            | set(view_scope.get("locator_keys") or ())
                        )
                scope_key = step_inline_scope["scope_key"]
            elif owner_scope is not None:
                view_owner = str(operation.get("view_owner") or "")
                operation_python = owner_scope["page_python"]
                operation_locators = owner_scope["root_locator_keys"]
                if view_owner:
                    view_scope = owner_scope["views"].get(view_owner) or {}
                    operation_python = view_scope.get("python") or []
                    operation_locators = (
                        owner_scope["root_locator_keys"]
                        | set(view_scope.get("locator_keys") or ())
                    )
                scoped_operation_python = [
                    path for path in operation_python if path.exists()
                ]
                operation_records = sorted(
                    (
                        record
                        for path in scoped_operation_python
                        for record in call_records_by_path.get(
                            str(path),
                            [],
                        )
                    ),
                    key=lambda item: (item["path"], item["line"]),
                )
                operation_constants = {
                    value
                    for path in scoped_operation_python
                    for value in constants_by_path.get(str(path), set())
                }
                scope_key = tuple(str(path) for path in scoped_operation_python)
            else:
                operation_records = call_records
                operation_constants = constants
                operation_locators = locator_keys
                scope_key = tuple(str(path) for path in scoped_python)
            if implementation_method:
                implementation_records = [
                    record
                    for record in operation_records
                    if record.get("_implementation_method")
                    == implementation_method
                ]
                if not implementation_records:
                    errors.append(
                        f"Step {step_id} 声明实现方法缺少计划调用: "
                        f"{implementation_method}"
                    )
                operation_records = implementation_records
                scope_key = (*scope_key, implementation_method)
            call_cursor = call_cursors.get(scope_key, 0)
            operation_to_match = operation
            source = str(operation.get("source") or "")
            if implementation_method and source.startswith("runtime."):
                binding = source.split(".", 1)[1]
                method_binding = runtime_method_parameters.get((
                    implementation_method,
                    binding,
                )) or {}
                parameters = method_binding.get("parameters") or set()
                call_path = method_binding.get("path")
                if (
                    parameters
                    and call_path
                    and str(Path(call_path).resolve()) in changed_python_paths
                ):
                    matched_runtime_calls[(
                        "get_variable",
                        binding,
                    )] += 1
                if parameters:
                    operation_to_match = {
                        **operation,
                        "_runtime_method_parameters": sorted(parameters),
                    }
            if implementation_method and source.startswith("table."):
                column = source.split(".", 1)[1]
                parameters = table_method_parameters.get((
                    implementation_method,
                    column,
                )) or set()
                if parameters:
                    operation_to_match = {
                        **operation_to_match,
                        "_table_method_parameters": sorted(parameters),
                    }
            match_index = _find_ordered_call(
                operation_records,
                call_cursor,
                operation_to_match,
            )
            matched_record = None
            if match_index is None:
                if implementation_location == "step_inline_base_api":
                    errors.append(
                        f"Step {step_id} step_inline_base_api 缺少 "
                        "owned Page/View 的有序计划调用: "
                        f"{op} target={target or None} "
                        f"value={expected_value!r}"
                    )
                elif implementation_method:
                    errors.append(
                        f"Step {step_id} 声明实现方法 "
                        f"{implementation_method} 缺少有序计划调用: "
                        f"{op} target={target or None} "
                        f"value={expected_value!r}"
                    )
                else:
                    errors.append(
                        f"Step {step_id} 生成代码缺少有序计划调用: {op} "
                        f"target={target or None} value={expected_value!r}"
                    )
            else:
                call_cursors[scope_key] = match_index + 1
                matched_record = operation_records[match_index]
                if str(Path(matched_record.get("path")).resolve()) in (
                        changed_python_paths
                ):
                    matched_runtime_calls.update(
                        _planned_runtime_call_signatures(operation)
                    )
                implementation_trace.append({
                    "step_id": str(step_id),
                    "action_ids": list(operation.get("action_ids") or []),
                    "implementation_location": implementation_location,
                    "implementation_method": implementation_method or None,
                    "path": _project_relative_path(
                        project_root,
                        matched_record.get("path"),
                    ),
                    "line": matched_record.get("line"),
                    "call": matched_record.get("name"),
                })
            if target:
                if (
                    require_explicit_locator_references
                    and matched_record is not None
                    and not _uses_explicit_locator_reference(
                        matched_record,
                        operation,
                    )
                ):
                    errors.append(
                        f"Step {step_id} 计划目标必须使用显式 locator 引用: "
                        f"$loc:{target} 或 ${target}"
                    )
                elif (
                    not require_explicit_locator_references
                    and not (
                        {target, f"${target}", f"$loc:{target}"}
                        & operation_constants
                    )
                ):
                    errors.append(
                        f"Step {step_id} 生成代码缺少计划目标引用: {target}"
                    )
                if operation_locators and target not in operation_locators:
                    errors.append(
                        f"Step {step_id} locator YAML 缺少计划目标: {target}"
                    )
            if (
                expected_value is not None
                and operation.get("source") in {None, "literal"}
                and not isinstance(expected_value, (list, tuple, dict))
                and not (
                    capability_by_name(op)
                    and capability_by_name(op).ast_match_profile
                    == "semantic_control_value"
                )
                and str(expected_value) not in operation_constants
            ):
                errors.append(
                    f"Step {step_id} 生成代码缺少计划值: {expected_value!r}"
                )
            checked.append({
                "step_id": str(step_id),
                "op": op,
                "target": target or None,
                "value": expected_value,
                "status": "checked",
            })
        if step.get("table_usage") is not None:
            checked.append({
                "step_id": str(step_id),
                "op": "table_usage",
                "target": None,
                "value": (step.get("table_usage") or {}).get("consumption"),
                "status": "checked" if not table_errors else "failed",
            })
    errors.extend(_validate_added_runtime_variable_calls(
        project_root,
        changed_python_files,
        call_records_by_path,
        matched_runtime_calls,
        generation_input_snapshot,
    ))
    call_records = sorted(
        (
            record
            for records in call_records_by_path.values()
            for record in records
        ),
        key=lambda item: (item["path"], item["line"]),
    )
    locator_keys = {
        key
        for keys in locator_keys_by_path.values()
        for key in keys
    }
    public_call_records = [
        {
            key: value
            for key, value in record.items()
            if not str(key).startswith("_")
        }
        for record in call_records
    ]
    return errors, {
        "status": "passed" if not errors else "failed",
        "checked_operations": len(checked),
        "operations": checked,
        "python_calls": sorted(set(calls)),
        "ordered_call_records": public_call_records,
        "implementation_trace": implementation_trace,
        "locator_keys": sorted(locator_keys),
        "warnings": [
            *resolution_warnings,
            *implementation_warnings,
        ],
        "errors": errors,
    }


def _project_relative_path(project_root, value):
    try:
        return Path(value).resolve().relative_to(
            Path(project_root).resolve()
        ).as_posix()
    except (TypeError, ValueError):
        return str(value or "")


def validate_owner_resolution_snapshot(
    project_root,
    owners,
    brief,
    *,
    generation_input_snapshot=None,
):
    if not isinstance(brief, dict):
        return [], []
    errors = []
    warnings = []
    windows = {
        normalize(str(item.get("root_name") or "")): item
        for item in (
            (brief.get("window_ownership") or {}).get("windows") or []
        )
    }
    for owner_id, owner in (owners or {}).items():
        if not isinstance(owner, dict):
            continue
        resolution = owner.get("resolution") or {}
        strategy = resolution.get("strategy")
        window = windows.get(normalize(str(
            owner.get("evidence_root") or owner.get("root_locator") or ""
        )))
        owner_match = (window or {}).get("owner_match") or {}
        if strategy == "create_new" and owner_match.get(
            "suggested_strategy"
        ) in {"reuse_existing", "ambiguous"}:
            warnings.append(
                f"window_owner {owner_id} 以 create_new 覆盖 "
                f"{owner_match.get('suggested_strategy')} 建议: "
                f"{resolution.get('reason')}"
            )
            continue
        if strategy == "create_new" and resolution.get("candidate_id"):
            matches = _brief_candidate_matches(
                brief,
                resolution.get("candidate_id"),
            )
            candidate = matches[0][1] if matches else {}
            expected = str(candidate.get("locator_sha256") or "")
            actual = _candidate_input_sha256(
                project_root,
                _owned_path(project_root, owner.get("root_locator_file")),
                generation_input_snapshot,
            )
            if not expected or actual != expected:
                errors.append(
                    f"window_owner {owner_id} legacy candidate "
                    f"{resolution.get('candidate_id')} 在事务开始前快照已变化"
                )
            continue
        if strategy != "reuse_existing":
            continue
        candidate_id = str(resolution.get("candidate_id") or "")
        matches = _brief_candidate_matches(brief, candidate_id)
        if not matches:
            errors.append(
                f"window_owner {owner_id} 冻结 candidate 不存在: "
                f"{candidate_id}"
            )
            continue
        candidate = matches[0][1]
        for field, hash_field in (
            ("page_object", "page_sha256"),
            ("root_locator_file", "locator_sha256"),
        ):
            expected = str(candidate.get(hash_field) or "")
            if not expected:
                continue
            path = _owned_path(project_root, owner.get(field))
            actual = _candidate_input_sha256(
                project_root,
                path,
                generation_input_snapshot,
            )
            if actual != expected:
                errors.append(
                    f"window_owner {owner_id} candidate {candidate_id} "
                    f"{field} 在事务开始前快照已变化"
                )
    return errors, warnings


def _candidate_input_sha256(project_root, path, snapshot):
    if path is None:
        return None
    if snapshot is None:
        return _file_sha256(path) if path.is_file() else None
    if snapshot.get("snapshot_version") != "1.0":
        return None
    try:
        relative = path.resolve().relative_to(
            Path(project_root).resolve()
        ).as_posix()
    except ValueError:
        return None
    return str(
        ((snapshot.get("files") or {}).get(relative) or {}).get("sha256")
        or ""
    ) or None


def _file_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_implementation_resolution_snapshot(
        project_root,
        plan_artifact,
        brief,
        *,
        generation_input_snapshot=None,
        reject_existing_create=False,
):
    if (
        not isinstance(brief, dict)
        or (plan_artifact or {}).get("plan_version") != PLAN_VERSION
    ):
        return [], []
    plan = (plan_artifact or {}).get("plan") or {}
    candidates = _brief_implementation_candidates(brief)
    errors = []
    warnings = []
    checked = set()
    target_steps = {
        str(item.get("id") or ""): item
        for item in (brief.get("target") or {}).get("steps") or ()
        if item.get("id")
    }
    for step_id, step in (plan.get("steps") or {}).items():
        behavior_resolution = (step or {}).get("behavior_resolution") or {}
        if behavior_resolution.get("strategy") == "reuse":
            candidate_id = str(
                behavior_resolution.get("candidate_id") or ""
            )
            candidate = candidates.get(candidate_id) or {}
            expected = str(candidate.get("file_sha256") or "")
            path = _owned_path(
                project_root,
                step.get("behavior_file"),
            )
            actual = _candidate_input_sha256(
                project_root,
                path,
                generation_input_snapshot,
            )
            if not expected or actual != expected:
                errors.append(
                    f"Step {step_id} behavior candidate {candidate_id} "
                    "在事务开始前快照已变化"
                )
            elif not _step_candidate_matches_source(
                path,
                candidate,
                target_steps.get(str(step_id)) or {},
            ):
                errors.append(
                    f"Step {step_id} behavior candidate {candidate_id} "
                    "symbol 或 Gherkin pattern 不匹配"
                )
        for operation in (step or {}).get("operations") or []:
            method = str(operation.get("implementation_method") or "")
            resolution = operation.get("implementation_resolution") or {}
            identity = (method, json.dumps(resolution, sort_keys=True))
            if not method or identity in checked:
                continue
            checked.add(identity)
            path = _implementation_path(plan, operation)
            strategy = resolution.get("strategy")
            if strategy in {"reuse", "modify"}:
                candidate_id = str(resolution.get("candidate_id") or "")
                candidate = candidates.get(candidate_id) or {}
                expected = str(candidate.get("file_sha256") or "")
                actual = _candidate_input_sha256(
                    project_root,
                    _owned_path(project_root, path),
                    generation_input_snapshot,
                )
                if not expected or actual != expected:
                    errors.append(
                        f"实现方法 {method} candidate {candidate_id} "
                        "在事务开始前快照已变化"
                    )
            elif strategy == "create" and reject_existing_create:
                source = _owned_path(project_root, path)
                if source and source.is_file() and _source_defines_method(
                    source,
                    method,
                ):
                    errors.append(
                        f"实现方法 {method} 已存在，不能声明 create；"
                        "请刷新复用候选"
                    )
    return errors, warnings


def _step_candidate_matches_source(path, candidate, target_step):
    if (
        path is None
        or not path.is_file()
        or not str((target_step or {}).get("text") or "")
    ):
        return False
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    except (OSError, SyntaxError, UnicodeError):
        return False
    symbol = str(candidate.get("symbol") or "")
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == symbol
    ]
    matching_contracts = [
        contract
        for contract in candidate_step_pattern_contracts(candidate)
        if step_pattern_contract_matches(contract, target_step)
    ]
    return bool(
        len(functions) == 1
        and len(matching_contracts) == 1
        and any(
            decorator_name(decorator.func)
            == str(matching_contracts[0].get("decorator") or "").casefold()
            and decorator_pattern(decorator)
            == str(matching_contracts[0].get("pattern") or "")
            for decorator in functions[0].decorator_list
            if isinstance(decorator, ast.Call)
            and decorator_name(decorator.func) in STEP_DECORATORS
        )
    )


def _validate_implementation_change_set(
        project_root,
        changed_files,
        plan_artifact,
):
    if (plan_artifact or {}).get("plan_version") != PLAN_VERSION:
        return []
    plan = (plan_artifact or {}).get("plan") or {}
    changed = {
        _absolute(project_root, Path(path)).resolve()
        for path in changed_files
    }
    errors = []
    checked = set()
    for step in (plan.get("steps") or {}).values():
        for operation in (step or {}).get("operations") or []:
            method = str(operation.get("implementation_method") or "")
            strategy = (
                operation.get("implementation_resolution") or {}
            ).get("strategy")
            identity = (method, strategy)
            if identity in checked or strategy not in {"modify", "create"}:
                continue
            checked.add(identity)
            path = _owned_path(
                project_root,
                _implementation_path(plan, operation),
            )
            if path is None or path.resolve() not in changed:
                errors.append(
                    f"实现方法 {method} 声明 {strategy}，"
                    "但 owner 文件未在事务中修改"
                )
    return errors


def _implementation_path(plan, operation):
    owner = (plan.get("window_owners") or {}).get(
        str(operation.get("window_owner") or "")
    ) or {}
    view_owner = str(operation.get("view_owner") or "")
    if view_owner:
        return (
            (owner.get("views") or {}).get(view_owner) or {}
        ).get("view_object")
    return owner.get("page_object")


def _source_defines_method(path, implementation_method):
    owner, separator, method = str(implementation_method).rpartition(".")
    if not separator:
        return False
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"), str(path))
    except (OSError, SyntaxError, UnicodeError):
        return False
    return _class_defines_method(tree, owner, method)


def _validate_table_usage_code(
        step_id,
        step,
        scoped_python,
        trees_by_path,
    *,
    step_text=None,
    brief=None,
):
    usage = step.get("table_usage")
    if not isinstance(usage, dict) or not usage:
        return []
    errors = []
    behavior_path = step.get("behavior_file")
    page_path = step.get("page_object")
    behavior_tree = _declared_tree(
        behavior_path,
        scoped_python,
        trees_by_path,
    )
    if behavior_tree is None:
        errors.append(
            f"Step {step_id} table_usage 缺少可解析 behavior_file"
        )
        return errors
    behavior_function = _table_behavior_function(
        behavior_tree,
        step_text,
    )
    if behavior_function is None:
        errors.append(
            f"Step {step_id} 无法唯一定位读取 context.table 的 Step Definition"
        )

    consumer = usage.get("consumer")
    consumption = usage.get("consumption")
    page_tree = None
    page_function = None
    table_helpers = []
    if consumer == "page_object":
        page_tree = _declared_tree(
            page_path,
            scoped_python,
            trees_by_path,
        )
        if page_tree is None:
            errors.append(
                f"Step {step_id} table_usage 缺少可解析 page_object"
            )
        elif behavior_function is not None:
            page_function, table_bindings = _table_page_binding(
                behavior_function,
                page_tree,
            )
            if page_function is None:
                errors.append(
                    f"Step {step_id} 无法唯一定位消费表格的 Page Object 方法"
                )
            else:
                shape_errors, table_helpers = _validate_table_shape_code(
                    step_id,
                    usage,
                    behavior_function,
                    [item["argument"] for item in table_bindings],
                    behavior_tree,
                )
                errors.extend(shape_errors)
                if consumption == "each_row":
                    errors.extend(_validate_each_row_code(
                        step_id,
                        usage,
                        page_function,
                        {
                            item["parameter"]
                            for item in table_bindings
                            if item.get("parameter")
                        },
                        planned_operations=step.get("operations") or (),
                        brief=brief,
                    ))
    elif consumer == "step_definition":
        if behavior_function is not None:
            table_values = _table_derived_names(behavior_function)
            shape_errors, table_helpers = _validate_table_shape_code(
                step_id,
                usage,
                behavior_function,
                [behavior_function],
                behavior_tree,
            )
            errors.extend(shape_errors)
            if consumption == "each_row":
                errors.extend(_validate_each_row_code(
                    step_id,
                    usage,
                    behavior_function,
                    table_values,
                    owner_label="Step Definition",
                    planned_operations=step.get("operations") or (),
                    brief=brief,
                ))
    elif consumer == "scenario_context":
        context_key = usage.get("context_key")
        context_value = (
            _context_table_assignment(behavior_function, context_key)
            if behavior_function is not None
            else None
        )
        if context_value is None:
            errors.append(
                f"Step {step_id} 未从 context.table 写入 "
                f"context.{context_key}"
            )
        else:
            shape_errors, table_helpers = _validate_table_shape_code(
                step_id,
                usage,
                behavior_function,
                [context_value],
                behavior_tree,
            )
            errors.extend(shape_errors)

    constants = _string_constants(
        behavior_function,
        page_function,
        *table_helpers,
    )
    missing_columns = sorted(
        column
        for column in (usage.get("columns") or {})
        if column not in constants
    )
    if missing_columns:
        errors.append(
            f"Step {step_id} 生成代码未引用表格列: {missing_columns}"
        )
    if any(
        _call_name(node.func) == "run_case_matrix"
        for tree in (behavior_tree, page_tree)
        if tree is not None
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    ):
        errors.append(
            f"Step {step_id} 新 Table Usage 不能调用 run_case_matrix"
        )
    return errors


def _declared_tree(path_value, scoped_python, trees_by_path):
    if path_value:
        suffix = Path(path_value).as_posix().casefold()
        match = next(
            (
                path for path in scoped_python
                if path.as_posix().casefold().endswith(suffix)
            ),
            None,
        )
        return trees_by_path.get(str(match)) if match is not None else None
    return None


def _has_context_table(tree):
    return any(
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "context"
        and node.attr == "table"
        for node in ast.walk(tree)
    )


def _request_step_text(request, step_id):
    return next(
        (
            str(step.get("text") or "")
            for step in ((request or {}).get("target") or {}).get("steps") or ()
            if str(step.get("id") or "") == str(step_id)
        ),
        None,
    )


def _table_behavior_function(tree, step_text):
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and _has_context_table(node)
    ]
    if step_text:
        exact = [
            node
            for node in functions
            if step_text in _gherkin_patterns(node)
        ]
        if len(exact) == 1:
            return exact[0]
    return functions[0] if len(functions) == 1 else None


def _gherkin_patterns(function):
    values = []
    for decorator in function.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        if _call_name(decorator.func) not in {
            "given", "when", "then", "step",
        }:
            continue
        if decorator.args and isinstance(decorator.args[0], ast.Constant):
            if isinstance(decorator.args[0].value, str):
                values.append(decorator.args[0].value)
    return values


def _table_page_binding(behavior_function, page_tree):
    page_methods = {}
    for node in ast.walk(page_tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        page_methods.setdefault(node.name, []).append(node)
    table_values = _table_derived_names(behavior_function)
    candidates = {}
    for node in ast.walk(behavior_function):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        methods = page_methods.get(node.func.attr) or []
        if not methods:
            continue
        for method in methods:
            parameters = _bound_parameters(method)
            keyword_parameters = {
                parameter: parameter for parameter in parameters
            }
            bindings = []
            for index, argument in enumerate(node.args):
                if not _uses_table_value(argument, table_values):
                    continue
                bindings.append({
                    "argument": argument,
                    "parameter": (
                        parameters[index]
                        if index < len(parameters)
                        else None
                    ),
                })
            for keyword in node.keywords:
                if not _uses_table_value(keyword.value, table_values):
                    continue
                bindings.append({
                    "argument": keyword.value,
                    "parameter": keyword_parameters.get(keyword.arg),
                })
            if bindings:
                candidate = candidates.setdefault(id(method), {
                    "method": method,
                    "bindings": [],
                })
                candidate["bindings"].extend(bindings)
    if len(candidates) != 1:
        return None, []
    candidate = next(iter(candidates.values()))
    return candidate["method"], candidate["bindings"]


def _bound_parameters(method):
    parameters = [
        argument.arg
        for argument in (
            list(method.args.posonlyargs) + list(method.args.args)
        )
    ]
    if parameters and parameters[0] in {"self", "cls"}:
        parameters = parameters[1:]
    return parameters


def _uses_table_value(expression, table_values):
    return _has_context_table(expression) or any(
        isinstance(child, ast.Name) and child.id in table_values
        for child in ast.walk(expression)
    )


def _table_derived_names(function, *, seed_names=()):
    assignments = [
        (target, node.value)
        for node in ast.walk(function)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (
            node.targets if isinstance(node, ast.Assign) else [node.target]
        )
        if isinstance(target, ast.Name)
    ]
    values = set(seed_names)
    changed = True
    while changed:
        changed = False
        for target, value in assignments:
            if target.id in values:
                continue
            if _has_context_table(value) or any(
                isinstance(child, ast.Name) and child.id in values
                for child in ast.walk(value)
            ):
                values.add(target.id)
                changed = True
    return values


def _validate_table_shape_code(
        step_id,
        usage,
        behavior_function,
        table_expressions,
        behavior_tree,
):
    expressions, helpers = _resolved_table_expressions(
        behavior_function,
        table_expressions,
        behavior_tree,
    )
    shape = usage.get("shape")
    errors = []
    if not _matches_table_shape(
            shape,
            expressions,
            helper_names={helper.name for helper in helpers},
    ):
        errors.append(
            f"Step {step_id} context.table 未转换为 {shape}"
        )
    if usage.get("ordered") and _reorders_table(expressions):
        errors.append(f"Step {step_id} 未保持表格顺序")
    return errors, helpers


def _resolved_table_expressions(
        function,
        roots,
        module_tree,
        *,
        source_names=None,
        visited_helpers=None,
):
    assignments = {}
    for node in ast.walk(function):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                assignments.setdefault(target.id, []).append(node.value)
    expressions = []
    pending = list(roots)
    visited_nodes = set()
    resolved_names = set()
    while pending:
        expression = pending.pop()
        if id(expression) in visited_nodes:
            continue
        visited_nodes.add(id(expression))
        expressions.append(expression)
        for child in ast.walk(expression):
            if not isinstance(child, ast.Name):
                continue
            if child.id in resolved_names:
                continue
            resolved_names.add(child.id)
            pending.extend(assignments.get(child.id) or [])
    source_names = set(
        source_names
        if source_names is not None
        else _table_derived_names(function)
    )
    helper_functions = {}
    for node in getattr(module_tree, "body", ()):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            helper_functions.setdefault(node.name, []).append(node)
    visited_helpers = set(visited_helpers or ())
    used_helpers = []
    for expression in list(expressions):
        for call in ast.walk(expression):
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
                continue
            helpers = helper_functions.get(call.func.id) or []
            if len(helpers) != 1:
                continue
            helper = helpers[0]
            if id(helper) in visited_helpers:
                continue
            table_parameters = _table_call_parameters(
                call,
                helper,
                source_names,
            )
            if not table_parameters:
                continue
            helper_sources = _table_derived_names(
                helper,
                seed_names=table_parameters,
            )
            returns = [
                node.value
                for node in ast.walk(helper)
                if isinstance(node, ast.Return)
                and node.value is not None
                and _uses_table_value(node.value, helper_sources)
            ]
            if not returns:
                continue
            nested_expressions, nested_helpers = _resolved_table_expressions(
                helper,
                returns,
                module_tree,
                source_names=helper_sources,
                visited_helpers={*visited_helpers, id(helper)},
            )
            used_helpers.append(helper)
            used_helpers.extend(nested_helpers)
            expressions.extend(nested_expressions)
    unique_helpers = {id(helper): helper for helper in used_helpers}
    return expressions, list(unique_helpers.values())


def _table_call_parameters(call, helper, source_names):
    parameters = [
        argument.arg
        for argument in (
            list(helper.args.posonlyargs)
            + list(helper.args.args)
        )
    ]
    keyword_parameters = {
        argument.arg
        for argument in helper.args.kwonlyargs
    } | set(parameters)
    result = set()
    for index, argument in enumerate(call.args):
        if (
            index < len(parameters)
            and _uses_table_value(argument, source_names)
        ):
            result.add(parameters[index])
    for keyword in call.keywords:
        if (
            keyword.arg in keyword_parameters
            and _uses_table_value(keyword.value, source_names)
        ):
            result.add(keyword.arg)
    return result


def _matches_table_shape(shape, expressions, *, helper_names=()):
    if shape == "mapping":
        return any(
            isinstance(expression, (ast.Dict, ast.DictComp))
            or (
                isinstance(expression, ast.Call)
                and _call_name(expression.func) in {"dict", "OrderedDict"}
            )
            for expression in expressions
        )
    if shape in {"records", "list", "action_sequence"}:
        return any(
            isinstance(expression, (ast.List, ast.ListComp))
            or (
                isinstance(expression, ast.Call)
                and _call_name(expression.func) in {"list", "tuple"}
            )
            for expression in expressions
        )
    if shape == "object":
        collection_calls = {
            "dict", "list", "tuple", "set", "sorted", "reversed",
        }
        return any(
            isinstance(expression, ast.Call)
            and _call_name(expression.func) not in collection_calls
            and _call_name(expression.func) not in helper_names
            for expression in expressions
        )
    return False


def _reorders_table(expressions):
    reorder_calls = {"reversed", "sorted", "set"}
    return any(
        isinstance(child, ast.Call)
        and _call_name(child.func) in reorder_calls
        for expression in expressions
        for child in ast.walk(expression)
    )


def _validate_each_row_code(
        step_id,
        usage,
        page_function,
        table_parameters,
        *,
        owner_label="Page Object 方法",
    planned_operations=(),
        brief=None,
):
    business_receivers = _table_business_receivers(
        page_function,
        table_parameters,
    )
    loops = [
        node
        for node in ast.walk(page_function)
        if isinstance(node, (ast.For, ast.AsyncFor))
        and any(
            _expression_uses_name(node.iter, parameter)
            for parameter in table_parameters
        )
    ]
    if not loops:
        return [
            f"Step {step_id} each_row {owner_label} "
            f"{page_function.name} 缺少逐行迭代"
        ]
    errors = []
    if usage.get("ordered") and any(
        not _ordered_iteration(loop.iter, table_parameters)
        for loop in loops
    ):
        errors.append(f"Step {step_id} each_row 未保持表格顺序")
    reset_calls = [
        child
        for loop in loops
        for child in _direct_loop_calls(loop)
        if _is_each_row_reset_call(
            child,
            step_id,
            planned_operations,
            business_receivers,
            brief,
        )
    ]
    if usage.get("reset_between_rows") and not reset_calls:
        errors.append(f"Step {step_id} each_row 缺少逐行重置")
    if usage.get("reset_between_rows") is False and reset_calls:
        errors.append(f"Step {step_id} each_row 不应逐行重置")
    loop_operations = (
        [
            operation
            for operation in planned_operations
            if isinstance(operation, dict)
        ]
        if owner_label == "Page Object 方法"
        else [
            operation
            for operation in planned_operations
            if isinstance(operation, dict)
            and operation.get("implementation_location")
            == "step_inline_base_api"
        ]
    )
    if loop_operations and not any(
        _loop_contains_ordered_operations(
            loop,
            loop_operations,
            business_receivers,
        )
        for loop in loops
    ):
        errors.append(
            f"Step {step_id} each_row Step Definition "
            "缺少循环内有序计划调用"
        )
    return errors


def _direct_loop_calls(loop):
    return [
        statement.value
        for statement in loop.body
        if isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
    ]


def _loop_contains_ordered_operations(
        loop,
        operations,
        business_receivers,
    ):
    records = []
    for node in _direct_loop_calls(loop):
        receiver, view_owner = _call_receiver(node.func)
        if receiver not in business_receivers:
            continue
        records.append({
            "name": _call_name(node.func),
            "receiver": receiver,
            "view_owner": view_owner,
            "target": _literal_arg(node, 0),
            "value": _literal_arg(node, 1),
            "args": [
                _literal_arg(node, index)
                for index in range(len(node.args))
            ],
            "_arg_references": [
                _argument_reference(argument) for argument in node.args
            ],
            "_arg_runtime_bindings": [
                sorted(_runtime_binding_references(argument))
                for argument in node.args
            ],
            "_arg_table_columns": [
                sorted(_subscript_columns(argument))
                for argument in node.args
            ],
            "keywords": {
                keyword.arg: _literal_value(keyword.value)
                for keyword in node.keywords
                if keyword.arg
            },
            "_keyword_table_columns": {
                keyword.arg: sorted(_subscript_columns(keyword.value))
                for keyword in node.keywords
                if keyword.arg
            },
            "_keyword_references": {
                keyword.arg: _argument_reference(keyword.value)
                for keyword in node.keywords
                if keyword.arg
            },
            "_keyword_runtime_bindings": {
                keyword.arg: sorted(
                    _runtime_binding_references(keyword.value)
                )
                for keyword in node.keywords
                if keyword.arg
            },
        })
    cursor = 0
    for operation in operations:
        loop_operation = {
            key: value for key, value in operation.items()
            if key != "implementation_location"
        }
        match = _find_ordered_call(records, cursor, loop_operation)
        if match is None:
            return False
        cursor = match + 1
    return True


def _is_each_row_reset_call(
        call,
    step_id,
        planned_operations,
        business_receivers,
    brief,
):
    name = _call_name(call.func)
    receiver = _root_call_receiver(call.func)
    if receiver not in business_receivers:
        return False
    planned_resets = [
        operation
        for operation in planned_operations
        if isinstance(operation, dict)
        and _operation_is_row_reset(step_id, operation, brief)
    ]
    if not planned_resets:
        return name == "reset" or name.startswith("reset_")
    if any(
        name == str(
            operation.get("implementation_method") or ""
        ).rsplit(".", 1)[-1]
        for operation in planned_resets
        if operation.get("implementation_method")
    ):
        return True
    target = _normalize_target(_call_target(call))
    return bool(target) and any(
        str(operation.get("op") or "") == name
        and _normalize_target(operation.get("target")).casefold()
        == target.casefold()
        for operation in planned_resets
    )


def _operation_is_row_reset(step_id, operation, brief):
    if "reset" in _normalize_target(operation.get("target")).casefold():
        return True
    action_id = str(operation.get("target_action_id") or "")
    action = next((
        item
        for item in (brief or {}).get("actions") or ()
        if isinstance(item, dict)
        and str(item.get("id") or "") == action_id
        and str(item.get("step_id") or "") == str(step_id)
    ), None)
    target = (action or {}).get("target") or {}
    return any(
        "reset" in _normalize_target(value).casefold()
        for value in (
            target.get("name"),
            target.get("locator_name"),
        )
        if value
    )


def _table_business_receivers(function, table_parameters):
    parameters = {
        argument.arg
        for argument in (
            list(function.args.posonlyargs)
            + list(function.args.args)
            + list(function.args.kwonlyargs)
        )
    }
    receivers = parameters - set(table_parameters) - {"context", "ctx"}
    assignments = [
        node
        for node in ast.walk(function)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
    ]
    changed = True
    while changed:
        changed = False
        for assignment in assignments:
            targets = (
                assignment.targets
                if isinstance(assignment, ast.Assign)
                else [assignment.target]
            )
            names = {
                target.id
                for target in targets
                if isinstance(target, ast.Name)
            }
            value = assignment.value
            source = _root_expression_name(value)
            if not names or not (
                isinstance(value, ast.Call)
                and _call_name(value.func) == "get_page"
                or source in receivers
            ):
                continue
            new_names = names - receivers
            if new_names:
                receivers.update(new_names)
                changed = True
    return receivers


def _root_call_receiver(function):
    return (
        _root_expression_name(function.value)
        if isinstance(function, ast.Attribute)
        else None
    )


def _root_expression_name(value):
    while isinstance(value, ast.Attribute):
        value = value.value
    return value.id if isinstance(value, ast.Name) else None


def _call_target(call):
    target = _literal_arg(call, 0)
    if target is not None:
        return target
    for keyword in call.keywords:
        if keyword.arg in {"target", "locator", "locator_or_name"}:
            return _literal_value(keyword.value)
    return None


def _expression_uses_name(expression, name):
    return any(
        isinstance(child, ast.Name) and child.id == name
        for child in ast.walk(expression)
    )


def _ordered_iteration(expression, table_parameters):
    if isinstance(expression, ast.Name):
        return expression.id in table_parameters
    if not isinstance(expression, ast.Call):
        return False
    if _call_name(expression.func) not in {"enumerate", "iter", "list", "tuple"}:
        return False
    return any(
        _expression_uses_name(expression, parameter)
        for parameter in table_parameters
    )


def _string_constants(*nodes):
    return {
        child.value
        for node in nodes
        if node is not None
        for child in ast.walk(node)
        if isinstance(child, ast.Constant)
        and isinstance(child.value, str)
    }


def _context_table_assignment(tree, context_key):
    if not context_key:
        return None
    table_values = _table_derived_names(tree)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        if not any(
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "context"
            and target.attr == context_key
            for target in targets
        ):
            continue
        if _uses_table_value(value, table_values):
            return value
    return None


def validations_passed(validations):
    return all(
        value.get("status") == "passed"
        for value in (validations or {}).values()
    )


def _call_scope_map(tree):
    scopes = {}

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.classes = []
            self.functions = []

        def visit_ClassDef(self, node):
            self.classes.append(node.name)
            self.generic_visit(node)
            self.classes.pop()

        def visit_FunctionDef(self, node):
            self.functions.append(node.name)
            self.generic_visit(node)
            self.functions.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            if self.functions:
                method = self.functions[-1]
                scopes[id(node)] = (
                    f"{self.classes[-1]}.{method}"
                    if self.classes
                    else method
                )
            self.generic_visit(node)

    Visitor().visit(tree)
    return scopes


def _validate_step_method_orchestration(
        step_id,
        step,
        project_root,
        trees_by_path,
        *,
        window_owners=None,
        brief=None,
        strict_provenance=False,
        step_text=None,
):
    if not strict_provenance:
        return [], {}, {}, {}
    behavior_file = step.get("behavior_file")
    operations = step.get("operations") or []
    if not behavior_file or not operations:
        return [], {}, {}, {}
    path = _owned_path(project_root, behavior_file)
    if path is None or not path.is_file():
        return [
            f"Step {step_id} behavior_file 不存在: {behavior_file}"
        ], {}, {}, {}
    segments = []
    previous = None
    for operation in operations:
        method = str(operation.get("implementation_method") or "")
        if method and method != previous:
            segments.append(operation)
            previous = method
    if not segments:
        return [], {}, {}, {}
    if strict_provenance and not str(step_text or "").strip():
        return [
            f"Step {step_id} 缺少 Request Gherkin 文本，"
            "无法验证 behavior_file 编排"
        ], {}, {}, {}
    tree = trees_by_path.get(str(path))
    functions = _matching_step_functions(tree, step_text)
    if not functions:
        return [
            f"Step {step_id} behavior_file 缺少 Step 函数"
        ], {}, {}, {}
    if len(functions) != 1:
        return [
            f"Step {step_id} behavior_file Step 函数匹配不唯一: "
            f"{[node.name for node in functions]}"
        ], {}, {}, {}
    routes = []
    errors = []
    for operation in segments:
        route, route_errors = _declared_method_route(
            operation,
            project_root,
            window_owners or {},
            trees_by_path,
        )
        errors.extend(route_errors)
        if route is not None:
            routes.append(route)
    imports = _direct_imports(tree)
    get_page_names = {
        local_name
        for local_name, identity in imports.items()
        if identity in {
            ("autowork_core.page", "get_page"),
            ("autowork_core.page.singleton", "get_page"),
        }
    }
    for route in routes:
        if route["page_identity"] not in imports.values():
            module, class_name = route["page_identity"]
            errors.append(
                f"Step {step_id} 必须从 {module} 直接导入 {class_name}"
            )
    bindings = _canonical_page_bindings(
        functions[0],
        imports,
        get_page_names,
        context_name=_first_parameter(functions[0]),
    )
    table_usage = step.get("table_usage") or {}
    require_table_loop = bool(
        table_usage.get("consumer") == "step_definition"
        and table_usage.get("consumption") == "each_row"
    )
    step_table_parameters = (
        _table_derived_names(functions[0])
        if require_table_loop
        else set()
    )
    calls = _canonical_step_calls(
        functions[0],
        table_parameters=step_table_parameters,
    )
    if require_table_loop:
        calls = [
            call for call in calls
            if call.get("inside_table_loop") is True
        ]
    cursor = 0
    runtime_parameters = {}
    table_parameters = {}
    calls_by_operation = {}
    for route in routes:
        match = next(
            (
                index for index in range(cursor, len(calls))
                if _matches_declared_route(calls[index], route, bindings)
            ),
            None,
        )
        if match is None:
            break
        call = calls[match]
        method_operations = [
            operation
            for operation in operations
            if str(operation.get("implementation_method") or "")
            == route["implementation_method"]
        ]
        if any(
            _operation_uses_exact_method_reuse(operation, brief)
            for operation in method_operations
        ):
            errors.extend(_validate_reuse_method_call_arguments(
                call,
                route,
                method_operations,
            ))
            for operation in method_operations:
                calls_by_operation[id(operation)] = {
                    "path": str(path),
                    "line": call.get("line"),
                    "name": route.get("method_name"),
                }
        for operation in operations:
            if str(operation.get("implementation_method") or "") != (
                    route["implementation_method"]
            ):
                continue
            source = str(operation.get("source") or "")
            if source.startswith("table."):
                column = source.split(".", 1)[1]
                parameters = _step_table_call_parameters(
                    call,
                    route.get("parameters") or (),
                    column,
                )
                if not parameters:
                    errors.append(
                        f"Step {step_id} 业务方法 "
                        f"{route['implementation_method']} 未接收 "
                        f"table column {column}"
                    )
                    continue
                table_parameters.setdefault((
                    route["implementation_method"],
                    column,
                ), set()).update(parameters)
                continue
            if not source.startswith("runtime."):
                continue
            binding = source.split(".", 1)[1]
            parameters = _runtime_call_parameters(
                call,
                route.get("parameters") or (),
                binding,
            )
            if not parameters:
                errors.append(
                    f"Step {step_id} 业务方法 "
                    f"{route['implementation_method']} 未接收 "
                    f"runtime binding {binding}"
                )
                continue
            method_binding = runtime_parameters.setdefault((
                route["implementation_method"],
                binding,
            ), {
                "parameters": set(),
                "path": str(path),
            })
            method_binding["parameters"].update(parameters)
        cursor = match + 1
    else:
        return (
            errors,
            runtime_parameters,
            table_parameters,
            calls_by_operation,
        )
    errors.append(
        f"Step {step_id} behavior_file 缺少业务方法编排: "
        f"{[route['implementation_method'] for route in routes]}"
    )
    return errors, runtime_parameters, table_parameters, calls_by_operation


def _declared_method_route(
        operation,
        project_root,
        window_owners,
        trees_by_path,
):
    errors = []
    implementation_method = str(
        operation.get("implementation_method") or ""
    )
    if "." not in implementation_method:
        return None, errors
    method_owner, method_name = implementation_method.rsplit(".", 1)
    window_owner = str(operation.get("window_owner") or "")
    owner = window_owners.get(window_owner)
    if not isinstance(owner, dict):
        return None, [f"未知 window_owner: {window_owner}"]
    page_path = _owned_path(project_root, owner.get("page_object"))
    page_tree = trees_by_path.get(str(page_path)) if page_path else None
    page_module = _module_name(project_root, page_path)
    if page_tree is None or not page_module:
        return None, [f"window_owner {window_owner} Page Object 无法解析"]
    view_owner = str(operation.get("view_owner") or "")
    if not view_owner:
        method = _class_method(page_tree, method_owner, method_name)
        if method is None:
            errors.append(
                f"{implementation_method} 不在声明的 Page Object 中"
            )
        return {
            "implementation_method": implementation_method,
            "page_identity": (page_module, method_owner),
            "view_owner": None,
            "method_name": method_name,
            "parameters": _bound_parameters(method) if method else [],
        }, errors

    view = (owner.get("views") or {}).get(view_owner) or {}
    view_path = _owned_path(project_root, view.get("view_object"))
    view_tree = trees_by_path.get(str(view_path)) if view_path else None
    view_module = _module_name(project_root, view_path)
    if view_tree is None or not view_module:
        return None, [
            f"view_owner {window_owner}.{view_owner} View Object 无法解析"
        ]
    method = _class_method(view_tree, method_owner, method_name)
    if method is None:
        errors.append(
            f"{implementation_method} 不在声明的 View Object 中"
        )
    declared_active = _class_string_attribute(
        view_tree,
        method_owner,
        "active_locator",
    )
    planned_active = str(view.get("active_locator") or "")
    if declared_active.lstrip("$") != planned_active.lstrip("$"):
        errors.append(
            f"view_owner {window_owner}.{view_owner} active_locator "
            "与 Plan 不一致"
        )
    declared_root = normalize(_class_string_attribute(
        view_tree,
        method_owner,
        "root_locator",
    ))
    planned_root = normalize(str(view.get("root_locator") or ""))
    if declared_root != planned_root:
        errors.append(
            f"view_owner {window_owner}.{view_owner} root_locator "
            "与 Plan 不一致"
        )
    page_classes = _classes_with_declared_view(
        page_tree,
        view_owner,
        (view_module, method_owner),
    )
    if len(page_classes) != 1:
        errors.append(
            f"view_owner {window_owner}.{view_owner} 必须由声明 Page "
            "唯一直接绑定"
        )
        return None, errors
    return {
        "implementation_method": implementation_method,
        "page_identity": (page_module, page_classes[0]),
        "view_owner": view_owner,
        "method_name": method_name,
        "parameters": _bound_parameters(method) if method else [],
    }, errors


def _module_name(project_root, path):
    if path is None:
        return None
    try:
        relative = Path(path).resolve().relative_to(
            Path(project_root).resolve()
        )
    except ValueError:
        return None
    if relative.suffix.casefold() != ".py":
        return None
    parts = list(relative.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) if parts else None


def _class_defines_method(tree, class_name, method_name):
    return _class_method(tree, class_name, method_name) is not None


def _class_method(tree, class_name, method_name):
    return next((
        member
        for node in (getattr(tree, "body", None) or [])
        if isinstance(node, ast.ClassDef) and node.name == class_name
        for member in node.body
        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
        and member.name == method_name
    ), None)


def _classes_with_declared_view(tree, property_name, view_identity):
    imports = _direct_imports(tree)
    result = []
    for class_node in (
        node
        for node in (getattr(tree, "body", None) or [])
        if isinstance(node, ast.ClassDef)
    ):
        properties = [
            member
            for member in class_node.body
            if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
            and member.name == property_name
            and any(
                _call_name(decorator) == "property"
                for decorator in member.decorator_list
            )
        ]
        if len(properties) != 1:
            continue
        annotation = properties[0].returns
        if not (
            isinstance(annotation, ast.Name)
            and imports.get(annotation.id) == view_identity
        ):
            continue
        returns = [
            statement.value
            for statement in properties[0].body
            if isinstance(statement, ast.Return)
        ]
        if len(returns) != 1:
            continue
        value = returns[0]
        if not (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Attribute)
            and isinstance(value.func.value, ast.Name)
            and value.func.value.id == "self"
            and value.func.attr == "get_view"
            and len(value.args) == 1
            and isinstance(value.args[0], ast.Name)
            and imports.get(value.args[0].id) == view_identity
        ):
            continue
        result.append(class_node.name)
    return result


def _class_string_attribute(tree, class_name, attribute_name):
    for node in (getattr(tree, "body", None) or []):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for statement in node.body:
            if isinstance(statement, ast.Assign):
                targets = statement.targets
                value = statement.value
            elif isinstance(statement, ast.AnnAssign):
                targets = [statement.target]
                value = statement.value
            else:
                continue
            if not any(
                isinstance(target, ast.Name)
                and target.id == attribute_name
                for target in targets
            ):
                continue
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return value.value
    return ""


def _direct_imports(tree):
    imports = {}
    for node in (getattr(tree, "body", None) or []):
        if (
            not isinstance(node, ast.ImportFrom)
            or node.level
            or not node.module
        ):
            continue
        for alias in node.names:
            if alias.name == "*":
                continue
            imports[alias.asname or alias.name] = (
                node.module,
                alias.name,
            )
    return imports


def _first_parameter(function):
    positional = [
        *function.args.posonlyargs,
        *function.args.args,
    ]
    return positional[0].arg if positional else None


def _canonical_page_bindings(
        function,
        imports,
        get_page_names,
        *,
        context_name,
):
    bindings = {}
    if not context_name:
        return bindings
    for statement in function.body:
        if isinstance(statement, ast.Assign):
            targets = statement.targets
            value = statement.value
        elif isinstance(statement, ast.AnnAssign):
            targets = [statement.target]
            value = statement.value
        else:
            continue
        if not (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id in get_page_names
            and len(value.args) >= 2
            and isinstance(value.args[0], ast.Name)
            and value.args[0].id == context_name
            and isinstance(value.args[1], ast.Name)
        ):
            continue
        page_identity = imports.get(value.args[1].id)
        if page_identity is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                bindings[target.id] = page_identity
    return bindings


def _canonical_step_calls(function, *, table_parameters=()):
    calls = []

    def collect(statements, *, inside_table_loop=False):
        for statement in statements:
            if isinstance(statement, (ast.For, ast.AsyncFor)):
                current_table_loop = bool(
                    inside_table_loop
                    or any(
                        _expression_uses_name(statement.iter, parameter)
                        for parameter in table_parameters
                    )
                )
                collect(
                    statement.body,
                    inside_table_loop=current_table_loop,
                )
                continue
            if not (
                isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Call)
                and isinstance(statement.value.func, ast.Attribute)
            ):
                continue
            node = statement.value
            value = node.func.value
            receiver = None
            view_owner = None
            if isinstance(value, ast.Name):
                receiver = value.id
            elif (
                isinstance(value, ast.Attribute)
                and isinstance(value.value, ast.Name)
            ):
                receiver = value.value.id
                view_owner = value.attr
            if receiver:
                calls.append({
                    "receiver": receiver,
                    "view_owner": view_owner,
                    "method_name": node.func.attr,
                    "line": getattr(node, "lineno", 0),
                    "inside_table_loop": inside_table_loop,
                    "args": [
                        _literal_value(argument) for argument in node.args
                    ],
                    "arg_references": [
                        _argument_reference(argument) for argument in node.args
                    ],
                    "_arg_table_columns": [
                        sorted(_subscript_columns(argument))
                        for argument in node.args
                    ],
                    "keywords": {
                        keyword.arg: _literal_value(keyword.value)
                        for keyword in node.keywords
                        if keyword.arg
                    },
                    "keyword_references": {
                        keyword.arg: _argument_reference(keyword.value)
                        for keyword in node.keywords
                        if keyword.arg
                    },
                    "_keyword_table_columns": {
                        keyword.arg: sorted(
                            _subscript_columns(keyword.value)
                        )
                        for keyword in node.keywords
                        if keyword.arg
                    },
                    "arg_runtime_bindings": [
                        _runtime_binding_references(argument)
                        for argument in node.args
                    ],
                    "keyword_runtime_bindings": {
                        keyword.arg: _runtime_binding_references(keyword.value)
                        for keyword in node.keywords
                        if keyword.arg
                    },
                })

    collect(function.body)
    return calls


def _operation_uses_exact_method_reuse(operation, brief):
    resolution = operation.get("implementation_resolution") or {}
    if resolution.get("strategy") != "reuse":
        return False
    candidate = (_brief_implementation_candidates(brief) or {}).get(
        str(resolution.get("candidate_id") or "")
    ) or {}
    return bool(candidate.get("call_sequence"))


def _validate_reuse_method_call_arguments(call, route, operations):
    inputs = []
    qualified = False
    for operation in operations:
        source = str(operation.get("source") or "")
        provenance = operation.get("value_provenance") or {}
        if provenance.get("kind") or source.startswith("runtime."):
            qualified = True
        descriptor = None
        if source.startswith("examples."):
            descriptor = {
                "kind": "reference",
                "value": source.split(".", 1)[1],
            }
        elif source.startswith(("runtime.", "table.")):
            continue
        elif operation.get("value") is not None and provenance.get("kind"):
            descriptor = {
                "kind": "literal",
                "value": operation.get("value"),
            }
        if descriptor is not None and descriptor not in inputs:
            inputs.append(descriptor)
    if not qualified:
        return []
    parameters = list(route.get("parameters") or ())
    if len(inputs) != len(parameters):
        return [
            f"业务方法 {route['implementation_method']} 调用参数数量不一致: "
            f"expected={len(inputs)} actual={len(parameters)}"
        ]
    errors = []
    for index, (descriptor, parameter) in enumerate(zip(inputs, parameters)):
        keywords = call.get("keywords") or {}
        references = call.get("keyword_references") or {}
        args = call.get("args") or []
        arg_references = call.get("arg_references") or []
        actual = keywords.get(parameter) if parameter in keywords else (
            args[index] if index < len(args) else None
        )
        actual_reference = (
            references.get(parameter)
            if parameter in references
            else (
                arg_references[index]
                if index < len(arg_references)
                else None
            )
        )
        if descriptor["kind"] == "literal" and actual != descriptor["value"]:
            errors.append(
                f"业务方法 {route['implementation_method']} 参数 {parameter} "
                "未使用冻结值"
            )
        elif (
            descriptor["kind"] == "reference"
            and actual_reference != descriptor["value"]
        ):
            errors.append(
                f"业务方法 {route['implementation_method']} 参数 {parameter} "
                f"未引用Step参数 {descriptor['value']}"
            )
    return errors


def _runtime_call_parameters(call, parameters, binding):
    result = set()
    for index, bindings in enumerate(
            call.get("arg_runtime_bindings") or ()
    ):
        if binding in bindings and index < len(parameters):
            result.add(parameters[index])
    parameter_names = set(parameters)
    for keyword, bindings in (
            call.get("keyword_runtime_bindings") or {}
    ).items():
        if binding in bindings and keyword in parameter_names:
            result.add(keyword)
    return result


def _step_table_call_parameters(call, parameters, column):
    result = set()
    for index, columns in enumerate(call.get("_arg_table_columns") or ()):
        if column in columns and index < len(parameters):
            result.add(parameters[index])
    parameter_names = set(parameters)
    for keyword, columns in (
        call.get("_keyword_table_columns") or {}
    ).items():
        if column in columns and keyword in parameter_names:
            result.add(keyword)
    return result


def _matches_declared_route(call, route, bindings):
    return all((
        bindings.get(call.get("receiver")) == route["page_identity"],
        call.get("view_owner") == route["view_owner"],
        call.get("method_name") == route["method_name"],
    ))


def _matching_step_functions(tree, step_text):
    functions = [
        node
        for node in (getattr(tree, "body", None) or [])
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    if not step_text:
        return functions
    matched = []
    for node in functions:
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            if decorator_name(decorator.func) not in STEP_DECORATORS:
                continue
            pattern = decorator_pattern(decorator)
            if pattern is not None and re.fullmatch(
                step_pattern_to_regex(pattern),
                step_text,
            ):
                matched.append(node)
                break
    return matched


def _validate_modified_step_behaviors(
    project_root,
    plan,
    trees_by_path,
    request,
):
    errors = []
    for step_id, step in (plan or {}).items():
        resolution = (step or {}).get("behavior_resolution") or {}
        if resolution.get("strategy") != "modify":
            continue
        path = _owned_path(project_root, (step or {}).get("behavior_file"))
        tree = trees_by_path.get(str(path)) if path is not None else None
        symbol = str(resolution.get("symbol") or "")
        pattern = str(resolution.get("step_pattern") or "")
        decorator_name_value = str(
            resolution.get("step_decorator") or ""
        ).casefold()
        target_step = next(
            (
                item
                for item in ((request or {}).get("target") or {}).get(
                    "steps"
                ) or ()
                if str(item.get("id") or "") == str(step_id)
            ),
            {},
        )
        contract = {
            "decorator": decorator_name_value,
            "pattern": pattern,
        }
        if any((
                path is None,
                tree is None,
                not symbol,
                not pattern,
                not decorator_name_value,
                not step_pattern_contract_matches(contract, target_step),
        )):
            errors.append(
                f"Step {step_id} modify缺少冻结的既有Step定义身份"
            )
            continue
        matches = [
            node
            for node in (tree.body or [])
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(
                decorator_name(decorator.func) == decorator_name_value
                and decorator_pattern(decorator) == pattern
                for decorator in node.decorator_list
                if isinstance(decorator, ast.Call)
                and decorator_name(decorator.func) in STEP_DECORATORS
            )
        ]
        if len(matches) != 1 or matches[0].name != symbol:
            errors.append(
                f"Step {step_id} modify不得新增或替换冻结的Step decorator"
            )
    return errors


def _validate_issue_placeholders(
        project_root,
        steps,
        request,
        trees_by_path,
    ):
    errors = []
    trace = []
    for step_id, step in (steps or {}).items():
        issues = list((step or {}).get("unresolved_issues") or ())
        if not issues:
            continue
        path = _owned_path(project_root, (step or {}).get("behavior_file"))
        tree = trees_by_path.get(str(path)) if path is not None else None
        step_text = _request_step_text(request, step_id)
        functions = _matching_step_functions(tree, step_text)
        if path is None or tree is None or len(functions) != 1:
            errors.append(
                f"Step {step_id} typed issue placeholder无法唯一定位Step函数"
            )
            continue
        imports = _direct_imports(tree)
        helper_names = {
            local_name
            for local_name, identity in imports.items()
            if identity == (
                "autowork_core.runtime.generation_issue",
                "unresolved_generation_issue",
            )
        }
        if len(helper_names) != 1:
            errors.append(
                f"Step {step_id} typed issue placeholder必须直接导入"
                " unresolved_generation_issue"
            )
            continue
        function = functions[0]
        body = list(function.body)
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body = body[1:]
        calls = [
            statement.value
            for statement in body
            if isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Call)
            and isinstance(statement.value.func, ast.Name)
            and statement.value.func.id in helper_names
        ]
        if len(body) != len(issues) or len(calls) != len(issues):
            errors.append(
                f"Step {step_id} typed issue placeholder只能包含计划helper调用"
            )
            continue
        for issue, call in zip(issues, calls):
            keywords = {
                keyword.arg: _literal_value(keyword.value)
                for keyword in call.keywords
                if keyword.arg
            }
            if any((
                _literal_arg(call, 0) != issue.get("issue_id"),
                keywords.get("step_id") != issue.get("step_id"),
                keywords.get("issue_type") != issue.get("issue_type"),
                len(call.args) != 1,
                set(keywords) != {"step_id", "issue_type"},
            )):
                errors.append(
                    f"Step {step_id} typed issue placeholder参数与Plan不一致"
                )
                continue
            trace.append({
                "step_id": str(step_id),
                "action_ids": list(issue.get("action_ids") or ()),
                "implementation_location": "typed_issue_placeholder",
                "implementation_method": None,
                "path": _project_relative_path(project_root, path),
                "line": getattr(call, "lineno", 0),
                "call": "unresolved_generation_issue",
                "issue_id": issue.get("issue_id"),
            })
    return errors, trace


def _window_owner_scopes(project_root, owners):
    errors = []
    scopes = {}
    for owner_id, owner in owners.items():
        if not isinstance(owner, dict):
            errors.append(f"window_owner {owner_id} 必须是 object")
            continue
        page_path = _owned_path(project_root, owner.get("page_object"))
        root_path = _owned_path(
            project_root,
            owner.get("root_locator_file"),
        )
        if page_path is None or not page_path.is_file():
            errors.append(
                f"window_owner {owner_id} Page Object 不存在: "
                f"{owner.get('page_object')}"
            )
        if root_path is None or not root_path.is_file():
            errors.append(
                f"window_owner {owner_id} root locator 文件不存在: "
                f"{owner.get('root_locator_file')}"
            )
            continue
        try:
            root_data = yaml.safe_load(
                root_path.read_text(encoding="utf-8")
            ) or {}
            root_compiled = compile_locators(root_data)
        except Exception as error:
            errors.append(
                f"window_owner {owner_id} root locator 无效: "
                f"{type(error).__name__}: {error}"
            )
            continue

        view_data = []
        view_scopes = {}
        for view_id, view in (owner.get("views") or {}).items():
            view_path = _owned_path(
                project_root,
                (view or {}).get("locator_file"),
            )
            view_object = _owned_path(
                project_root,
                (view or {}).get("view_object"),
            )
            if view_path is None or not view_path.is_file():
                errors.append(
                    f"view_owner {owner_id}.{view_id} locator 文件不存在: "
                    f"{(view or {}).get('locator_file')}"
                )
                continue
            if view_object is not None and not view_object.is_file():
                errors.append(
                    f"view_owner {owner_id}.{view_id} View Object 不存在: "
                    f"{(view or {}).get('view_object')}"
                )
            try:
                raw_view = yaml.safe_load(
                    view_path.read_text(encoding="utf-8")
                ) or {}
                planned_view_root = normalize(str(
                    (view or {}).get("root_locator") or ""
                ))
                if planned_view_root:
                    owned_package = compile_window_locator_package(
                        raw_view,
                        package_name=str(
                            (view or {}).get("locator_file") or ""
                        ),
                    )
                    if owned_package.root_name != planned_view_root:
                        raise ValueError(
                            "WindowView root_locator 不匹配: "
                            f"declared={planned_view_root}, "
                            f"actual={owned_package.root_name}"
                        )
                    compiled_view = owned_package.locators
                else:
                    compiled_view = compile_locators(
                        raw_view,
                        external_locators=root_compiled,
                    )
            except Exception as error:
                errors.append(
                    f"view_owner {owner_id}.{view_id} locator 无效: "
                    f"{type(error).__name__}: {error}"
                )
                continue
            if not (view or {}).get("root_locator"):
                view_data.append(raw_view)
            view_scopes[str(view_id)] = {
                "python": [view_object] if view_object is not None else [],
                "locator_keys": set(compiled_view),
            }
        try:
            package = compile_window_locator_package(root_data, view_data)
            if package.root_name != normalize(str(
                owner.get("root_locator") or ""
            )):
                errors.append(
                    f"window_owner {owner_id} root_locator 不匹配: "
                    f"declared={owner.get('root_locator')} "
                    f"actual={package.root_name}"
                )
        except Exception as error:
            errors.append(
                f"window_owner {owner_id} locator 包无效: "
                f"{type(error).__name__}: {error}"
            )
        scopes[str(owner_id)] = {
            "page_python": [page_path] if page_path is not None else [],
            "root_locator_keys": set(root_compiled),
            "views": view_scopes,
        }
    return errors, scopes


def _owned_path(project_root, value):
    if not value:
        return None
    project_root = Path(project_root).resolve()
    path = _absolute(project_root, Path(str(value)))
    try:
        path.relative_to(project_root)
    except ValueError:
        return None
    return path


def _absolute(project_root, path):
    path = Path(path)
    return (
        path.resolve()
        if path.is_absolute()
        else (project_root / path).resolve()
    )


def _result(errors, paths, **extra):
    return {
        "status": "failed" if errors else "passed",
        "paths": [str(path) for path in paths],
        "errors": errors,
        **extra,
    }


def _call_name(value):
    if isinstance(value, ast.Attribute):
        return value.attr
    if isinstance(value, ast.Name):
        return value.id
    return ""


def _call_receiver(value):
    if not isinstance(value, ast.Attribute):
        return None, None
    receiver = value.value
    if isinstance(receiver, ast.Name):
        return receiver.id, None
    if (
        isinstance(receiver, ast.Attribute)
        and isinstance(receiver.value, ast.Name)
    ):
        return receiver.value.id, receiver.attr
    return None, None


def _step_inline_scope(
        step_id,
        step,
        project_root,
        trees_by_path,
        call_records_by_path,
        window_owners,
        step_text,
):
    behavior_file = step.get("behavior_file")
    path = _owned_path(project_root, behavior_file)
    if path is None or not path.is_file():
        return _empty_inline_scope(step_id), [
            f"Step {step_id} step_inline_base_api behavior_file 不存在: "
            f"{behavior_file}"
        ]
    tree = trees_by_path.get(str(path))
    functions = _matching_step_functions(tree, step_text)
    if len(functions) != 1:
        return _empty_inline_scope(step_id), [
            f"Step {step_id} step_inline_base_api 无法唯一定位 Step 函数"
        ]
    function = functions[0]
    imports = _direct_imports(tree)
    get_page_names = {
        local_name
        for local_name, identity in imports.items()
        if identity in {
            ("autowork_core.page", "get_page"),
            ("autowork_core.page.singleton", "get_page"),
        }
    }
    bindings = _canonical_page_bindings(
        function,
        imports,
        get_page_names,
        context_name=_first_parameter(function),
    )
    errors = []
    owner_identities = {}
    for owner_id in {
        str(operation.get("window_owner") or "")
        for operation in step.get("operations") or ()
        if operation.get("implementation_location")
        == "step_inline_base_api"
    }:
        owner = (window_owners or {}).get(owner_id) or {}
        page_path = _owned_path(project_root, owner.get("page_object"))
        page_tree = trees_by_path.get(str(page_path)) if page_path else None
        page_module = _module_name(project_root, page_path)
        page_classes = _classes_with_root_locator(
            page_tree,
            owner.get("root_locator"),
        )
        if not page_module or len(page_classes) != 1:
            errors.append(
                f"Step {step_id} step_inline_base_api 无法唯一解析 "
                f"window_owner {owner_id} Page class"
            )
            continue
        identity = (page_module, page_classes[0])
        owner_identities[identity] = owner_id
        if identity not in imports.values():
            errors.append(
                f"Step {step_id} step_inline_base_api 必须从 "
                f"{page_module} 直接导入 {page_classes[0]}"
            )
        for operation in step.get("operations") or ():
            if (
                operation.get("implementation_location")
                != "step_inline_base_api"
                or str(operation.get("window_owner") or "") != owner_id
                or not operation.get("view_owner")
            ):
                continue
            errors.extend(_validate_inline_view_route(
                step_id,
                owner,
                page_tree,
                page_classes[0],
                str(operation.get("view_owner")),
                project_root,
                trees_by_path,
            ))
    records = []
    function_scope = function.name
    for record in call_records_by_path.get(str(path), ()):
        if record.get("_implementation_method") != function_scope:
            continue
        page_identity = bindings.get(record.get("receiver"))
        records.append({
            **record,
            "window_owner": owner_identities.get(page_identity),
        })
    constants = {
        node.value
        for node in ast.walk(function)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
    }
    return {
        "records": records,
        "constants": constants,
        "scope_key": ("step_inline", str(path), function.name),
    }, errors


def _empty_inline_scope(step_id):
    return {
        "records": [],
        "constants": set(),
        "scope_key": ("step_inline", str(step_id)),
    }


def _classes_with_root_locator(tree, root_locator):
    expected = normalize(str(root_locator or ""))
    if tree is None or not expected:
        return []
    return [
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and normalize(_class_string_attribute(
            tree,
            node.name,
            "root_locator",
        )) == expected
    ]


def _validate_inline_view_route(
        step_id,
        owner,
        page_tree,
        page_class,
        view_owner,
        project_root,
        trees_by_path,
):
    view = (owner.get("views") or {}).get(view_owner) or {}
    view_path = _owned_path(project_root, view.get("view_object"))
    view_tree = trees_by_path.get(str(view_path)) if view_path else None
    view_module = _module_name(project_root, view_path)
    planned_active = str(view.get("active_locator") or "").lstrip("$")
    planned_root = normalize(str(view.get("root_locator") or ""))
    active_classes = [
        node.name
        for node in (getattr(view_tree, "body", None) or [])
        if isinstance(node, ast.ClassDef)
        and _class_string_attribute(
            view_tree,
            node.name,
            "active_locator",
        ).lstrip("$") == planned_active
    ]
    view_classes = [
        class_name
        for class_name in active_classes
        if normalize(_class_string_attribute(
            view_tree,
            class_name,
            "root_locator",
        )) == planned_root
    ]
    if view_module and len(active_classes) == 1 and not view_classes:
        return [
            f"view_owner {view_owner} root_locator 与 Plan 不一致"
        ]
    if not view_module or len(view_classes) != 1:
        return [
            f"Step {step_id} step_inline_base_api 无法唯一解析 "
            f"view_owner {view_owner} View class"
        ]
    owners = _classes_with_declared_view(
        page_tree,
        view_owner,
        (view_module, view_classes[0]),
    )
    if owners != [page_class]:
        return [
            f"Step {step_id} step_inline_base_api view_owner "
            f"{view_owner} 未由 {page_class} 唯一绑定"
        ]
    return []


def _literal_arg(node, index):
    if len(node.args) <= index:
        return None
    try:
        return ast.literal_eval(node.args[index])
    except (ValueError, TypeError):
        return None


def _argument_reference(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _argument_reference(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _runtime_binding_references(node):
    references = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        if _call_name(child.func) != "get_variable":
            continue
        binding = _literal_arg(child, 0)
        if isinstance(binding, str) and binding:
            references.add(binding)
    return references


def snapshot_runtime_variable_calls(path):
    path = Path(path)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    except (OSError, SyntaxError, UnicodeError):
        return []
    aliases = _runtime_variable_aliases(tree)
    result = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        signature = _runtime_variable_call_signature(node, aliases)
        if signature is None:
            continue
        name, binding = signature
        result.append({
            "api": name,
            "binding": binding,
        })
    return sorted(
        result,
        key=lambda item: (
            str(item.get("api") or ""),
            str(item.get("binding") or ""),
        ),
    )


def _validate_added_runtime_variable_calls(
        project_root,
        changed_python_files,
        call_records_by_path,
    matched_runtime_calls,
        generation_input_snapshot,
    ):
    if not isinstance(generation_input_snapshot, dict):
        return []
    if generation_input_snapshot.get("snapshot_version") != "1.0":
        return []
    baseline = Counter()
    current = Counter()
    project_root = Path(project_root).resolve()
    snapshot_files = generation_input_snapshot.get("files") or {}
    for path in changed_python_files:
        path = Path(path).resolve()
        try:
            relative = path.relative_to(project_root).as_posix()
        except ValueError:
            continue
        for item in (
                (snapshot_files.get(relative) or {}).get(
                    "runtime_variable_calls"
                ) or ()
        ):
            if isinstance(item, dict):
                baseline[(item.get("api"), item.get("binding"))] += 1
        for record in call_records_by_path.get(str(path), ()):
            signature = _runtime_variable_record_signature(record)
            if signature is not None:
                current[signature] += 1
    added = current - baseline
    unexpected = added - Counter(matched_runtime_calls or {})
    return [
        "未由Plan声明的变量调用: "
        f"api={api} binding={binding!r} count={count}"
        for (api, binding), count in sorted(
            unexpected.items(),
            key=lambda item: (
                str(item[0][0] or ""),
                str(item[0][1] or ""),
            ),
        )
    ]


def _planned_runtime_call_signatures(operation):
    result = []
    op = str(operation.get("op") or "")
    binding = str(operation.get("result_binding") or "").strip()
    if op in {"save_attr", "save_text"} and binding:
        result.append((op, binding))
    source = str(operation.get("source") or "")
    if source.startswith("runtime."):
        result.append(("get_variable", source.split(".", 1)[1]))
    return result


def _runtime_variable_record_signature(record):
    signature = record.get("_runtime_variable_signature")
    if (
        isinstance(signature, (list, tuple))
        and len(signature) == 2
    ):
        return tuple(signature)
    name = str(record.get("name") or "")
    indexes = {
        "get_variable": 0,
        "save_attr": 2,
        "save_text": 1,
        "set_variable": 0,
    }
    if name not in indexes:
        return None
    binding = _call_argument(
        record,
        index=indexes[name],
        keyword="variable_name",
    )
    return name, binding if isinstance(binding, str) else None


_RUNTIME_VARIABLE_APIS = frozenset({
    "get_variable",
    "save_attr",
    "save_text",
    "set_variable",
})


def _runtime_variable_aliases(tree):
    aliases = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.module not in {
            "autowork_core.actions.variable_actions",
        }:
            continue
        for alias in node.names:
            if alias.name in _RUNTIME_VARIABLE_APIS:
                aliases[alias.asname or alias.name] = (
                    alias.name,
                    "function",
                )
    assignments = [
        (target, node.value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (
            node.targets if isinstance(node, ast.Assign) else [node.target]
        )
        if isinstance(target, ast.Name)
    ]
    changed = True
    while changed:
        changed = False
        for target, value in assignments:
            identity = _runtime_callable_identity(value, aliases)
            if identity is None or aliases.get(target.id) == identity:
                continue
            aliases[target.id] = identity
            changed = True
    return aliases


def _runtime_callable_identity(value, aliases):
    if (
        isinstance(value, ast.Attribute)
        and value.attr in _RUNTIME_VARIABLE_APIS
    ):
        return value.attr, "bound"
    if isinstance(value, ast.Name):
        if value.id in aliases:
            return aliases[value.id]
        if value.id in _RUNTIME_VARIABLE_APIS:
            return value.id, "function"
    if (
        isinstance(value, ast.Call)
        and _call_name(value.func) == "getattr"
        and len(value.args) >= 2
        and isinstance(value.args[1], ast.Constant)
        and value.args[1].value in _RUNTIME_VARIABLE_APIS
    ):
        return str(value.args[1].value), "bound"
    return None


def _runtime_variable_call_signature(node, aliases):
    identity = _runtime_callable_identity(node.func, aliases)
    if identity is None:
        return None
    api, style = identity
    indexes = {
        "bound": {
            "get_variable": 0,
            "save_attr": 2,
            "save_text": 1,
            "set_variable": 0,
        },
        "function": {
            "get_variable": 1,
            "save_attr": 3,
            "save_text": 2,
            "set_variable": 1,
        },
    }
    binding = next((
        keyword.value.value
        for keyword in node.keywords
        if keyword.arg == "variable_name"
        and isinstance(keyword.value, ast.Constant)
        and isinstance(keyword.value.value, str)
    ), None)
    if binding is None:
        binding = _literal_arg(node, indexes[style][api])
    return api, binding if isinstance(binding, str) else None


def _literal_value(node):
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        return None


def _subscript_columns(node):
    return {
        child.slice.value
        for child in ast.walk(node)
        if isinstance(child, ast.Subscript)
        and isinstance(child.slice, ast.Constant)
        and isinstance(child.slice.value, str)
    }


def _find_ordered_call(records, start, operation):
    expected_name = str(operation.get("op") or "")
    capability = capability_by_name(expected_name)
    profile = capability.ast_match_profile if capability else None
    expected_target = _normalize_target(operation.get("target"))
    expected_value = _expected_value(operation)
    parameters = operation.get("parameters") or {}
    source = (
        operation.get("source")
        or parameters.get("expected_source")
        or parameters.get("value_source")
    )
    runtime_binding = (
        str(source).split(".", 1)[1]
        if str(source or "").startswith("runtime.")
        else None
    )
    require_value = (
        expected_value is not None
        and (
            source in {None, "literal"}
            or str(source).startswith("observed_property.")
        )
    )
    for index in range(start, len(records)):
        record = records[index]
        if (
            operation.get("implementation_location")
            == "step_inline_base_api"
            and (
                record.get("window_owner")
                != str(operation.get("window_owner") or "")
                or str(record.get("view_owner") or "")
                != str(operation.get("view_owner") or "")
            )
        ):
            continue
        if record.get("name") != expected_name:
            continue
        if profile == "ocr_assertion":
            actual_region = _call_argument(
                record,
                index=2,
                keyword="region",
            )
            if (
                expected_target
                and _normalize_target(actual_region) != expected_target
            ):
                continue
        elif (
            expected_target
            and _normalize_target(record.get("target")) != expected_target
        ):
            continue
        if profile == "runtime_value_producer":
            expected_binding = str(
                operation.get("result_binding") or ""
            )
            binding_index = 2 if expected_name == "save_attr" else 1
            binding_keyword = (
                "variable_name"
                if expected_name in {"save_attr", "save_text"}
                else None
            )
            actual_binding = _call_argument(
                record,
                index=binding_index,
                keyword=binding_keyword,
            )
            if actual_binding != expected_binding:
                continue
            if expected_name == "save_attr":
                actual_attr = _call_argument(
                    record,
                    index=1,
                    keyword="attr_name",
                )
                if actual_attr != parameters.get("attr_name"):
                    continue
            return index
        if profile == "frozen_click_offset":
            if parameters:
                actual_offset_x = _call_argument(
                    record,
                    index=1,
                    keyword="offset_x",
                )
                actual_offset_y = _call_argument(
                    record,
                    index=2,
                    keyword="offset_y",
                )
                if any((
                    actual_offset_x != parameters.get("offset_x"),
                    actual_offset_y != parameters.get("offset_y"),
                )):
                    continue
            return index
        if runtime_binding is not None:
            value_index, value_keyword = _operation_value_argument(
                expected_name
            )
            if value_index is None:
                continue
            actual_bindings = _call_runtime_bindings(
                record,
                index=value_index,
                keyword=value_keyword,
            )
            if actual_bindings != {runtime_binding}:
                actual_reference = _call_argument_reference(
                    record,
                    index=value_index,
                    keyword=value_keyword,
                )
                if actual_reference not in set(
                    operation.get("_runtime_method_parameters") or ()
                ):
                    continue
        if str(source or "").startswith("table."):
            source_column = str(source).split(".", 1)[1]
            if source_column not in _call_table_columns(
                record,
                expected_name,
            ):
                value_index, value_keyword = _operation_value_argument(
                    expected_name
                )
                if value_index is None:
                    continue
                actual_reference = _call_argument_reference(
                    record,
                    index=value_index,
                    keyword=value_keyword,
                )
                if actual_reference not in set(
                    operation.get("_table_method_parameters") or ()
                ):
                    continue
        if profile == "attribute_assertion":
            expected_attr = parameters.get("attr_name")
            actual_attr = _call_argument(
                record,
                index=1,
                keyword="attr_name",
            )
            actual_expected = _call_argument(
                record,
                index=2,
                keyword="expected",
            )
            if expected_attr is None or actual_attr != expected_attr:
                continue
            if require_value and actual_expected != expected_value:
                continue
            return index
        if profile == "frozen_scroll":
            actual_direction = _call_argument(
                record,
                index=1,
                keyword="direction",
            )
            actual_steps = _call_argument(
                record,
                index=2,
                keyword="steps",
            )
            if any((
                actual_direction != parameters.get("direction"),
                actual_steps != parameters.get("steps"),
            )):
                continue
            return index
        if profile == "frozen_drag":
            actual_delta_x = _call_argument(
                record,
                index=1,
                keyword="delta_x",
            )
            actual_delta_y = _call_argument(
                record,
                index=2,
                keyword="delta_y",
            )
            if any((
                actual_delta_x != parameters.get("delta_x"),
                actual_delta_y != parameters.get("delta_y"),
            )):
                continue
            return index
        if profile == "collection_assertion":
            actual_expected = _call_argument(
                record,
                index=1,
                keyword="expected",
            )
            actual_max_items = _call_argument(
                record,
                index=3,
                keyword="max_items",
            )
            if actual_expected != parameters.get("expected"):
                continue
            if (
                actual_max_items is not None
                and actual_max_items != parameters.get("max_items")
            ):
                continue
            return index
        if profile == "ocr_assertion":
            actual_expected = _call_argument(
                record,
                index=0,
                keyword="text",
            )
            actual_timeout = _call_argument(
                record,
                index=1,
                keyword="timeout",
            )
            if (
                parameters.get("timeout") is not None
                and actual_timeout != parameters.get("timeout")
            ):
                continue
            dynamic_source = str(source or "").startswith((
                "table.",
                "step_argument.",
                "examples.",
                "data.",
                "context.",
            ))
            if not dynamic_source:
                if actual_expected != expected_value:
                    continue
                return index
        if profile == "semantic_control_value":
            if expected_name == "set_slider_value":
                actual_minimum = _call_argument(
                    record,
                    index=2,
                    keyword="expected_minimum",
                )
                actual_maximum = _call_argument(
                    record,
                    index=3,
                    keyword="expected_maximum",
                )
                if any((
                    actual_minimum != parameters.get("expected_minimum"),
                    actual_maximum != parameters.get("expected_maximum"),
                )):
                    continue
            dynamic_source = str(source or "").startswith((
                "table.",
                "step_argument.",
                "examples.",
                "data.",
                "context.",
            ))
            if not dynamic_source:
                value_index, value_keyword = _operation_value_argument(
                    expected_name
                )
                actual_value = _call_argument(
                    record,
                    index=value_index,
                    keyword=value_keyword,
                )
                if require_value and actual_value != expected_value:
                    continue
                return index
        if str(source or "").startswith((
            "step_argument.",
            "examples.",
        )):
            expected_argument = (
                parameters.get("argument")
                or str(source).split(".", 1)[1]
            )
            value_index, value_keyword = _operation_value_argument(
                expected_name
            )
            if value_index is None:
                continue
            actual_argument = _call_argument_reference(
                record,
                index=value_index,
                keyword=value_keyword,
            )
            if actual_argument != expected_argument:
                continue
            return index
        if str(source or "").startswith("data."):
            value_index, value_keyword = _operation_value_argument(
                expected_name
            )
            if value_index is None:
                continue
            expected_reference = (
                "$data:" + str(source).split(".", 1)[1]
            )
            actual_reference = _call_argument(
                record,
                index=value_index,
                keyword=value_keyword,
            )
            if actual_reference != expected_reference:
                continue
            return index
        if str(source or "").startswith("context."):
            value_index, value_keyword = _operation_value_argument(
                expected_name
            )
            if value_index is None:
                continue
            actual_reference = _call_argument_reference(
                record,
                index=value_index,
                keyword=value_keyword,
            )
            if actual_reference != str(source):
                continue
            return index
        if require_value and record.get("value") != expected_value:
            continue
        return index
    return None


def _operation_value_argument(operation):
    capability = capability_by_name(operation)
    return (
        capability.value_argument
        if capability and capability.value_argument is not None
        else (None, None)
    )


def _call_table_columns(record, operation):
    capability = capability_by_name(operation)
    value_parameter = (
        capability.table_value_argument
        if capability is not None
        else None
    )
    if value_parameter is None:
        return set()
    argument_index, keyword = value_parameter
    arguments = record.get("_arg_table_columns") or []
    columns = set(
        arguments[argument_index]
        if len(arguments) > argument_index
        else ()
    )
    keyword_columns = record.get("_keyword_table_columns") or {}
    columns.update(keyword_columns.get(keyword) or ())
    return columns


def _call_argument(record, *, index, keyword):
    keywords = record.get("keywords") or {}
    if keyword in keywords:
        return keywords[keyword]
    args = record.get("args") or []
    return args[index] if len(args) > index else None


def _call_argument_reference(record, *, index, keyword):
    keywords = record.get("_keyword_references") or {}
    if keyword in keywords:
        return keywords[keyword]
    args = record.get("_arg_references") or []
    return args[index] if len(args) > index else None


def _call_runtime_bindings(record, *, index, keyword):
    keywords = record.get("_keyword_runtime_bindings") or {}
    if keyword in keywords:
        return set(keywords[keyword] or ())
    args = record.get("_arg_runtime_bindings") or []
    return set(args[index] if len(args) > index else ())


def _expected_value(operation):
    parameters = operation.get("parameters") or {}
    if "expected" in parameters:
        return parameters.get("expected")
    if "value" in parameters:
        return parameters.get("value")
    return operation.get("value")


def _normalize_target(value):
    text = str(value or "").strip()
    if text.startswith("$loc:"):
        return text[5:]
    if text.startswith("$"):
        return text[1:]
    return text


def _uses_explicit_locator_reference(record, operation):
    capability = capability_by_name(str(operation.get("op") or ""))
    if capability and capability.ast_match_profile == "ocr_assertion":
        value = _call_argument(record, index=2, keyword="region")
    else:
        value = record.get("target")
    text = str(value or "").strip()
    if text.startswith("$loc:"):
        return bool(text[5:])
    return bool(
        text.startswith("$")
        and not text.startswith("$$")
        and ":" not in text
        and len(text) > 1
    )