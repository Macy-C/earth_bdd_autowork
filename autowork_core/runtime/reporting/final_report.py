from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import shutil
import sys
import uuid
from datetime import datetime
from pathlib import Path

from autowork_core.runtime.status import status_category
from config.paths import Paths


FINAL_REPORT_VERSION = "1.0"
FINAL_REPORT_DIR = Path("artifacts/final-reports")
FINAL_REPORT_JSON = FINAL_REPORT_DIR / "autowork-final-report.json"
FINAL_REPORT_HTML = FINAL_REPORT_DIR / "autowork-final-report.html"
FINAL_MERGE_LOG = FINAL_REPORT_DIR / "autowork-final-merge.json"
DEFAULT_SOURCE_REPORT = Path("artifacts/reports/autowork-report.json")
DEFAULT_SOURCE_REPORT_TEXT = DEFAULT_SOURCE_REPORT.as_posix()
DEFAULT_TARGET_REPORT_TEXT = FINAL_REPORT_JSON.as_posix()
DEFAULT_FINAL_REPORT_TEXT = DEFAULT_TARGET_REPORT_TEXT


class FinalReportMergeError(ValueError):
    pass


def merge_report(
        source_report,
        target_report,
        *,
        feature_file=None,
        feature_name=None,
        scenario_name=None,
        example_id=None,
        allow_add=True,
):
    """Return a final report with source results replacing target results.

    The returned report is still plain Spark report data. It deliberately does
    not expose retry/flaky metadata; operation provenance belongs to the
    separate merge log written by the file-level helpers.
    """
    source = _public_copy(source_report)
    target = _public_copy(target_report)
    source_feature = _select_feature(
        source,
        feature_file=feature_file,
        feature_name=feature_name,
    )

    if scenario_name is None and example_id is None:
        _replace_feature(target, source_feature, allow_add=allow_add)
        return _normalize_report(target)

    source_item = _select_scenario(
        source_feature,
        scenario_name=scenario_name,
        example_id=example_id,
    )
    target_feature = _find_feature_like(target, source_feature)
    if target_feature is None:
        if not allow_add:
            raise FinalReportMergeError("目标报告中没有匹配的 Feature")
        target_feature = _feature_shell(source_feature)
        target.setdefault("features", []).append(target_feature)
    _merge_scenario(target_feature, source_item, allow_add=allow_add)
    return _normalize_report(target)


def delete_result(
        target_report,
        *,
        feature_file=None,
        feature_name=None,
        scenario_name=None,
        example_id=None,
):
    """Return a final report after deleting one Feature/Scenario/Example."""
    target = _public_copy(target_report)
    feature = _select_feature(
        target,
        feature_file=feature_file,
        feature_name=feature_name,
    )
    features = target.get("features") or []

    if scenario_name is None and example_id is None:
        target["features"] = [item for item in features if item is not feature]
        return _normalize_report(target)

    _delete_scenario(feature, scenario_name=scenario_name, example_id=example_id)
    if not feature.get("scenarios"):
        target["features"] = [item for item in features if item is not feature]
    return _normalize_report(target)


def create_final_report(
        source_path=None,
        *,
    final_report_json=None,
        target_path=None,
        html_path=None,
        log_path=None,
        project_root=None,
):
    project_root = Path(project_root or Paths.BASE_DIR).resolve()
    source_path = _resolve_path(
        source_path or DEFAULT_SOURCE_REPORT,
        project_root=project_root,
    )
    target_path, html_path, log_path = final_report_paths(
        final_report_json=final_report_json,
        target_path=target_path,
        html_path=html_path,
        log_path=log_path,
        project_root=project_root,
    )
    report = _load_report(source_path)
    report = snapshot_report_assets(
        report,
        source_report_path=source_path,
        final_report_path=html_path,
        project_root=project_root,
    )
    report = _normalize_report(report)
    _write_json_atomic(target_path, report)
    render_final_report(report, html_path=html_path)
    _append_merge_log(
        log_path,
        "create",
        project_root=project_root,
        source_path=source_path,
        target_path=target_path,
        html_path=html_path,
    )
    return target_path, html_path


