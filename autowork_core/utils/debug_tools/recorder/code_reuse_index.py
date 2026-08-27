from __future__ import annotations

import ast
import hashlib
import json
import re
import threading
from datetime import datetime
from pathlib import Path

import yaml

from autowork_core.common.compile import compile_locator
from autowork_core.utils.debug_tools.recorder.ai_capability_registry import (
    capability_by_name,
)
from autowork_core.utils.debug_tools.recorder.models import SCHEMA_VERSION
from autowork_core.utils.debug_tools.recorder.writer import write_json_atomic


CODE_REUSE_INDEX_VERSION = "2.5"
MAX_WINDOW_METHOD_CANDIDATES = 6
_INDEX_LOCK = threading.RLock()
_INDEX_ROOTS = (
    Path("Bdd/steps"),
    Path("Bdd/page_obj"),
    Path("Bdd/locators"),
    Path("Bdd/data"),
)
_OPERATION_NAMES = {
    "assert_attr_contains",
    "assert_attr_equal",
    "assert_disabled",
    "assert_enabled",
    "assert_exists",
    "assert_not_exists",
    "assert_not_visible",
    "assert_text_contains",
    "assert_text_empty",
    "assert_text_equal",
    "assert_text_not_contains",
    "assert_visible",
    "click",
    "double_click",
    "expand_dropdown",
    "focus",
    "input_text",
    "right_click",
    "scroll_to",
    "select_dropdown_option",
    "send_text_keys",
    "wait_enabled",
    "wait_exposed",
    "wait_exists",
    "wait_not_exists",
    "wait_ready",
    "wait_visible",
}
_TOKEN_PATTERN = re.compile(r"[0-9A-Za-z_\u4e00-\u9fff]+")


def build_code_reuse_index(project_root, cache_path):
    project_root = Path(project_root).resolve()
    cache_path = Path(cache_path).resolve()
    with _INDEX_LOCK:
        previous = _load_index(cache_path)
        if previous.get("code_reuse_index_version") != CODE_REUSE_INDEX_VERSION:
            previous = {}
        previous_files = previous.get("files") or {}
        files = {}
        parsed_files = 0
        reused_files = 0
        warnings = []
        for path in _source_files(project_root):
            relative = path.relative_to(project_root).as_posix()
            try:
                digest = _sha256(path)
            except OSError as error:
                warnings.append(f"{relative}: {type(error).__name__}: {error}")
                continue
            existing = previous_files.get(relative) or {}
            if existing.get("sha256") == digest and isinstance(
                    existing.get("entries"), list
            ):
                entries = existing["entries"]
                reused_files += 1
            else:
                try:
                    entries = _index_file(path, relative, digest)
                    parsed_files += 1
                except Exception as error:
                    warnings.append(
                        f"{relative}: {type(error).__name__}: {error}"
                    )
                    entries = []
            files[relative] = {
                "sha256": digest,
                "size": path.stat().st_size,
                "entries": entries,
            }
        _link_table_step_patterns(files)
        value = {
            "schema_version": SCHEMA_VERSION,
            "code_reuse_index_version": CODE_REUSE_INDEX_VERSION,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "project_root": str(project_root),
            "files": files,
            "stats": {
                "file_count": len(files),
                "entry_count": sum(
                    len(item.get("entries") or ())
                    for item in files.values()
                ),
                "parsed_files": parsed_files,
                "reused_files": reused_files,
            },
            "warnings": warnings,
        }
        value["index_fingerprint"] = _stable_hash({
            "files": {
                path: item.get("sha256")
                for path, item in files.items()
            },
            "entries": [
                entry
                for item in files.values()
                for entry in item.get("entries") or ()
            ],
        })
        write_json_atomic(cache_path, value)
        return value


def find_reuse_candidates(index, request, semantics=None, *, limit=12):
    query = _query_context(request, semantics or {})
    target_steps = [
        step
        for step in (request.get("target") or {}).get("steps") or ()
        if isinstance(step, dict) and str(step.get("text") or "").strip()
    ]
    query_tokens = set(query["tokens"])
    query_operations = set(query["operations"])
    query_locators = set(query["locators"])
    ranked = []
    for file_value in (index.get("files") or {}).values():
        for entry in file_value.get("entries") or ():
            if entry.get("kind") == "application_lifecycle":
                continue
            if entry.get("executable") is False:
                continue
            tokens = set(entry.get("tokens") or ())
            operations = set(entry.get("operations") or ())
            references = set(entry.get("references") or ())
            reasons = []
            score = 0
            shared_tokens = sorted(query_tokens & tokens)
            shared_operations = sorted(query_operations & operations)
            shared_locators = sorted(query_locators & references)
            matched_step_texts = [
                str(target_step.get("text") or "")
                for target_step in target_steps
                if entry.get("kind") == "step_definition"
                and _step_entry_matches_target(entry, target_step)
            ]
            exact_step_pattern = bool(matched_step_texts)
            if exact_step_pattern:
                score = 100
                reasons.append("exact_step_pattern")
            if shared_tokens:
                score += 0 if exact_step_pattern else min(36, len(shared_tokens) * 6)
                reasons.append(f"shared_tokens={shared_tokens[:6]}")
            if shared_operations:
                score += min(36, len(shared_operations) * 12)
                reasons.append(f"shared_operations={shared_operations}")
            if shared_locators:
                score += min(36, len(shared_locators) * 12)
                reasons.append(f"shared_locators={shared_locators[:6]}")
            if entry.get("kind") == "capability":
                score += 8
                reasons.append("user_confirmed_capability")
            quality = entry.get("quality") or {}
            penalty = int(quality.get("score_penalty") or 0)
            if penalty:
                score -= penalty
                reasons.append(
                    "quality_penalty="
                    f"{penalty}:{quality.get('recommendation')}"
                )
            if score <= 0:
                continue
            ranked.append({
                **_reuse_candidate(entry),
                "score": min(100, score),
                "reasons": reasons,
                "matched_tokens": shared_tokens,
                "matched_step_texts": matched_step_texts,
            })
    ranked.sort(key=lambda item: (
        -item["score"],
        item.get("path") or "",
        item.get("symbol") or item.get("key") or "",
    ))
    return ranked[:max(1, int(limit))]


