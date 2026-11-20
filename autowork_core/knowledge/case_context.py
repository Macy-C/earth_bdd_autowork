from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path, PurePosixPath


OPERATION_CN = {
    "click": "点击",
    "double_click": "双击",
    "right_click": "右击",
    "focus": "聚焦控件",
    "bring_to_front": "置前窗口",
    "minimize_window": "最小化窗口",
    "set_window_topmost": "置顶窗口",
    "unset_window_topmost": "取消窗口置顶",
    "send_to_back": "置后窗口",
    "input_text": "输入文本",
    "send_text_keys": "发送按键",
    "clear_text": "清空文本",
    "remove_text": "删除文本",
    "expand_dropdown": "展开下拉框",
    "select": "选择项",
    "select_dropdown_option": "选择下拉项",
    "set_checked": "设置勾选状态",
    "select_radio": "选择单选项",
    "select_tab": "选择标签页",
    "select_list_item": "选择列表项",
    "select_tree_item": "选择树节点",
    "set_tree_expanded": "展开或收起树节点",
    "set_slider_value": "设置滑块值",
    "click_ocr_relative": "点击OCR相对位置",
    "click_pic_relative": "点击图片相对位置",
    "set_root": "切换根定位器",
    "scroll_to": "滚动到目标",
    "drag_by_offset": "按偏移拖拽",
    "get_attr": "读取属性",
    "get_text": "读取文本",
    "get_collection_items": "读取集合项",
    "save_text": "读取并保存文本变量",
    "save_attr": "读取并保存属性变量",
    "get_ocr_text": "读取OCR文本",
    "extract_ocr_regex": "正则提取OCR文本",
    "get_ocr_relative_position": "获取OCR相对坐标",
    "get_pic_region": "获取图片匹配区域",
    "get_pic_relative_position": "获取图片相对坐标",
    "save_ocr_debug_image": "保存OCR调试图",
    "save_pic_debug_image": "保存图片匹配调试图",
    "set_variable": "保存变量",
    "get_variable": "读取变量",
    "wait_enabled": "等待控件可用",
    "wait_visible": "等待控件可见",
    "wait_exists": "等待元素存在",
    "wait_not_exists": "等待元素消失",
    "wait_ready": "等待控件就绪",
    "wait_exposed": "等待窗口暴露",
    "wait_ocr_text_present": "等待OCR文本出现",
    "wait_ocr_text_absent": "等待OCR文本消失",
    "wait_pic_present": "等待目标图片出现",
    "wait_pic_absent": "等待目标图片消失",
    "assert_exists": "检查元素存在",
    "assert_not_exists": "检查元素不存在",
    "assert_visible": "检查元素可见",
    "assert_not_visible": "检查元素不可见",
    "assert_enabled": "检查控件可用",
    "assert_disabled": "检查控件禁用",
    "assert_text_equal": "检查文本相等",
    "assert_text_contains": "检查文本包含",
    "assert_text_not_contains": "检查文本不包含",
    "assert_text_empty": "检查文本为空",
    "assert_attr_equal": "检查属性相等",
    "assert_attr_contains": "检查属性包含",
    "assert_collection_equal": "检查集合相等",
    "assert_ocr_contains": "检查OCR包含文本",
    "assert_ocr_not_contains": "检查OCR不包含文本",
    "assert_pic_exists": "检查图片存在",
    "assert_pic_not_exists": "检查图片不存在",
}
TARGET_ARG_KEYS = (
    "locator_or_name",
    "pic_or_name",
    "variable_name",
    "target",
    "root",
    "text",
    "pattern",
    "region",
)


def pick_target_arg_key(args):
    if not args:
        return None
    for key in TARGET_ARG_KEYS:
        if key in args and args[key] is not None:
            return key
    return next((key for key in TARGET_ARG_KEYS if key in args), None)


def strip_ref_prefix(text):
    return re.sub(r"^\$(?!\$)(?:(?:loc(?:ator)?|data):)?", "", str(text or ""))


def slugify_token(value, *, max_len=48):
    text = str(value or "").strip()
    text = re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-._")
    if not text:
        text = "case"
    return text[:max_len].rstrip("-._") or "case"


def infer_product_name(feature_source):
    norm = PurePosixPath(str(feature_source or "").replace("\\", "/"))
    parts = list(norm.parts)
    for root_marker in ("test_features", "features"):
        if root_marker in parts:
            idx = parts.index(root_marker)
            if idx + 2 < len(parts):
                raw_prod = parts[idx + 1]
                return raw_prod.capitalize() if raw_prod.isascii() and raw_prod.islower() else raw_prod
    return "Default"