def merge_report_file(
        source_path=None,
        *,
    final_report_json=None,
        target_path=None,
        html_path=None,
        log_path=None,
        feature_file=None,
        feature_name=None,
        scenario_name=None,
        example_id=None,
        allow_add=True,
        project_root=None,
):
    project_root = Path(project_root or Paths.BASE_DIR).resolve()
    source_path = _resolve_path(
        source_path or DEFAULT_SOURCE_REPORT,
        project_root=project_root,
    )
    target_path, html_path, log_path = final_report_paths(
        final_report_json=final_report_json,
        target_path=target_path,
        html_path=html_path,
        log_path=log_path,
        project_root=project_root,
    )
    source = snapshot_report_assets(
        _load_report(source_path),
        source_report_path=source_path,
        final_report_path=html_path,
        project_root=project_root,
    )
    target = _load_report(target_path)
    report = merge_report(
        source,
        target,
        feature_file=feature_file,
        feature_name=feature_name,
        scenario_name=scenario_name,
        example_id=example_id,
        allow_add=allow_add,
    )
    _write_json_atomic(target_path, report)
    render_final_report(report, html_path=html_path)
    _append_merge_log(
        log_path,
        "merge",
        project_root=project_root,
        source_path=source_path,
        target_path=target_path,
        html_path=html_path,
        scope=_scope_payload(
            feature_file=feature_file,
            feature_name=feature_name,
            scenario_name=scenario_name,
            example_id=example_id,
        ),
        allow_add=allow_add,
    )
    return target_path, html_path


def delete_result_file(
        *,
    final_report_json=None,
        target_path=None,
        html_path=None,
        log_path=None,
        feature_file=None,
        feature_name=None,
        scenario_name=None,
        example_id=None,
        project_root=None,
):
    project_root = Path(project_root or Paths.BASE_DIR).resolve()
    target_path, html_path, log_path = final_report_paths(
        final_report_json=final_report_json,
        target_path=target_path,
        html_path=html_path,
        log_path=log_path,
        project_root=project_root,
    )
    report = delete_result(
        _load_report(target_path),
        feature_file=feature_file,
        feature_name=feature_name,
        scenario_name=scenario_name,
        example_id=example_id,
    )
    _write_json_atomic(target_path, report)
    render_final_report(report, html_path=html_path)
    _append_merge_log(
        log_path,
        "delete",
        project_root=project_root,
        target_path=target_path,
        html_path=html_path,
        scope=_scope_payload(
            feature_file=feature_file,
            feature_name=feature_name,
            scenario_name=scenario_name,
            example_id=example_id,
        ),
    )
    return target_path, html_path


def render_final_report(report_or_path=None, *, final_report_json=None, html_path=None, project_root=None):
    project_root = Path(project_root or Paths.BASE_DIR).resolve()
    target_path, html_path, _log_path = final_report_paths(
        final_report_json=final_report_json,
        target_path=report_or_path if report_or_path is not None and not isinstance(report_or_path, dict) else None,
        html_path=html_path,
        log_path=None,
        project_root=project_root,
    )
    report = report_or_path if isinstance(report_or_path, dict) else _load_report(target_path)
    html = render_report_html(report)
    _write_text_atomic(html_path, html)
    return html_path


def create(
        report_json=DEFAULT_SOURCE_REPORT_TEXT,
        *,
    final_report_json=None,
        target_report=None,
        html_report=None,
        merge_log=None,
        project_root=None,
):
    return create_final_report(
        report_json,
        final_report_json=final_report_json,
        target_path=target_report,
        html_path=html_report,
        log_path=merge_log,
        project_root=project_root,
    )


def merge(
        report_json=DEFAULT_SOURCE_REPORT_TEXT,
        *,
    final_report_json=None,
        feature_file=None,
        feature_name=None,
        scenario_name=None,
        example_id=None,
        allow_add=True,
        target_report=None,
        html_report=None,
        merge_log=None,
        project_root=None,
):
    project_root = Path(project_root or Paths.BASE_DIR).resolve()
    source_path = _resolve_path(report_json or DEFAULT_SOURCE_REPORT, project_root=project_root)
    target_path, html_path, log_path = final_report_paths(
        final_report_json=final_report_json,
        target_path=target_report,
        html_path=html_report,
        log_path=merge_log,
        project_root=project_root,
    )
    source = snapshot_report_assets(
        _load_report(source_path),
        source_report_path=source_path,
        final_report_path=html_path,
        project_root=project_root,
    )
    target = _load_report(target_path)
    scopes = _operation_scopes(
        source,
        feature_file=feature_file,
        feature_name=feature_name,
        scenario_name=scenario_name,
        example_id=example_id,
    )
    for scope in scopes:
        target = merge_report(source, target, allow_add=allow_add, **scope)
    _write_json_atomic(target_path, target)
    render_final_report(target, html_path=html_path)
    _append_merge_log(
        log_path,
        "merge",
        project_root=project_root,
        source_path=source_path,
        target_path=target_path,
        html_path=html_path,
        scopes=scopes,
        allow_add=allow_add,
    )
    return target_path, html_path