def step_pattern_matches(pattern, value):
    pattern = str(pattern or "").strip()
    value = str(value or "").strip()
    if not pattern or not value:
        return False
    parts = re.split(r"(\{[^{}]+\})", pattern)
    expression = "".join(
        r".+?" if re.fullmatch(r"\{[^{}]+\}", part) else re.escape(part)
        for part in parts
    )
    return bool(re.fullmatch(expression, value, flags=re.IGNORECASE))


def step_pattern_contract_matches(contract, target_step):
    contract = dict(contract or {})
    decorator = str(contract.get("decorator") or "").strip().casefold()
    target_type = _target_step_type(target_step)
    return bool(
        step_pattern_matches(
            contract.get("pattern"),
            (target_step or {}).get("text"),
        )
        and (
            decorator == "step"
            or decorator == target_type
        )
    )


def _target_step_type(target_step):
    for field in ("semantic_type", "keyword"):
        value = str((target_step or {}).get(field) or "").strip().casefold()
        if value in {"given", "when", "then"}:
            return value
    return ""


def _step_entry_matches_target(entry, target_step):
    return any(
        step_pattern_contract_matches(contract, target_step)
        for contract in candidate_step_pattern_contracts(entry)
    )


def candidate_step_pattern_contracts(candidate):
    value = dict(candidate or {})
    raw_contracts = (
        value.get("step_pattern_contracts")
        or value.get("step_parameter_contracts")
        or ()
    )
    return [
        {
            "decorator": str(contract.get("decorator") or "").casefold(),
            "pattern": str(contract.get("pattern") or ""),
        }
        for contract in raw_contracts
        if isinstance(contract, dict)
        and str(contract.get("decorator") or "").casefold()
        in {"given", "when", "then", "step"}
        and str(contract.get("pattern") or "")
    ]


def _reuse_candidate(entry):
    return {
        "candidate_id": "reuse-" + _stable_hash({
            "path": entry.get("path"),
            "kind": entry.get("kind"),
            "symbol": entry.get("symbol"),
            "key": entry.get("key"),
            "definition_fingerprint": entry.get("definition_fingerprint"),
        })[:16],
        "kind": entry.get("kind"),
        "path": entry.get("path"),
        "symbol": entry.get("symbol"),
        "signature": entry.get("signature"),
        "key": entry.get("key"),
        "line": entry.get("line"),
        "definition_fingerprint": entry.get("definition_fingerprint"),
        "file_sha256": entry.get("file_sha256"),
        "step_patterns": entry.get("step_patterns") or [],
        "step_pattern_contracts": (
            entry.get("step_pattern_contracts") or []
        ),
        "operations": entry.get("operations") or [],
        "call_sequence": entry.get("call_sequence") or [],
        "step_parameters": entry.get("step_parameters") or [],
        "step_parameter_contracts": (
            entry.get("step_parameter_contracts") or []
        ),
        "references": entry.get("references") or [],
        "table_usage_hint": entry.get("table_usage_hint"),
        "quality": entry.get("quality") or {},
        "advisory_only": True,
    }


def build_window_asset_catalog(index):
    pages = []
    roots = []
    methods_by_path = {}
    for file_value in (index.get("files") or {}).values():
        for entry in file_value.get("entries") or ():
            if entry.get("kind") == "window_page":
                pages.append(entry)
            elif entry.get("kind") == "window_root":
                roots.append(entry)
            elif (
                entry.get("kind") == "page_object_method"
                and entry.get("executable") is not False
            ):
                methods_by_path.setdefault(entry.get("path"), []).append(
                    entry
                )

    roots_by_identity = {
        (entry.get("path"), entry.get("key")): entry
        for entry in roots
    }
    used_roots = set()
    candidates = []
    for page in pages:
        locator_path = _project_locator_path(
            (page.get("window_page") or {}).get("root_locator_file")
        )
        root_name = (page.get("window_page") or {}).get("root_locator")
        root = roots_by_identity.get((locator_path, root_name))
        if root is None:
            continue
        used_roots.add((locator_path, root_name))
        candidates.append(_window_candidate(
            "canonical_window",
            root,
            page=page,
            methods=methods_by_path.get(page.get("path")) or [],
        ))
    for root in roots:
        identity = (root.get("path"), root.get("key"))
        if identity in used_roots:
            continue
        candidates.append(_window_candidate("legacy_root", root))
    candidates.sort(key=lambda item: (
        item["kind"],
        item.get("page_object") or "",
        item["root_locator_file"],
        item["root_locator"],
    ))
    return {
        "catalog_version": "1.0",
        "index_fingerprint": index.get("index_fingerprint"),
        "candidates": candidates,
    }