def normalize_case_tags(feature_code, tags):
    if isinstance(tags, (str, bytes)):
        raise ValueError("Case tags must be a sequence of tag names")
    required = f"feature:{feature_code}"
    result = []
    for name in tags or ():
        if not isinstance(name, str) or not name.strip() or any(character in name for character in "\r\n\x00"):
            raise ValueError("Case tag names must be nonempty single-line text")
        classification = re.fullmatch(r"(?:feature|需求)[:：](.*)", name, flags=re.I)
        if classification:
            if classification.group(1) != feature_code:
                raise ValueError(f"Case tag {name!r} conflicts with Feature filename {feature_code!r}")
            name = required
        if name not in result:
            result.append(name)
    if required not in result:
        result.append(required)
    return result


def derive_case_identity(feature_source, feature_name, scenario_name, views=()):
    source = PurePosixPath(str(feature_source or "").replace("\\", "/"))
    if source.is_absolute() or ".." in source.parts or source.suffix != ".feature" or ":" in str(source) or not str(source).startswith(("Bdd/test_features/", "Bdd/features/")):
        raise ValueError("Case Feature must be a repository-relative Bdd Feature path")
    norm_source = source.as_posix()
    product = infer_product_name(norm_source)
    feature_code = PurePosixPath(norm_source).stem
    clean_feature_name = re.sub(r"^Rerun\s*-\s*", "", str(feature_name or feature_code).strip(), flags=re.I).strip() or feature_code
    clean_scenario_name = str(scenario_name or "").strip()
    if not clean_scenario_name:
        raise ValueError("Case Scenario name is required")
    feature_slug = slugify_token(clean_feature_name, max_len=40)
    scenario_slug = slugify_token(clean_scenario_name, max_len=48)
    case_key = f"{norm_source}::{clean_scenario_name}"
    case_suffix = hashlib.sha256(case_key.encode("utf-8")).hexdigest()[:12]
    local_file_name = f"{slugify_token(product, max_len=24)}__{slugify_token(feature_code, max_len=40)}__{scenario_slug}__{case_suffix}.md"
    spec_file_name = local_file_name.removesuffix(".md") + ".polish-spec.json"
    remote_file_name = (
        f"{feature_code}_{feature_slug}__{scenario_slug}__{case_suffix}.md"
    )
    tags = [f"feature:{feature_code}"]
    return {
        "case_key": case_key,
        "product": product,
        "feature_source": norm_source,
        "feature_code": feature_code,
        "feature_id": hashlib.sha256(norm_source.encode("utf-8")).hexdigest()[:16],
        "feature_name": clean_feature_name,
        "scenario_name": clean_scenario_name,
        "local_file_name": local_file_name,
        "spec_file_name": spec_file_name,
        "remote_file_name": remote_file_name,
        "tags": tags,
    }


def case_source_paths(feature, scenario):
    paths = {feature["source"]}
    paths.update("Bdd/steps/" + str(name).replace("\\", "/") for name in (scenario.get("scope") or {}).get("files", ()))
    paths.update(path for step in scenario.get("steps", ()) for action in step.get("actions", ()) for path in (action.get("owner") or {}).get("sources", ()))
    return sorted(paths)