def delete(
        *,
        final_report_json=None,
        feature_file=None,
        feature_name=None,
        scenario_name=None,
        example_id=None,
        target_report=None,
        html_report=None,
        merge_log=None,
        project_root=None,
):
    project_root = Path(project_root or Paths.BASE_DIR).resolve()
    target_path, html_path, log_path = final_report_paths(
        final_report_json=final_report_json,
        target_path=target_report,
        html_path=html_report,
        log_path=merge_log,
        project_root=project_root,
    )
    target = _load_report(target_path)
    scope = _scope_payload(
        feature_file=feature_file,
        feature_name=feature_name,
        scenario_name=scenario_name,
        example_id=example_id,
    )
    target = delete_result(target, **scope)
    _write_json_atomic(target_path, target)
    render_final_report(target, html_path=html_path)
    _append_merge_log(
        log_path,
        "delete",
        project_root=project_root,
        target_path=target_path,
        html_path=html_path,
        scope=scope,
    )
    return target_path, html_path


def render(*, final_report_json=None, target_report=None, html_report=None, project_root=None):
    target_path, html_path, _log_path = final_report_paths(
        final_report_json=final_report_json,
        target_path=target_report,
        html_path=html_report,
        project_root=project_root,
    )
    html = render_final_report(
        target_path,
        html_path=html_path,
        project_root=project_root,
    )
    return target_path, html


def run_configured(
        action="create",
        *,
    report_json=DEFAULT_SOURCE_REPORT_TEXT,
    source_report=None,
        feature_file=None,
        feature_name=None,
        scenario_name=None,
        example_id=None,
        allow_add=True,
        target_report=None,
        html_report=None,
        merge_log=None,
        project_root=None,
):
    return run_action(action, {
        "report_json": source_report or report_json,
        "final_report_json": target_report,
        "feature_file": feature_file,
        "feature_name": feature_name,
        "scenario_name": scenario_name,
        "example_id": example_id,
        "allow_add": allow_add,
        "target_report": target_report,
        "html_report": html_report,
        "merge_log": merge_log,
        "project_root": project_root,
    })


def run_action(action, config=None):
    config = dict(config or {})
    action = str(action or "create").strip().lower()
    if action == "create":
        return create(
            config.get("report_json", DEFAULT_SOURCE_REPORT_TEXT),
            final_report_json=config.get("final_report_json"),
            target_report=config.get("target_report"),
            html_report=config.get("html_report"),
            merge_log=config.get("merge_log"),
            project_root=config.get("project_root"),
        )
    if action == "merge":
        return merge(
            config.get("report_json", DEFAULT_SOURCE_REPORT_TEXT),
            final_report_json=config.get("final_report_json"),
            feature_file=config.get("feature_file"),
            feature_name=config.get("feature_name"),
            scenario_name=config.get("scenario_name"),
            example_id=config.get("example_id"),
            allow_add=config.get("allow_add", True),
            target_report=config.get("target_report"),
            html_report=config.get("html_report"),
            merge_log=config.get("merge_log"),
            project_root=config.get("project_root"),
        )
    if action == "delete":
        return delete(
            final_report_json=config.get("final_report_json"),
            feature_file=config.get("feature_file"),
            feature_name=config.get("feature_name"),
            scenario_name=config.get("scenario_name"),
            example_id=config.get("example_id"),
            target_report=config.get("target_report"),
            html_report=config.get("html_report"),
            merge_log=config.get("merge_log"),
            project_root=config.get("project_root"),
        )
    if action == "render":
        return render(
            final_report_json=config.get("final_report_json"),
            target_report=config.get("target_report"),
            html_report=config.get("html_report"),
            project_root=config.get("project_root"),
        )
    raise FinalReportMergeError(f"未知最终报告操作: {action}")