def match_window_owner_candidates(catalog, recorded_window):
    recorded_window = dict(recorded_window or {})
    root_name = str(recorded_window.get("root_name") or "")
    root_criteria = recorded_window.get("root_criteria") or {}
    identities = [
        item
        for item in recorded_window.get("window_identities") or ()
        if isinstance(item, dict)
    ]
    matches = []
    for candidate in catalog.get("candidates") or ():
        score, reasons = _window_candidate_score(
            candidate,
            root_name,
            identities,
            root_criteria,
        )
        if score <= 0:
            continue
        matches.append({
            **candidate,
            "method_candidates": _window_method_candidates(
                candidate,
                recorded_window,
            ),
            "score": score,
            "strength": "strong" if score >= 70 else "weak",
            "reasons": reasons,
        })
    matches.sort(key=lambda item: (
        -item["score"],
        item["kind"],
        item.get("page_object") or "",
        item["root_locator_file"],
    ))
    strong = [item for item in matches if item["strength"] == "strong"]
    canonical = [
        item for item in strong if item["kind"] == "canonical_window"
    ]
    if len(strong) == 1 and len(canonical) == 1:
        strategy = "reuse_existing"
    elif len(strong) == 1 and strong[0]["kind"] == "legacy_root":
        strategy = "create_new"
    elif strong:
        strategy = "ambiguous"
    elif identities:
        strategy = "create_new"
    else:
        strategy = "unresolved"
    return {
        "root_name": root_name,
        "identity_status": recorded_window.get("identity_status"),
        "suggested_strategy": strategy,
        "candidates": matches,
        "advisory_only": True,
    }


def append_capability_candidates(
        candidates,
        recording_root,
        query,
        *,
        limit=6,
        project_root=None,
    ):
    try:
        from autowork_core.utils.debug_tools.recorder.capability import (
            load_capability_catalog,
        )

        value = load_capability_catalog(recording_root)
    except (OSError, ValueError, json.JSONDecodeError):
        return candidates
    query_tokens = set(_tokens(query))
    additions = []
    current_code_fingerprint = None
    for entry in value.get("capabilities") or ():
        if entry.get("status") != "confirmed":
            continue
        text = " ".join(str(item or "") for item in (
            entry.get("capability_id"),
            (entry.get("feature") or {}).get("name"),
            (entry.get("scenario") or {}).get("name"),
            (entry.get("step") or {}).get("text"),
            entry.get("path"),
        ))
        shared = sorted(query_tokens & set(_tokens(text)))
        if not shared:
            continue
        semantic_contract = entry.get("semantic_contract") or {}
        source = entry.get("source") or {}
        runtime_verified = False
        expected_code_fingerprint = source.get(
            "runtime_code_snapshot_fingerprint"
        )
        if (
            semantic_contract.get("runtime_verification") == "passed"
            and expected_code_fingerprint
            and project_root is not None
        ):
            if current_code_fingerprint is None:
                from autowork_core.utils.debug_tools.recorder.generation_transaction import (
                    snapshot_runtime_code,
                )
                from autowork_core.utils.debug_tools.recorder.transaction_integrity import (
                    runtime_code_snapshot_fingerprint,
                )

                current_code_fingerprint = runtime_code_snapshot_fingerprint(
                    snapshot_runtime_code(project_root)
                )
            runtime_verified = (
                current_code_fingerprint == expected_code_fingerprint
            )
        additions.append({
            "candidate_id": "reuse-capability-" + _stable_hash({
                "id": entry.get("capability_id"),
                "path": entry.get("path"),
            })[:12],
            "kind": "capability",
            "path": entry.get("path"),
            "symbol": None,
            "key": entry.get("capability_id"),
            "file_sha256": None,
            "score": min(100, 30 + len(shared) * 8),
            "reasons": [f"shared_tokens={shared[:6]}", "user_confirmed_capability"],
            "operations": [],
            "references": [],
            "semantic_contract": semantic_contract,
            "quality": {
                "recommendation": "trusted_reference",
                "antipatterns": [],
                "score_penalty": 0,
                "runtime_verified": runtime_verified,
                "runtime_snapshot_current": runtime_verified,
                "advisory_only": True,
            },
            "advisory_only": True,
        })
    combined = [*candidates, *additions]
    combined.sort(key=lambda item: (-item["score"], item.get("path") or ""))
    return combined[:max(len(candidates), int(limit), 1)]


def _source_files(project_root):
    ignored_resource_refs = _ignored_debug_resource_refs(project_root)
    for root in _INDEX_ROOTS:
        directory = project_root / root
        if not directory.exists():
            continue
        for path in sorted(item for item in directory.rglob("*") if item.is_file()):
            if "__pycache__" in path.parts or _is_debug_python_source(
                project_root,
                path,
            ):
                continue
            relative = path.relative_to(project_root)
            if relative in ignored_resource_refs:
                continue
            if path.suffix.casefold() in {".py", ".yaml", ".yml"}:
                yield path


def _is_debug_python_source(project_root, path):
    if path.suffix.casefold() != ".py":
        return False
    try:
        relative = path.relative_to(project_root)
    except ValueError:
        return False
    return bool(
        path.stem.casefold().startswith(("test_debug", "debug_"))
        or any(
            part.casefold() in {"test", "tests", "debug", "debug_tools"}
            for part in relative.parts[:-1]
        )
    )


def _ignored_debug_resource_refs(project_root):
    debug_refs = set()
    production_refs = set()
    for root in (Path("Bdd/steps"), Path("Bdd/page_obj")):
        directory = project_root / root
        if not directory.exists():
            continue
        for path in directory.rglob("*.py"):
            refs = _python_resource_refs(project_root, path)
            target = (
                debug_refs
                if _is_debug_python_source(project_root, path)
                else production_refs
            )
            target.update(refs)
    return debug_refs - production_refs


