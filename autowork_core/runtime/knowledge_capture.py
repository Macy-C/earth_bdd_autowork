from contextlib import contextmanager
import json
from pathlib import Path
from uuid import uuid4

from autowork_core.runtime.evidence import RunEvidenceCollector
from autowork_core.runtime.evidence.context import ACTIVE_ACTION, EXECUTION_ORIGIN
from autowork_core.runtime.evidence.models import (
    captured_value, row_fact as _row_fact, step_fact as _step_fact, utc_now,
)


ACTIVE_KNOWLEDGE_ACTION = ACTIVE_ACTION
KNOWLEDGE_ORIGIN = EXECUTION_ORIGIN


class KnowledgeCapture:
    def __init__(self, output_root, *, image_provider=None, collector=None):
        self._owns_collector = collector is None
        self.collector = collector if collector is not None else RunEvidenceCollector(
            output_root, detailed=True, image_provider=image_provider,
        )
        self.collector.detailed = True
        if image_provider is not None:
            self.collector.image_provider = image_provider
        self.run_id = self.collector.execution_id
        self.directory = Path(output_root) / self.run_id
        self.directory.mkdir(parents=True, exist_ok=True)
        self._metadata = {}
        self._case_ids = {}
        self._finished = False
        self._safe_persist()

    @property
    def document(self):
        result = self.collector.snapshot()
        result.update(format_version=1, run_id=self.run_id, source_execution_id=self.collector.execution_id)
        result.update(self._metadata)
        for feature in result["features"]:
            for scenario in feature["scenarios"]:
                if scenario["id"] in self._case_ids:
                    scenario["capture_case_id"] = self._case_ids[scenario["id"]]
        return result

    @property
    def current_feature(self):
        return self.collector.current_feature

    @property
    def current_scenario(self):
        return self.collector.current_scenario

    @property
    def current_step(self):
        return self.collector.current_step

    @property
    def image_provider(self):
        return self.collector.image_provider

    @image_provider.setter
    def image_provider(self, value):
        self.collector.image_provider = value

    def remember_source(self, path):
        return self.collector.remember_source(path)

    def before_feature(self, context, feature):
        if self._owns_collector:
            from config.paths import Paths
            self.collector.project_root = Path(Paths.BASE_DIR).resolve()
        self.collector.before_feature(context, feature)

    def before_scenario(self, context, scenario):
        self.collector.before_scenario(context, scenario)

    def before_step(self, context, step):
        self.collector.before_step(context, step)

    def after_step(self, context, step):
        self.collector.after_step(context, step)

    def after_scenario(self, context, scenario):
        self.collector.after_scenario(context, scenario)

    def after_feature(self, context, feature):
        self.collector.after_feature(context, feature)

    def start_step(self, identity):
        return self.collector.start_step(identity)

    def finish_step(self, status, exception=None):
        self.collector.finish_step(status, exception)

    def invoke(self, operation, callback, *, arguments=None, owner=None, phases=("after",)):
        try:
            return self.collector.invoke(operation, callback, arguments=arguments, owner=owner, phases=phases)
        finally:
            if self._owns_collector:
                self._safe_persist()

    def refresh_model_statuses(self):
        self.collector.refresh_model_statuses()

    def record_error(self, stage, error):
        self.collector.record_error(stage, error)

    _exception = staticmethod(RunEvidenceCollector.exception_record)

    def finish(self, test_status):
        if self._finished and not self._owns_collector:
            return
        self._finished = True
        snapshot = self.collector.snapshot()
        capture_status = "incomplete" if snapshot["errors"] else "complete"
        self._metadata.update(test_status=test_status, finished_at=utc_now(), capture_status=capture_status)
        if capture_status == "complete" and test_status == "passed":
            for feature in snapshot["features"]:
                for scenario in feature["scenarios"]:
                    if scenario.get("test_status") == "passed":
                        self._case_ids.setdefault(scenario["id"], uuid4().hex)
        self._safe_persist()
        if not snapshot["features"]:
            return
        from autowork_core.knowledge.case_export import export_capture_markdown
        from loguru import logger
        try:
            exported = export_capture_markdown(self.directory)
            self._metadata["markdown_export"] = {
                "status": exported["status"],
                "files": [Path(item["output_path"]).name for item in exported["cases"]],
                "errors": exported["errors"],
            }
            for item in exported["cases"]:
                s = item.get("summary") or {}
                pre_c = s.get("pre_image_chars", 0)
                pre_b = s.get("pre_image_budget", 32768)
                if pre_c:
                    logger.info(
                        "Knowledge Markdown: {} (前置摘要区 {}/{} 字符, {:.1f}%)",
                        item["output_path"], pre_c, pre_b, (pre_c * 100.0 / pre_b) if pre_b else 0.0,
                    )
                else:
                    logger.info("Knowledge Markdown: {}", item["output_path"])
                for warn_msg in s.get("warnings") or ():
                    logger.warning("Knowledge Markdown budget warning ({}): {}", Path(item["output_path"]).name, warn_msg)
        except Exception as error:
            self._metadata["markdown_export"] = {"status": "failed", "error": self._exception(error)}
            logger.warning("Knowledge Markdown export failed: {}", type(error).__name__)
        self._safe_persist()

    def _persist(self):
        import shutil
        import hashlib

        snapshot = self.document
        source_root = self.collector.directory
        if source_root is not None and source_root.resolve() != self.directory.resolve():
            for feature in snapshot["features"]:
                for scenario in feature["scenarios"]:
                    for step in scenario["steps"]:
                        for action in step["actions"]:
                            for image in action["images"]:
                                if image.get("status") != "captured":
                                    continue
                                source = source_root / image["path"]
                                destination = self.directory / image["path"]
                                destination.parent.mkdir(parents=True, exist_ok=True)
                                if not destination.exists():
                                    shutil.copyfile(source, destination)
                                if hashlib.sha256(destination.read_bytes()).hexdigest() != image["sha256"]:
                                    raise ValueError("Knowledge media copy digest mismatch")
        destination = self.directory / "observations.json"
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(destination)

    def _safe_persist(self):
        try:
            self._persist()
        except Exception as error:
            self._metadata["capture_status"] = "incomplete"
            self.record_error("knowledge_projection", error)