def run_default(
        *,
        action="create",
    report_json=DEFAULT_SOURCE_REPORT_TEXT,
        feature_file=None,
        feature_name=None,
        scenario_name=None,
        example_id=None,
        allow_add=True,
        target_report=None,
        html_report=None,
        merge_log=None,
        project_root=None,
):
    target, html = run_configured(
        action,
        report_json=report_json,
        feature_file=feature_file,
        feature_name=feature_name,
        scenario_name=scenario_name,
        example_id=example_id,
        allow_add=allow_add,
        target_report=target_report,
        html_report=html_report,
        merge_log=merge_log,
        project_root=project_root,
    )
    print(f"Final report JSON: {target}")
    print(f"Final report HTML: {html}")
    return 0


def run_entrypoint(config=None, argv=None):
    args = sys.argv[1:] if argv is None else list(argv)
    if args:
        return main(args)
    config = config or {}
    action = str(config.get("ACTION", "create") or "create").strip().lower()
    action_config = _entrypoint_action_config(config, action)
    target, html = run_action(action, action_config)
    print(f"Final report JSON: {target}")
    print(f"Final report HTML: {html}")
    return 0


def _entrypoint_action_config(config, action):
    key = str(action or "create").upper()
    final_report = config.get("FINAL_REPORT_JSON", config.get("TARGET_REPORT"))
    input_report = config.get("INPUT_REPORT_JSON", config.get("REPORT_JSON", config.get("SOURCE_REPORT", DEFAULT_SOURCE_REPORT_TEXT)))
    value = config.get(key)
    if isinstance(value, dict):
        return _with_default_report_paths(value, action, input_report, final_report)

    actions = config.get("ACTIONS")
    if isinstance(actions, dict) and isinstance(actions.get(key), dict):
        return _with_default_report_paths(actions[key], action, input_report, final_report)

    scope = config.get("SCOPE") if isinstance(config.get("SCOPE"), dict) else {}
    return {
        "report_json": input_report,
        "feature_file": config.get("FEATURE_FILE", scope.get("feature_file")),
        "feature_name": config.get("FEATURE_NAME", scope.get("feature_name")),
        "scenario_name": config.get("SCENARIO_NAME", scope.get("scenario_name")),
        "example_id": config.get("EXAMPLE_ID", scope.get("example_id")),
        "allow_add": config.get("ALLOW_ADD", scope.get("allow_add", True)),
        "final_report_json": final_report,
        "target_report": config.get("TARGET_REPORT", scope.get("target_report")),
        "html_report": config.get("HTML_REPORT", scope.get("html_report")),
        "merge_log": config.get("MERGE_LOG", scope.get("merge_log")),
        "project_root": config.get("PROJECT_ROOT", scope.get("project_root")),
    }


def _with_default_report_paths(config, action, input_report, final_report):
    result = dict(config)
    uses_input = str(action or "").lower() in {"create", "merge"}
    if uses_input and input_report and "report_json" not in result and "input_report" not in result:
        result["report_json"] = input_report
    if uses_input and "input_report" in result and "report_json" not in result:
        result["report_json"] = result.pop("input_report")
    if final_report and "final_report_json" not in result and "target_report" not in result:
        result["final_report_json"] = final_report
    return result


def render_report_html(report_data):
    template_path = Path(__file__).with_name("templates") / "spark_report.html"
    template = template_path.read_text(encoding="utf-8")
    report_json = _script_safe_json(_public_copy(report_data))
    pattern = r"const reportData = [\s\S]*?\n\n    const state ="
    replacement = "const reportData = " + report_json + ";\n\n    const state ="
    html, count = re.subn(pattern, lambda _match: replacement, template, count=1)
    if count != 1:
        raise RuntimeError("未能在报告模板中替换 reportData")
    return html


def snapshot_report_assets(
        report_data,
        *,
        source_report_path,
        final_report_path,
        project_root=None,
):
    project_root = Path(project_root or Paths.BASE_DIR).resolve()
    source_dir = Path(source_report_path).resolve().parent
    final_dir = Path(final_report_path).resolve().parent
    assets_dir = final_dir / "assets"
    report = _public_copy(report_data)

    def visit(value):
        if isinstance(value, dict):
            attachments = value.get("attachments")
            if isinstance(attachments, list):
                for attachment in attachments:
                    if isinstance(attachment, dict):
                        _snapshot_attachment(
                            attachment,
                            source_dir=source_dir,
                            final_dir=final_dir,
                            assets_dir=assets_dir,
                            project_root=project_root,
                        )
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(report)
    return report