def _python_resource_refs(project_root, path):
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    except (OSError, SyntaxError, UnicodeError):
        return set()
    refs = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.casefold().endswith((".yaml", ".yml"))
        ):
            continue
        resource = Path(node.value.replace("\\", "/"))
        if resource.is_absolute() or ".." in resource.parts:
            continue
        for directory in (Path("Bdd/locators"), Path("Bdd/data")):
            candidate = directory / resource
            if (project_root / candidate).is_file():
                refs.add(candidate)
    return refs


def _index_file(path, relative, digest):
    if path.suffix.casefold() == ".py":
        return _index_python(path, relative, digest)
    return _index_yaml(path, relative, digest)


def _index_python(path, relative, digest):
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    pywinauto_symbols = _module_pywinauto_symbols(tree)
    module_constants = _module_string_constants(tree)
    parent = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    step_matchers = _step_matchers_by_function(tree)
    direct_quality = {
        name: _function_quality(
            node,
            pywinauto_symbols=pywinauto_symbols,
        )
        for name, node in functions.items()
    }
    entries = _window_page_entries(tree, relative, digest)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        owner = parent.get(node)
        class_name = owner.name if isinstance(owner, ast.ClassDef) else None
        symbol = f"{class_name}.{node.name}" if class_name else node.name
        calls = sorted({
            _call_name(child.func)
            for child in ast.walk(node)
            if isinstance(child, ast.Call) and _call_name(child.func)
        })
        references = sorted({
            value[1:]
            for value in (
                child.value
                for child in ast.walk(node)
                if isinstance(child, ast.Constant)
                and isinstance(child.value, str)
            )
            if value.startswith("$") and not value.startswith("$$")
        })
        decorator_text = " ".join(
            ast.unparse(item) if hasattr(ast, "unparse") else ""
            for item in node.decorator_list
        )
        entries.append({
            "kind": (
                "page_object_method"
                if Path("Bdd/page_obj") in Path(relative).parents
                else "step_definition"
            ),
            "path": relative,
            "symbol": symbol,
            "signature": _function_signature(node, symbol),
        "key": None,
        "line": getattr(node, "lineno", 0),
            "definition_fingerprint": _stable_hash({
                "ast": ast.dump(node, include_attributes=False),
            }),
            "file_sha256": digest,
            "operations": sorted(set(calls) & _OPERATION_NAMES),
            "call_sequence": _operation_call_sequence(node),
            "step_parameters": _step_parameter_names(node),
            "step_parameter_contracts": _step_parameter_contracts(
                node,
                matcher=step_matchers.get(id(node)),
            ),
            "delegated_calls": calls,
            "references": references,
            "table_usage_hint": _python_table_usage_hint(node),
            "quality": _function_quality_with_helpers(
                node,
                functions,
                direct_quality,
                pywinauto_symbols=pywinauto_symbols,
            ),
            "executable": not _function_is_placeholder(node),
            "step_patterns": _gherkin_patterns(node),
            "step_pattern_contracts": _step_pattern_contracts(node),
            "tokens": _tokens(" ".join((
                symbol,
                decorator_text,
                ast.get_docstring(node) or "",
                " ".join(references),
            ))),
        })
        lifecycle_entries = _application_lifecycle_entries(
            node,
            relative,
            digest,
            functions,
            direct_quality,
            pywinauto_symbols,
            module_constants,
        )
        entries.extend(lifecycle_entries)
    return entries


def _function_signature(node, symbol):
    try:
        arguments = ast.unparse(node.args)
    except (AttributeError, ValueError):
        arguments = ", ".join(
            argument.arg
            for argument in (
                list(node.args.posonlyargs)
                + list(node.args.args)
                + list(node.args.kwonlyargs)
            )
        )
    name = str(symbol or node.name).rsplit(".", 1)[-1]
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    return f"{prefix} {name}({arguments})"


def _operation_call_sequence(node):
    result = []
    for statement in node.body:
        if (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            continue
        if _is_get_page_binding(statement):
            continue
        if not (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Call)
        ):
            return []
        call = statement.value
        operation = _call_name(call.func)
        if operation not in _OPERATION_NAMES:
            return []
        record = {"operation": operation}
        target = _call_argument_node(
            call,
            0,
            ("target", "locator", "locator_or_name"),
        )
        if isinstance(target, ast.Constant) and isinstance(target.value, str):
            reference = _locator_reference_name(target.value)
            if reference:
                record["target"] = reference
        capability = capability_by_name(operation)
        value_argument = (
            capability.value_argument
            if capability is not None
            else None
        )
        if value_argument is not None:
            value_index, value_keyword = value_argument
            value = _call_argument_node(
                call,
                value_index,
                (value_keyword,) if value_keyword else (),
            )
            if isinstance(value, ast.Name):
                record["value_parameter"] = value.id
            elif isinstance(value, ast.Constant) and isinstance(
                    value.value,
                    (str, int, float, bool),
            ):
                record["value"] = value.value
        result.append(record)
    return result


def _step_parameter_names(node):
    parameters = [
        argument.arg
        for argument in (
            list(node.args.posonlyargs)
            + list(node.args.args)
            + list(node.args.kwonlyargs)
        )
        if argument.arg != "context"
    ]
    return sorted(set(parameters))


def _step_parameter_contracts(node, *, matcher=None):
    parameters = set(_step_parameter_names(node))
    contracts = []
    for step_contract in _step_pattern_contracts(node):
        step_type = step_contract["decorator"]
        pattern = step_contract["pattern"]
        supported = matcher in {"parse", "cfparse"}
        contracts.append({
            "decorator": step_type,
            "matcher": matcher or "unknown",
            "pattern": pattern,
            "parameter_bindings": [
                {
                    "parameter": name,
                    "capture_kind": "named",
                    "capture": name,
                }
                for name in _parse_named_step_parameters(pattern)
                if supported and name in parameters
            ],
        })
    return contracts


