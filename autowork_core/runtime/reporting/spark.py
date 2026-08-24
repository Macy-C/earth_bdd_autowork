"""采集测试执行数据与附件，并生成 Spark 风格的 JSON 和 HTML 报告。

Collects execution data and attachments, then produces Spark-style
JSON and HTML reports.
"""

import json
import os
import platform
import re
import sys
import time
import traceback

from autowork_core.common.runtime_diagnostics import (
    runtime_diagnostic_payload,
)
from pathlib import Path

from loguru import logger

from autowork_core.runtime.status import status_category, status_text
from autowork_core.runtime.run_state import active_step_scope
from autowork_core.runtime.reporting.run_result_bridge import (
    publish_run_result,
)
from config.paths import Paths
from config.settings import settings


OUTLINE_EXAMPLE_RE = re.compile(
    r"^(?P<name>.+?)\s+--\s+"
    r"(?P<example>@\d+(?:\.\d+)?)"
    r"(?:\s+.*)?$"
)


class SparkReporter:
    def __init__(self):
        self.data = None
        self._feature_by_id = {}
        self._scenario_by_id = {}
        self._step_by_id = {}
        self._started_at = None
        self._run_start = None
        self._known_debug_files = set()

    def before_all(self, context):
        self._started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self._run_start = time.monotonic()
        self.data = {
            "title": "桌面自动化测试报告",
            "project": "BDD Autowork",
            "startedAt": self._started_at,
            "duration": "-",
            "environment": self._environment_info(),
            "features": [],
        }
        self._known_debug_files = self._debug_image_snapshot()

    def before_feature(self, context, feature):
        feature_data = {
            "name": str(getattr(feature, "name", "")),
            "file": self._location_path(getattr(feature, "filename", "")),
            "status": "unknown",
            "rawStatus": "unknown",
            "tags": self._tags(feature),
            "scenarios": [],
        }
        self._feature_by_id[id(feature)] = feature_data
        self.data["features"].append(feature_data)

    def after_feature(self, context, feature):
        feature_data = self._feature_by_id.get(id(feature))
        if feature_data:
            raw_status = status_text(getattr(feature, "status", "unknown"))
            feature_data["rawStatus"] = raw_status
            if raw_status == "skipped" and bool(
                    getattr(feature, "should_skip", False)
            ):
                units = list(self._feature_execution_units(feature))
                feature_data.update({
                    "status": "skipped",
                    "skipScope": "feature",
                    "skipReason": self._skip_reason(feature),
                    "plannedUnits": len(units),
                    "plannedSteps": sum(
                        len(self._scenario_all_steps(unit))
                        for unit in units
                    ),
                })
                feature_data["scenarios"] = []
                return
            self._group_outline_examples(feature_data)
            feature_data["status"] = self._aggregate_feature_status(feature_data)
            failure_kind = self._failure_kind(raw_status)
            if failure_kind:
                feature_data["failureKind"] = failure_kind

    def before_scenario(self, context, scenario):
        feature_data = self._feature_for(context)
        scenario_data = self._new_scenario_data(scenario)
        scope = active_step_scope() or {}
        scenario_data["stepScope"] = {
            "files": list(scope.get("files") or ()),
            "entryFile": scope.get("entry_file"),
            "origin": scope.get("origin"),
            "fingerprint": scope.get("fingerprint"),
            "declarations": list(scope.get("declarations") or ()),
        }
        scenario_data["_start"] = time.monotonic()
        self._scenario_by_id[id(scenario)] = scenario_data
        feature_data["scenarios"].append(scenario_data)

    def after_scenario(self, context, scenario, record_path=None):
        scenario_data = self._scenario_by_id.get(id(scenario))
        if not scenario_data:
            return
        raw_status = status_text(getattr(scenario, "status", "unknown"))
        scenario_data["status"] = status_category(raw_status)
        scenario_data["rawStatus"] = raw_status
        self._sync_missing_steps(context, scenario, scenario_data)
        failure_kind = self._failure_kind(
            raw_status,
            scenario_data.get("steps", []),
        )
        if failure_kind:
            scenario_data["failureKind"] = failure_kind
        if scenario_data["status"] == "skipped":
            scenario_data["skipScope"] = "scenario"
            scenario_data["skipReason"] = self._skip_reason(scenario)
        scenario_data["duration"] = self._elapsed(
            scenario_data.pop("_start", None)
        )
        if scenario_data["status"] == "failed" and not scenario_data.get("steps"):
            scenario_data.setdefault("steps", []).append({
                "keyword": "Scenario",
                "name": "场景失败，但未捕获到步骤明细",
                "status": "failed",
                "duration": "-",
                "error": self._scenario_error(scenario) or "场景失败：未捕获到具体 step 信息",
                "attachments": [],
            })
        if record_path and scenario_data["status"] == "failed":
            scenario_data.setdefault("attachments", []).append({
                "name": "场景失败录屏",
                "type": "video",
                "path": self._report_relative_path(record_path),
            })

    def before_step(self, context, step):
        scenario_data = self._scenario_by_id.get(id(getattr(context, "scenario", None)))
        if not scenario_data:
            return
        step_data = {
            "keyword": str(getattr(step, "keyword", "") or getattr(step, "step_type", "") or "Step").strip(),
            "name": str(getattr(step, "name", "")),
            "status": "unknown",
            "duration": "-",
            "attachments": [],
            "_start": time.monotonic(),
            "_step_id": id(step),
            "_debug_start_time": time.time(),
        }
        self._step_by_id[id(step)] = step_data
        scenario_data.setdefault("steps", []).append(step_data)

    def after_step(self, context, step, screenshot_path=None, screenshot_error=None):
        step_data = self._step_by_id.get(id(step))
        if not step_data:
            return
        step_data["status"] = status_category(getattr(step, "status", "unknown"))
        step_data["rawStatus"] = status_text(
            getattr(step, "status", "unknown")
        )
        failure_kind = self._failure_kind(step_data["rawStatus"])
        if failure_kind:
            step_data["failureKind"] = failure_kind
        step_data["duration"] = self._elapsed(step_data.pop("_start", None))
        error = self._step_error(step)
        if error:
            step_data["error"] = error
        error_detail = self._step_error_detail(context, step)
        if error_detail:
            step_data["errorDetail"] = error_detail
        diagnostic = runtime_diagnostic_payload(
            getattr(step, "exception", None)
        )
        if diagnostic is not None:
            step_data["diagnostic"] = diagnostic
        if step_data["status"] == "failed":
            self._attach_new_debug_images(step_data)
        if screenshot_path:
            step_data.setdefault("attachments", []).append({
                "name": "失败截图",
                "type": "image",
                "path": self._report_relative_path(screenshot_path),
            })
        if screenshot_error:
            detail = f"失败截图保存失败: {screenshot_error}"
            if step_data.get("errorDetail"):
                step_data["errorDetail"] = f"{step_data['errorDetail']}\n\n{detail}"
            else:
                step_data["errorDetail"] = detail
    def after_all(self, context):
        if not self.data:
            return
        self.data["duration"] = self._elapsed(self._run_start)
        public_data = self._public_data()
        Paths.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        json_path = Paths.REPORTS_DIR / "autowork-report.json"
        html_path = Paths.REPORTS_DIR / "autowork-report.html"
        json_path.write_text(json.dumps(public_data, ensure_ascii=False, indent=2), encoding="utf-8")
        html_path.write_text(self._render_html(), encoding="utf-8")
        logger.info(f"自定义报告已生成: {html_path}")
        try:
            run_result = publish_run_result(
                public_data,
                report_path=html_path,
            )
            logger.info(f"运行结果索引已生成: {run_result}")
        except Exception as error:
            logger.warning(
                "运行结果索引生成失败，不影响测试结果: "
                f"{type(error).__name__}: {error}"
            )

    def _feature_for(self, context):
        feature = getattr(context, "feature", None)
        feature_data = self._feature_by_id.get(id(feature))
        if feature_data:
            return feature_data
        fallback = {
            "name": str(getattr(feature, "name", "Unknown Feature")),
            "file": self._location_path(getattr(feature, "filename", "")),
            "status": "unknown",
            "tags": self._tags(feature),
            "scenarios": [],
        }
        self.data["features"].append(fallback)
        self._feature_by_id[id(feature)] = fallback
        return fallback

    def _new_scenario_data(self, scenario):
        outline_info = self._outline_example_info(scenario)
        scenario_data = {
            "type": "scenario",
            "name": str(getattr(scenario, "name", "")),
            "status": "unknown",
            "rawStatus": "unknown",
            "duration": "-",
            "tags": self._tags(scenario),
            "owner": "",
            "attachments": [],
            "steps": [],
        }
        if outline_info:
            scenario_data.update({
                "type": "example",
                "outlineName": outline_info["outline_name"],
                "exampleId": outline_info["example_id"],
                "params": outline_info["params"],
            })
        return scenario_data

    def _aggregate_feature_status(self, feature_data):
        statuses = []
        for scenario in feature_data.get("scenarios", []):
            if scenario.get("type") == "outline":
                statuses.extend(status_category(example.get("status", "unknown")) for example in scenario.get("examples", []))
            else:
                statuses.append(status_category(scenario.get("status", "unknown")))
        if "failed" in statuses:
            return "failed"
        if statuses and all(status == "skipped" for status in statuses):
            return "skipped"
        if statuses and all(status == "passed" for status in statuses):
            return "passed"
        return "unknown"

    def _public_data(self):
        def strip_private(value):
            if isinstance(value, dict):
                return {key: strip_private(item) for key, item in value.items() if not str(key).startswith("_")}
            if isinstance(value, list):
                return [strip_private(item) for item in value]
            return value
        return strip_private(self.data)

    def _group_outline_examples(self, feature_data):
        scenarios = feature_data.get("scenarios", [])
        grouped = []
        outline_map = {}

        for scenario in scenarios:
            if scenario.get("type") != "example":
                grouped.append(scenario)
                continue

            outline_name = scenario.get("outlineName") or self._strip_example_suffix(scenario.get("name", ""))
            outline = outline_map.get(outline_name)
            if outline is None:
                outline = {
                    "type": "outline",
                    "name": outline_name,
                    "status": "unknown",
                    "duration": "-",
                    "tags": scenario.get("tags", []),
                    "owner": scenario.get("owner", ""),
                    "examples": [],
                }
                outline_map[outline_name] = outline
                grouped.append(outline)

            outline["examples"].append(scenario)

        for outline in outline_map.values():
            outline["status"] = self._aggregate_status(outline.get("examples", []))
            outline["duration"] = self._sum_duration(outline.get("examples", []))

        feature_data["scenarios"] = grouped

    def _aggregate_status(self, items):
        statuses = [status_category(item.get("status", "unknown")) for item in items]
        if "failed" in statuses:
            return "failed"
        if statuses and all(status == "skipped" for status in statuses):
            return "skipped"
        if statuses and all(status == "passed" for status in statuses):
            return "passed"
        return "unknown"

    def _sum_duration(self, items):
        total = 0
        has_duration = False
        for item in items:
            seconds = self._duration_seconds(item.get("duration"))
            if seconds is None:
                continue
            has_duration = True
            total += seconds
        return self._format_seconds(total) if has_duration else "-"

    def _duration_seconds(self, value):
        if not value or value == "-":
            return None
        try:
            parts = [int(part) for part in str(value).split(":")]
            if len(parts) == 3:
                return parts[0] * 3600 + parts[1] * 60 + parts[2]
            if len(parts) == 2:
                return parts[0] * 60 + parts[1]
            if len(parts) == 1:
                return parts[0]
        except Exception:
            return None
        return None

    def _outline_example_info(self, scenario):
        name = str(getattr(scenario, "name", ""))
        match = OUTLINE_EXAMPLE_RE.match(name)
        row = getattr(scenario, "_row", None) or getattr(scenario, "row", None)
        params = self._row_params(row)

        if not match:
            return None

        parent = getattr(scenario, "parent", None)
        outline_name = (
            str(parent.name)
            if type(parent).__name__ == "ScenarioOutline"
            else match.group("name").strip()
        )
        return {
            "outline_name": outline_name,
            "example_id": match.group("example").strip().lstrip("@"),
            "params": params,
        }

    def _row_params(self, row):
        if row is None:
            return {}
        headings = getattr(row, "headings", None)
        cells = getattr(row, "cells", None)
        if headings and cells:
            return {str(key): str(value) for key, value in zip(headings, cells)}
        if isinstance(row, dict):
            return {str(key): str(value) for key, value in row.items()}
        return {}

    def _strip_example_suffix(self, name):
        match = OUTLINE_EXAMPLE_RE.match(str(name))
        return match.group("name").strip() if match else str(name)

    def _sync_missing_steps(self, context, scenario, scenario_data):
        recorded = {step.get("_step_id") for step in scenario_data.get("steps", []) if step.get("_step_id")}
        scenario_status = status_category(getattr(scenario, "status", "unknown"))
        missing_steps = []
        for step in self._ordered_scenario_steps(context, scenario):
            if id(step) in recorded:
                continue
            step_status = status_category(getattr(step, "status", "unknown"))
            blocked = scenario_status == "failed" and step_status in ("unknown", "skipped")
            if blocked:
                step_status = "skipped"
            step_data = {
                "keyword": str(getattr(step, "keyword", "") or getattr(step, "step_type", "") or "Step").strip(),
                "name": str(getattr(step, "name", "")),
                "status": step_status,
                "rawStatus": status_text(
                    getattr(step, "status", "unknown")
                ),
                "duration": "-",
                "attachments": [],
                "_step_id": id(step),
            }
            error = self._step_error(step)
            if not error and blocked:
                error = f"因前置步骤失败未执行: {step_data['keyword']} {step_data['name']}".strip()
            if error:
                step_data["error"] = error
            error_detail = self._step_error_detail(context, step)
            if not error_detail and error:
                error_detail = self._format_error_detail(context, step, error)
            if error_detail:
                step_data["errorDetail"] = error_detail
            missing_steps.append(step_data)

        scenario_data.setdefault("steps", []).extend(missing_steps)
        self._sort_steps(context, scenario, scenario_data)

    def _sort_steps(self, context, scenario, scenario_data):
        order = {id(step): index for index, step in enumerate(self._ordered_scenario_steps(context, scenario))}
        scenario_data.setdefault("steps", []).sort(key=lambda item: order.get(item.get("_step_id"), 10**9))

    def _ordered_scenario_steps(self, context, scenario):
        all_steps = getattr(scenario, "all_steps", None)
        if all_steps is not None:
            return list(all_steps)
        steps = list(getattr(scenario, "background_steps", []) or [])
        steps.extend(getattr(scenario, "steps", []) or [])
        return steps

    def _feature_execution_units(self, feature):
        for item in getattr(feature, "run_items", ()) or ():
            item_type = type(item).__name__
            if item_type == "Rule":
                yield from self._feature_execution_units(item)
            elif item_type == "ScenarioOutline":
                yield from getattr(item, "scenarios", ()) or ()
            else:
                yield item

    @staticmethod
    def _scenario_all_steps(scenario):
        all_steps = getattr(scenario, "all_steps", None)
        if all_steps is not None:
            return list(all_steps)
        steps = list(getattr(scenario, "background_steps", []) or [])
        steps.extend(getattr(scenario, "steps", []) or [])
        return steps

    def _skip_reason(self, item):
        reason = str(getattr(item, "skip_reason", "") or "").strip()
        if reason:
            return reason
        tags = {
            str(tag).strip().lstrip("@").casefold()
            for tag in (
                getattr(item, "effective_tags", None)
                or getattr(item, "tags", ())
            )
        }
        for candidate in ("maint", "skip", "rep"):
            if candidate in tags:
                return candidate
        return "skipped"

    @staticmethod
    def _failure_kind(raw_status, steps=()):
        raw_status = str(raw_status or "").strip().lower()
        step_statuses = {
            str(step.get("rawStatus") or "").strip().lower()
            for step in steps or ()
            if isinstance(step, dict)
        }
        if raw_status == "failed":
            return "assertion"
        if raw_status == "hook_error":
            return "hook"
        if raw_status == "cleanup_error":
            return "cleanup"
        if raw_status in {"undefined", "pending"} or step_statuses & {
            "undefined",
            "pending",
        }:
            return "definition"
        if raw_status == "error":
            return "automation"
        return None

    def _render_html(self):
        template_path = Path(__file__).with_name("templates") / "spark_report.html"
        template = template_path.read_text(encoding="utf-8")
        report_json = self._script_safe_json(self._public_data())
        pattern = r"const reportData = [\s\S]*?\n\n    const state ="
        replacement = "const reportData = " + report_json + ";\n\n    const state ="
        html, count = re.subn(pattern, lambda _: replacement, template, count=1)
        if count != 1:
            raise RuntimeError("未能在报告模板中替换 reportData")
        return html

    def _script_safe_json(self, value):
        return (
            json.dumps(value, ensure_ascii=False, indent=6)
            .replace("&", "\\u0026")
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029")
        )

    def _environment_info(self):
        info = {
            "OS": platform.platform(),
            "Python": platform.python_version(),
            "AppLaunchMode": str(getattr(settings, "app_launch_mode", "")),
            "Backend": str(getattr(settings, "backend", "")),
            "Recording": str(getattr(settings, "effective_record_mode", "")),
            "Report": "Autowork Spark",
        }
        try:
            import behave
            info["Behave"] = getattr(behave, "__version__", "")
        except Exception:
            pass
        return info

    def _tags(self, item):
        return [str(tag) for tag in getattr(item, "tags", [])]

    def _elapsed(self, start):
        if start is None:
            return "-"
        seconds = max(0.0, time.monotonic() - start)
        return self._format_seconds(int(seconds))

    def _format_seconds(self, seconds):
        minutes, sec = divmod(int(seconds), 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours:02d}:{minutes:02d}:{sec:02d}"

    def _step_error(self, step):
        status = status_text(getattr(step, "status", ""))
        if "undefined" in status:
            keyword = str(getattr(step, "keyword", "") or getattr(step, "step_type", "") or "Step").strip()
            name = str(getattr(step, "name", ""))
            return f"步骤未定义: {keyword} {name}".strip()
        for attr in ("error_message", "exception"):
            value = getattr(step, attr, None)
            if value:
                return str(value)
        return ""

    def _step_error_detail(self, context, step):
        error = self._step_error(step)
        exception = getattr(step, "exception", None)
        if not error and not exception:
            return ""

        return self._format_error_detail(context, step, error, exception=exception)

    def _format_error_detail(self, context, step, error, exception=None):

        feature_name = str(getattr(getattr(context, "feature", None), "name", "") or "-") if context else "-"
        scenario_name = str(getattr(getattr(context, "scenario", None), "name", "") or "-") if context else "-"
        step_name = str(getattr(step, "name", ""))
        lines = [
            "-" * 80,
            f"ERROR in step '{step_name}':",
            f"  Feature:  {feature_name}",
            f"  Scenario: {scenario_name}",
        ]

        if exception:
            lines.extend(traceback.format_exception(type(exception), exception, getattr(exception, "__traceback__", None)))
        elif error:
            lines.append(error)

        if error:
            lines.extend(["", f"exception: {error}"])
        lines.append("-" * 80)
        return "\n".join(str(line).rstrip("\n") for line in lines)

    def _debug_image_snapshot(self):
        return {str(path) for path in self._debug_image_files()}

    def _debug_image_files(self):
        if not Paths.SCREENSHOTS_DIR.exists():
            return []
        return [
            path for path in Paths.SCREENSHOTS_DIR.glob("*.png")
            if path.name.lower().startswith(("ocr_", "pic_"))
        ]

    def _attach_new_debug_images(self, step_data):
        start_time = float(step_data.pop("_debug_start_time", 0) or 0)
        current_files = self._debug_image_files()
        current = {str(path) for path in current_files}
        new_files = []

        for path in current_files:
            path_text = str(path)
            if path_text in self._known_debug_files:
                continue
            try:
                modified_time = path.stat().st_mtime
            except OSError:
                continue
            if start_time and modified_time < start_time:
                continue
            new_files.append((path, modified_time))

        self._known_debug_files = current

        for path, _ in sorted(new_files, key=lambda item: item[1]):
            lower = path.name.lower()
            kind = "OCR" if lower.startswith("ocr_") else "PIC"
            step_data.setdefault("attachments", []).append({
                "name": f"{kind} 调试图",
                "type": "image",
                "path": self._report_relative_path(path),
            })

    def _scenario_error(self, scenario):
        for attr in ("error_message", "exception"):
            value = getattr(scenario, attr, None)
            if value:
                return str(value)
        status = status_text(getattr(scenario, "status", ""))
        if "undefined" in status:
            return "场景包含未定义步骤"
        return ""

    def _location_path(self, path):
        if not path:
            return ""
        try:
            return str(Path(path).resolve().relative_to(Paths.BASE_DIR))
        except Exception:
            return str(path)

    def _report_relative_path(self, path):
        if not path:
            return ""
        try:
            return os.path.relpath(str(path), str(Paths.REPORTS_DIR)).replace("\\", "/")
        except Exception:
            return str(path).replace("\\", "/")