def _replace_feature(target, source_feature, *, allow_add):
    features = target.setdefault("features", [])
    index = _find_feature_index_like(target, source_feature)
    replacement = _normalize_feature(copy.deepcopy(source_feature))
    if index is None:
        if not allow_add:
            raise FinalReportMergeError("目标报告中没有匹配的 Feature")
        features.append(replacement)
    else:
        features[index] = replacement


def _merge_scenario(target_feature, source_item, *, allow_add):
    target_feature.pop("skipScope", None)
    target_feature.pop("skipReason", None)
    target_feature.pop("plannedUnits", None)
    target_feature.pop("plannedSteps", None)
    scenarios = target_feature.setdefault("scenarios", [])
    item = copy.deepcopy(source_item)
    if item.get("type") == "example":
        _merge_example(scenarios, item, allow_add=allow_add)
    else:
        index = _find_scenario_index(scenarios, item)
        if index is None:
            if not allow_add:
                raise FinalReportMergeError("目标 Feature 中没有匹配的 Scenario")
            scenarios.append(item)
        else:
            scenarios[index] = item
    _normalize_feature(target_feature)


def _merge_example(scenarios, source_example, *, allow_add):
    outline_name = str(source_example.get("outlineName") or "")
    if not outline_name:
        raise FinalReportMergeError("Example 缺少 outlineName")
    outline = _find_outline(scenarios, outline_name)
    if outline is None:
        if not allow_add:
            raise FinalReportMergeError("目标 Feature 中没有匹配的 Scenario Outline")
        outline = {
            "type": "outline",
            "name": outline_name,
            "status": "unknown",
            "duration": "-",
            "tags": list(source_example.get("tags") or []),
            "owner": source_example.get("owner", ""),
            "examples": [],
        }
        scenarios.append(outline)
    examples = outline.setdefault("examples", [])
    index = _find_example_index(examples, source_example)
    if index is None:
        if not allow_add:
            raise FinalReportMergeError("目标 Scenario Outline 中没有匹配的 Example")
        examples.append(source_example)
    else:
        examples[index] = source_example
    _normalize_outline(outline)


def _delete_scenario(feature, *, scenario_name, example_id):
    scenarios = feature.setdefault("scenarios", [])
    if example_id is not None:
        outline = _find_outline(scenarios, scenario_name)
        if outline is None:
            raise FinalReportMergeError("目标 Feature 中没有匹配的 Scenario Outline")
        examples = outline.setdefault("examples", [])
        before = len(examples)
        outline["examples"] = [
            item for item in examples
            if str(item.get("exampleId") or "") != str(example_id)
        ]
        if len(outline["examples"]) == before:
            raise FinalReportMergeError("目标 Scenario Outline 中没有匹配的 Example")
        if outline["examples"]:
            _normalize_outline(outline)
        else:
            scenarios.remove(outline)
    else:
        before = len(scenarios)
        feature["scenarios"] = [
            item for item in scenarios
            if str(item.get("name") or "") != str(scenario_name or "")
        ]
        if len(feature["scenarios"]) == before:
            raise FinalReportMergeError("目标 Feature 中没有匹配的 Scenario")
    if feature.get("scenarios"):
        _normalize_feature(feature)


def _select_feature(report, *, feature_file=None, feature_name=None):
    features = report.get("features") or []
    if feature_file is None and feature_name is None:
        if len(features) != 1:
            raise FinalReportMergeError("报告中有多个 Feature，请指定 feature_file 或 feature_name")
        return features[0]
    matches = [
        feature for feature in features
        if _feature_matches(feature, feature_file=feature_file, feature_name=feature_name)
    ]
    if not matches:
        raise FinalReportMergeError("报告中没有匹配的 Feature")
    if len(matches) > 1:
        raise FinalReportMergeError("报告中匹配到多个 Feature，请使用 feature_file 精确指定")
    return matches[0]