def _step_pattern_contracts(node):
    contracts = []
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        step_type = _call_name(decorator.func)
        if step_type not in {"given", "when", "then", "step"}:
            continue
        if not decorator.args or not isinstance(
                decorator.args[0], ast.Constant
        ) or not isinstance(decorator.args[0].value, str):
            continue
        contracts.append({
            "decorator": step_type,
            "pattern": decorator.args[0].value,
        })
    return contracts


def _parse_named_step_parameters(pattern):
    return list(dict.fromkeys(
        match.group(1)
        for match in re.finditer(
            r"\{([A-Za-z_][A-Za-z0-9_]*)(?::[^{}]+)?\}",
            str(pattern or ""),
        )
    ))


def _step_matchers_by_function(tree):
    matcher = "parse"
    result = {}
    for node in getattr(tree, "body", ()):
        selected = _selected_step_matcher(node)
        if selected is not None:
            matcher = selected
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            result[id(node)] = matcher
    return result


def _selected_step_matcher(node):
    if not (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and _call_name(node.value.func) == "use_step_matcher"
            and node.value.args
    ):
        return None
    value = node.value.args[0]
    if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
        return None
    return value.value


def _is_get_page_binding(statement):
    if not (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and isinstance(statement.value, ast.Call)
    ):
        return False
    call = statement.value
    return bool(
        _call_name(call.func) == "get_page"
        and len(call.args) == 2
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id == "context"
    )


def _call_argument_node(call, index, keywords):
    for keyword in call.keywords:
        if keyword.arg in set(keywords):
            return keyword.value
    return call.args[index] if len(call.args) > index else None


def _locator_reference_name(value):
    value = str(value or "")
    if value.startswith("$loc:"):
        return value[5:]
    if value.startswith("$") and not value.startswith("$$"):
        return value[1:]
    return None


def _application_lifecycle_entries(
        node,
        relative,
        digest,
        functions,
        direct_quality,
        pywinauto_symbols,
        module_constants,
):
    decorators = [
        item
        for item in node.decorator_list
        if isinstance(item, ast.Call)
        and _call_name(item.func) in {
            "before_app_start",
            "after_app_start",
            "after_app_stop",
        }
    ]
    if not decorators:
        return []
    data_keys = {
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant)
        and isinstance(child.value, str)
        and child.value.endswith("_key")
    }
    data_keys.update(
        module_constants.get(child.id)
        for child in ast.walk(node)
        if isinstance(child, ast.Name)
        and child.id in module_constants
        and str(module_constants.get(child.id) or "").endswith("_key")
    )
    for helper_name in _local_helper_calls(node, functions):
        helper = functions.get(helper_name)
        if helper is None:
            continue
        data_keys.update(
            child.value
            for child in ast.walk(helper)
            if isinstance(child, ast.Constant)
            and isinstance(child.value, str)
            and child.value.endswith("_key")
        )
        data_keys.update(
            module_constants.get(child.id)
            for child in ast.walk(helper)
            if isinstance(child, ast.Name)
            and child.id in module_constants
            and str(module_constants.get(child.id) or "").endswith("_key")
        )
    result = []
    for decorator in decorators:
        tags = []
        for keyword in decorator.keywords:
            if keyword.arg != "tags":
                continue
            try:
                value = ast.literal_eval(keyword.value)
            except (ValueError, TypeError):
                value = None
            tags = [value] if isinstance(value, str) else list(value or ())
        phase = _call_name(decorator.func)
        dependency_id = "lifecycle-" + _stable_hash({
            "path": relative,
            "symbol": node.name,
            "phase": phase,
            "required_tags": sorted(str(item) for item in tags if item),
            "definition": ast.dump(node, include_attributes=False),
        })[:16]
        result.append({
            "kind": "application_lifecycle",
            "dependency_id": dependency_id,
            "path": relative,
            "symbol": node.name,
            "key": None,
            "line": getattr(node, "lineno", 0),
            "file_sha256": digest,
            "definition_fingerprint": _stable_hash({
                "ast": ast.dump(node, include_attributes=False),
            }),
            "phase": phase,
            "required_tags": sorted(str(item) for item in tags if item),
            "data_keys": sorted(data_keys),
            "delegated_calls": sorted(_local_helper_calls(node, functions)),
            "quality": _function_quality_with_helpers(
                node,
                functions,
                direct_quality,
                pywinauto_symbols=pywinauto_symbols,
            ),
            "advisory_only": True,
            "generation_allowed": False,
            "tokens": _tokens(" ".join((
                node.name,
                *tags,
                *data_keys,
            ))),
        })
    return result


def _function_is_placeholder(node):
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
    if all(
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
        and _call_name(statement.value.func)
        == "unresolved_generation_issue"
        for statement in body
    ):
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
            and _call_name(statement.exc.func) in {
                "NotImplementedError",
                "PendingAutomationError",
            }
        )
        for statement in body
    )