def initialize_knowledge_capture(context):
    from config.settings import settings
    if not settings.knowledge_capture_enabled:
        return
    from config.paths import Paths
    from autowork_core.runtime.knowledge_observation import capture_window_image
    from loguru import logger
    try:
        collector = getattr(context.autowork_run, "collector", None)
        if collector is not None and not collector.active:
            raise RuntimeError("Run evidence is unavailable")
        capture = KnowledgeCapture(
            Paths.ARTIFACTS_DIR / "knowledge-captures", collector=collector,
            image_provider=lambda action, phase: capture_window_image(context, action, phase),
        )
        context.autowork_run.knowledge_capture = capture
        capture.collector.before_all(context)
        logger.info("Knowledge capture enabled: {}", capture.directory)
    except Exception as error:
        context.autowork_run.knowledge_capture_error = KnowledgeCapture._exception(error)
        logger.warning("Knowledge capture initialization failed: {}", type(error).__name__)


def dispatch_knowledge_hook(context, name, *args):
    capture = getattr(context.autowork_run, "knowledge_capture", None)
    if capture is None:
        return
    if name != "after_all" and capture.collector is getattr(context.autowork_run, "collector", None):
        return
    try:
        if name == "after_all":
            if not capture.collector._finalized:
                capture.refresh_model_statuses()
            capture.finish(_observed_run_status(context, capture))
        else:
            getattr(capture, name)(context, *args)
    except Exception as error:
        capture.record_error(name, error)
        capture._safe_persist()


def _observed_run_status(context, capture):
    if getattr(context, "aborted", False):
        return "aborted"
    if getattr(context, "failed", False):
        return "failed"
    statuses = {feature["test_status"] for feature in capture.document["features"]}
    if not statuses:
        return "not_run"
    if statuses == {"skipped"}:
        return "skipped"
    if statuses & {"failed", "error", "hook_error", "undefined", "pending", "pending_warn"}:
        return "failed"
    return "passed" if statuses <= {"passed", "skipped"} else "incomplete"


@contextmanager
def activated_knowledge_origin(plan):
    token = KNOWLEDGE_ORIGIN.set(plan)
    try:
        yield
    finally:
        KNOWLEDGE_ORIGIN.reset(token)