def _select_scenario(feature, *, scenario_name=None, example_id=None):
    if scenario_name is None:
        raise FinalReportMergeError("合并单个场景时必须指定 scenario_name")
    scenarios = feature.get("scenarios") or []
    if example_id is not None:
        outline = _find_outline(scenarios, scenario_name)
        if outline is None:
            raise FinalReportMergeError("源报告中没有匹配的 Scenario Outline")
        examples = [
            item for item in outline.get("examples") or []
            if str(item.get("exampleId") or "") == str(example_id)
        ]
        if len(examples) != 1:
            raise FinalReportMergeError("源报告中没有唯一匹配的 Example")
        return {**copy.deepcopy(examples[0]), "outlineName": outline.get("name")}
    matches = [
        item for item in scenarios
        if str(item.get("name") or "") == str(scenario_name)
    ]
    if len(matches) != 1:
        raise FinalReportMergeError("源报告中没有唯一匹配的 Scenario")
    return matches[0]


def _operation_scopes(
        report,
        *,
        feature_file=None,
        feature_name=None,
        scenario_name=None,
        example_id=None,
):
    feature = _select_feature(
        report,
        feature_file=feature_file,
        feature_name=feature_name,
    )
    scope = {
        "feature_file": feature_file or feature.get("file"),
        "feature_name": None if (feature_file or feature.get("file")) else (feature_name or feature.get("name")),
    }
    scope = {key: value for key, value in scope.items() if value}
    if scenario_name is not None or example_id is not None:
        scope["scenario_name"] = scenario_name
        scope["example_id"] = example_id
        return [scope]

    scenario_scopes = _single_executable_scope(feature)
    if scenario_scopes is not None:
        return [{**scope, **scenario_scopes}]
    return [scope]


def _single_executable_scope(feature):
    scenarios = list(feature.get("scenarios") or [])
    if len(scenarios) != 1:
        return None
    item = scenarios[0]
    if item.get("type") == "outline":
        examples = list(item.get("examples") or [])
        if len(examples) != 1:
            return None
        return {
            "scenario_name": item.get("name"),
            "example_id": examples[0].get("exampleId"),
        }
    return {"scenario_name": item.get("name")}


def _find_feature_like(report, source_feature):
    index = _find_feature_index_like(report, source_feature)
    return None if index is None else report.get("features", [])[index]


def _find_feature_index_like(report, source_feature):
    source_file = source_feature.get("file")
    source_name = source_feature.get("name")
    features = report.get("features") or []
    for index, feature in enumerate(features):
        if source_file and _same_path(feature.get("file"), source_file):
            return index
    for index, feature in enumerate(features):
        if str(feature.get("name") or "") == str(source_name or ""):
            return index
    return None


def _feature_matches(feature, *, feature_file=None, feature_name=None):
    if feature_file is not None:
        return _same_path(feature.get("file"), feature_file)
    return str(feature.get("name") or "") == str(feature_name or "")


def _find_scenario_index(scenarios, source_item):
    for index, item in enumerate(scenarios):
        if _scenario_key(item) == _scenario_key(source_item):
            return index
    return None


def _find_outline(scenarios, name):
    for item in scenarios:
        if item.get("type") == "outline" and str(item.get("name") or "") == str(name or ""):
            return item
    return None


def _find_example_index(examples, source_example):
    source_id = str(source_example.get("exampleId") or "")
    for index, item in enumerate(examples):
        if source_id and str(item.get("exampleId") or "") == source_id:
            return index
        if not source_id and str(item.get("name") or "") == str(source_example.get("name") or ""):
            return index
    return None


def _scenario_key(item):
    if item.get("type") == "outline":
        return "outline", str(item.get("name") or ""), ""
    if item.get("type") == "example":
        return "example", str(item.get("outlineName") or ""), str(item.get("exampleId") or item.get("name") or "")
    return "scenario", str(item.get("name") or ""), ""


def _feature_shell(source_feature):
    return {
        "name": source_feature.get("name", ""),
        "file": source_feature.get("file", ""),
        "status": "unknown",
        "rawStatus": "unknown",
        "tags": list(source_feature.get("tags") or []),
        "scenarios": [],
    }


def _normalize_report(report):
    result = _public_copy(report)
    result.setdefault("title", "桌面自动化测试报告")
    result.setdefault("project", "BDD Autowork")
    result.setdefault("startedAt", "-")
    result.setdefault("duration", "-")
    result.setdefault("environment", {})
    result["features"] = [
        _normalize_feature(feature)
        for feature in result.get("features") or []
    ]
    return result