def case_fingerprint(document, feature, scenario):
    payload = {
        "format": "standalone-case-v2", "run_id": document.get("run_id"),
        "feature_source": feature["source"], "scenario": scenario,
        "sources": {path: document.get("sources", {}).get(path) for path in case_source_paths(feature, scenario)},
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def scan_captures(
    captures_root=None,
    *,
    query=None,
    feature_source=None,
    scenario_name=None,
):
    """Scan knowledge-captures directories, filter out failed runs, and deduplicate by case_key."""
    from autowork_core.knowledge.case_export import _view_name
    from config.paths import Paths

    if captures_root is None or str(captures_root).strip().lower() in {"", "auto", "latest"}:
        root_path = Paths.ARTIFACTS_DIR / "knowledge-captures"
    else:
        root_path = Path(captures_root)
        if not root_path.is_absolute():
            root_path = Paths.BASE_DIR / root_path
    root_path = root_path.resolve()

    if not root_path.exists():
        return {
            "status": "empty",
            "captures_root": str(root_path),
            "selected_count": 0,
            "selected_cases": [],
            "skipped_failed_runs": [],
            "skipped_incomplete_runs": [],
            "superseded_runs": [],
        }

    run_dirs = [root_path] if (root_path / "observations.json").is_file() else sorted(
        p for p in root_path.iterdir() if p.is_dir() and (p / "observations.json").is_file()
    )

    skipped_failed = []
    skipped_incomplete = []
    candidates_by_key = {}
    q_norm = str(query).strip().lower() if query else None

    for run_dir in run_dirs:
        try:
            doc = json.loads((run_dir / "observations.json").read_text(encoding="utf-8-sig"))
        except Exception as err:
            skipped_failed.append({
                "run_id": run_dir.name,
                "capture_dir": str(run_dir),
                "test_status": "corrupt",
                "reason": f"Failed to read observations.json: {err}",
            })
            continue

        run_id = doc.get("run_id") or run_dir.name
        run_status = doc.get("test_status", "unknown")
        sort_ts = str(doc.get("finished_at") or doc.get("started_at") or run_id)

        if run_status != "passed":
            skipped_failed.append({
                "run_id": run_id,
                "capture_dir": str(run_dir),
                "test_status": run_status,
                "reason": "Run test_status is not passed",
            })
            continue
        if doc.get("capture_status") != "complete":
            skipped_incomplete.append({
                "run_id": run_id,
                "capture_dir": str(run_dir),
                "capture_status": doc.get("capture_status", "unknown"),
                "reason": "Run capture_status is not complete",
            })
            continue
        if not doc.get("finished_at"):
            skipped_incomplete.append({
                "run_id": run_id,
                "capture_dir": str(run_dir),
                "capture_status": doc.get("capture_status", "complete"),
                "reason": "Run finished_at is missing",
            })
            continue

        for feat in doc.get("features") or []:
            f_src = str(feat.get("source") or "").replace("\\", "/")
            if feature_source and f_src != str(feature_source).replace("\\", "/"):
                continue
            f_name = feat.get("name") or PurePosixPath(f_src).stem
            scenarios = feat.get("scenarios") or []
            if not scenarios:
                continue
            names = [scenario.get("name") for scenario in scenarios]
            if len(names) != len(set(names)):
                raise ValueError(f"Duplicate Scenario names are ambiguous in {f_src}")
            for scen in scenarios:
                s_name = scen.get("name")
                if scenario_name and scenario_name != s_name:
                    continue
                s_status = scen.get("test_status") or feat.get("test_status") or run_status
                if s_status != "passed":
                    skipped_failed.append({
                        "run_id": run_id,
                        "capture_dir": str(run_dir),
                        "feature_source": f_src,
                        "scenario_name": s_name,
                        "test_status": s_status,
                        "reason": "Scenario test_status is not passed",
                    })
                    continue

                views = list(dict.fromkeys(
                    _view_name(act)
                    for st in scen.get("steps", ())
                    for act in st.get("actions", ())
                ))
                identity = derive_case_identity(f_src, f_name, s_name, views=views)
                if q_norm:
                    searchable = " ".join([
                        run_id,
                        identity["case_key"],
                        identity["product"],
                        identity["feature_code"],
                        identity["feature_name"],
                        identity["scenario_name"],
                    ]).lower()
                    if q_norm not in searchable:
                        continue

                action_count = sum(len(st.get("actions", ())) for st in scen.get("steps", ()))
                png_count = len({
                    img["sha256"]
                    for st in scen.get("steps", ())
                    for act in st.get("actions", ())
                    for img in act.get("images", ())
                    if img.get("status") == "captured" and img.get("sha256")
                })
                record = {
                    **identity,
                    "run_id": run_id,
                    "capture_dir": str(run_dir),
                    "started_at": doc.get("started_at"),
                    "finished_at": doc.get("finished_at"),
                    "sort_timestamp": sort_ts,
                    "test_status": s_status,
                    "capture_status": doc.get("capture_status", "unknown"),
                    "action_count": action_count,
                    "unique_png_count": png_count,
                    "evidence_sha256": case_fingerprint(doc, feat, scen),
                }
                candidates_by_key.setdefault(identity["case_key"], []).append(record)

    selected_cases = []
    superseded_runs = []
    for case_key, items in candidates_by_key.items():
        items_sorted = sorted(items, key=lambda x: (x["sort_timestamp"], x["run_id"]))
        selected_cases.append(items_sorted[-1])
        for old_item in items_sorted[:-1]:
            superseded_runs.append({
                "case_key": case_key,
                "superseded_run_id": old_item["run_id"],
                "capture_dir": old_item["capture_dir"],
                "kept_run_id": items_sorted[-1]["run_id"],
            })

    selected_cases.sort(key=lambda x: (x["sort_timestamp"], x["case_key"]))
    return {
        "status": "ok" if selected_cases else "no_matching_passed_cases",
        "captures_root": str(root_path),
        "selected_count": len(selected_cases),
        "selected_cases": selected_cases,
        "skipped_failed_runs": skipped_failed,
        "skipped_incomplete_runs": skipped_incomplete,
        "superseded_runs": superseded_runs,
    }


def inspect_capture_for_polish(
    capture_dir=None,
    *,
    query=None,
    feature_source=None,
    scenario_name=None,
    output_spec_path=None,
    persist_spec=True,
):
    """Extract compact structured evidence and default polish spec for AI or deterministic build."""
    selection = scan_captures(
        capture_dir, query=query,
        feature_source=feature_source, scenario_name=scenario_name,
    )
    cases = selection["selected_cases"]
    if not cases:
        raise ValueError("No matching passed capture cases")
    if len(cases) != 1 and output_spec_path:
        raise ValueError("output_spec_path requires a single selected case")
    results = [
        _inspect_single_case(
            item["capture_dir"], feature_source=item["feature_source"],
            scenario_name=item["scenario_name"], output_spec_path=output_spec_path,
            persist_spec=persist_spec,
        )
        for item in cases
    ]
    return {**results[0], "cases": results, "scan_summary": selection}


def _matches_step_pattern(pat, s_name):
    pat_str = str(pat or "").strip()
    s_str = str(s_name or "").strip()
    if not pat_str or not s_str:
        return False
    if pat_str.lower() == s_str.lower():
        return True
    import parse

    try:
        if parse.parse(pat_str, s_str, case_sensitive=False) is not None:
            return True
    except Exception:
        pass
    try:
        wildcard_re = re.sub(r"\\\{[^{}]*\\\}", ".+", re.escape(pat_str))
        if re.fullmatch(wildcard_re, s_str, flags=re.I):
            return True
    except re.error:
        pass
    try:
        if re.fullmatch(pat_str, s_str, flags=re.I):
            return True
    except re.error:
        pass
    return False


def _is_unmatched_step_def(node, scenario_step_names):
    if not isinstance(node, ast.FunctionDef) or not scenario_step_names:
        return False
    step_patterns = []
    for dec in node.decorator_list:
        if not isinstance(dec, ast.Call):
            continue
        func = dec.func
        dec_name = func.id if isinstance(func, ast.Name) else (func.attr if isinstance(func, ast.Attribute) else "")
        if dec_name.lower() in {"given", "when", "then", "step"} and dec.args:
            first_arg = dec.args[0]
            if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
                step_patterns.append(first_arg.value)
    if not step_patterns:
        return False
    for pat in step_patterns:
        for s_name in scenario_step_names:
            if _matches_step_pattern(pat, s_name):
                return False
    return True


def _select_active_step_nodes(tree, text, scenario_step_names):
    if "execute_steps" in text:
        return list(tree.body)
    initial_kept = [
        node for node in tree.body
        if not _is_unmatched_step_def(node, scenario_step_names)
    ]
    called_names = {
        sub.id
        for node in initial_kept
        if not isinstance(node, (ast.Import, ast.ImportFrom))
        for sub in ast.walk(node)
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load)
    }
    return [
        node for node in tree.body
        if node in initial_kept or (isinstance(node, ast.FunctionDef) and node.name in called_names)
    ]


