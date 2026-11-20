"""Project unified run evidence into Spark JSON/HTML and publish Run Result."""

import json
from pathlib import Path
import platform

from loguru import logger

from autowork_core.runtime.evidence.models import plain_record
from autowork_core.runtime.evidence.projection import report_projection
from autowork_core.runtime.reporting.run_result_bridge import publish_run_result
from autowork_core.runtime.reporting.template import render_html
from config.paths import Paths
from config.settings import settings


class SparkReporter:
    def __init__(self, collector=None):
        self.collector = collector
        self.data = None
        self.run_result_path = None
        self._completed = False
        self._diagnosis = None
        if settings.spark_report and settings.report_diagnosis_enabled:
            from .diagnosis import ReportDiagnosis
            self._diagnosis = ReportDiagnosis(settings)

    def before_all(self, context):
        if self.collector is None:
            from autowork_core.runtime.evidence import RunEvidenceCollector
            self.collector = RunEvidenceCollector()
        self.collector.before_all(context)
        self._refresh()

    def before_feature(self, context, feature):
        self.collector.before_feature(context, feature)
        self._refresh()

    def after_feature(self, context, feature):
        self.collector.after_feature(context, feature)
        self._refresh()

    def before_scenario(self, context, scenario):
        self.collector.before_scenario(context, scenario)
        self._refresh()

    def after_scenario(self, context, scenario, record_path=None):
        self.collector.after_scenario(context, scenario, record_path)
        self._refresh()

    def before_step(self, context, step):
        self.collector.before_step(context, step)
        self._refresh()

    def after_step(self, context, step, screenshot_path=None, screenshot_error=None):
        self.collector.after_step(context, step, screenshot_path=screenshot_path, screenshot_error=screenshot_error)
        self._refresh()

    def consume(self, snapshot):
        self.data = report_projection(snapshot)
        self.data["environment"] = self._environment_info()

    def _refresh(self):
        self.consume(self.collector.snapshot())

    def _public_data(self):
        return plain_record(self.data)

    def _location_path(self, path):
        from autowork_core.runtime.run_state import report_source_path
        if not path:
            return ""
        path = report_source_path(path) or path
        try:
            return Path(path).resolve().relative_to(Path(Paths.BASE_DIR).resolve()).as_posix()
        except ValueError:
            return str(path)

    def after_all(self, context):
        if self._completed:
            return Paths.REPORTS_DIR / "autowork-report.html"
        if self.collector is not None:
            self._refresh()
        if not self.data:
            return
        public_data = self._public_data()
        Paths.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        json_path = Paths.REPORTS_DIR / "autowork-report.json"
        html_path = Paths.REPORTS_DIR / "autowork-report.html"
        json_path.write_text(json.dumps(public_data, ensure_ascii=False, indent=2), encoding="utf-8")
        html_path.write_text(self._render_html(), encoding="utf-8")
        if self._diagnosis is not None:
            from .diagnosis import apply_diagnosis
            try:
                snapshot = self.collector.snapshot()
                directory = (self.collector.directory or Paths.REPORTS_DIR / snapshot["execution_id"]) / "diagnostics"
                diagnosis = self._diagnosis.run(snapshot, directory, project_root=Paths.BASE_DIR)
                apply_diagnosis(self.data, diagnosis)
            except Exception as error:
                self.data["diagnosis"] = {"enabled": True, "status": "failed", "items": [], "reason": type(error).__name__}
                logger.warning("报告诊断失败，保留基础报告: {}", type(error).__name__)
            public_data = self._public_data()
            json_path.write_text(json.dumps(public_data, ensure_ascii=False, indent=2), encoding="utf-8")
            html_path.write_text(self._render_html(), encoding="utf-8")
        logger.info("自定义报告已生成: {}", html_path)
        try:
            self.run_result_path = publish_run_result(public_data, report_path=html_path)
            logger.info("运行结果索引已生成: {}", self.run_result_path)
        except Exception as error:
            logger.warning("运行结果索引生成失败，不影响测试结果: {}: {}", type(error).__name__, error)
        self._completed = True
        return html_path

    def _render_html(self):
        return render_html(self._script_safe_json(self._public_data()))

    def _script_safe_json(self, value):
        return (json.dumps(value, ensure_ascii=False, indent=6)
                .replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
                .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))

    def _environment_info(self):
        info = {
            "OS": platform.platform(), "Python": platform.python_version(),
            "AppLaunchMode": str(settings.app_launch_mode), "Backend": str(settings.backend),
            "Recording": str(settings.effective_record_mode), "Report": "Autowork Spark",
        }
        try:
            import behave
            info["Behave"] = behave.__version__
        except Exception:
            pass
        return info