def _normalize_feature(feature):
    feature = _public_copy(feature)
    for scenario_item in feature.get("scenarios") or []:
        if scenario_item.get("type") == "outline":
            _normalize_outline(scenario_item)
    if feature.get("skipScope") == "feature" and not feature.get("scenarios"):
        feature["status"] = "skipped"
        return feature
    statuses = []
    for scenario_item in feature.get("scenarios") or []:
        if scenario_item.get("type") == "outline":
            statuses.extend(status_category(item.get("status", "unknown")) for item in scenario_item.get("examples") or [])
        else:
            statuses.append(status_category(scenario_item.get("status", "unknown")))
    feature["status"] = _aggregate_status(statuses)
    return feature


def _normalize_outline(outline):
    examples = outline.get("examples") or []
    outline["status"] = _aggregate_status(
        [status_category(item.get("status", "unknown")) for item in examples]
    )
    outline["duration"] = _sum_duration(item.get("duration") for item in examples)
    return outline


def _aggregate_status(statuses):
    statuses = [status_category(status) for status in statuses]
    if "failed" in statuses:
        return "failed"
    if statuses and all(status == "skipped" for status in statuses):
        return "skipped"
    if statuses and all(status == "passed" for status in statuses):
        return "passed"
    return "unknown"


def _sum_duration(values):
    total = 0
    has_duration = False
    for value in values:
        seconds = _duration_seconds(value)
        if seconds is None:
            continue
        total += seconds
        has_duration = True
    return _format_seconds(total) if has_duration else "-"


def _duration_seconds(value):
    if not value or value == "-":
        return None
    try:
        parts = [int(part) for part in str(value).split(":")]
    except ValueError:
        return None
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 1:
        return parts[0]
    return None


def _format_seconds(seconds):
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _public_copy(value):
    if isinstance(value, dict):
        return {
            key: _public_copy(item)
            for key, item in copy.deepcopy(value).items()
            if not str(key).startswith("_")
        }
    if isinstance(value, list):
        return [_public_copy(item) for item in copy.deepcopy(value)]
    return copy.deepcopy(value)


