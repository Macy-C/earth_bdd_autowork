from __future__ import annotations

import ast
import base64
import hashlib
import json
import re
from io import BytesIO
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import yaml
from behave.parser import Parser
from PIL import Image


from autowork_core.common.compile import CompiledLocator
from autowork_core.runtime.knowledge_observation import READ_ACTIONS, VARIABLE_ACTIONS, WAIT_ACTIONS, ASSERT_ACTIONS


SOURCE_ROW = re.compile(r"^\|\s*([\w-]+)\s*\|\s*`([^`]+)`[^|]*\|\s*`([a-fA-F0-9]{64})`\s*\|\s*$", re.MULTILINE)
CODE_BLOCK = re.compile(r"```(gherkin|python|yaml)\s*\r?\n(.*?)```", re.DOTALL)
ALLOWED_SOURCE_ROOTS = (
    "Bdd/steps/", "Bdd/page_obj/", "Bdd/locators/", "Bdd/features/",
    "Bdd/test_features/", "autowork_core/", "docs/",
)
DEFAULT_CALL_KEYS = frozenset({
    "timeout", "interval", "wait_type", "wait_timeout", "visual_timeout",
    "first_only", "use_paste", "overwrite", "allow_empty", "default",
    "max_items", "hold_duration", "msg",
})
WEKNORA_WIKI_SUMMARY_LIMIT = 32768
PRE_IMAGE_SAFE_BUDGET = 30000
READ_ONLY_OPERATIONS = READ_ACTIONS | WAIT_ACTIONS | ASSERT_ACTIONS | VARIABLE_ACTIONS
VIEW_LABELS = {}


def _filter_window_page_source(source_text, used_class_names):
    try:
        tree = ast.parse(source_text)
    except SyntaxError:
        return source_text
    lines = source_text.splitlines(keepends=True)
    drop_ranges = []
    replacements = {}
    all_view_classes = {
        alias.asname or alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("Bdd.page_obj")
        for alias in node.names
    }
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            kept_aliases = [
                alias for alias in node.names
                if (alias.asname or alias.name) in used_class_names or (node.module or "").startswith("autowork_core")
            ]
            if not kept_aliases:
                drop_ranges.append((node.lineno, getattr(node, "end_lineno", node.lineno)))
            elif len(kept_aliases) < len(node.names):
                alias_str = ", ".join(
                    f"{a.name} as {a.asname}" if a.asname else a.name
                    for a in kept_aliases
                )
                prefix = "." * (node.level or 0) + (node.module or "")
                replacements[node.lineno] = (
                    getattr(node, "end_lineno", node.lineno),
                    f"from {prefix} import {alias_str}\n",
                )
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.FunctionDef):
                    is_prop = any(
                        (isinstance(d, ast.Name) and d.id in {"property", "cached_property"})
                        or (isinstance(d, ast.Attribute) and d.attr in {"property", "cached_property"})
                        for d in item.decorator_list
                    )
                    seg = ast.get_source_segment(source_text, item) or ""
                    is_view_getter = "get_view(" in seg or any(
                        re.search(rf"\b{re.escape(cls)}\b", seg) for cls in all_view_classes
                    )
                    if not (is_prop and is_view_getter):
                        continue
                    if not any(re.search(rf"\b{re.escape(cls)}\b", seg) for cls in used_class_names):
                        start_line = min([item.lineno] + [d.lineno for d in item.decorator_list])
                        end_line = getattr(item, "end_lineno", item.lineno)
                        drop_ranges.append((start_line, end_line))
    drop_set = {line_no for start, end in drop_ranges for line_no in range(start, end + 1)}
    kept = []
    idx = 1
    while idx <= len(lines):
        if idx in replacements:
            end_ln, rep_text = replacements[idx]
            kept.append(rep_text)
            idx = end_ln + 1
            continue
        if idx not in drop_set:
            kept.append(lines[idx - 1])
        idx += 1
    cleaned = re.sub(r"(?m)^\s*#-+\s*\r?\n", "", "".join(kept))
    return re.sub(r"(?:\r?\n[ \t]*){3,}", "\n\n", cleaned).strip()


def _filter_step_unused_imports(source_text, scenario_step_names=()):
    from autowork_core.knowledge.case_context import _select_active_step_nodes

    try:
        tree = ast.parse(source_text)
    except SyntaxError:
        return source_text
    kept_body = _select_active_step_nodes(tree, source_text, scenario_step_names)
    return _slice_ast_nodes_with_used_imports(tree, source_text, kept_body)