def _function_quality(node, *, pywinauto_symbols=()):
    if _function_is_placeholder(node):
        return {
            "recommendation": "placeholder",
            "antipatterns": ["placeholder"],
            "score_penalty": 100,
            "runtime_verified": False,
            "advisory_only": True,
        }
    antipatterns = set()
    operation_calls = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        name = _call_name(child.func)
        if name == "sleep":
            antipatterns.add("fixed_sleep")
        if name in {
            "write_text",
            "write_bytes",
            "unlink",
            "rename",
            "replace",
            "rmdir",
        }:
            antipatterns.add("file_side_effect")
        if name in _OPERATION_NAMES:
            operation_calls.append(_call_quality_identity(child))
    counts = {
        identity: operation_calls.count(identity)
        for identity in set(operation_calls)
    }
    if any(count > 1 for count in counts.values()):
        antipatterns.add("repeated_ui_call")
    if any(
        isinstance(child, (ast.Import, ast.ImportFrom))
        and any(
            module.startswith("pywinauto")
            for module in _imported_modules(child)
        )
        for child in ast.walk(node)
    ):
        antipatterns.add("direct_pywinauto")
    if _uses_pywinauto_symbol(node, pywinauto_symbols):
        antipatterns.add("direct_pywinauto")
    ordered = sorted(antipatterns)
    severe = {"direct_pywinauto"}
    recommendation = (
        "anti_pattern"
        if severe & antipatterns
        else "usable_with_repairs"
        if antipatterns
        else "trusted_reference"
    )
    penalty = min(80, sum(
        30 if item in severe else 15
        for item in antipatterns
    ))
    return {
        "recommendation": recommendation,
        "antipatterns": ordered,
        "score_penalty": penalty,
        "runtime_verified": False,
        "advisory_only": True,
    }


def _function_quality_with_helpers(
        node,
        functions,
        direct_quality,
        *,
        pywinauto_symbols=(),
):
    antipatterns = set(
        (_function_quality(
            node,
            pywinauto_symbols=pywinauto_symbols,
        ).get("antipatterns") or ())
    )
    pending = [
        name
        for name in _local_helper_calls(node, functions)
        if name != node.name
    ]
    visited = set()
    while pending:
        helper_name = pending.pop()
        if helper_name in visited:
            continue
        visited.add(helper_name)
        helper = functions.get(helper_name)
        if helper is None:
            continue
        antipatterns.update(
            (direct_quality.get(helper_name) or {}).get("antipatterns") or ()
        )
        pending.extend(
            name
            for name in _local_helper_calls(helper, functions)
            if name not in visited
        )
    return _quality_from_antipatterns(antipatterns)


def _local_helper_calls(node, functions):
    return {
        child.func.id
        for child in ast.walk(node)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id in functions
    }


def _quality_from_antipatterns(values):
    antipatterns = set(values or ())
    if "placeholder" in antipatterns:
        return {
            "recommendation": "placeholder",
            "antipatterns": sorted(antipatterns),
            "score_penalty": 100,
            "runtime_verified": False,
            "advisory_only": True,
        }
    severe = {"direct_pywinauto"}
    recommendation = (
        "anti_pattern"
        if severe & antipatterns
        else "usable_with_repairs"
        if antipatterns
        else "trusted_reference"
    )
    penalty = min(80, sum(
        30 if item in severe else 15
        for item in antipatterns
    ))
    return {
        "recommendation": recommendation,
        "antipatterns": sorted(antipatterns),
        "score_penalty": penalty,
        "runtime_verified": False,
        "advisory_only": True,
    }


def _call_quality_identity(node):
    return _stable_hash({
        "name": _call_name(node.func),
        "args": [
            ast.dump(argument, include_attributes=False)
            for argument in node.args
        ],
        "keywords": sorted(
            (
                str(keyword.arg or ""),
                ast.dump(keyword.value, include_attributes=False),
            )
            for keyword in node.keywords
        ),
    })


def _imported_modules(node):
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        return [str(node.module or "")]
    return []


def _module_pywinauto_symbols(tree):
    symbols = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("pywinauto"):
                    symbols.add(alias.asname or alias.name.split(".", 1)[0])
        elif (
            isinstance(node, ast.ImportFrom)
            and str(node.module or "").startswith("pywinauto")
        ):
            symbols.update(alias.asname or alias.name for alias in node.names)
    return symbols


def _module_string_constants(tree):
    result = {}
    for node in tree.body:
        if not (
            isinstance(node, (ast.Assign, ast.AnnAssign))
        ):
            continue
        value = node.value
        if not (
            isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            continue
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
        )
        for target in targets:
            if isinstance(target, ast.Name):
                result[target.id] = value.value
    return result


def _uses_pywinauto_symbol(node, symbols):
    symbols = set(symbols or ())
    if not symbols:
        return False
    return any(
        isinstance(child, ast.Name)
        and child.id in symbols
        for child in ast.walk(node)
    )


def _python_table_usage_hint(node):
    has_context_table = _contains_context_table(node)
    columns = sorted({
        child.slice.value
        for child in ast.walk(node)
        if isinstance(child, ast.Subscript)
        and isinstance(child.slice, ast.Constant)
        and isinstance(child.slice.value, str)
    })
    if has_context_table:
        context_key = _context_table_assignment(node)
        if context_key:
            return {
                "consumption": "scenario_state",
                "shape": "records",
                "context_key": context_key,
                "columns": columns,
                "reason": "Existing Step stores context.table for later Steps.",
            }
        if any(
            isinstance(child, ast.DictComp)
            and _contains_context_table(child)
            for child in ast.walk(node)
        ):
            return {
                "consumption": "whole_table",
                "shape": "mapping",
                "context_key": None,
                "columns": columns,
                "reason": "Existing Step converts context.table to one mapping.",
            }
        return {
            "consumption": None,
            "shape": "records",
            "context_key": None,
            "columns": columns,
            "reason": "Existing Step reads context.table.",
        }
    parameters = {
        argument.arg
        for argument in (
            list(node.args.posonlyargs)
            + list(node.args.args)
            + list(node.args.kwonlyargs)
        )
        if argument.arg not in {"self", "cls", "context"}
    }
    if any(
        isinstance(child, (ast.For, ast.AsyncFor, ast.comprehension))
        and isinstance(child.iter, ast.Name)
        and child.iter.id in parameters
        for child in ast.walk(node)
    ):
        method_calls = {
            _call_name(child.func)
            for child in ast.walk(node)
            if isinstance(child, ast.Call)
        }
        reset_between_rows = (
            True
            if "reset" in method_calls
            or any(name.startswith("reset_") for name in method_calls)
            else None
        )
        return {
            "consumption": "each_row",
            "shape": "records",
            "reset_between_rows": reset_between_rows,
            "context_key": None,
            "columns": columns,
            "reason": "Existing business method iterates a row collection.",
        }
    return None