def _snapshot_attachment(
        attachment,
        *,
        source_dir,
        final_dir,
        assets_dir,
        project_root,
):
    raw = str(attachment.get("path") or "").strip()
    if not raw or _looks_external(raw):
        return
    source = Path(raw)
    if not source.is_absolute():
        source = (source_dir / source).resolve()
    else:
        source = source.resolve()
    try:
        source.relative_to(project_root)
    except ValueError:
        return
    if not source.is_file():
        return
    digest = _file_sha256(source)
    suffix = source.suffix if source.suffix and len(source.suffix) <= 12 else ".bin"
    assets_dir.mkdir(parents=True, exist_ok=True)
    destination = assets_dir / f"{digest}{suffix}"
    if source != destination and not destination.exists():
        temporary = destination.with_name(f"{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            shutil.copyfile(source, temporary)
            if _file_sha256(temporary) != digest:
                raise RuntimeError("最终报告附件快照完整性校验失败")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    attachment["path"] = _relative_path(destination, final_dir)


def _looks_external(value):
    normalized = "".join(char for char in value if ord(char) > 32 and ord(char) != 127)
    return bool(re.match(r"^[a-z][a-z0-9+.-]*:", normalized, flags=re.I))


def _script_safe_json(value):
    return (
        json.dumps(value, ensure_ascii=False, indent=6)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _load_report(path):
    path = Path(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FinalReportMergeError(f"无法读取报告 JSON: {path}") from error
    if not isinstance(value, dict) or not isinstance(value.get("features"), list):
        raise FinalReportMergeError("报告 JSON 必须包含 features 列表")
    return value


def _write_json_atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_text_atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(value, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _append_merge_log(log_path, operation, *, project_root=None, **payload):
    log_path = Path(log_path)
    project_root = Path(project_root or Paths.BASE_DIR).resolve()
    if log_path.exists():
        try:
            log = json.loads(log_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log = {}
    else:
        log = {}
    operations = list(log.get("operations") or []) if isinstance(log, dict) else []
    record = {
        "op": operation,
        "time": datetime.now().isoformat(timespec="seconds"),
        **{
            key: _log_value(value, project_root=project_root)
            for key, value in payload.items()
            if value not in (None, {}, [])
        },
    }
    operations.append(record)
    _write_json_atomic(log_path, {
        "final_merge_version": FINAL_REPORT_VERSION,
        "operations": operations,
    })


def _log_value(value, *, project_root):
    if isinstance(value, Path):
        return _relative_path(value, project_root)
    if isinstance(value, dict):
        return {key: _log_value(item, project_root=project_root) for key, item in value.items()}
    return value


def _scope_payload(**values):
    return {key: value for key, value in values.items() if value is not None}


def final_report_paths(
        *,
        final_report_json=None,
        target_path=None,
        html_path=None,
        log_path=None,
        project_root=None,
):
    project_root = Path(project_root or Paths.BASE_DIR).resolve()
    target = _resolve_path(
        target_path or final_report_json or FINAL_REPORT_JSON,
        project_root=project_root,
    )
    html = _resolve_path(
        html_path or target.with_suffix(".html"),
        project_root=project_root,
    )
    log = _resolve_path(
        log_path or target.with_name(f"{target.stem}-merge.json"),
        project_root=project_root,
    )
    return target, html, log


def _default_output_paths(
        *,
        target_path=None,
        html_path=None,
        log_path=None,
        project_root=None,
):
    project_root = Path(project_root or Paths.BASE_DIR).resolve()
    target = _resolve_path(target_path or FINAL_REPORT_JSON, project_root=project_root)
    html = _resolve_path(html_path or FINAL_REPORT_HTML, project_root=project_root)
    log = _resolve_path(log_path or FINAL_MERGE_LOG, project_root=project_root)
    return target, html, log


def _resolve_path(value, *, project_root):
    path = Path(value)
    if not path.is_absolute():
        path = project_root / path
    return path.resolve()


def _relative_path(path, base):
    path = Path(path).resolve()
    base = Path(base).resolve()
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.as_posix()


def _same_path(left, right):
    return str(left or "").replace("\\", "/").casefold() == str(right or "").replace("\\", "/").casefold()


def _file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Create, merge, delete, and render final Spark reports.",
    )
    parser.add_argument("--project-root", default=str(Paths.BASE_DIR))
    parser.add_argument("--target", default=None)
    parser.add_argument("--html", default=None)
    parser.add_argument("--log", default=None)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create")
    create.add_argument("source", nargs="?", default=None)

    merge = subparsers.add_parser("merge")
    merge.add_argument("source", nargs="?", default=None)
    _add_scope_args(merge)
    merge.add_argument("--no-add", action="store_true")

    merge_latest = subparsers.add_parser("merge-latest")
    _add_scope_args(merge_latest)
    merge_latest.add_argument("--no-add", action="store_true")

    delete = subparsers.add_parser("delete")
    _add_scope_args(delete)

    subparsers.add_parser("render")
    return parser


def _add_scope_args(parser):
    parser.add_argument("--feature-file", default=None)
    parser.add_argument("--feature-name", default=None)
    parser.add_argument("--scenario", dest="scenario_name", default=None)
    parser.add_argument("--example-id", default=None)


def main(argv=None):
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    project_root = Path(args.project_root).resolve()
    try:
        if args.command == "create":
            target, html = create_final_report(
                args.source,
                target_path=args.target,
                html_path=args.html,
                log_path=args.log,
                project_root=project_root,
            )
        elif args.command in {"merge", "merge-latest"}:
            target, html = merge(
                None if args.command == "merge-latest" else args.source,
                target_report=args.target,
                html_report=args.html,
                merge_log=args.log,
                feature_file=args.feature_file,
                feature_name=args.feature_name,
                scenario_name=args.scenario_name,
                example_id=args.example_id,
                allow_add=not args.no_add,
                project_root=project_root,
            )
        elif args.command == "delete":
            target, html = delete(
                target_report=args.target,
                html_report=args.html,
                merge_log=args.log,
                feature_file=args.feature_file,
                feature_name=args.feature_name,
                scenario_name=args.scenario_name,
                example_id=args.example_id,
                project_root=project_root,
            )
        else:
            html = render_final_report(
                args.target,
                html_path=args.html,
                project_root=project_root,
            )
            target = _default_output_paths(
                target_path=args.target,
                html_path=args.html,
                log_path=args.log,
                project_root=project_root,
            )[0]
    except FinalReportMergeError as error:
        print(str(error), file=sys.stderr)
        return 2

    print(f"Final report JSON: {_relative_path(target, project_root)}")
    print(f"Final report HTML: {_relative_path(html, project_root)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