def _inspect_single_case(
    capture_dir,
    *,
    feature_source,
    scenario_name,
    output_spec_path=None,
    persist_spec=True,
):
    from autowork_core.knowledge.case_export import VIEW_LABELS, _qless, _view_name
    from autowork_core.runtime.knowledge_observation import OBSERVED_ACTIONS

    capture_dir = Path(capture_dir).resolve()
    doc = json.loads((capture_dir / "observations.json").read_text(encoding="utf-8-sig"))
    feature = next(
        f for f in (doc.get("features") or [])
        if f.get("source") == feature_source or str(f.get("source", "")).endswith(str(feature_source))
    )
    scenarios = [s for s in (feature.get("scenarios") or []) if not scenario_name or s.get("name") == scenario_name]
    if len(scenarios) != 1:
        raise ValueError("Inspection requires exactly one matching Scenario")
    target_scenario = scenarios[0]

    sources_dict = doc.get("sources") or {}
    f_src = feature["source"]
    missing_sources = [path for path in case_source_paths(feature, target_scenario) if path not in sources_dict]
    if missing_sources:
        raise ValueError(f"Capture is missing frozen source snapshots: {', '.join(missing_sources)}")

    scope_files = sorted({
        f"Bdd/steps/{str(name).replace(chr(92), '/')}"
        for name in (target_scenario.get("scope") or {}).get("files", ())
    })
    scenario_step_names = list(dict.fromkeys(
        st.get("name", "")
        for st in [*(target_scenario.get("steps") or ()), *(target_scenario.get("declared_steps") or ())]
        if st.get("name")
    ))
    observed_runtime_ops = OBSERVED_ACTIONS | {
        act.get("operation")
        for st in target_scenario.get("steps", ())
        for act in st.get("actions", ())
        if act.get("operation")
    }
    step_functions = []
    static_gaps = []
    uses_random_helper = False
    for path in scope_files:
        src_entry = sources_dict.get(path) or {}
        text = src_entry.get("text") or src_entry.get("content") or ""
        if not text:
            continue
        try:
            tree = ast.parse(text)
            active_body = _select_active_step_nodes(tree, text, scenario_step_names)
            active_mod = ast.Module(body=active_body, type_ignores=[])
            non_import_segments = "\n".join(
                ast.get_source_segment(text, n) or ""
                for n in active_body
                if not isinstance(n, (ast.Import, ast.ImportFrom))
            )
            if "random_" in non_import_segments or "random." in non_import_segments or "randint" in non_import_segments:
                uses_random_helper = True
            for node in active_body:
                if isinstance(node, ast.FunctionDef):
                    decos = [ast.get_source_segment(text, d) for d in node.decorator_list]
                    step_functions.append({
                        "path": path,
                        "name": node.name,
                        "decorators": decos,
                        "docstring": ast.get_docstring(node),
                        "source": ast.get_source_segment(text, node),
                    })
            local_var_map = {}
            for node in ast.walk(active_mod):
                if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                    val_seg = ast.get_source_segment(text, node.value) or ""
                    m_var = re.search(r"\b(?:get_variable|save_text|save_attr)\(\s*(?:[^,]+,\s*)*['\"]([^'\"]+)['\"]\s*\)", val_seg)
                    if m_var:
                        local_var_map[node.targets[0].id] = m_var.group(1)
            assert_compare_ids = {
                id(sub)
                for n in ast.walk(active_mod)
                if isinstance(n, ast.Assert)
                for sub in ast.walk(n.test)
                if isinstance(sub, ast.Compare)
            }
            for node in ast.walk(active_mod):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr not in observed_runtime_ops:
                    static_gaps.append({
                        "path": path,
                        "line": node.lineno,
                        "call": ast.get_source_segment(text, node),
                        "status": "static_call_site_not_a_runtime_event",
                    })
                elif isinstance(node, (ast.Compare, ast.Assert)) and id(node) not in assert_compare_ids:
                    seg = ast.get_source_segment(text, node) or ""
                    referenced_vars = list(dict.fromkeys(
                        re.findall(r"\bget_variable\(\s*['\"]([^'\"]+)['\"]\s*\)", seg)
                        + [
                            local_var_map[sub.id]
                            for sub in ast.walk(node)
                            if isinstance(sub, ast.Name) and sub.id in local_var_map
                        ]
                    ))
                    static_gaps.append({
                        "path": path,
                        "line": node.lineno,
                        "kind": type(node).__name__,
                        "segment": seg,
                        "variables": referenced_vars,
                        "status": "Python_check_Step_status_only",
                    })
        except SyntaxError:
            if "random_" in text or "random." in text or "randint" in text:
                uses_random_helper = True

    frame_ids = {}
    compact_steps = []
    all_actions_flat = []
    observed_views = []
    for st_idx, step in enumerate(target_scenario.get("steps", ()), 1):
        st_actions = []
        for a_idx, action in enumerate(step.get("actions", ()), 1):
            code_num = f"{st_idx}.{a_idx}"
            view = _view_name(action)
            if view not in observed_views:
                observed_views.append(view)
            args = dict(action.get("arguments") or {})
            target_key = pick_target_arg_key(args)
            binding = args.get(target_key, "")
            if isinstance(binding, dict):
                binding_display = binding.get("value", str(binding))
            else:
                binding_display = str(binding)
            short_target = strip_ref_prefix(binding_display)
            if "//Text[@name=" in short_target:
                m = re.search(r"@name=['\"]?([^'\"]+)['\"]?", short_target)
                if m:
                    short_target = m.group(1)
            op_cn = OPERATION_CN.get(action["operation"], action["operation"])
            semantic_title = f"{op_cn} {short_target}".strip()

            img_refs = []
            for img in action.get("images", ()):
                if img.get("status") == "captured" and img.get("sha256"):
                    fid = frame_ids.setdefault(img["sha256"], f"F{len(frame_ids) + 1:02}")
                    img_refs.append({"phase": img["phase"], "frame_id": fid, "sha256": img["sha256"]})
                else:
                    img_refs.append({"phase": img.get("phase", "image"), "frame_id": "missing"})

            item = {
                "code": code_num,
                "step_index": st_idx,
                "action_index": a_idx,
                "view": view,
                "operation": action["operation"],
                "target": binding_display,
                "short_target": short_target,
                "arguments": args,
                "return_value": action.get("return_value", action.get("exception")),
                "outcome": action.get("outcome"),
                "images": img_refs,
                "default_title": semantic_title,
            }
            st_actions.append(item)
            all_actions_flat.append(item)
        compact_steps.append({
            "step_index": st_idx,
            "keyword": step.get("keyword", ""),
            "name": step.get("name", ""),
            "test_status": step.get("test_status", "unknown"),
            "views": list(dict.fromkeys(a["view"] for a in st_actions)),
            "actions": st_actions,
        })

    identity = derive_case_identity(
        f_src,
        feature.get("name") or PurePosixPath(f_src).stem,
        target_scenario.get("name") or "Scenario",
        views=observed_views,
    )
    parameter_baselines = _detect_parameter_baselines(all_actions_flat, static_gaps)
    frame_notes = _detect_sequence_frame_notes(all_actions_flat, static_gaps)

    step_narratives = {}
    for st in compact_steps:
        st_idx = str(st["step_index"])
        views_cn = " / ".join(VIEW_LABELS.get(v, v) for v in st["views"]) or "BasePage"
        step_narratives[st_idx] = {
            "title": _qless(f"{_qless(st['keyword'] + ' ' + st['name'], max_len=140)}（{_qless(views_cn, max_len=48)}）", max_len=200),
            "intro": _default_step_intro(st, VIEW_LABELS),
        }

    non_image_groups = _detect_non_image_groups(compact_steps, static_gaps, VIEW_LABELS)
    sequence_stages = _detect_sequence_stages(compact_steps, static_gaps, VIEW_LABELS)
    inference_boundaries = _default_inference_boundaries(all_actions_flat, uses_random_helper)

    polish_spec = {
        "schema_version": "2.0",
        **identity,
        "run_id": doc["run_id"],
        "capture_case_id": target_scenario.get("capture_case_id", ""),
        "evidence_sha256": case_fingerprint(doc, feature, target_scenario),
        "polished_by": "auto_extracted_default",
        "title": (
            f"{identity['feature_code']}（{identity['feature_name']}）/ "
            f"{_qless(identity['scenario_name'], max_len=60)} 工程案例记录"
        ),
        "view_labels": dict(VIEW_LABELS),
        "data_compliance_note": (
            "测试输入中的随机后缀来自测试辅助函数，仅用于测试数据唯一性，不是自动数据脱敏；图片和原始输入均未自动脱敏，发布前须确认数据合规性。"
            if uses_random_helper
            else "图片和原始输入未自动脱敏，发布前须确认使用的是获准非临床测试数据。"
        ),
        "method_summary": _default_method_summary(compact_steps, parameter_baselines, VIEW_LABELS, identity=identity),
        "sequence_stages": sequence_stages,
        "inference_boundaries": inference_boundaries,
        "parameter_baselines": parameter_baselines,
        "step_narratives": step_narratives,
        "non_image_groups": non_image_groups,
        "action_titles": {a["code"]: a["default_title"] for a in all_actions_flat},
        "frame_notes": frame_notes,
    }

    if output_spec_path is None:
        output_spec_path = capture_dir / identity["spec_file_name"]
    else:
        output_spec_path = Path(output_spec_path)
    if persist_spec:
        output_spec_path.parent.mkdir(parents=True, exist_ok=True)
    if output_spec_path.is_file():
        existing = json.loads(output_spec_path.read_text(encoding="utf-8-sig"))
        if any(existing.get(key) != polish_spec.get(key) for key in ("schema_version", "case_key", "run_id", "evidence_sha256")):
            raise ValueError("Existing polish specification belongs to different evidence; it was not overwritten")
        polish_spec = existing
    elif persist_spec:
        output_spec_path.write_text(json.dumps(polish_spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return {
        "status": "inspected",
        "capture_dir": str(capture_dir),
        "polish_spec_path": str(output_spec_path) if persist_spec or output_spec_path.is_file() else None,
        **identity,
        "run_id": doc["run_id"],
        "action_count": len(all_actions_flat),
        "unique_png_count": len(frame_ids),
        "observed_views": observed_views,
        "compact_steps": compact_steps,
        "step_functions": step_functions,
        "static_gaps": static_gaps,
        "polish_spec": polish_spec,
    }


def _detect_parameter_baselines(all_actions, static_gaps=()):
    from autowork_core.knowledge.case_export import _qless
    from autowork_core.runtime.knowledge_observation import READ_ACTIONS

    ast_var_checks = {}
    for gap in static_gaps or ():
        if gap.get("status") != "Python_check_Step_status_only":
            continue
        seg = gap.get("segment") or gap.get("call") or ""
        path = gap.get("path") or ""
        line = gap.get("line") or ""
        var_names = gap.get("variables") or re.findall(r"get_variable\(\s*['\"]([^'\"]+)['\"]\s*\)", str(seg))
        for vname in var_names:
            entry = f"{path}:{line} {_qless(seg, max_len=72)}"
            if entry not in ast_var_checks.setdefault(vname, []):
                ast_var_checks[vname].append(entry)

    records = []
    for idx, action in enumerate(all_actions):
        op = action["operation"]
        if op not in {"set_variable", "save_text", "save_attr"}:
            continue
        vname = str((action.get("arguments") or {}).get("variable_name") or action.get("short_target") or "")
        val_str = _qless(action.get("return_value"), max_len=40)
        code_id = action["code"]
        if op in {"save_text", "save_attr"}:
            cur_target = _qless(action.get("target") or "", max_len=40)
            fids = [img["frame_id"] for img in action.get("images", ()) if img.get("frame_id") != "missing"]
            f_str = f", [{fids[0]}](#{fids[0].lower()})" if fids else ""
            source_read = f"`{code_id} {op}({cur_target})`{f_str}"
        else:
            source_read = "Step 输入参数 / 字面量"
            for prev_act in all_actions[:idx]:
                if prev_act["step_index"] == action["step_index"] and prev_act["operation"] in READ_ACTIONS:
                    prev_ret = prev_act.get("return_value")
                    prev_match = (
                        _qless(prev_ret, max_len=40) == val_str
                        or (isinstance(prev_ret, list) and len(prev_ret) == 1 and _qless(prev_ret[0], max_len=40) == val_str)
                    )
                    if prev_match:
                        prev_target = _qless(prev_act.get("target") or "", max_len=40)
                        fids = [img["frame_id"] for img in prev_act.get("images", ()) if img.get("frame_id") != "missing"]
                        f_str = f", [{fids[0]}](#{fids[0].lower()})" if fids else ""
                        source_read = f"`{prev_act['code']} {prev_act['operation']}({prev_target})`{f_str}"
        consumers = [
            f"`{c_act['code']} get_variable({vname})`"
            for c_act in all_actions
            if c_act["operation"] == "get_variable"
            and str((c_act.get("arguments") or {}).get("variable_name") or c_act.get("short_target") or "") == vname
        ]
        checks = "；".join(f"`{chk}`" for chk in ast_var_checks.get(vname, ())) or "-"
        records.append({
            "variable": vname,
            "write_action": f"`{code_id} {op}` (`{val_str}`)",
            "source_action": source_read,
            "consume_actions": "、".join(consumers) or "-",
            "code_checks": checks,
        })
    return records


def _detect_sequence_frame_notes(all_actions, static_gaps):
    return {}


def _detect_non_image_groups(compact_steps, static_gaps, view_labels):
    from autowork_core.knowledge.case_export import DEFAULT_CALL_KEYS, _qless
    from autowork_core.runtime.knowledge_observation import VARIABLE_ACTIONS

    groups = {}
    for st in compact_steps:
        st_idx = st["step_index"]
        actions = st["actions"]
        idx = 0
        while idx < len(actions):
            act = actions[idx]
            has_ok_img = any(img["frame_id"] != "missing" for img in act["images"])
            if has_ok_img:
                idx += 1
                continue
            start_idx = idx
            first_view = act["view"]
            first_is_var = act["operation"] in VARIABLE_ACTIONS
            while (
                idx < len(actions)
                and not any(img["frame_id"] != "missing" for img in actions[idx]["images"])
                and actions[idx]["view"] == first_view
                and (actions[idx]["operation"] in VARIABLE_ACTIONS) == first_is_var
            ):
                idx += 1
            run_slice = actions[start_idx:idx]
            first_a, last_a = run_slice[0], run_slice[-1]
            vlabel = view_labels.get(first_a["view"], first_a["view"])
            key = f"{st_idx}.{first_a['action_index']}"
            if len(run_slice) == 1:
                next_act = actions[idx] if idx < len(actions) else None
                missing_note = ""
                if next_act and any(img["frame_id"] == "missing" for img in next_act["images"]):
                    first_ok = next((img["frame_id"] for img in next_act["images"] if img["frame_id"] != "missing"), None)
                    missing_note = (
                        f"；紧接动作 `{next_act['code']} {next_act['operation']}({_qless(next_act['target'])})` "
                        f"的 `before` 截图缺失（其 `after` 截图为 `{first_ok}`）"
                    )
                args_copy = dict(first_a.get("arguments") or {})
                target_k = pick_target_arg_key(args_copy)
                if target_k in args_copy:
                    args_copy.pop(target_k)
                opt_vals = [
                    _qless(v, max_len=24)
                    if k in ("data_or_name", "attr_name", "value", "option_or_name", "expected", "value_or_name")
                    else f"{k}={_qless(v, max_len=20)}"
                    for k, v in args_copy.items()
                    if k not in DEFAULT_CALL_KEYS and v is not None
                ]
                opt_str = ("，参数 " + "、".join(opt_vals)) if opt_vals else ""
                groups[key] = {
                    "heading": f"##### A{key} {vlabel} Step {key}：{first_a['operation']}",
                    "body": (
                        f"在 `{first_a['view']}` 执行动作 `{key}`："
                        f"调用 `{first_a['operation']}({_qless(first_a['target'])})`{opt_str}，返回 `{_qless(first_a['return_value'])}`"
                        f"（{first_a['outcome']}）（无截图）{missing_note}。"
                    ),
                }
            else:
                ops = list(dict.fromkeys(a["operation"] for a in run_slice))
                if first_is_var:
                    items_desc = "、".join(
                        f"`{a['code']} {_qless(a['short_target'], max_len=24)}={_qless(a['return_value'], max_len=24)}`"
                        for a in run_slice
                    )
                else:
                    items_desc = "、".join(
                        f"`{a['code']} {a['operation']}({_qless(a['short_target'], max_len=24)})→{_qless(a['return_value'], max_len=24)}`"
                        for a in run_slice
                    )
                groups[key] = {
                    "span_end": last_a["action_index"],
                    "heading": f"##### A{key}-{last_a['action_index']} {vlabel} Step {key}–{st_idx}.{last_a['action_index']}：批量 {', '.join(ops)}（无截图）",
                    "body": (
                        f"在 `{first_a['view']}` 连续执行 {len(run_slice)} 次 `{', '.join(ops)}`（无截图）：{items_desc}。"
                    ),
                }
    return groups


def _detect_sequence_stages(compact_steps, static_gaps, view_labels):
    from autowork_core.knowledge.case_export import _qless

    stages = []
    for st in compact_steps:
        st_idx = st["step_index"]
        actions = st["actions"]
        if not actions:
            continue
        views = " / ".join(view_labels.get(v, v) for v in st["views"])
        chain = " → ".join(
            f"`{a['code']} {a['operation']}({_qless(a['short_target'], max_len=24)})`"
            + (f"（{'/'.join(img['frame_id'] for img in a['images'] if img['frame_id'] != 'missing')}）" if any(img["frame_id"] != "missing" for img in a["images"]) else "")
            for a in actions
        )
        stages.append({
            "step_index": st_idx,
            "title": f"Step {st_idx}（{views}，共 {len(actions)} 个公开动作）",
            "detail": chain,
        })
    return stages


def _default_step_intro(st, view_labels):
    from autowork_core.knowledge.case_export import _qless

    actions = st["actions"]
    if not actions:
        return "本步无公开 UIA 动作记录，仅执行静态调用或 Python 比较。"
    views = " / ".join(view_labels.get(v, v) for v in st["views"])
    fids = list(dict.fromkeys(
        img["frame_id"]
        for a in actions
        for img in a["images"]
        if img["frame_id"] != "missing"
    ))
    frame_range = f"`{fids[0]}`–`{fids[-1]}`" if len(fids) > 1 else (f"`{fids[0]}`" if fids else "无截图")
    return (
        f"在 `{views}` 执行 Step {st['step_index']}（状态 `{_qless(st.get('test_status', 'unknown'))}`），"
        f"共 {len(actions)} 个公开动作（`{actions[0]['code']}`–`{actions[-1]['code']}`），关联截图 {frame_range}。"
    )


def _default_method_summary(compact_steps, parameter_baselines, view_labels, *, identity=None):
    parts = []
    for st in compact_steps:
        views = "、".join(view_labels.get(v, v) for v in st["views"]) or "BasePage"
        fids = list(dict.fromkeys(
            img["frame_id"]
            for a in st.get("actions", ())
            for img in a.get("images", ())
            if img.get("frame_id") != "missing"
        ))
        fid_range = (
            f"`{fids[0]}`–`{fids[-1]}`（共 {len(fids)} 张唯一 PNG）"
            if len(fids) > 1
            else (f"`{fids[0]}`" if fids else "无截图（纯变量操作）")
        )
        parts.append(f"Step {st['step_index']} 在 {views} 执行 {len(st['actions'])} 个公开动作（关联截图 {fid_range}）")
    prefix = (
        f"本案例（{identity['feature_code']} / {identity['scenario_name']}）按运行采集与源码快照导出："
        if identity and identity.get("feature_code") and identity.get("scenario_name")
        else "本案例按运行采集与源码快照导出："
    )
    return prefix + "；".join(parts) + "。"


def _default_inference_boundaries(all_actions, uses_random_helper):
    return [
        "交互修改动作（`click`、`double_click`、`right_click`、`input_text`、`select`）返回 `null (returned_none)` 仅代表调用无异常返回，不直接证明截图瞬间控件可用或异步物理流程已完成。",
        "只读动作（`get_attr`、`get_text`、`get_variable`、`assert_exists`、`assert_not_exists`、`wait_enabled`、`wait_visible`、`wait_exists`）不会改变界面选中行、高亮状态或右侧预览图像；截图中的既有高亮或预览归属由前序交互动作产生，不得归因为只读断言的结果。",
        "`assert_exists` 仅验证目标文本或元素在界面存在，不单独证明集合数量唯一或结果归属。",
        "单次运行的输入值、返回值与状态受当前测试环境约束；参数适用条件须结合正式资料库原文核对，不得直接升格为通用产品规格。",
    ]