def _gherkin_patterns(node):
    return [
        contract["pattern"]
        for contract in _step_pattern_contracts(node)
    ]


def _link_table_step_patterns(files):
    entries = [
        entry
        for file_value in files.values()
        for entry in file_value.get("entries") or ()
    ]
    table_steps = [
        entry
        for entry in entries
        if entry.get("kind") == "step_definition"
        and entry.get("table_usage_hint")
        and entry.get("step_patterns")
    ]
    page_methods = {}
    for entry in entries:
        if entry.get("kind") == "page_object_method":
            entry["step_patterns"] = []
            method_name = str(entry.get("symbol") or "").rsplit(".", 1)[-1]
            page_methods.setdefault(method_name, []).append(entry)
    for step in table_steps:
        for method_name in step.get("delegated_calls") or ():
            methods = page_methods.get(method_name) or []
            if len(methods) != 1:
                continue
            methods[0]["step_patterns"] = sorted({
                *(methods[0].get("step_patterns") or ()),
                *(step.get("step_patterns") or ()),
            })


def _contains_context_table(node):
    return any(
        isinstance(child, ast.Attribute)
        and isinstance(child.value, ast.Name)
        and child.value.id == "context"
        and child.attr == "table"
        for child in ast.walk(node)
    )


def _context_table_assignment(node):
    for child in ast.walk(node):
        if not isinstance(child, (ast.Assign, ast.AnnAssign)):
            continue
        targets = child.targets if isinstance(child, ast.Assign) else [child.target]
        if not _contains_context_table(child.value):
            continue
        target = next(
            (
                item for item in targets
                if isinstance(item, ast.Attribute)
                and isinstance(item.value, ast.Name)
                and item.value.id == "context"
            ),
            None,
        )
        if target is not None:
            return target.attr
    return None


def _index_yaml(path, relative, digest):
    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict):
        return []
    kind = (
        "locator"
        if Path("Bdd/locators") in Path(relative).parents
        else "data"
    )
    entries = [
        {
            "kind": kind,
            "path": relative,
            "symbol": None,
            "key": str(key),
            "line": None,
            "file_sha256": digest,
            "operations": [],
            "references": [str(key)],
            "tokens": _tokens(" ".join((
                str(key),
                json.dumps(item, ensure_ascii=False, sort_keys=True),
            ))),
        }
        for key, item in value.items()
    ]
    if kind == "locator":
        compiled = {
            str(name): compile_locator(raw, name=str(name))
            for name, raw in value.items()
        }
        entries.extend(
            {
                "kind": "window_root",
                "path": relative,
                "symbol": None,
                "key": str(name),
                "line": None,
                "file_sha256": digest,
                "operations": [],
                "references": [str(name)],
                "window_root": {
                    "root_locator_file": relative,
                    "root_locator": str(name),
                    "criteria": dict(locator.criteria),
                },
                "tokens": _tokens(" ".join((
                    str(name),
                    json.dumps(
                        locator.criteria,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ))),
            }
            for name, locator in compiled.items()
            if _is_top_level_window(locator)
        )
    return entries


def _window_page_entries(tree, relative, digest):
    entries = []
    for node in (getattr(tree, "body", None) or []):
        if not isinstance(node, ast.ClassDef) or not any(
            _base_name(base) == "WindowPage" for base in node.bases
        ):
            continue
        attributes = {
            target.id: statement.value.value
            for statement in node.body
            if isinstance(statement, (ast.Assign, ast.AnnAssign))
            for target in (
                statement.targets
                if isinstance(statement, ast.Assign)
                else [statement.target]
            )
            if isinstance(target, ast.Name)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        }
        root_file = attributes.get("root_locator_file")
        root_name = attributes.get("root_locator")
        if not root_file or not root_name:
            continue
        entries.append({
            "kind": "window_page",
            "path": relative,
            "symbol": node.name,
            "key": root_name,
            "line": getattr(node, "lineno", 0),
            "file_sha256": digest,
            "operations": [],
            "references": [root_name],
            "window_page": {
                "page_object": relative,
                "page_class": node.name,
                "root_locator_file": root_file,
                "root_locator": root_name,
            },
            "tokens": _tokens(" ".join((
                node.name,
                root_file,
                root_name,
            ))),
        })
    return entries


def _base_name(value):
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Attribute):
        return value.attr
    return ""


def _is_top_level_window(locator):
    if locator.root_name or not isinstance(locator.criteria, dict):
        return False
    return bool(
        locator.top_level
        or str(locator.criteria.get("control_type") or "").casefold()
        == "window"
    )


def _project_locator_path(value):
    path = Path(str(value or "").replace("\\", "/"))
    if not path.parts:
        return ""
    if path.parts[0].casefold() == "bdd":
        return path.as_posix()
    return (Path("Bdd/locators") / path).as_posix()