def _slice_ast_nodes_with_used_imports(tree, source_text, kept_body):
    drop_set = set()
    kept_ids = {id(n) for n in kept_body}
    for node in tree.body:
        if id(node) not in kept_ids:
            start_line = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", ())])
            end_line = getattr(node, "end_lineno", node.lineno)
            for ln in range(start_line, end_line + 1):
                drop_set.add(ln)
    active_mod = ast.Module(body=kept_body, type_ignores=[])
    used_names = {
        node.id
        for node in ast.walk(active_mod)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    lines = source_text.splitlines(keepends=True)
    replacements = {}
    for node in kept_body:
        if isinstance(node, ast.ImportFrom):
            kept = [alias for alias in node.names if (alias.asname or alias.name) in used_names]
            if not kept:
                for ln in range(node.lineno, getattr(node, "end_lineno", node.lineno) + 1):
                    drop_set.add(ln)
            elif len(kept) < len(node.names):
                alias_str = ", ".join(
                    f"{a.name} as {a.asname}" if a.asname else a.name
                    for a in kept
                )
                prefix = "." * (node.level or 0) + (node.module or "")
                replacements[node.lineno] = (
                    getattr(node, "end_lineno", node.lineno),
                    f"from {prefix} import {alias_str}\n",
                )
        elif isinstance(node, ast.Import):
            kept = [alias for alias in node.names if (alias.asname or alias.name.split(".", 1)[0]) in used_names]
            if not kept:
                for ln in range(node.lineno, getattr(node, "end_lineno", node.lineno) + 1):
                    drop_set.add(ln)
            elif len(kept) < len(node.names):
                alias_str = ", ".join(
                    f"{a.name} as {a.asname}" if a.asname else a.name
                    for a in kept
                )
                replacements[node.lineno] = (
                    getattr(node, "end_lineno", node.lineno),
                    f"import {alias_str}\n",
                )
    kept_lines = []
    idx = 1
    while idx <= len(lines):
        if idx in replacements:
            end_ln, rep_text = replacements[idx]
            kept_lines.append(rep_text)
            idx = end_ln + 1
            continue
        if idx not in drop_set:
            kept_lines.append(lines[idx - 1])
        idx += 1
    return re.sub(r"(?:\r?\n[ \t]*){3,}", "\n\n", "".join(kept_lines)).strip()


def _extract_source_ownership(sources_dict, scenarios, used_class_names, class_kinds_out=None, declared_yamls_out=None):
    from autowork_core.runtime.knowledge_observation import OBSERVED_ACTIONS, VARIABLE_ACTIONS

    path_to_classes = {}
    path_to_steps = {}
    runtime_classes = {}
    class_to_yamls = {}
    declared_yamls = set()
    class_kinds = {}
    classes_with_uia = set()
    classes_with_custom = set()

    for s in scenarios:
        for st_idx, st in enumerate(s.get("steps") or (), 1):
            step_tag = f"Step {st_idx}"
            for act in st.get("actions") or ():
                v_name = _view_name(act)
                owner = act.get("owner") or {}
                p_name = str(owner.get("page") or "").rsplit(".", 1)[-1]
                op = act.get("operation", "")
                if op in OBSERVED_ACTIONS and op not in VARIABLE_ACTIONS:
                    if v_name and v_name != "BasePage":
                        classes_with_uia.add(v_name)
                    if p_name and p_name != "BasePage":
                        classes_with_uia.add(p_name)
                elif op and op not in OBSERVED_ACTIONS:
                    if v_name and v_name != "BasePage":
                        classes_with_custom.add(v_name)
                    if p_name and p_name != "BasePage":
                        classes_with_custom.add(p_name)
                if v_name and v_name != "BasePage":
                    class_kinds.setdefault(
                        v_name,
                        "HelperPage" if (v_name in classes_with_custom and v_name not in classes_with_uia) else "WindowView",
                    )
                if p_name and p_name != "BasePage" and p_name != v_name:
                    class_kinds[p_name] = "WindowPage"
                for src_path in owner.get("sources") or ():
                    norm_p = str(src_path).replace("\\", "/")
                    cls_list = runtime_classes.setdefault(norm_p, [])
                    for cn in (v_name, p_name):
                        if cn and cn != "BasePage" and cn not in cls_list:
                            cls_list.append(cn)
                    st_list = path_to_steps.setdefault(norm_p, [])
                    if step_tag not in st_list:
                        st_list.append(step_tag)

    for path, info in sorted((sources_dict or {}).items()):
        if not (path.startswith("Bdd/page_obj/") and path.endswith(".py")):
            continue
        text = (info or {}).get("text") or ""
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            cname = node.name
            if used_class_names and cname not in used_class_names:
                continue
            base_names = {
                b.id if isinstance(b, ast.Name) else (b.attr if isinstance(b, ast.Attribute) else "")
                for b in getattr(node, "bases", ())
            }
            cls_seg = ast.get_source_segment(text, node) or ""
            has_child_views = "get_view(" in cls_seg
            if "WindowView" in base_names:
                class_kinds[cname] = "WindowView"
            elif ("WindowPage" in base_names or "BasePage" in base_names) and not has_child_views and cname not in classes_with_uia and cname in classes_with_custom:
                class_kinds[cname] = "HelperPage"
            elif "WindowPage" in base_names:
                class_kinds[cname] = "WindowPage"
            cls_list = path_to_classes.setdefault(path, [])
            if cname not in cls_list:
                cls_list.append(cname)
            for item in node.body:
                if isinstance(item, ast.Assign):
                    for target in item.targets:
                        if isinstance(target, ast.Name) and target.id in {"locator_file", "locator_files", "root_locator_file"}:
                            try:
                                val = ast.literal_eval(item.value)
                            except Exception:
                                val = None
                            rels = [val] if isinstance(val, str) else (list(val) if isinstance(val, (list, tuple)) else [])
                            for rel in rels:
                                if isinstance(rel, str) and rel.strip():
                                    norm_yaml = "Bdd/locators/" + rel.strip().lstrip("/")
                                    if not norm_yaml.endswith((".yaml", ".yml")):
                                        norm_yaml += ".yaml"
                                    class_to_yamls.setdefault(cname, []).append((target.id, norm_yaml))

    for cname, yaml_items in class_to_yamls.items():
        for _attr_kind, norm_yaml in yaml_items:
            declared_yamls.add(norm_yaml)
            cls_list = path_to_classes.setdefault(norm_yaml, [])
            if cname not in cls_list:
                cls_list.append(cname)

    for norm_p, fallback_cls in runtime_classes.items():
        if not path_to_classes.get(norm_p):
            path_to_classes[norm_p] = list(fallback_cls)

    if isinstance(class_kinds_out, dict):
        class_kinds_out.update(class_kinds)
    if isinstance(declared_yamls_out, set):
        declared_yamls_out.update(declared_yamls)
    return path_to_classes, path_to_steps


def _step_decorator_patterns(node):
    patterns = []
    for dec in getattr(node, "decorator_list", ()):
        if not isinstance(dec, ast.Call):
            continue
        func = dec.func
        dec_name = func.id if isinstance(func, ast.Name) else (func.attr if isinstance(func, ast.Attribute) else "")
        if dec_name.lower() in {"given", "when", "then", "step"} and dec.args:
            first_arg = dec.args[0]
            if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
                patterns.append(first_arg.value)
    return patterns


def _split_step_source_blocks(entry_id, path, source_text, scenarios, used_class_names):
    from autowork_core.knowledge.case_context import (
        _matches_step_pattern,
        _select_active_step_nodes,
    )

    scenario_step_names = list(dict.fromkeys(
        st.get("name", "")
        for s in scenarios
        for st in [*(s.get("steps") or ()), *(s.get("declared_steps") or ())]
        if st.get("name")
    ))
    try:
        tree = ast.parse(source_text)
    except SyntaxError:
        cleaned = _filter_step_unused_imports(source_text, scenario_step_names)
        if not cleaned:
            return []
        return [{
            "sub_id": entry_id,
            "func_name": None,
            "step_indices": [],
            "classes": [],
            "code": cleaned,
        }]

    kept_body = _select_active_step_nodes(tree, source_text, scenario_step_names)
    if not kept_body:
        return []
    step_funcs = []
    shared_nodes = []
    for node in kept_body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _step_decorator_patterns(node):
            step_funcs.append(node)
        else:
            shared_nodes.append(node)

    if not step_funcs:
        cleaned = _slice_ast_nodes_with_used_imports(tree, source_text, kept_body)
        if not cleaned:
            return []
        return [{
            "sub_id": entry_id,
            "func_name": None,
            "step_indices": [],
            "classes": sorted(c for c in used_class_names if c != "BasePage" and c in cleaned),
            "code": cleaned,
        }]

    all_steps = [
        (st_idx, st)
        for s in scenarios
        for st_idx, st in enumerate(s.get("steps") or (), 1)
    ]
    helper_defs = [
        n for n in shared_nodes
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    import_nodes = [
        n for n in shared_nodes
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]

    blocks = []
    for func_node in step_funcs:
        patterns = _step_decorator_patterns(func_node)
        matched_indices = []
        step_views = []
        for st_idx, st in all_steps:
            st_name = st.get("name", "")
            if st_name and any(_matches_step_pattern(p, st_name) for p in patterns):
                if st_idx not in matched_indices:
                    matched_indices.append(st_idx)
                for act in st.get("actions") or ():
                    vn = _view_name(act)
                    if vn and vn != "BasePage" and vn not in step_views:
                        step_views.append(vn)
                    pn = str((act.get("owner") or {}).get("page") or "").rsplit(".", 1)[-1]
                    if pn and pn != "BasePage" and pn not in step_views:
                        step_views.append(pn)

        func_used_names = {
            n.id for n in ast.walk(func_node)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        }
        kept_helpers = [h for h in helper_defs if h.name in func_used_names]
        slice_nodes = [*import_nodes, *kept_helpers, func_node]
        code_slice = _slice_ast_nodes_with_used_imports(tree, source_text, slice_nodes)
        for cls_name in sorted(used_class_names):
            if cls_name and cls_name != "BasePage" and re.search(rf"\b{re.escape(cls_name)}\b", code_slice):
                if cls_name not in step_views:
                    step_views.append(cls_name)
        st_tag = "-".join(f"Step{i}" for i in matched_indices) if matched_indices else func_node.name
        sub_id = f"{entry_id}-{st_tag}" if len(step_funcs) > 1 or matched_indices else entry_id
        blocks.append({
            "sub_id": sub_id,
            "func_name": func_node.name,
            "step_indices": matched_indices,
            "classes": step_views,
            "code": code_slice,
        })
    return blocks


def case_contract(text):
    references = []
    for identity, path, digest in SOURCE_ROW.findall(text):
        normalized = PurePosixPath(path.replace("\\", "/"))
        relative = normalized.as_posix()
        if normalized.is_absolute() or ".." in normalized.parts or ":" in relative or not relative.startswith(ALLOWED_SOURCE_ROOTS) or normalized.suffix not in {".py", ".feature", ".yaml", ".yml", ".md"}:
            raise ValueError(f"Case source is outside the permitted project sources: {path}")
        references.append({"id": identity, "path": relative, "sha256": digest.lower()})
    if not references:
        raise ValueError("The case must include its existing source-path / SHA256 table")
    declarations = []
    for language, block in CODE_BLOCK.findall(text):
        if language == "gherkin":
            parsed = Parser().parse(block)
            if parsed is not None:
                declarations.extend(_templates(parsed))
    if not declarations:
        raise ValueError("The case must preserve its complete Gherkin declaration")
    return references, declarations


def export_capture_markdown(capture_dir):
    """Export every observed scenario from one finished run without publication filtering."""
    capture_dir = Path(capture_dir).resolve()
    document = json.loads((capture_dir / "observations.json").read_text(encoding="utf-8-sig"))
    if not document.get("finished_at"):
        raise ValueError("Markdown export requires a finished capture run")
    cases = []
    errors = []
    for feature in document.get("features", ()):
        for scenario in feature.get("scenarios", ()):
            try:
                cases.append(_build_single_scenario_case(
                    capture_dir, feature_source=feature["source"], scenario_name=scenario["name"],
                ))
            except Exception as error:
                errors.append({
                    "feature_source": feature.get("source"), "scenario_name": scenario.get("name"),
                    "type": type(error).__name__, "message": str(error),
                })
    return {
        "status": "partial" if cases and errors else "failed" if errors else "completed",
        "cases": cases,
        "errors": errors,
    }


def build_standalone_case(
    capture_dir=None,
    *,
    query=None,
    feature_source=None,
    scenario_name=None,
    output_path=None,
    method_summary=None,
    polish_spec_path=None,
):
    """Build one immutable artifact per selected capture case; AI polishing is optional."""
    from autowork_core.knowledge.case_context import scan_captures

    cases = scan_captures(
        capture_dir, query=query,
        feature_source=feature_source, scenario_name=scenario_name,
    )["selected_cases"]
    if not cases:
        raise ValueError("No matching passed capture cases")
    if len(cases) != 1 and (output_path or polish_spec_path or method_summary):
        raise ValueError("Per-case overrides require exactly one selected case")
    built_cases = [
        _build_single_scenario_case(
            item["capture_dir"], feature_source=item["feature_source"],
            scenario_name=item["scenario_name"], output_path=output_path,
            method_summary=method_summary, polish_spec_path=polish_spec_path,
        )
        for item in cases
    ]
    first = dict(built_cases[0])
    first["cases"] = built_cases
    return first


def _build_single_scenario_case(
    capture_dir,
    *,
    feature_source,
    scenario_name,
    output_path=None,
    method_summary=None,
    polish_spec_path=None,
):
    from autowork_core.knowledge.case_context import (
        _inspect_single_case,
        case_fingerprint,
        derive_case_identity,
        normalize_case_tags,
    )
    from config.paths import Paths

    capture_dir = Path(capture_dir).resolve()
    doc = json.loads((capture_dir / "observations.json").read_text(encoding="utf-8-sig"))
    raw_feature = next(
        f for f in (doc.get("features") or [])
        if f.get("source") == feature_source or str(f.get("source", "")).endswith(str(feature_source))
    )
    matched_scenarios = [
        s for s in (raw_feature.get("scenarios") or [])
        if not scenario_name or s.get("name") == scenario_name
    ]
    if len(matched_scenarios) != 1:
        raise ValueError("Build requires exactly one matching Scenario")

    feature = dict(raw_feature)
    feature["scenarios"] = matched_scenarios
    target_scen = matched_scenarios[0]
    views = list(dict.fromkeys(
        _view_name(act)
        for st in target_scen.get("steps", ())
        for act in st.get("actions", ())
    ))
    identity = derive_case_identity(
        feature["source"],
        feature.get("name") or PurePosixPath(feature["source"]).stem,
        target_scen.get("name") or "Scenario",
        views=views,
    )

    pseudo_capture = SimpleNamespace(
        run_id=doc["run_id"],
        directory=capture_dir,
        document=doc,
        record_error=lambda stage, err: doc.setdefault("errors", []).append(
            {"stage": stage, "type": type(err).__name__, "message": str(err)}
        ),
        _safe_persist=lambda: None,
    )

    if polish_spec_path:
        spec_candidate = Path(polish_spec_path)
    else:
        spec_candidate = capture_dir / identity["spec_file_name"]

    polish_spec = None
    if spec_candidate.is_file():
        loaded = json.loads(spec_candidate.read_text(encoding="utf-8"))
        expected = {"case_key": identity["case_key"], "run_id": doc["run_id"], "evidence_sha256": case_fingerprint(doc, feature, target_scen)}
        if any(loaded.get(key) != value for key, value in expected.items()):
            raise ValueError("Polish specification does not match this Run, Scenario and frozen evidence")
        polish_spec = loaded
    if polish_spec is None:
        polish_spec = _inspect_single_case(
            capture_dir,
            feature_source=feature["source"],
            scenario_name=target_scen.get("name"),
            output_spec_path=capture_dir / identity["spec_file_name"],
        )["polish_spec"]

    markdown, summary = _synthesize_standalone_case(
        pseudo_capture, feature, polish_spec=polish_spec,
        method_summary=method_summary, identity=identity,
    )
    from tempfile import NamedTemporaryFile
    from autowork_core.knowledge.case_verifier import verify_case_markdown

    with NamedTemporaryFile(suffix=".md", dir=capture_dir, delete=False) as candidate:
        candidate.write(markdown.encode("utf-8"))
        candidate_path = Path(candidate.name)
    try:
        validation = verify_case_markdown(candidate_path, observations_path=capture_dir / "observations.json")
        if validation["status"] != "passed":
            raise ValueError("Built case failed verification: " + "; ".join(validation["errors"]))
    finally:
        candidate_path.unlink(missing_ok=True)
    summary["pre_image_chars"] = validation["summary"].get("pre_image_chars", summary.get("pre_image_chars", 0))
    summary["pre_image_budget"] = validation["summary"].get("pre_image_budget", WEKNORA_WIKI_SUMMARY_LIMIT)
    if validation.get("warnings"):
        summary["warnings"] = list(validation["warnings"])

    if output_path is None:
        output_path = capture_dir / identity["local_file_name"]
    else:
        output_path = Path(output_path)
        if not output_path.is_absolute():
            output_path = Paths.BASE_DIR / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload_bytes = markdown.encode("utf-8")
    doc_sha256 = hashlib.sha256(payload_bytes).hexdigest()
    if output_path.exists() and output_path.read_bytes() != payload_bytes:
        if output_path.name != identity["local_file_name"]:
            raise FileExistsError("The explicit output file exists and was not overwritten")
        output_path = output_path.with_name(output_path.stem + "__" + doc_sha256[:12] + ".md")
    if not output_path.exists():
        with output_path.open("xb") as destination:
            destination.write(payload_bytes)
    elif output_path.read_bytes() != payload_bytes:
        raise FileExistsError("A different artifact already occupies the output path")

    tags = normalize_case_tags(identity["feature_code"], [*identity["tags"], *normalize_case_tags(identity["feature_code"], polish_spec.get("tags"))])
    remote_file_name = identity["remote_file_name"]
    metadata = {
        "case_key": identity["case_key"],
        "product": identity["product"],
        "feature_source": identity["feature_source"],
        "feature_code": identity["feature_code"],
        "feature_id": identity["feature_id"],
        "scenario_name": identity["scenario_name"],
        "run_id": doc["run_id"],
        "content_sha256": doc_sha256,
        "remote_file_name": remote_file_name,
    }
    return {
        "status": "built",
        "output_path": str(output_path),
        "case_sha256": doc_sha256,
        **identity,
        "remote_file_name": remote_file_name,
        "tags": tags,
        "metadata": metadata,
        "run_id": doc["run_id"],
        "summary": summary,
    }


def _synthesize_standalone_case(capture, feature, *, polish_spec=None, method_summary=None, identity=None):
    doc = capture.document
    feature_src = feature["source"]
    feature_id = feature.get("id") or hashlib.sha256(feature_src.encode("utf-8")).hexdigest()[:16]
    feature_name = _qless(feature.get("name") or PurePosixPath(feature_src).stem, max_len=80)
    clean_feature_name = re.sub(r"^Rerun\s*-\s*", "", feature_name, flags=re.I).strip() or PurePosixPath(feature_src).stem
    scenarios = feature.get("scenarios") or []
    scenario_names = "、".join(f"`{scenario['name']}`" for scenario in scenarios) or "无"
    current_decl = _current_declaration(capture, feature)

    spec = dict(polish_spec or {})
    if method_summary:
        spec["method_summary"] = method_summary
    obs_text, step_overview_rows, step_full_chains = _observations(capture, feature, polish_spec=spec)

    all_actions = [
        action
        for s in scenarios
        for st in s.get("steps", ())
        for action in st.get("actions", ())
    ]
    captured_slots = [
        img
        for act in all_actions
        for img in act.get("images", ())
        if img.get("status") == "captured" and img.get("sha256")
    ]
    unique_pngs = len(dict.fromkeys(img["sha256"] for img in captured_slots))
    missing_slots = sum(1 for act in all_actions for img in act.get("images", ()) if img.get("status") != "captured")

    used_locator_keys = set()
    observed_views = []
    pages_used = []

    def _collect_locator_ref(val):
        if isinstance(val, str):
            if val.startswith("$") and not val.startswith(("$$", "$data:")):
                used_locator_keys.add(re.sub(r"^\$(?:loc(?:ator)?:)?", "", val).strip())
            elif "|" in val and re.match(r"^\s*root\s*[:=]", val, flags=re.I):
                used_locator_keys.add(re.split(r"[:=]", val.split("|", 1)[0], maxsplit=1)[1].strip())
        elif isinstance(val, dict):
            for k in ("name", "root", "root_name", "region", "region_name", "parent", "parent_key", "parent_name"):
                if isinstance(val.get(k), str) and val[k]:
                    used_locator_keys.add(val[k])
        elif isinstance(val, CompiledLocator):
            for attr in ("name", "root_name", "region_name"):
                v = getattr(val, attr, None)
                if isinstance(v, str) and v:
                    used_locator_keys.add(v)

    for act in all_actions:
        v_name = _view_name(act)
        if v_name not in observed_views:
            observed_views.append(v_name)
        owner = act.get("owner") or {}
        p_name = str(owner.get("page") or "").rsplit(".", 1)[-1]
        if p_name and p_name not in pages_used:
            pages_used.append(p_name)
        if isinstance(owner.get("root_locator"), str) and owner["root_locator"]:
            used_locator_keys.add(owner["root_locator"])
        win_loc = owner.get("window_locator")
        if isinstance(win_loc, dict) and isinstance(win_loc.get("root_name"), str) and win_loc["root_name"]:
            used_locator_keys.add(win_loc["root_name"])
        for val in (act.get("arguments") or {}).values():
            _collect_locator_ref(val)
        for val in (act.get("resolved_arguments") or {}).values():
            _collect_locator_ref(val)
        _collect_locator_ref(act.get("resolved_locator"))

    from autowork_core.knowledge.case_context import (
        _select_active_step_nodes,
        case_source_paths,
        derive_case_identity,
        normalize_case_tags,
    )

    source_entries = []
    sources_dict = doc.get("sources") or {}
    missing_sources = [path for path in case_source_paths(feature, scenarios[0]) if path not in sources_dict]
    if missing_sources:
        raise ValueError(f"Capture is missing frozen source snapshots: {', '.join(missing_sources)}")

    from autowork_core.runtime.knowledge_observation import OBSERVED_ACTIONS

    observed_runtime_ops = OBSERVED_ACTIONS | {
        act.get("operation")
        for act in all_actions
        if act.get("operation")
    }

    scope_files = {
        f"Bdd/steps/{str(name).replace(chr(92), '/')}"
        for s in scenarios
        for name in (s.get("scope") or {}).get("files", ())
    }
    scenario_step_names = list(dict.fromkeys(
        st.get("name", "")
        for s in scenarios
        for st in [*(s.get("steps") or ()), *(s.get("declared_steps") or ())]
        if st.get("name")
    ))
    gaps = []
    static_imported_classes = set()
    for path in sorted(scope_files):
        src = sources_dict.get(path)
        if not src:
            continue
        text = src.get("text") or src.get("content") or ""
        try:
            tree = ast.parse(text)
            active_body = _select_active_step_nodes(tree, text, scenario_step_names)
            active_mod = ast.Module(body=active_body, type_ignores=[])
            used_names = {
                node.id for node in ast.walk(active_mod)
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
            }
            for node in active_body:
                if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("Bdd.page_obj"):
                    for alias in node.names:
                        cname = alias.asname or alias.name
                        if cname in used_names:
                            static_imported_classes.add(cname)
            assert_compare_ids = {
                id(sub)
                for n in ast.walk(active_mod)
                if isinstance(n, ast.Assert)
                for sub in ast.walk(n.test)
                if isinstance(sub, ast.Compare)
            }
            for node in ast.walk(active_mod):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr not in observed_runtime_ops:
                    gaps.append({"path": path, "line": node.lineno, "call": ast.get_source_segment(text, node), "status": "static_call_site_not_a_runtime_event"})
                elif isinstance(node, (ast.Compare, ast.Assert)) and id(node) not in assert_compare_ids:
                    gaps.append({"path": path, "line": node.lineno, "kind": type(node).__name__, "call": ast.get_source_segment(text, node), "status": "Python_check_Step_status_only"})
        except SyntaxError:
            pass
    feature["unobserved_static_sites"] = gaps

    used_class_names = set(observed_views) | set(pages_used) | static_imported_classes
    class_kinds = {}
    declared_active_yamls = set()
    path_to_classes, path_to_steps = _extract_source_ownership(
        sources_dict, scenarios, used_class_names,
        class_kinds_out=class_kinds, declared_yamls_out=declared_active_yamls,
    )
    extra_static_paths = {
        path for path in sources_dict
        if any(c in static_imported_classes for c in (path_to_classes.get(path) or ()))
    }
    ordered_paths = [feature_src] if feature_src in sources_dict else []
    for path in sorted({*(path for scenario in scenarios for path in case_source_paths(feature, scenario)), *extra_static_paths}):
        if path not in ordered_paths and path.startswith(ALLOWED_SOURCE_ROOTS):
            ordered_paths.append(path)
    for idx, path in enumerate(ordered_paths, 1):
        info = sources_dict[path]
        source_entries.append({
            "id": f"S{idx:02}",
            "path": path,
            "sha256": info["sha256"],
            "status": "unchanged",
        })

    uia_or_snapshotted_classes = {
        c for c in (set(observed_views) | set(pages_used) | {
            cls_name for p in ordered_paths for cls_name in (path_to_classes.get(p) or ())
        })
        if c and c != "BasePage"
    }
    static_only_classes = {
        c for c in static_imported_classes
        if c and c != "BasePage" and c not in uia_or_snapshotted_classes
    }
    view_classes = {c for c in uia_or_snapshotted_classes if class_kinds.get(c) == "WindowView"}
    page_container_classes = {c for c in uia_or_snapshotted_classes if class_kinds.get(c) == "WindowPage"}
    helper_classes = {c for c in uia_or_snapshotted_classes if class_kinds.get(c) == "HelperPage"}
    role_parts = []
    if view_classes and page_container_classes:
        role_parts.append("界面视图类 WindowView：" + "、".join(f"`{c}`" for c in sorted(view_classes)))
        role_parts.append("窗口容器类 WindowPage：" + "、".join(f"`{c}`" for c in sorted(page_container_classes)))
    if helper_classes:
        role_parts.append(
            "辅助/模拟器调用类："
            + "、".join(f"`{c}`（非 UIA 方法调用已记入第 3/4/5 章，类定义见第 6 章）" for c in sorted(helper_classes))
        )
    if static_only_classes:
        role_parts.append(
            "静态调用辅助类："
            + "、".join(f"`{c}`（非 UIA 静态调用，调用点见 3.3 节与第 6 章 Step 源码）" for c in sorted(static_only_classes))
        )
    active_classes_display = "、".join(f"`{c}`" for c in sorted(uia_or_snapshotted_classes)) or "`BasePage`"
    if role_parts:
        active_classes_display += "（" + "；".join(role_parts) + "）"

    id_info = dict(identity or {})
    feature_code = id_info.get("feature_code") or PurePosixPath(feature_src).stem
    doc_title = _qless(
        spec.get("title") or f"{feature_code}（{clean_feature_name}）/ {_qless(scenarios[0]['name'], max_len=60)} 工程案例记录",
        max_len=120,
    )
    data_note = _qless(
        spec.get("data_compliance_note")
        or "图片和原始输入未自动脱敏，发布前须确认使用的是获准非临床测试数据。",
        max_len=240,
    )
    case_key_val = spec.get("case_key") or id_info.get("case_key")
    capture_case_id = spec.get("capture_case_id") or id_info.get("capture_case_id") or ""
    remote_path_val = id_info.get("remote_file_name") or derive_case_identity(feature_src, clean_feature_name, scenarios[0]["name"])["remote_file_name"]
    tags_list = normalize_case_tags(
        feature_code,
        [
            *(id_info.get("tags") or [f"feature:{feature_code}"]),
            *normalize_case_tags(feature_code, spec.get("tags")),
        ],
    )
    tags_display = "、".join(_inline_code(tag) for tag in tags_list)

    header = (
        f"# {doc_title}\n\n"
        "## 1. 身份、适用范围与本次状态\n\n"
        f"- 案例唯一键（Case Key）：`{case_key_val}`\n"
        f"- 采集案例 ID（Capture Case ID）：`{capture_case_id}`\n"
        f"- 证据指纹（Evidence Hash）：`{spec['evidence_sha256']}`\n"
        f"- Feature 身份：`{feature_src}`\n"
        f"- Feature 需求标识：{feature_code}（{clean_feature_name}）；Feature ID：`{feature_id}`；Scenario：{scenario_names}；Run ID：`{capture.run_id}`。\n"
        f"- 本次实际调用的 Page/View 类：{active_classes_display}。\n"
        f"- 建议上传文件名：`{remote_path_val}`\n"
        f"- 文档分类标签：{tags_display}\n"
        f"- 时间：`{doc.get('started_at', 'unknown')}` 至 `{doc.get('finished_at', 'unknown')}`。\n"
        f"- 脚本状态：`{_qless(feature.get('test_status', doc.get('test_status', 'unknown')))}`；`partial_execution={_qless(_partial_execution(feature))}`。\n"
        f"- 采集状态：`{_qless(doc.get('capture_status', 'unknown'))}`，`partial_public_actions_only`。共 {len(all_actions)} 个公开动作，{len(captured_slots)} 次成功截图关联，{unique_pngs} 张唯一 PNG，{missing_slots} 次截图缺失。\n"
        "- 文档角色：工程案例运行与实现记录，不是产品手册或正式规格；第 1–3 章为全流程总览、Step 级操作汇总、变量关系与源码索引，第 4–7 章为按 Step 展开的完整动作链、原图证据、精确追溯索引、源码快照与来源指纹。\n"
        f"- 测试输入与合规说明：{data_note}\n\n"
        "阅读导航：[测试方法与确定性时序](#case-method) → [场景实现与定位器索引](#case-code-snapshots) → [Step 1 图文与完整动作链](#step-1) → [精确追溯索引](#case-trace) → [源码与定位器快照](#case-sources) → [来源指纹](#case-fingerprints)。\n\n"
        "## 2. 完整场景声明\n\n"
        + _fence(current_decl, "gherkin")
        + '\n<a id="case-method"></a>\n\n'
        + "## 3. 测试方法、确定性执行时序与数据关联\n\n"
        + "### 3.1 Step 执行概览与完整动作返回链\n\n"
    )
    summary_text = _qless(
        spec.get("method_summary")
        or "本案例由运行采集证据与源码快照确定性组装，保持真实 Step.Action 执行顺序。",
        max_len=1200,
    )
    header += f"{summary_text}\n\n"
    if step_overview_rows:
        header += _table(
            ["场景步骤", "状态", "调用的 Page/View 类与窗口根", "核心操作、读写参数与断言汇总", "关联截图范围"],
            step_overview_rows,
        )
        header += "> 各 Step 按执行顺序展开的逐动作调用参数、返回值、动作性质标签与 `before/after` 原图证据见[第 4 章](#step-1)。\n\n"

    baselines = spec.get("parameter_baselines") or []
    if baselines:
        header += "### 3.2 变量读写与代码检查记录表\n\n"
        if "variable" in baselines[0]:
            header += _table(
                ["变量名", "写入动作与记录值", "同 Step 值来源动作", "读取消费动作", "Step 源码中的比较/断言语句"],
                [
                    [
                        f"`{_qless(b.get('variable', '-'), max_len=32)}`",
                        _qless(b.get("write_action", "-"), max_len=60),
                        _qless(b.get("source_action", "-"), max_len=80),
                        _qless(b.get("consume_actions", "-"), max_len=80),
                        _qless(b.get("code_checks", "-"), max_len=120),
                    ]
                    for b in baselines
                ],
            )
        else:
            header += _table(
                ["参数", "前序读取动作与截图", "保存变量", "后序读取动作与截图", "保存变量", "比较读取动作", "本次属性返回（前 / 后）"],
                [
                    [
                        _qless(b.get("parameter", "-"), max_len=32),
                        _qless(b.get("before_read", "-"), max_len=80),
                        _qless(b.get("before_var", "-"), max_len=40),
                        _qless(b.get("after_read", "-"), max_len=80),
                        _qless(b.get("after_var", "-"), max_len=40),
                        _qless(b.get("compare_actions", "-"), max_len=40),
                        _qless(b.get("values", "-"), max_len=60),
                    ]
                    for b in baselines
                ],
            )

    if gaps:
        header += "### 3.3 未作为公开 UIA 动作记录的静态调用与代码断言\n\n"
        header += _table(
            ["来源文件", "行号", "静态调用或比较语句原文", "覆盖性质"],
            [
                [
                    f"`{g['path']}`",
                    str(g["line"]),
                    f"`{_qless(g.get('call') or g.get('kind'), max_len=72)}`",
                    f"`{g['status']}`",
                ]
                for g in gaps
            ],
        )

    boundaries = spec.get("inference_boundaries") or [
        "交互修改动作（`click`、`double_click`、`right_click`、`input_text`、`select`）返回 `null (returned_none)` 仅代表调用无异常返回，不直接证明截图瞬间控件可用或异步物理流程已完成。",
        "只读动作（`get_attr`、`get_text`、`get_variable`、`assert_exists`、`assert_not_exists`、`wait_enabled`、`wait_visible`、`wait_exists`）不会改变界面选中行、高亮状态或右侧预览图像；截图中的既有高亮或预览归属由前序交互动作产生，不得归因为只读断言的结果。",
        "`assert_exists` 仅验证目标文本或元素在界面存在，不单独证明集合数量唯一或结果归属。",
        "单次运行的输入值、返回值与状态受当前测试环境约束；参数适用条件须结合正式资料库原文核对，不得直接升格为通用产品规格。",
    ]
    header += "### 3.4 动作与观察边界说明\n\n"
    for b_item in boundaries:
        header += f"- {_qless(b_item, max_len=320)}\n"
    header += "\n"

    header += (
        '<a id="case-code-snapshots"></a>\n\n'
        "### 3.5 场景实现与定位器源码快照（索引表见本节，完整代码块归位见第 6 章）\n\n"
    )
    if "### 8.1 精确追溯索引与未覆盖边界（Trace Index）\n\n" in obs_text:
        step_part, trace_part = obs_text.split("\n### 8.1 精确追溯索引与未覆盖边界（Trace Index）\n\n", 1)
    else:
        step_part, trace_part = obs_text, ""

    candidate_blocks = []
    for entry in source_entries:
        path = entry["path"]
        owner_classes = path_to_classes.get(path) or []
        owner_steps = path_to_steps.get(path) or []
        owner_suffix = f"（{'、'.join(owner_classes)}）" if owner_classes else ""
        if path.endswith((".feature", ".md")) or path.startswith("autowork_core/page/singleton.py"):
            candidate_blocks.append({"entry": entry, "path": path, "role": "场景声明或框架公共底座", "note": "见第 2 章 / 第 7 章", "block": None})
            continue
        src = sources_dict.get(path)
        if not src or not src.get("text", "").strip():
            candidate_blocks.append({"entry": entry, "path": path, "role": "空文件快照", "note": "无可执行代码", "block": None})
            continue
        raw_text = src["text"]
        lang = "python" if path.endswith(".py") else ("yaml" if path.endswith((".yaml", ".yml")) else "text")
        base_role = "Step 实现源码" if path.startswith("Bdd/steps/") else ("Page/View 类定义" if path.startswith("Bdd/page_obj/") else "YAML 定位器配置")
        role = base_role + owner_suffix
        if path.endswith((".yaml", ".yml")):
            parsed_yaml = yaml.safe_load(raw_text) or {}
            if isinstance(parsed_yaml, dict):
                norm_used = {str(k).strip().lower() for k in used_locator_keys if k}
                matched_keys = [k for k in parsed_yaml if str(k).strip().lower() in norm_used]
                if not matched_keys and path not in declared_active_yamls:
                    candidate_blocks.append({"entry": entry, "path": path, "role": role, "note": "本场景未调用其中定位器（见第 7 章指纹）", "block": None})
                    continue
                dumped_yaml = raw_text.rstrip()
                cls_tag = "、".join(owner_classes) if owner_classes else "未标注"
                yaml_with_comment = f"# {entry['id']}: {cls_tag}\n{dumped_yaml}"
                block_str = (
                    f"### [{entry['id']}] `{path}`（YAML 定位器配置｜所属 Page/View：{cls_tag}）\n"
                    + _fence(yaml_with_comment, lang)
                    + "\n"
                )
                candidate_blocks.append({"entry": entry, "path": path, "role": role, "note": None, "block": block_str})
                continue
        elif path.startswith("Bdd/page_obj/") and path.endswith(".py"):
            raw_text = _filter_window_page_source(raw_text, used_class_names)
            cls_tag = "、".join(owner_classes) if owner_classes else "Page/View"
            py_with_comment = f"# {entry['id']}: {cls_tag}\n{raw_text.rstrip()}"
            block_str = (
                f"### [{entry['id']}] `{path}`（Page/View 类定义｜所属类：{cls_tag}）\n"
                + _fence(py_with_comment, lang)
                + "\n"
            )
            candidate_blocks.append({"entry": entry, "path": path, "role": role, "note": None, "block": block_str})
            continue
        elif path.startswith("Bdd/steps/") and path.endswith(".py"):
            step_slices = _split_step_source_blocks(entry["id"], path, raw_text, scenarios, used_class_names)
            if not step_slices:
                candidate_blocks.append({"entry": entry, "path": path, "role": role, "note": "本场景未调用其中步骤函数（见第 7 章指纹）", "block": None})
                continue
            step_blocks_rendered = []
            scen_title = _qless(scenarios[0]["name"], max_len=64) if scenarios else "Scenario"
            for s_item in step_slices:
                st_label = "、".join(f"Step {i}" for i in s_item["step_indices"]) if s_item["step_indices"] else "环境初始化与辅助定义"
                fn_suffix = f" :: `{s_item['func_name']}`" if s_item["func_name"] else ""
                code_with_comment = (
                    f"# {s_item['sub_id']}: {feature_code} / {scen_title} ({st_label})\n"
                    f"{s_item['code'].rstrip()}"
                )
                step_blocks_rendered.append(
                    f"### [{s_item['sub_id']}] `{path}`{fn_suffix}（{st_label} 实现源码｜所属需求：{feature_code}｜所属业务场景：{scen_title}）\n"
                    + _fence(code_with_comment, lang)
                )
            role = f"Step 实现源码（所属需求：{feature_code}｜所属业务场景：{scen_title}）"
            block_str = "\n".join(step_blocks_rendered) + "\n"
            candidate_blocks.append({"entry": entry, "path": path, "role": role, "note": None, "block": block_str})
            continue
        block_str = (
            f"### [{entry['id']}] `{path}`\n"
            + _fence(raw_text.rstrip(), lang)
            + "\n"
        )
        candidate_blocks.append({"entry": entry, "path": path, "role": role, "note": None, "block": block_str})

    source_index_rows = []
    deferred_blocks = []
    active_source_ids = []
    for item in candidate_blocks:
        entry = item["entry"]
        if item["block"] is None:
            source_index_rows.append([entry["id"], f"`{item['path']}`", item["role"], item["note"]])
        else:
            deferred_blocks.append(item["block"])
            active_source_ids.append(f"`{entry['id']}`（{item['role']}）")
            source_index_rows.append([entry["id"], f"`{item['path']}`", item["role"], "完整代码块后置见第 6 章"])

    active_sources_summary = "、".join(active_source_ids) if active_source_ids else "无活跃源码块"
    header += (
        f"- 本场景共涉及 {len(deferred_blocks)} 个活跃源码与 YAML 定位器快照（{active_sources_summary}），"
        "完整代码块后置见[第 6 章](#case-sources)，完整文件指纹见[第 7 章](#case-fingerprints)。\n\n"
    )
    role_col_header = f"证据角色与所属 Page/View ({feature_code})"
    ch4_heading = "# 4. 动作与原图证据\n\n## 4. 按 Step 组织的全部动作、定位目标与原图（图文同块）\n"
    ch6_extra = ("\n" + "".join(deferred_blocks)) if deferred_blocks else ""
    body = (
        header
        + ch4_heading
        + step_part
        + '\n<a id="case-trace"></a>\n\n'
        + "## 5. 精确追溯索引与未覆盖边界\n\n"
        + trace_part
        + '\n<a id="case-sources"></a>\n\n'
        + "## 6. 实现与资源证据索引（采集时的来源快照）\n\n"
        + "本节按来源文件与 Step 拆分承载本场景实际调用的 YAML 定位器配置、Page/View 类定义及 Step 实现源码快照：\n\n"
        + _table(["来源编号", "仓库相对路径", role_col_header, "快照位置"], source_index_rows)
        + ch6_extra
        + '\n<a id="case-fingerprints"></a>\n\n'
        + "## 7. 来源指纹\n\n"
        + _table(
            ["来源", "仓库相对路径", "运行快照 SHA256"],
            [[entry["id"], f"`{entry['path']}`", f"`{entry['sha256']}`"] for entry in source_entries],
        )
    )
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    body_fingerprint = hashlib.sha256(body.encode("utf-8")).hexdigest()
    body = body.replace(
        f"- 案例唯一键（Case Key）：`{case_key_val}`\n",
        f"- 案例唯一键（Case Key）：`{case_key_val}`\n- 内容指纹（Content Hash）：`{body_fingerprint}`\n",
        1,
    )
    pre_image_chars = len(body.split("![F01](", 1)[0]) if "![F01](" in body else len(body.split("\n## 4. ", 1)[0])
    summary = {
        "run_id": capture.run_id,
        "feature": feature_src,
        "body_fingerprint": body_fingerprint,
        "polished_by": spec.get("polished_by", "auto_extracted_default"),
        "test_status": feature.get("test_status"),
        "capture_status": doc.get("capture_status"),
        "action_count": len(all_actions),
        "unique_png_count": unique_pngs,
        "parameter_baseline_count": len(baselines),
        "source_count": len(source_entries),
        "pre_image_chars": pre_image_chars,
        "pre_image_budget": WEKNORA_WIKI_SUMMARY_LIMIT,
        "deferred_source_blocks": len(deferred_blocks),
    }
    return body, summary


def _templates(feature):
    return list(feature.scenarios) + [scenario for rule in feature.rules for scenario in rule.scenarios]


def _logical_name(feature, scenario):
    source_scenario = scenario.get("source_scenario")
    if source_scenario is not None:
        return source_scenario["name"]
    selection = feature.get("selection") or {}
    if selection.get("scenario_name"):
        return selection["scenario_name"]
    parent = scenario.get("parent") or {}
    if parent.get("kind") == "ScenarioOutline":
        return parent["name"]
    return scenario["name"]


def _matching_template(scenarios, name):
    matching = [scenario for scenario in scenarios if name == scenario.name or name.startswith(scenario.name + " -- @")]
    if len(matching) != 1:
        raise ValueError(f"Case scope cannot uniquely identify observed scenario: {name}")
    return matching[0]


def _partial_execution(feature):
    if feature.get("partial_execution") or len(feature.get("scenarios", ())) != len(feature.get("declared_cases", ())):
        return True
    return any(
        len(scenario.get("steps", ())) != len(scenario.get("declared_steps", ()))
        or any(step.get("test_status") in {"running", "untested", "skipped", "undefined"} for step in scenario.get("steps", ()))
        for scenario in feature.get("scenarios", ())
    )


def _node_start(node):
    tags = getattr(node, "tags", ())
    return min([node.line] + [tag.line for tag in tags if hasattr(tag, "line")])


def _current_declaration(capture, feature):
    source = capture.document["sources"][feature["source"]]["text"]
    parsed = Parser().parse(source, filename=feature["source"])
    scenarios = _templates(parsed)
    selected = [_matching_template(scenarios, _logical_name(feature, scenario)) for scenario in feature["scenarios"]]
    if not selected:
        return "# No Scenario was executed.\n"
    lines = source.splitlines(keepends=True)
    top_level = sorted(list(parsed.scenarios) + list(parsed.rules), key=_node_start)
    sections = [(1, _node_start(top_level[0]) if top_level else len(lines) + 1)]
    for scenario in selected:
        parent = scenario.parent
        if type(parent).__name__ == "Rule":
            sections.append((_node_start(parent), min(_node_start(item) for item in parent.scenarios)))
            siblings = parent.scenarios
        else:
            siblings = top_level
        following = [item for item in siblings if _node_start(item) > _node_start(scenario)]
        end = min((_node_start(item) for item in following), default=len(lines) + 1)
        if type(parent).__name__ == "Rule" and not following:
            later_rules = [item for item in top_level if _node_start(item) > _node_start(parent)]
            end = min((_node_start(item) for item in later_rules), default=len(lines) + 1)
        sections.append((_node_start(scenario), end))
    return "\n".join("".join(lines[start - 1:end - 1]) for start, end in sorted(set(sections)))


def _fence(text, language):
    backticks = max((len(match) for match in re.findall(r"`+", text)), default=0)
    marker = "`" * max(3, backticks + 1)
    return f"{marker}{language}\n{text}\n{marker}\n"


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _code_cell(value):
    return _inline_code(_encoded(value).replace("|", "\\|"))


def _inline_code(text):
    marker = "`" * max(1, 1 + max((len(item) for item in re.findall(r"`+", text)), default=0))
    padding = " " if text.startswith(("`", " ")) or text.endswith(("`", " ")) else ""
    return marker + padding + text + padding + marker


def _table(headers, rows):
    if not rows:
        return ""
    return (
        "| " + " | ".join(headers) + " |\n"
        "| " + " | ".join("---" for _ in headers) + " |\n"
        + "".join("| " + " | ".join(row) + " |\n" for row in rows)
        + "\n"
    )


def _qless(value, max_len=48):
    if value is None:
        text = "null"
    elif isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (int, float)):
        text = str(value)
    elif isinstance(value, (list, tuple)):
        text = "[" + ", ".join(_qless(item, max_len=max_len) for item in value) + "]"
    elif isinstance(value, dict):
        text = "{" + ", ".join(f"{k}: {_qless(v, max_len=max_len)}" for k, v in value.items()) + "}"
    else:
        text = str(value)
    text = text.replace('"', "").replace("'", "").replace("\r", " ").replace("\n", " ").replace("|", "/")
    if max_len and len(text) > max_len:
        return text[: max_len - 3] + "..."
    return text


def _frame_preview(value, max_len, *, escape_separators=False):
    text = _qless(value, max_len=0)
    escaped = {
        "&": "&amp;", "<": "&lt;", ">": "&gt;", "`": "&#96;",
        "[": "&#91;", "]": "&#93;", "(": "&#40;", ")": "&#41;",
        "*": "&#42;", "_": "&#95;", "\\": "&#92;",
    }
    if escape_separators:
        escaped.update({";": ",", "；": ",", "。": ",", "！": ",", "？": ","})
    tokens = [escaped.get(character, character) for character in text]
    if sum(map(len, tokens)) <= max_len:
        return "".join(tokens)
    result = []
    size = 0
    for token in tokens:
        if size + len(token) > max_len - 3:
            break
        result.append(token)
        size += len(token)
    return "".join(result) + "..."


def _short_time(timestamp):
    if not timestamp:
        return "unknown"
    text = str(timestamp)
    return text.split("T", 1)[1].replace("+00:00", "Z") if "T" in text else text


def _view_name(action):
    owner = action.get("owner") or {}
    raw = owner.get("view") or owner.get("page") or "BasePage"
    return str(raw).rsplit(".", 1)[-1]


def _view_label(view):
    return VIEW_LABELS.get(view, view)


def _action_query_and_target(action):
    query = action.get("resolved_locator") if "resolved_locator" in action else action.get("query")
    target = action.get("target_identity") if "target_identity" in action else action.get("target")
    return query, target


def _control_summary(target_obj):
    if not isinstance(target_obj, dict):
        return "无UIA控件记录"
    controls = target_obj.get("controls") or []
    if not controls or not isinstance(controls[0], dict):
        return "无UIA控件记录"
    ctrl = controls[0]
    ctype = _qless(ctrl.get("control_type") or "Control", max_len=20)
    aid = _qless(ctrl.get("automation_id") or "", max_len=28)
    name = str(ctrl.get("name") or "")
    if len(name) > 28 and "." in name:
        name = name.rsplit(".", 1)[-1]
    name = _qless(name, max_len=28)
    parts = ([f"auto_id={aid}"] if aid else []) + ([f"name={name}"] if name else [])
    return f"{ctype}(" + ", ".join(parts) + ")" if parts else ctype


def _natural_call(action, common_opts, concise=False):
    from autowork_core.knowledge.case_context import pick_target_arg_key

    args = dict(action.get("arguments") or {})
    target_key = pick_target_arg_key(args)
    binding = args.pop(target_key) if target_key else ""
    if isinstance(binding, dict):
        binding_str = _qless(binding.get("value", binding), max_len=64)
    else:
        binding_str = _qless(binding, max_len=64)
    opts = {k: v for k, v in args.items() if k not in common_opts and v is not None}
    opt_vals = [
        _qless(v, max_len=36)
        if k in ("data_or_name", "attr_name", "value", "option_or_name", "expected", "value_or_name")
        else f"{k}={_qless(v, max_len=24)}"
        for k, v in opts.items()
    ]
    actual = _qless(action.get("return_value", action.get("exception")), max_len=32)
    outcome = _qless(action.get("outcome", "unknown"), max_len=20)
    if concise:
        opt_str = ("," + ",".join(opt_vals)) if opt_vals else ""
        return f"调用 `{action['operation']}({binding_str}{opt_str})` 返回 `{actual}`"
    opt_str = ("，参数 " + "、".join(opt_vals)) if opt_vals else ""
    return f"调用 `{action['operation']}({binding_str})`{opt_str}，返回 `{actual}`（{outcome}）"


def _compact_locator_query(query_obj, shared_queries_out=None):
    if isinstance(query_obj, CompiledLocator):
        from autowork_core.runtime.knowledge_observation import captured_value

        query_obj = captured_value(query_obj)
    if not isinstance(query_obj, dict):
        return query_obj
    if shared_queries_out is not None:
        for nested_key in ("root_locator", "region_locator"):
            nested = query_obj.get(nested_key)
            if isinstance(nested, dict) and nested:
                compacted_nested = _compact_locator_query(nested, shared_queries_out)
                shared_queries_out.append(compacted_nested)
    standard_raw_keys = {
        "root", "region", "by", "value", "top_level", "backend",
        "control_type", "auto_id", "name", "class_name", "screen",
        "criteria", "prefix", "parent", "parent_key", "parent_name",
    }
    omitted_false_or_null = {
        "region_name", "root_name", "screen_selector", "root_cache_name",
        "top_level", "needs_root", "needs_monitor", "returns_position",
    }
    compacted = {}
    for k, v in query_obj.items():
        if k in ("root_locator", "region_locator") and (v is None or isinstance(v, dict)):
            continue
        if k == "raw" and isinstance(v, dict):
            extra_raw = {rk: rv for rk, rv in v.items() if rk not in standard_raw_keys}
            if extra_raw:
                compacted["raw"] = extra_raw
            continue
        if k in omitted_false_or_null and (v is None or v is False):
            continue
        compacted[k] = v
    return compacted


def _summarize_step_operations(st_actions, common):
    from autowork_core.knowledge.case_context import pick_target_arg_key, strip_ref_prefix
    from autowork_core.runtime.knowledge_observation import OBSERVED_ACTIONS

    if not st_actions:
        return "无公开动作记录"
    groups = []
    for act in st_actions:
        op = act["operation"]
        args = dict(act.get("arguments") or {})
        t_key = pick_target_arg_key(args)
        raw_target = args.pop(t_key, "") if t_key else ""
        if isinstance(raw_target, dict):
            target_str = str(raw_target.get("value") or raw_target)
        else:
            target_str = str(raw_target)
        short_t = strip_ref_prefix(target_str)
        if "//Text[@name=" in short_t:
            m = re.search(r"@name=['\"]?([^'\"]+)['\"]?", short_t)
            if m:
                short_t = m.group(1)
        short_t = _qless(short_t, max_len=28)
        ret = act.get("return_value", act.get("exception"))
        if op == "set_variable":
            val_str = _qless(args.get("value", ret), max_len=18)
            item_desc = f"{short_t}={val_str}"
        elif op in {"save_attr", "save_text"}:
            var_name = _qless(args.get("variable_name", ""), max_len=18)
            ret_str = _qless(ret, max_len=18)
            item_desc = f"{var_name}←{short_t}({ret_str})"
        elif op == "get_variable":
            item_desc = f"{short_t}={_qless(ret, max_len=16)}"
        elif op in {"get_attr", "get_text"}:
            item_desc = f"{short_t}→{_qless(ret, max_len=18)}"
        elif op in {"input_text", "select"}:
            val_arg = args.get("data_or_name", args.get("option_or_name", args.get("value", "")))
            item_desc = f"{short_t}={_qless(val_arg, max_len=18)}" if val_arg != "" else short_t
        elif op.startswith(("assert_", "wait_")):
            item_desc = f"{short_t}→{_qless(ret, max_len=12)}" if short_t else f"→{_qless(ret, max_len=12)}"
        elif op not in OBSERVED_ACTIONS:
            param_parts = [f"{k}={_qless(v, max_len=14)}" for k, v in args.items() if k not in DEFAULT_CALL_KEYS and v is not None]
            if short_t:
                param_parts.insert(0, short_t)
            p_str = ", ".join(param_parts)
            item_desc = f"{p_str}→{_qless(ret, max_len=16)}" if p_str else f"→{_qless(ret, max_len=16)}"
        else:
            item_desc = short_t or op

        if groups and groups[-1][0] == op:
            groups[-1][1].append(item_desc)
        else:
            groups.append((op, [item_desc]))

    segments = []
    for op, items in groups:
        if len(items) > 10:
            shown = ", ".join(items[:7]) + f" ... {items[-1]}（共{len(items)}项）"
        else:
            shown = ", ".join(items)
        segments.append(f"`{op}({shown})`")
    return _qless(" → ".join(segments), max_len=900)


def _format_step_action_chain_bullets(st_idx, views, st_actions, common, image_summary_fn, max_bullet_chars=330):
    if not st_actions:
        return []
    views_str = " / ".join(views) if views else "BasePage"
    items = []
    for a_idx, act in enumerate(st_actions, 1):
        code = f"{st_idx}.{a_idx}"
        call_str = f"`{code}` {_natural_call(act, common[act['operation']], concise=True)}（{_qless(act.get('outcome'))}，{image_summary_fn(act)}）"
        items.append((code, call_str))

    bullets = []
    cur_codes = []
    cur_calls = []
    cur_len = 0
    for code, call_str in items:
        sep_len = 3 if cur_calls else 0
        prefix_est = len(f"- **[Step {st_idx} / {views_str} 动作链 {cur_codes[0] if cur_codes else code}–{code}]**：。")
        if cur_calls and (prefix_est + cur_len + sep_len + len(call_str) > max_bullet_chars):
            span = f"{cur_codes[0]}–{cur_codes[-1]}" if len(cur_codes) > 1 else cur_codes[0]
            bullets.append(f"- **[Step {st_idx} / {views_str} 动作链 {span}]**：{' → '.join(cur_calls)}。")
            cur_codes = [code]
            cur_calls = [call_str]
            cur_len = len(call_str)
        else:
            cur_codes.append(code)
            cur_calls.append(call_str)
            cur_len += sep_len + len(call_str)
    if cur_calls:
        span = f"{cur_codes[0]}–{cur_codes[-1]}" if len(cur_codes) > 1 else cur_codes[0]
        bullets.append(f"- **[Step {st_idx} / {views_str} 动作链 {span}]**：{' → '.join(cur_calls)}。")
    return bullets


def _observations(capture, feature, polish_spec=None):
    spec = polish_spec or {}
    custom_view_labels = {**VIEW_LABELS, **(spec.get("view_labels") or {})}
    action_titles = spec.get("action_titles") or {}
    frame_notes = spec.get("frame_notes") or {}
    step_narratives = spec.get("step_narratives") or {}
    non_image_groups = spec.get("non_image_groups") or {}

    def _vlabel(view):
        return custom_view_labels.get(view, view)

    all_actions = [
        (s_idx, st_idx, a_idx, scenario, step, action)
        for s_idx, scenario in enumerate(feature["scenarios"], 1)
        for st_idx, step in enumerate(scenario["steps"], 1)
        for a_idx, action in enumerate(step["actions"], 1)
    ]
    common = {}
    for operation in dict.fromkeys(act["operation"] for *_, act in all_actions):
        arg_lists = [act.get("arguments") or {} for *_, act in all_actions if act["operation"] == operation]
        if arg_lists:
            common[operation] = {
                key: arg_lists[0][key]
                for key in sorted(DEFAULT_CALL_KEYS & set(arg_lists[0]))
                if all(key in item and _encoded(item[key]) == _encoded(arg_lists[0][key]) for item in arg_lists)
            }
        else:
            common[operation] = {}

    frame_ids = {}
    frame_usages = {}
    verified_payloads = {}
    region_ids = {}
    regions = {}
    target_ids = {}
    target_rows = {}
    target_scopes = {}
    query_ids = {}
    query_rows = {}
    query_scopes = {}
    missing_entries = []
    slot_status = {}
    action_target_id = {}
    action_query_id = {}
    deferred_shared_queries = []

    for s_idx, st_idx, a_idx, scenario, step, action in all_actions:
        view = _view_name(action)
        scope_label = f"Step {st_idx}" if len(feature["scenarios"]) == 1 else f"S{s_idx}.Step {st_idx}"
        raw_query_obj, target_obj = _action_query_and_target(action)
        shared_qs = []
        query_obj = _compact_locator_query(raw_query_obj, shared_qs) if raw_query_obj is not None else None
        for sq in shared_qs:
            deferred_shared_queries.append((scope_label, sq))
        if query_obj is not None or target_obj is not None:
            pair = {"query": query_obj, "target": target_obj}
            t_key = _encoded(pair)
            tid = target_ids.setdefault(t_key, f"T{len(target_ids) + 1:02}")
            target_rows[tid] = pair
            target_scopes.setdefault(tid, [])
            if scope_label not in target_scopes[tid]:
                target_scopes[tid].append(scope_label)
            if query_obj is not None:
                q_key = _encoded(query_obj)
                qid = query_ids.setdefault(q_key, f"Q{len(query_ids) + 1:02}")
                query_rows[qid] = query_obj
                query_scopes.setdefault(qid, [])
                if scope_label not in query_scopes[qid]:
                    query_scopes[qid].append(scope_label)
                action_query_id[id(action)] = qid
            else:
                action_query_id[id(action)] = "-"
            action_target_id[id(action)] = tid
        else:
            action_target_id[id(action)] = "-"
            action_query_id[id(action)] = "-"

        for image in action.get("images", []):
            if image.get("status") != "captured":
                err_id = f"E{len(missing_entries) + 1}"
                missing_entries.append((
                    err_id,
                    action["id"],
                    image.get("phase", "unknown"),
                    image.get("observed_at", "unknown"),
                    image.get("error") or {"message": "missing"},
                ))
                slot_status[id(image)] = ("missing", err_id)
                continue
            try:
                payload, digest = _verified_image(capture.directory, image)
            except Exception as error:
                capture.record_error(f"export_image:{action['id']}:{image['phase']}", error)
                err_id = f"E{len(missing_entries) + 1}"
                missing_entries.append((
                    err_id,
                    action["id"],
                    image.get("phase", "unknown"),
                    image.get("observed_at", "unknown"),
                    {"type": type(error).__name__, "message": str(error)},
                ))
                slot_status[id(image)] = ("invalid", err_id)
                continue
            fid = frame_ids.setdefault(digest, f"F{len(frame_ids) + 1:02}")
            verified_payloads.setdefault(digest, base64.b64encode(payload).decode("ascii"))
            raw_reg = image.get("region") or {}
            if isinstance(raw_reg, dict) and raw_reg.get("captured_at") == image.get("observed_at"):
                reg_obj = {k: v for k, v in raw_reg.items() if k != "captured_at"}
            else:
                reg_obj = raw_reg
            reg_key = _encoded(reg_obj)
            rid = region_ids.setdefault(reg_key, f"R{len(region_ids) + 1:02}")
            regions[rid] = reg_obj
            frame_usages.setdefault(digest, []).append((s_idx, st_idx, a_idx, action, image, rid))
            slot_status[id(image)] = ("ok", digest, fid, rid)

    for scope_label, sq in deferred_shared_queries:
        sq_key = _encoded(sq)
        sq_id = query_ids.setdefault(sq_key, f"Q{len(query_ids) + 1:02}")
        query_rows[sq_id] = sq
        query_scopes.setdefault(sq_id, [])
        if scope_label not in query_scopes[sq_id]:
            query_scopes[sq_id].append(scope_label)

    def _action_image_summary(action, precise=False, seen_anchors=None, compact=False):
        parts = []
        for image in action.get("images", []):
            state = slot_status.get(id(image))
            if not state:
                continue
            if state[0] == "ok":
                _, digest, fid, rid = state
                if compact and not precise:
                    parts.append(f"{image['phase']}:{fid}")
                    continue
                ref = f"{image['phase']}: [{fid}](#{fid.lower()})"
                if precise:
                    anchor = ""
                    if seen_anchors is not None and digest not in seen_anchors:
                        seen_anchors.add(digest)
                        anchor = f'<a id="img-{digest}"></a>'
                    ref += f" @ {image.get('observed_at', 'unknown')} / {rid} / `{digest}`{anchor}"
                parts.append(ref)
            else:
                _, err_id = state
                if compact and not precise:
                    parts.append(f"{image.get('phase', 'image')}:missing({err_id})")
                    continue
                ref = f"{image.get('phase', 'image')}: missing {err_id}"
                if precise:
                    ref += f" @ {image.get('observed_at', 'unknown')}"
                parts.append(ref)
        return ("/".join(parts) if compact and not precise else "; ".join(parts)) or ("-" if compact and not precise else "No image recorded")

    def _render_frame_block(digest):
        from autowork_core.knowledge.case_context import pick_target_arg_key
        from autowork_core.runtime.knowledge_observation import OBSERVED_ACTIONS

        fid = frame_ids[digest]
        usages = frame_usages[digest]
        scenario_index, step_index, action_index, action, image, region_id = usages[0]
        view = _frame_preview(_view_name(action), 22, escape_separators=True)
        phase = _frame_preview(image.get("phase", "unknown"), 8, escape_separators=True)
        operation = _frame_preview(action["operation"], 32, escape_separators=True)
        if action["operation"] in READ_ONLY_OPERATIONS:
            kind_tag = "只读观察-不改变界面选中或预览"
        elif action["operation"] not in OBSERVED_ACTIONS:
            kind_tag = "辅助/外部调用"
        else:
            kind_tag = "交互动作"
        arguments = dict(action.get("arguments") or {})
        target_key = pick_target_arg_key(arguments)
        raw_binding = arguments.pop(target_key, "-") if target_key else "-"
        if isinstance(raw_binding, dict) and set(raw_binding).issubset({"root", "by", "value"}) and "value" in raw_binding:
            raw_binding_str = str(raw_binding["value"])
        else:
            raw_binding_str = _qless(raw_binding, max_len=0)
        common_opts = common.get(action["operation"], {})
        opts = {k: v for k, v in arguments.items() if k not in common_opts and v is not None}
        opt_parts = [
            _qless(v, max_len=24)
            if k in ("data_or_name", "attr_name", "value", "option_or_name", "expected", "value_or_name")
            else f"{k}={_qless(v, max_len=20)}"
            for k, v in opts.items()
        ]
        raw_call_args = raw_binding_str + ((", " + ", ".join(opt_parts)) if opt_parts else "")
        outcome = _frame_preview(action.get("return_value", action.get("exception")), 24, escape_separators=True)
        action_id = f"{step_index}.{action_index}"
        anchor = f"a-{step_index}-{action_index}" if scenario_index == 1 else f"s{scenario_index}-a-{step_index}-{action_index}"
        target_id = action_target_id.get(id(action), "-")
        query_id = action_query_id.get(id(action), "-")
        heading = f'##### {fid} {view} {phase} (Step {step_index} / Action {action_id})<a id="{fid.lower()}"></a>'
        body_prefix = f" Action {action_id} [{kind_tag}]: {operation}("
        obs_time = _frame_preview(_short_time(image.get("observed_at")), 22, escape_separators=True)
        body_suffix = f"), result {outcome}, refs {target_id}/{query_id}/{region_id}, [Details](#{anchor}), observed at {obs_time}/{region_id}"
        if len(usages) > 1:
            peer_items = []
            for u_s, u_st, u_a, _u_act, u_img, _u_rid in usages[1:]:
                u_aid = f"{u_st}.{u_a}"
                u_anc = f"a-{u_st}-{u_a}" if u_s == 1 else f"s{u_s}-a-{u_st}-{u_a}"
                u_ph = _frame_preview(u_img.get("phase", "image"), 6, escape_separators=True)
                peer_items.append(f"[{u_aid} {u_ph}](#{u_anc})")
            shown_peers = ", ".join(peer_items[:2])
            if len(peer_items) > 2:
                shown_peers += f" +{len(peer_items) - 2} more"
            body_suffix += f", shared ({len(usages)} slots: {shown_peers})"
        else:
            action_ok_fids = [
                slot_status[id(img)][2]
                for img in (action.get("images") or ())
                if slot_status.get(id(img), ("",))[0] == "ok"
            ]
            if len(action_ok_fids) == 2 and action_ok_fids[0] != action_ok_fids[1]:
                body_suffix += f", pair {action_ok_fids[0]}->{action_ok_fids[1]} ({phase} state)"
            else:
                body_suffix += ", single observation in window"
        placeholder_img_len = len(f"![{fid}](resource://OBec3Ido0Vdba121TswHBA)")
        fixed_len = len(heading) + 1 + placeholder_img_len + len(body_prefix) + len(body_suffix)
        arg_budget = max(18, min(86, 374 - fixed_len))
        binding = _frame_preview(raw_call_args, arg_budget, escape_separators=True)
        body_tail = f"{body_prefix}{binding}{body_suffix}"
        if len(body_tail) < 168:
            pad = " (full runtime parameters and window geometry recorded in Chapter 5 Trace Index)"
            needed = max(0, 168 - len(body_tail) - 1)
            max_pad = max(0, 374 - (len(heading) + 1 + placeholder_img_len + len(body_tail) + 1))
            body_tail = body_tail + pad[: max(needed, min(len(pad), max_pad))] + "."
        return f"{heading} ![{fid}](data:image/png;base64,{verified_payloads[digest]}){body_tail}\n\n"

    from autowork_core.runtime.knowledge_observation import VARIABLE_ACTIONS

    output = []
    step_overview_rows = []
    step_full_chains = []
    embedded = set()
    for s_idx, scenario in enumerate(feature["scenarios"], 1):
        output.append(f"\n### Scenario: {scenario['name']}\n\n")
        scope_files = ", ".join((scenario.get("scope") or {}).get("files", ())) or "none"
        output.append(
            f"- 场景身份：`{_qless(scenario.get('id', scenario['name']), max_len=64)}`；"
            f"测试状态：`{_qless(scenario.get('test_status', 'unknown'))}`；"
            f"步骤文件作用域：`{_qless(scope_files, max_len=80)}`。\n"
        )
        if scenario.get("example"):
            example = scenario["example"]
            output.append(
                f"- 场景大纲样本：行 `{_qless(example.get('id'))}`，样本值 `{_qless(example.get('cells', []), max_len=80)}`。\n"
            )
        output.append(
            "- 图块标注规则：每个截图块均标注触发动作属于 `[交互动作]` 或 `[只读观察-不改变界面选中或预览]`；完整源码快照见第 6 章。\n\n"
        )

        for st_idx, step in enumerate(scenario["steps"], 1):
            st_actions = step["actions"]
            if st_actions:
                views = list(dict.fromkeys(_vlabel(_view_name(a)) for a in st_actions))
                roots = list(dict.fromkeys(_qless((a.get("owner") or {}).get("root_locator") or "window") for a in st_actions))
                st_fids = list(dict.fromkeys(
                    slot_status[id(img)][2]
                    for a in st_actions
                    for img in a.get("images", ())
                    if slot_status.get(id(img), ("",))[0] == "ok"
                ))
                all_var = all(a["operation"] in VARIABLE_ACTIONS for a in st_actions)
                no_img_label = "无截图（纯变量操作）" if all_var else "无截图（变量/辅助调用）"
                fid_range = (
                    f"`{st_fids[0]}`–`{st_fids[-1]}`（共 {len(st_fids)} 张唯一 PNG）"
                    if len(st_fids) > 1
                    else (f"`{st_fids[0]}`" if st_fids else no_img_label)
                )
                owner_col = f"`{', '.join(views)}` / `{', '.join(roots)}`"
                ops_summary_col = _summarize_step_operations(st_actions, common)
                full_calls = " → ".join(
                    f"`{st_idx}.{a_idx}` {_natural_call(a, common[a['operation']], concise=True)}（{_qless(a.get('outcome'))}，{_action_image_summary(a)}）"
                    for a_idx, a in enumerate(st_actions, 1)
                )
            else:
                views = ["BasePage"]
                fid_range = "无截图（无公开动作记录）"
                owner_col = "-"
                ops_summary_col = "无公开动作记录"
                full_calls = "无公开动作记录"
            step_overview_rows.append([
                f"Step {st_idx}：{_qless(step['keyword'] + ' ' + step['name'], max_len=120)}",
                f"`{_qless(step.get('test_status', 'unknown'))}`",
                owner_col,
                ops_summary_col,
                fid_range,
            ])
            step_full_chains.append(
                f"{st_idx}. **Step {st_idx}（`{_qless(step['keyword'] + ' ' + step['name'], max_len=120)}`，视图 {', '.join(views)}，状态 `{_qless(step.get('test_status', 'unknown'))}`，截图范围 {fid_range}）**：{full_calls}。"
            )

        for st_idx, step in enumerate(scenario["steps"], 1):
            step_views = list(dict.fromkeys(_vlabel(_view_name(a)) for a in step["actions"]))
            step_view_str = " / ".join(step_views) if step_views else "BasePage"
            step_anchor = f"step-{st_idx}" if s_idx == 1 else f"s{s_idx}-step-{st_idx}"
            st_nar = step_narratives.get(str(st_idx)) or {}
            clean_step_title = _qless(
                st_nar.get("title") or f"{_qless(step['keyword'] + ' ' + step['name'], max_len=140)}（{_qless(step_view_str, max_len=48)}）",
                max_len=200,
            )
            output.append(f"\n##### Step {st_idx}: {clean_step_title} <a id=\"{step_anchor}\"></a>\n\n")
            if not step["actions"]:
                output.append("No public-action event was observed; this does not prove that the Step performed no work.\n")
                continue
            if st_nar.get("intro"):
                output.append(f"{_qless(st_nar['intro'], max_len=600)}\n\n")
            else:
                st_fids = list(dict.fromkeys(
                    slot_status[id(img)][2]
                    for a in step["actions"]
                    for img in a.get("images", ())
                    if slot_status.get(id(img), ("",))[0] == "ok"
                ))
                frame_range = f"`{st_fids[0]}`–`{st_fids[-1]}`" if len(st_fids) > 1 else (f"`{st_fids[0]}`" if st_fids else "无截图")
                output.append(
                    f"在 `{step_view_str}` 执行 Step {st_idx}（状态 `{_qless(step.get('test_status', 'unknown'))}`），"
                    f"共 {len(step['actions'])} 个公开动作（`{st_idx}.1`–`{st_idx}.{len(step['actions'])}`），关联截图 {frame_range}。\n\n"
                )
            skip_until_a_idx = 0
            for a_idx, action in enumerate(step["actions"], 1):
                if a_idx <= skip_until_a_idx:
                    continue
                view = _view_name(action)
                vlabel = _vlabel(view)
                ok_slots = [img for img in action.get("images", []) if slot_status.get(id(img), ("",))[0] == "ok"]
                failed_slots = [
                    (img, slot_status[id(img)])
                    for img in action.get("images", [])
                    if slot_status.get(id(img), ("",))[0] in {"missing", "invalid"}
                ]
                if not ok_slots:
                    grp = non_image_groups.get(f"{st_idx}.{a_idx}")
                    if grp and not failed_slots:
                        skip_until_a_idx = grp.get("span_end", a_idx)
                        output.append(
                            f"{_qless(grp['heading'], max_len=120)}\n"
                            f"{_qless(grp['body'], max_len=600)}\n\n"
                        )
                        continue
                    fail_desc = ""
                    if failed_slots:
                        fail_notes = []
                        for img, (kind, err_id) in failed_slots:
                            if kind == "invalid":
                                fail_notes.append(f"Image {img.get('phase', 'image')}: missing or invalid at export ({err_id}); no replacement image was used")
                            else:
                                fail_notes.append(f"Image {img.get('phase', 'image')}: missing ({err_id}); see the capture error below")
                        fail_desc = "；" + "；".join(fail_notes)
                    act_title = _qless(action_titles.get(f"{st_idx}.{a_idx}") or action["operation"], max_len=40)
                    output.append(
                        f"##### A{st_idx}.{a_idx} {vlabel} Step {st_idx}.{a_idx}：{act_title}\n"
                        f"在 `{view}` 执行动作 `{st_idx}.{a_idx}`：{_natural_call(action, common[action['operation']])}"
                        f"（`{_short_time(action.get('started_at'))}`{fail_desc}）。\n\n"
                    )
                    continue

                emitted_new_frame = False
                reused_fids = []
                for img in action.get("images", []):
                    state = slot_status.get(id(img))
                    if not state:
                        continue
                    if state[0] == "invalid":
                        output.append(f"Image {img.get('phase', 'image')}: missing or invalid at export ({state[1]}); no replacement image was used.\n\n")
                    elif state[0] == "missing":
                        output.append(f"Image {img.get('phase', 'image')}: missing ({state[1]}); see the capture error below.\n\n")
                    elif state[0] == "ok":
                        _, digest, fid, rid = state
                        if digest not in embedded:
                            embedded.add(digest)
                            output.append(_render_frame_block(digest))
                            emitted_new_frame = True
                        elif fid not in reused_fids:
                            reused_fids.append(fid)
                if not emitted_new_frame and reused_fids:
                    from autowork_core.runtime.knowledge_observation import OBSERVED_ACTIONS

                    if action["operation"] in READ_ONLY_OPERATIONS:
                        kind_tag = "只读观察-不改变界面选中或预览"
                    elif action["operation"] not in OBSERVED_ACTIONS:
                        kind_tag = "辅助/外部调用"
                    else:
                        kind_tag = "交互动作"
                    fid = reused_fids[0]
                    act_title = _qless(action_titles.get(f"{st_idx}.{a_idx}") or action["operation"], max_len=40)
                    row_anchor = f"a-{st_idx}-{a_idx}" if s_idx == 1 else f"s{s_idx}-a-{st_idx}-{a_idx}"
                    output.append(
                        f"##### A{st_idx}.{a_idx} {vlabel} Step {st_idx}.{a_idx}：{act_title}（复用截图 {fid}）\n"
                        f"在 `{view}` 执行动作 `{st_idx}.{a_idx}` [{kind_tag}]：{_natural_call(action, common[action['operation']])}"
                        f"，复用同屏截图 [{fid}](#{fid.lower()})（[Details](#{row_anchor})）。\n\n"
                    )

    if all_actions:
        from autowork_core.knowledge.case_context import OPERATION_CN, pick_target_arg_key, strip_ref_prefix

        default_action_titles = {}
        for _s_idx, st_idx, a_idx, _scen, _step, action in all_actions:
            args = dict(action.get("arguments") or {})
            target_key = pick_target_arg_key(args)
            binding = args.get(target_key, "")
            binding_display = binding.get("value", str(binding)) if isinstance(binding, dict) else str(binding)
            short_target = strip_ref_prefix(binding_display)
            if "//Text[@name=" in short_target:
                m = re.search(r"@name=['\"]?([^'\"]+)['\"]?", short_target)
                if m:
                    short_target = m.group(1)
            default_action_titles[f"{st_idx}.{a_idx}"] = f"{OPERATION_CN.get(action['operation'], action['operation'])} {short_target}".strip()

        output.append("\n### 8.1 精确追溯索引与未覆盖边界（Trace Index）\n\n")
        output.append("#### 完整动作 ID、参数返回、调用时间与图片关联\n\n")
        seen_digest_anchors = set()
        trace_rows = []
        for s_idx, st_idx, a_idx, scenario, step, action in all_actions:
            view = _view_name(action)
            args = dict(action.get("arguments") or {})
            target_key = pick_target_arg_key(args)
            binding = {target_key: args.pop(target_key)} if target_key else {}
            options = {k: v for k, v in args.items() if k not in common[action["operation"]]}
            actual = action.get("return_value", action.get("exception"))
            tid = action_target_id.get(id(action), "-")
            row_anchor = f"a-{st_idx}-{a_idx}" if s_idx == 1 else f"s{s_idx}-a-{st_idx}-{a_idx}"
            trace_rows.append([
                f'<a id="{row_anchor}"></a>{st_idx}.{a_idx} (Step {st_idx})',
                _code_cell(action["id"]),
                f"`{action['operation']}`",
                _code_cell(binding),
                _code_cell(options),
                f"{_code_cell(actual)} ({action['outcome']})",
                f"{_code_cell(action.get('started_at'))} / {_code_cell(action.get('finished_at'))}",
                _action_image_summary(action, precise=True, seen_anchors=seen_digest_anchors),
                tid,
            ])
        output.append(_table(
            ["动作 (Step)", "完整 action_id", "方法", "原始目标/变量", "本次其余输入/选项", "原返回及调用状态", "开始/结束", "截图阶段、观察时点及区域", "目标索引"],
            trace_rows,
        ))
        shared_opt_rows = [[f"`{op}`", _code_cell(vals)] for op, vals in common.items() if vals]
        if shared_opt_rows:
            output.append("#### 本次调用共同参数\n\n")
            output.append(_table(["方法", "每次调用均相同的记录选项"], shared_opt_rows))
        if target_rows:
            output.append("#### 实际目标索引（含所属 Step）\n\n")
            output.append(_table(
                ["目标索引", "所属 Step", "查询索引", "本次记录的实际目标属性"],
                [
                    [
                        tid,
                        ", ".join(target_scopes.get(tid, ())),
                        query_ids.get(_encoded(pair["query"]), "-") if pair["query"] is not None else "-",
                        _code_cell(pair["target"]),
                    ]
                    for tid, pair in target_rows.items()
                ],
            ))
        if query_rows:
            output.append("#### 解析查询索引（含所属 Step）\n\n")
            output.append(_table(
                ["查询索引", "所属 Step", "原记录中的解析查询"],
                [[qid, ", ".join(query_scopes.get(qid, ())), _code_cell(qval)] for qid, qval in query_rows.items()],
            ))
        if regions:
            output.append("#### 实际截图区域\n\n")
            output.append(_table(
                ["区域索引", "实际区域及原记录中的窗口信息"],
                [[rid, _code_cell(rval)] for rid, rval in regions.items()],
            ))
        if missing_entries:
            output.append("#### 原始截图缺失或校验失败\n\n")
            output.append(_table(
                ["缺失项", "完整动作 ID", "阶段/观察时点", "原始错误"],
                [[err_id, _code_cell(act_id), f"{phase} / {obs_at}", _code_cell(err_obj)] for err_id, act_id, phase, obs_at, err_obj in missing_entries],
            ))
        has_custom_explanations = bool(
            (spec.get("view_labels") or {})
            or frame_notes
            or any(v != default_action_titles.get(k) for k, v in action_titles.items())
            or spec.get("polished_by") not in {None, "", "auto_extracted_default"}
        )
        explanation_rows = [
            [kind, _code_cell(reference), _code_cell(value)]
            for kind, values in (
                ("view_labels", spec.get("view_labels") or {}),
                ("action_titles", action_titles if has_custom_explanations else {}),
                ("frame_notes", frame_notes),
            )
            for reference, value in values.items()
        ]
        if explanation_rows:
            output.append("#### 完整页面、动作与图注说明\n\n")
            output.append(_table(["说明类型", "页面或动作/图片引用", "完整说明"], explanation_rows))
    return "".join(output), step_overview_rows, step_full_chains


def _verified_image(directory, image):
    path = (directory / image["path"]).resolve()
    path.relative_to(directory.resolve())
    payload = path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != image["sha256"]:
        raise ValueError("PNG bytes no longer match their captured fingerprint")
    with Image.open(BytesIO(payload)) as decoded:
        if decoded.format != "PNG" or decoded.size != (image["region"]["width"], image["region"]["height"]):
            raise ValueError("PNG format or actual region dimensions do not match")
        decoded.verify()
    return payload, digest
     