def _window_candidate(kind, root, *, page=None, methods=()):
    page_value = (page or {}).get("window_page") or {}
    root_value = root.get("window_root") or {}
    candidate_id = "window-owner-" + _stable_hash({
        "kind": kind,
        "page_object": page_value.get("page_object"),
        "root_locator_file": root_value.get("root_locator_file"),
        "root_locator": root_value.get("root_locator"),
    })[:16]
    return {
        "candidate_id": candidate_id,
        "kind": kind,
        "page_object": page_value.get("page_object"),
        "page_class": page_value.get("page_class"),
        "root_locator_file": root_value.get("root_locator_file"),
        "root_locator": root_value.get("root_locator"),
        "criteria": dict(root_value.get("criteria") or {}),
        "page_sha256": (page or {}).get("file_sha256"),
        "locator_sha256": root.get("file_sha256"),
        "method_candidates": [
            _reuse_candidate(method)
            for method in methods
        ],
    }


def _window_method_candidates(candidate, recorded_window):
    action_types = {
        str(item)
        for item in recorded_window.get("action_types") or ()
        if item
    }
    locator_names = {
        str(item)
        for item in recorded_window.get("locator_names") or ()
        if item
    }
    ranked = []
    for method in candidate.get("method_candidates") or ():
        shared_operations = sorted(
            action_types & set(method.get("operations") or ())
        )
        shared_locators = sorted(
            locator_names & set(method.get("references") or ())
        )
        if not shared_operations and not shared_locators:
            continue
        reasons = []
        score = 0
        if shared_operations:
            score += min(36, len(shared_operations) * 12)
            reasons.append(f"shared_operations={shared_operations}")
        if shared_locators:
            score += min(36, len(shared_locators) * 12)
            reasons.append(f"shared_locators={shared_locators}")
        ranked.append({
            **method,
            "score": score,
            "reasons": reasons,
        })
    ranked.sort(key=lambda item: (
        -item["score"],
        item.get("path") or "",
        item.get("symbol") or "",
    ))
    return ranked[:MAX_WINDOW_METHOD_CANDIDATES]


def _window_candidate_score(
        candidate,
        root_name,
        identities,
        root_criteria=None,
):
    criteria = candidate.get("criteria") or {}
    root_criteria = root_criteria or {}
    candidate_title = _identity_text(
        criteria.get("title") or criteria.get("name")
    )
    candidate_class = _identity_text(criteria.get("class_name"))
    candidate_auto_id = _identity_key(criteria.get("auto_id"))
    recorded_auto_id = _identity_key(root_criteria.get("auto_id"))
    recorded_root_auto_id = _generated_root_auto_id(root_name)
    title_match = bool(candidate_title) and any(
        candidate_title == _identity_text(item.get("title"))
        for item in identities
    )
    class_match = bool(candidate_class) and any(
        candidate_class == _identity_text(item.get("class_name"))
        for item in identities
    )
    auto_id_match = bool(candidate_auto_id) and (
        candidate_auto_id == recorded_auto_id
    )
    auto_id_hint = bool(candidate_auto_id) and (
        candidate_auto_id == recorded_root_auto_id
    )
    reasons = []
    score = 0
    if title_match:
        score += 70
        reasons.append("title_exact")
    if class_match:
        score += 70
        reasons.append("class_name_exact")
    if auto_id_match:
        score += 70
        reasons.append("auto_id_exact")
    elif auto_id_hint:
        score += 20
        reasons.append("root_auto_id_hint")
    if root_name and root_name == candidate.get("root_locator"):
        score += 20
        reasons.append("root_name_exact")
    return min(score, 100), reasons


def _generated_root_auto_id(value):
    text = str(value or "")
    base = re.sub(
        r"_window_[0-9a-f]{8}$",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return _identity_key(base) if base != text else ""


def _identity_key(value):
    return "".join(
        character
        for character in str(value or "").casefold()
        if character.isalnum()
    )


def _identity_text(value):
    return " ".join(str(value or "").casefold().split())


def _query_context(request, semantics):
    target = request.get("target") or {}
    text = " ".join(str(value or "") for value in (
        (target.get("feature") or {}).get("name"),
        (target.get("scenario") or {}).get("name"),
        *(step.get("text") for step in target.get("steps") or ()),
    ))
    operations = set()
    locators = set()
    for action in (semantics.get("actions") or {}).values():
        for candidate in action.get("assertion_candidates") or ():
            if candidate.get("operation"):
                operations.add(str(candidate["operation"]))
            if candidate.get("target"):
                locators.add(str(candidate["target"]))
        for candidate in action.get("intent_candidates") or ():
            if candidate.get("intent"):
                text += " " + str(candidate["intent"])
    return {
        "tokens": _tokens(text),
        "operations": sorted(operations),
        "locators": sorted(locators),
    }


def _tokens(value):
    text = re.sub(
        r"(?<=[a-z0-9])(?=[A-Z])",
        " ",
        str(value or ""),
    ).replace("_", " ")
    return sorted({
        token.casefold()
        for token in _TOKEN_PATTERN.findall(text)
        if len(token) >= 2
    })


def _call_name(value):
    if isinstance(value, ast.Attribute):
        return value.attr
    if isinstance(value, ast.Name):
        return value.id
    return ""


def _load_index(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if value.get("code_reuse_index_version") != CODE_REUSE_INDEX_VERSION:
        return {}
    return value


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_hash(value):
    return hashlib.sha256(json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")).hexdigest()
