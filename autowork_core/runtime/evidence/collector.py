from __future__ import annotations

from contextvars import ContextVar
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
from threading import RLock
from time import monotonic
import traceback
from types import MappingProxyType
from uuid import uuid4

from autowork_core.common.runtime_diagnostics import runtime_diagnostic_payload
from autowork_core.runtime.status import status_category, status_text, should_keep_artifacts
from autowork_core.runtime.run_state import active_step_scope, report_source_path
from .context import ACTIVE_ACTION, EXECUTION_ORIGIN
from .models import captured_value, plain_record, redacted, row_fact, step_fact, utc_now


class RunEvidenceCollector:
    def __init__(self, output_root=None, *, project_root=None, detailed=False,
                 image_provider=None, execution_id=None, max_actions=20000, max_logs=5000,
                 max_image_bytes=16 * 1024 * 1024, max_images=2000,
                 max_source_bytes=16 * 1024 * 1024,
                 max_execution_log_bytes=16 * 1024 * 1024, max_delivery_log_bytes=4 * 1024 * 1024):
        from config.paths import Paths

        self.execution_id = execution_id or utc_now().replace("-", "").replace(":", "").split(".")[0] + "-" + uuid4().hex[:12]
        self.run_id = self.execution_id
        self.project_root = Path(project_root or Paths.BASE_DIR).resolve()
        self.directory = None if output_root is None else Path(output_root) / self.execution_id
        self.detailed = detailed
        self.image_provider = image_provider
        self.max_actions = max_actions
        self.max_logs = max_logs
        self.max_image_bytes = max_image_bytes
        self.max_images = max_images
        self.max_source_bytes = max_source_bytes
        self.max_execution_log_bytes = max_execution_log_bytes
        self.max_delivery_log_bytes = max_delivery_log_bytes
        self._image_count = 0
        self._source_bytes = 0
        self.record = {
            "format_version": 1, "execution_id": self.execution_id, "run_id": self.execution_id,
            "started_at": utc_now(), "snapshot_state": "observed", "test_status": "running",
            "runner_outcome": "running", "capture_status": "recording", "features": [],
            "sources": {}, "errors": [], "exceptions": [], "attachments": [], "logs": [],
            "coverage": {"actions": "not_observed", "details": bool(detailed)},
        }
        self.current_feature = None
        self.current_scenario = None
        self._step = ContextVar("evidence_step_" + self.execution_id, default=None)
        self._steps = ContextVar("evidence_steps_" + self.execution_id, default=())
        self._feature_by_id = {}
        self._scenario_by_id = {}
        self._step_by_id = {}
        self._models = []
        self._origin_plan = None
        self._lock = RLock()
        self._started = monotonic()
        self._closed = False
        self._finalized = False
        self._action_count = 0
        self._seq = 0
        self._screenshot_steps = set()
        self._window_state = {}
        self._settled_action_ids = set()
        self.recorder = None
        self._record_started = False
        self._record_mode = "off"
        self._resources_started = False
        self._capture_settings = None
        self._log_sink = None
        self._log_capture = None
        self.delivery_name = None
        self.deliveries = {}
        self.cleanup_errors = []
        self._safe_persist()

    def start(self, configuration=None, *, media_factory=None):
        if self._resources_started or not self.active:
            return self
        self._resources_started = True
        if configuration is None:
            from config.settings import settings
            configuration = settings
        try:
            self._capture_settings = MappingProxyType({
                "record_enabled": configuration.record_enabled,
                "record_mode": configuration.effective_record_mode,
                "record_all": configuration.record_all,
                "log_file": configuration.log_file,
                "log_level": configuration.log_level,
                "log_rotation": configuration.log_rotation,
                "log_retention": configuration.log_retention,
            })
        except Exception as error:
            self.record_error("capture_settings", error)
            return self
        try:
            from .logging import install_log_capture
            self._log_sink = install_log_capture(self)
        except Exception as error:
            self.record_error("log_initialize", error)
        try:
            if media_factory is None:
                from autowork_core.utils.screenshot import ScreenRecorder
                media_factory = ScreenRecorder
            media_paths = {} if self.asset_directory is None else {
                "output_dir": self.asset_directory / "video",
                "screenshot_dir": self.asset_directory / "screenshots",
            }
            self.recorder = media_factory(
                fps=configuration.record_fps,
                monitor_index=configuration.record_monitor_index,
                buffer_seconds=configuration.record_buffer_seconds,
                segment_seconds=configuration.record_segment_seconds,
                max_width=configuration.record_max_width,
                ffmpeg_path=configuration.record_ffmpeg_path,
                ffmpeg_preset=configuration.record_ffmpeg_preset,
                ffmpeg_crf=configuration.record_ffmpeg_crf,
                ffmpeg_draw_mouse=configuration.record_ffmpeg_draw_mouse,
                **media_paths,
            )
        except Exception as error:
            self.record_error("media_initialize", error)
        return self

    @property
    def current_step(self):
        return self._step.get()

    @property
    def asset_directory(self):
        return None if self.directory is None else self.directory / "assets"

    @property
    def active(self):
        return not self._closed and not self._finalized

    def _identity(self, prefix, items):
        self._seq += 1
        return f"{prefix}/{len(items) + 1}"

    def _model_status(self, model, record):
        raw = status_text(getattr(model, "status", "unknown"))
        record.update(test_status=raw, rawStatus=raw, status=status_category(raw))

    def before_all(self, context):
        self.record["selection"] = captured_value({
            "paths": getattr(getattr(context, "config", None), "paths", None),
            "names": getattr(getattr(context, "config", None), "name", None),
            "tags": str(getattr(getattr(context, "config", None), "tags", "")),
        })

    def before_feature(self, context, feature):
        if id(feature) in self._feature_by_id:
            self.current_feature = self._feature_by_id[id(feature)]
            return
        self._origin_plan = EXECUTION_ORIGIN.get()
        source = getattr(self._origin_plan, "source_path", None) or report_source_path(getattr(feature, "filename", "")) or getattr(feature, "filename", "")
        source = self._relative_source(source)
        record = {
            "id": sha256(source.encode("utf-8")).hexdigest()[:16], "name": str(feature.name),
            "source": source, "execution_path": str(getattr(feature, "filename", "")),
            "partial_execution": bool(self._origin_plan or report_source_path(getattr(feature, "filename", ""))),
            "selection": captured_value(None if self._origin_plan is None else ({
                "scenario_name": self._origin_plan.scenario_name,
                "mode": getattr(self._origin_plan, "mode", "scenario_subset"),
                "step_index": getattr(self._origin_plan, "step_index", None),
                "example_id": self._origin_plan.example_id,
            } | (
                {"rule_name": self._origin_plan.rule_name}
                if getattr(self._origin_plan, "rule_name", None) is not None
                else {}
            ))),
            "tags": [str(tag) for tag in getattr(feature, "tags", ())],
            "declared_cases": [{"name": item.name, "line": item.line} for item in feature.walk_scenarios()],
            "scenarios": [], "test_status": "running", "_start": monotonic(),
        }
        self.record["features"].append(record)
        self._feature_by_id[id(feature)] = record
        self._models.append((feature, record))
        self.current_feature = record
        if self.detailed:
            try:
                self.remember_source(self.project_root / source)
            except Exception as error:
                self.record_error("feature_source", error)

    def after_feature(self, context, feature):
        record = self._feature_by_id.get(id(feature))
        if record is not None:
            self._model_status(feature, record)
            if record["status"] == "skipped" and bool(getattr(feature, "should_skip", False)):
                units = list(feature.walk_scenarios())
                record.update(skipScope="feature", skipReason=self._skip_reason(feature),
                              plannedUnits=len(units), plannedSteps=sum(len(list(item.all_steps)) for item in units))
            record["duration_seconds"] = monotonic() - record["_start"]
        self.current_feature = None
        self._safe_persist()

    def before_scenario(self, context, scenario):
        if id(scenario) in self._scenario_by_id:
            self.current_scenario = self._scenario_by_id[id(scenario)]
            return
        feature = self._feature_by_id.get(id(context.feature))
        if feature is None:
            self.before_feature(context, context.feature)
            feature = self.current_feature
        parent = getattr(scenario, "parent", None)
        row = getattr(scenario, "_row", None)
        scope = active_step_scope() or {}
        if self.detailed and not scope:
            from autowork_core.runtime.step_scope import resolved_step_scope_for_scenario
            scope = resolved_step_scope_for_scenario(context.feature, scenario, self.project_root / "Bdd/steps", require_files=False).runtime_scope()
        record = {
            "id": f"{feature['id']}/scenario-{len(feature['scenarios']) + 1}",
            "name": str(scenario.name), "line": getattr(scenario, "line", None),
            "tags": [str(tag) for tag in getattr(scenario, "effective_tags", ())],
            "parent": {"kind": type(parent).__name__, "name": getattr(parent, "name", None), "line": getattr(parent, "line", None)},
            "example": row_fact(row), "scope": captured_value(scope),
            "declared_steps": [step_fact(step) for step in scenario.all_steps],
            "steps": [], "attachments": [], "exceptions": [], "logs": [],
            "test_status": "running", "_start": monotonic(),
        }
        feature["scenarios"].append(record)
        self._scenario_by_id[id(scenario)] = record
        self._models.append((scenario, record))
        self.current_feature = feature
        self.current_scenario = record
        if self.detailed:
            try:
                original, origins = self._scenario_origin(scenario)
                record.update(source_scenario=original, step_origins=origins)
                if record["example"] is None:
                    record["example"] = original["example"]
                for source in scope.get("files", ()):
                    self.remember_source(self.project_root / "Bdd/steps" / source)
            except Exception as error:
                self.record_error("scenario_origin", error)
                record.update(source_scenario=None, step_origins=[])

    def before_step(self, context, step):
        scenario = self._scenario_by_id.get(id(context.scenario))
        if scenario is None:
            self.before_scenario(context, context.scenario)
            scenario = self.current_scenario
        key = (scenario["id"], id(step))
        if key in self._step_by_id:
            return
        identity = step_fact(step)
        identity.update(id=f"{scenario['id']}/step-{len(scenario['steps']) + 1}",
                        background=any(item is step or (item.line == step.line and item.name == step.name)
                                       for item in getattr(context.scenario, "background_steps", ())),
                        source_location={"path": self.current_feature["source"], "line": getattr(step, "line", None)},
                        attachments=[], exceptions=[], logs=[], _start=monotonic())
        origins = scenario.get("step_origins", ())
        positions = [index for index, item in enumerate(scenario["declared_steps"])
                     if item["line"] == identity["line"] and item["name"] == identity["name"]]
        if len(positions) == 1 and positions[0] < len(origins):
            origin = origins[positions[0]]
            identity["source_location"]["line"] = origin["line"]
            identity["background"] = origin["background"]
        record = self.start_step(identity)
        record["_model_id"] = id(step)
        scenario["steps"].append(record)
        self._step_by_id[key] = record
        self._models.append((step, record))

    def after_step(self, context, step, *, screenshot_path=None, screenshot_error=None):
        scenario = self._scenario_by_id.get(id(context.scenario))
        record = None if scenario is None else self._step_by_id.get((scenario["id"], id(step)))
        if record is None:
            return
        self._model_status(step, record)
        self._record_model_error(step, record)
        record["duration_seconds"] = max(0.0, monotonic() - record.get("_start", monotonic()))
        if screenshot_path:
            self.register_artifact(screenshot_path, node=record, name="失败截图", kind="image")
        if screenshot_error:
            record["screenshot_error"] = str(screenshot_error)
        if self.current_step is record:
            self._window_state.clear()
            self._step.set(self._steps.get()[-1] if self._steps.get() else None)
            self._steps.set(self._steps.get()[:-1])

    def after_scenario(self, context, scenario, record_path=None):
        self._window_state.clear()
        record = self._scenario_by_id.get(id(scenario))
        if record is None:
            return
        self._model_status(scenario, record)
        self._record_model_error(scenario, record)
        record["duration_seconds"] = max(0.0, monotonic() - record["_start"])
        if record["status"] == "skipped":
            record.update(skipScope="scenario", skipReason=self._skip_reason(scenario))
        recorded = {item.get("_model_id") for item in record["steps"]}
        for step in scenario.all_steps:
            if id(step) in recorded:
                continue
            missing = dict(step_fact(step), id=f"{record['id']}/planned-{len(record['steps']) + 1}",
                           actions=[], attachments=[], exceptions=[], logs=[], _model_id=id(step), executed=False)
            self._model_status(step, missing)
            if record["status"] == "failed" and missing["status"] in {"unknown", "skipped"}:
                missing["status"] = "skipped"
                missing["error"] = f"因前置步骤失败未执行: {step.keyword} {step.name}".strip()
            self._record_model_error(step, missing)
            record["steps"].append(missing)
            self._models.append((step, missing))
        order = {id(step): index for index, step in enumerate(scenario.all_steps)}
        record["steps"].sort(key=lambda item: order.get(item.get("_model_id"), 10**9))
        if record_path:
            self.register_artifact(record_path, node=record, name="场景失败录屏", kind="video")
        self._step.set(None)
        self._steps.set(())
        self.current_scenario = None
        self._safe_persist()

    def start_step(self, identity):
        parent = self.current_step
        record = dict(identity, actions=[], test_status="running")
        if parent is not None:
            record["parent_step_id"] = parent["id"]
        self._steps.set((*self._steps.get(), parent))
        self._step.set(record)
        return record

    def finish_step(self, status, exception=None):
        record = self.current_step
        if record is not None:
            record.update(test_status=status_text(status), rawStatus=status_text(status), status=status_category(status))
            if exception is not None:
                record["exception"] = self.exception_record(exception)
        self._window_state.clear()
        self._step.set(self._steps.get()[-1] if self._steps.get() else None)
        self._steps.set(self._steps.get()[:-1])

    def _record_model_error(self, model, record):
        error = getattr(model, "exception", None)
        message = getattr(model, "error_message", None)
        if error is not None:
            record["exception"] = self.exception_record(error)
            record["error"] = redacted(str(message)) if message else record["exception"]["message"]
            record["errorDetail"] = redacted("".join(traceback.format_exception(type(error), error, error.__traceback__)))
            diagnostic = runtime_diagnostic_payload(error)
            if diagnostic is not None:
                record["diagnostic"] = diagnostic
        elif message:
            record["error"] = redacted(message)
            record["errorDetail"] = redacted(message)
        elif status_text(getattr(model, "status", "")) in {"undefined", "pending"}:
            record["error"] = f"步骤未定义: {getattr(model, 'keyword', '')} {getattr(model, 'name', '')}".strip()
            record["errorDetail"] = record["error"]

    @staticmethod
    def exception_record(error, seen=None):
        seen = set() if seen is None else seen
        if id(error) in seen or len(seen) >= 12:
            return {"type": type(error).__name__, "message": "exception chain truncated"}
        seen.add(id(error))
        try:
            message = str(error)
        except Exception:
            message = "Exception message unavailable"
        result = {"type": f"{type(error).__module__}.{type(error).__qualname__}", "message": redacted(message)[:65536]}
        result["traceback"] = redacted("".join(traceback.format_exception(type(error), error, error.__traceback__, chain=False)))[:65536]
        cause = error.__cause__ or error.__context__
        if cause is not None:
            result["cause"] = RunEvidenceCollector.exception_record(cause, seen)
        if isinstance(error, BaseExceptionGroup):
            result["exceptions"] = [RunEvidenceCollector.exception_record(item, seen) for item in error.exceptions[:32]]
        return result

    def record_exception(self, stage, error, *, node=None):
        if self.active:
            record = self.exception_record(error)
            record.update(stage=stage, observed_at=utc_now())
            target = node if node is not None else self.current_step or self.current_scenario or self.record
            target.setdefault("exceptions", []).append(record)

    def record_error(self, stage, error):
        if not self._finalized:
            self.record["capture_status"] = "incomplete"
            if len(self.record["errors"]) < 200:
                self.record["errors"].append(dict(stage=stage, **self.exception_record(error)))

    def register_artifact(self, path, *, node=None, name="附件", kind="image", phase=None, region=None):
        if not self.active:
            return {"status": "not_requested", "reason": "collector is sealed"}
        active_action = ACTIVE_ACTION.get()
        target = node if node is not None else active_action[1] if active_action is not None and active_action[0] is self else self.current_step or self.current_scenario or self.record
        path = Path(path).resolve()
        relative = path.relative_to(self.project_root).as_posix() if path.is_relative_to(self.project_root) else str(path)
        record = {"artifact_id": uuid4().hex, "execution_id": self.execution_id,
                  "scope_id": target.get("id", self.execution_id), "name": name, "type": kind,
                  "path": relative, "observed_at": utc_now(), "status": "missing"}
        try:
            digest = sha256()
            size = 0
            with path.open("rb") as stream:
                if kind == "image" and path.stat().st_size > self.max_image_bytes:
                    raise OSError("Image evidence exceeds byte budget")
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    size += len(chunk)
                    digest.update(chunk)
            record.update(status="captured", size=size, sha256=digest.hexdigest())
        except OSError as error:
            record["error"] = self.exception_record(error)
            self.record_error("artifact", error)
        if phase is not None:
            record["phase"] = phase
        if region is not None:
            record["region"] = captured_value(region)
        if not any(item.get("path") == relative and item.get("phase") == phase for item in target.setdefault("attachments", [])):
            target["attachments"].append(record)
        return record

    def capture_failure(self, context, step):
        scenario = self._scenario_by_id.get(id(context.scenario))
        record = None if scenario is None else self._step_by_id.get((scenario["id"], id(step)))
        if record is None or record["id"] in self._screenshot_steps or not should_keep_artifacts(step.status):
            return
        self._screenshot_steps.add(record["id"])
        try:
            if self.recorder is None:
                raise RuntimeError("No run-owned screenshot provider")
            path = self.recorder.save_screenshot(
                f"{context.feature.name}_{context.scenario.name}_{step.name}_{scenario['id']}_{record['id']}"
            )
            self.register_artifact(path, node=record, name="失败截图")
        except Exception as error:
            record["screenshot_error"] = redacted(str(error))
            self.record_error("failure_screenshot", error)

    def invoke(self, operation, callback, *, arguments=None, owner=None, phases=("after",)):
        step = self.current_step
        if not self.active or step is None:
            return callback()
        if self._action_count >= self.max_actions:
            self.record_error("actions_limit", RuntimeError("Action capture limit reached"))
            return callback()
        self._action_count += 1
        self.record["coverage"]["actions"] = "partial_public_actions_only"
        parent = ACTIVE_ACTION.get()
        action = {"id": f"{step['id']}/action-{len(step['actions']) + 1}", "operation": operation,
                  "arguments": self._value(arguments or {}) if self.detailed else {"unavailable": "detail capture disabled"},
                  "owner": self._value(owner or {}), "started_at": utc_now(), "outcome": "running", "images": [], "attachments": []}
        if parent is not None and parent[0] is self:
            action["parent_action_id"] = parent[1]["id"]
        step["actions"].append(action)
        if self.detailed:
            if "before" in phases:
                for phase in phases:
                    if phase == "before":
                        self._observe_image(action, phase)
            elif not phases and parent is None and self._operation_group(operation) != "variable":
                for st_state in self._window_state.values():
                    if st_state.get("pending_image") is not None and st_state.get("pending_action") is not None:
                        self._settle_pending_image(st_state["pending_action"], "after", st_state)
                    st_state.update(
                        pending_action=None,
                        pending_image=None,
                        last_op_group=None,
                        last_captured_image=None,
                    )
        token = ACTIVE_ACTION.set((self, action))
        started = monotonic()
        action["called_at"] = utc_now()
        try:
            result = callback()
        except BaseException as error:
            action.update(outcome="exception", exception=self.exception_record(error))
            raise
        else:
            action["return_value"] = self._value(result) if self.detailed else {"unavailable": "detail capture disabled"}
            action["outcome"] = ("returned_false" if result is False else "returned_none" if result is None
                                 else "returned_empty" if isinstance(result, (str, list, tuple, dict)) and not result else "returned")
            return result
        finally:
            ACTIVE_ACTION.reset(token)
            action.update(finished_at=utc_now(), duration_seconds=monotonic() - started)
            if self.detailed:
                if "after" in phases:
                    self._observe_after_phase(step, action, phases)
                elif not phases and self._operation_group(operation) != "variable":
                    for st_state in self._window_state.values():
                        st_state["last_op_group"] = None
                        st_state["last_captured_image"] = None

    @staticmethod
    def _operation_group(operation):
        op = str(operation or "")
        if op in {"set_variable", "get_variable"}:
            return "variable"
        if op.startswith(("get_", "save_", "extract_")) or op == "read":
            return "read"
        if op.startswith("assert_"):
            return "assert"
        if op.startswith("wait_") or op == "wait":
            return "wait"
        return "mutate"

    @staticmethod
    def _action_scope_key(step, action):
        owner = action.get("owner") or {}
        view = str(owner.get("view") or "BasePage").rsplit(".", 1)[-1]
        cache_name = str(owner.get("window_cache_name") or "")
        if "::" in cache_name:
            cache_name = cache_name.rsplit("::", 1)[-1]
        if ":" in cache_name:
            cache_name = cache_name.split(":", 1)[0]
        root_loc = str(owner.get("root_locator") or "")
        win_loc_root = str((owner.get("window_locator") or {}).get("root_name") or "")
        win_key = (cache_name or root_loc or win_loc_root or "__default_window__").strip().lower()
        return (step.get("id", ""), view, win_key)

    def on_target_resolved(self, action, result):
        if not self.detailed or not self.active:
            return
        if result is None or result is False or result == []:
            return
        if id(action) in self._settled_action_ids:
            return
        self._settled_action_ids.add(id(action))
        step = self.current_step
        if step is None:
            return
        scope_key = self._action_scope_key(step, action)
        state = self._window_state.get(scope_key)
        if not state or state.get("pending_image") is None:
            return
        op_group = self._operation_group(action.get("operation"))
        if op_group in {"read", "assert", "wait"}:
            return
        self._settle_pending_image(action, "after", state)

    def _settle_pending_image(self, current_action, phase, state):
        pending_img = state.get("pending_image")
        if pending_img is None:
            return
        state["pending_action"] = None
        state["pending_image"] = None
        settled = self._capture_image_data(current_action, phase, record_error_on_fail=False)
        if settled is not None and settled.get("status") == "captured":
            self._replace_slot_image(pending_img, settled)

    def _observe_after_phase(self, step, action, phases):
        if "before" in phases:
            self._observe_image(action, "after")
            return
        scope_key = self._action_scope_key(step, action)
        state = self._window_state.setdefault(
            scope_key,
            {"pending_action": None, "pending_image": None, "last_op_group": None, "last_captured_image": None},
        )
        op_group = self._operation_group(action.get("operation"))
        if action.get("outcome") == "exception":
            self._observe_image(action, "after")
            state.update(pending_action=None, pending_image=None, last_op_group=None, last_captured_image=None)
            return
        has_target_res = action.get("target_resolution_count", 0) > 0
        if op_group in {"read", "assert"}:
            if (
                state.get("pending_image") is None
                and state.get("last_op_group") == op_group
                and state.get("last_captured_image") is not None
                and has_target_res
            ):
                action["images"].append(dict(state["last_captured_image"], phase="after"))
                return
            image = self._observe_image(action, "after")
            if image.get("status") == "captured":
                if state.get("pending_image") is not None and has_target_res:
                    self._replace_slot_image(state["pending_image"], dict(image))
                state.update(
                    pending_action=None,
                    pending_image=None,
                    last_op_group=op_group,
                    last_captured_image=dict(image),
                )
            else:
                state.update(pending_action=None, pending_image=None, last_op_group=None, last_captured_image=None)
            return
        if op_group == "wait":
            image = self._observe_image(action, "after")
            if image.get("status") == "captured":
                if state.get("pending_image") is not None and has_target_res:
                    self._replace_slot_image(state["pending_image"], dict(image))
                state.update(
                    pending_action=None,
                    pending_image=None,
                    last_op_group="wait",
                    last_captured_image=dict(image),
                )
            else:
                state.update(pending_action=None, pending_image=None, last_op_group=None, last_captured_image=None)
            return
        image = self._observe_image(action, "after")
        if image.get("status") == "captured":
            state.update(
                pending_action=action,
                pending_image=image,
                last_op_group="mutate",
                last_captured_image=None,
            )
        else:
            state.update(pending_action=None, pending_image=None, last_op_group=None, last_captured_image=None)

    def _is_digest_referenced(self, digest):
        for feature in self.record.get("features", ()):
            for scenario in feature.get("scenarios", ()):
                for st in scenario.get("steps", ()):
                    for act in st.get("actions", ()):
                        for img in act.get("images", ()):
                            if img.get("sha256") == digest:
                                return True
        step = self.current_step
        if step is not None:
            for act in step.get("actions", ()):
                for img in act.get("images", ()):
                    if img.get("sha256") == digest:
                        return True
        return False

    def _replace_slot_image(self, slot_image, new_image_data):
        old_digest = slot_image.get("sha256")
        old_path = slot_image.get("path")
        slot_image.clear()
        slot_image.update(new_image_data)
        if old_digest and old_digest != new_image_data.get("sha256") and old_path and self.directory is not None:
            if not self._is_digest_referenced(old_digest):
                orphan = self.directory / old_path
                try:
                    if orphan.is_file():
                        orphan.unlink()
                        self._image_count = max(0, self._image_count - 1)
                except OSError:
                    pass

    def _value(self, value):
        try:
            return captured_value(value)
        except Exception as error:
            self.record_error("serialize", error)
            return {"unavailable": "serialization failed"}

    def _capture_image_data(self, action, phase, *, record_error_on_fail=True):
        image = {"phase": phase, "observed_at": utc_now(), "status": "missing"}
        try:
            if self.image_provider is None or self.directory is None:
                raise RuntimeError("No confirmed window region is available")
            if self._image_count >= self.max_images:
                raise ValueError("Image observation budget exceeded")
            self._image_count += 1
            payload, region = self.image_provider(action, phase)
            image["observed_at"] = region.get("captured_at", image["observed_at"])
            if len(payload) > self.max_image_bytes:
                raise ValueError("Image evidence exceeds byte budget")
            from PIL import Image
            with Image.open(BytesIO(payload)) as decoded:
                if decoded.format != "PNG" or decoded.size != (region["width"], region["height"]):
                    raise ValueError("Invalid PNG capture or region dimensions")
                decoded.verify()
            digest = sha256(payload).hexdigest()
            relative = f"images/{digest}.png"
            destination = self.directory / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                destination.write_bytes(payload)
            image.update(status="captured", path=relative, sha256=digest, region=captured_value(region))
        except Exception as error:
            if record_error_on_fail:
                image["error"] = self.exception_record(error)
                self.record_error(f"image:{action['id']}:{phase}", error)
            else:
                return None
        return image

    def _observe_image(self, action, phase):
        image = self._capture_image_data(action, phase, record_error_on_fail=True)
        action["images"].append(image)
        return image

    def remember_source(self, path):
        path = Path(path).resolve()
        relative = path.relative_to(self.project_root).as_posix()
        if relative not in self.record["sources"]:
            payload = path.read_bytes()
            if len(payload) > 2 * 1024 * 1024:
                raise ValueError("Source snapshot exceeds limit")
            if self._source_bytes + len(payload) > self.max_source_bytes:
                raise ValueError("Total source snapshot budget exceeded")
            self._source_bytes += len(payload)
            self.record["sources"][relative] = {"path": relative, "sha256": sha256(payload).hexdigest(), "text": payload.decode("utf-8-sig")}
        return relative

    def record_log(self, message, *, level="INFO", source=None):
        if not self.active:
            return
        active_action = ACTIVE_ACTION.get()
        target = active_action[1] if active_action is not None and active_action[0] is self else self.current_step or self.current_scenario or self.current_feature or self.record
        if len(self.record["logs"]) >= self.max_logs:
            coverage = self.record["coverage"]
            coverage["logs_dropped"] = coverage.get("logs_dropped", 0) + 1
            if coverage["logs_dropped"] == 1:
                self.record_error("logs_limit", RuntimeError("Log capture limit reached"))
            return
        text = redacted(message)
        truncated = len(text) > 4096
        if truncated:
            coverage = self.record["coverage"]
            coverage["logs_truncated"] = coverage.get("logs_truncated", 0) + 1
            if coverage["logs_truncated"] == 1:
                self.record_error("log_text_limit", RuntimeError("Log message exceeds character budget"))
        self.record["logs"].append({"execution_id": self.execution_id,
                        "scope_id": target.get("id", self.execution_id),
                        "observed_at": utc_now(), "level": level,
                        "source": captured_value(source or {}), "message": text[:4096], "truncated": truncated})

    def snapshot(self, scope=None):
        with self._lock:
            if scope is None or scope == self.execution_id:
                return plain_record(self.record)
            for _, record in self._models:
                if record.get("id") == scope:
                    result = plain_record(record)
                    result["logs"] = [plain_record(item) for item in self.record["logs"]
                                      if item["scope_id"] == scope or item["scope_id"].startswith(scope + "/")]
                    return result
                for action in record.get("actions", ()):
                    if action["id"] == scope:
                        result = plain_record(action)
                        result["logs"] = [plain_record(item) for item in self.record["logs"] if item["scope_id"] == scope]
                        return result
        raise KeyError(scope)

    def refresh_model_statuses(self):
        for model, record in self._models:
            self._model_status(model, record)
            self._record_model_error(model, record)

    def finalize_run(self, outcome, error=None):
        if self._finalized:
            return
        self.refresh_model_statuses()
        if error is not None:
            self.record_exception("runner", error, node=self.record)
        self.record.update(runner_outcome=outcome, test_status=outcome,
                           finished_at=utc_now(), duration_seconds=monotonic() - self._started,
                           snapshot_state="finalized")
        if error is not None and not self.record["features"]:
            self.record["snapshot_state"] = "incomplete"
            self.record_error("execution_not_started", error)
        if self._log_capture is not None:
            self._log_capture.seal_execution()
        self.record["capture_status"] = "incomplete" if self.record["errors"] else "complete"
        self._safe_persist()
        self._finalized = True

    def _safe_persist(self):
        if self.directory is None:
            return
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            destination = self.directory / "run.json"
            temporary = self.directory / "run.json.tmp"
            temporary.write_text(json.dumps(self.snapshot(), ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
            temporary.replace(destination)
        except Exception as error:
            self.record_error("persist", error)
            self.record["persistence_failed"] = True

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._log_sink is not None:
            from loguru import logger
            try:
                logger.remove(self._log_sink)
            except Exception as error:
                self._cleanup_error("log_remove", error)
            self._log_sink = None
        if self._log_capture is not None:
            for operation in (self._log_capture.stop, self._log_capture.restore):
                try:
                    operation()
                except Exception as error:
                    self._cleanup_error("log_close", error)
        if self.recorder is not None and getattr(self.recorder, "_running", False):
            try:
                self.recorder.stop()
            except Exception as error:
                self._cleanup_error("media_close", error)
        if self._log_capture is not None:
            self._log_capture._persist_delivery()

    def _cleanup_error(self, stage, error):
        if not self._finalized:
            self.record_error(stage, error)
        if len(self.cleanup_errors) < 20:
            self.cleanup_errors.append({"stage": stage, **self.exception_record(error)})
        try:
            import sys
            sys.stderr.write(f"Evidence cleanup {stage} unavailable: {type(error).__name__}\n")
        except Exception:
            pass

    def start_recording(self, context, scenario, state):
        from config.settings import settings
        configuration = self._capture_settings if self is not None else {
            "record_enabled": settings.record_enabled,
            "record_mode": settings.effective_record_mode,
        }
        if configuration is None or not configuration["record_enabled"]:
            return
        recorder = self.recorder if self is not None else context.autowork_run.recorder
        if recorder is None:
            self.record_error("recording", RuntimeError("Run media is unavailable"))
            return
        state.record_path = recorder.start(context.feature.name, scenario.name, mode=configuration["record_mode"])
        state.record_started = bool(getattr(recorder, "_running", False))

    def finish_recording(self, context, scenario, state):
        from config.settings import settings
        configuration = self._capture_settings if self is not None else {
            "record_enabled": settings.record_enabled,
            "record_all": settings.record_all,
        }
        if configuration is None or not configuration["record_enabled"] or not state.record_started:
            return None
        recorder = self.recorder if self is not None else context.autowork_run.recorder
        state.record_started = False
        if configuration["record_all"]:
            path = recorder.stop()
            if should_keep_artifacts(scenario.status):
                return path
            recorder.delete()
        else:
            recorder.stop()
            if should_keep_artifacts(scenario.status):
                return recorder.save_buffered_video()
            recorder.clear()
        return None

    def _relative_source(self, path):
        path = Path(path).resolve()
        return path.relative_to(self.project_root).as_posix() if path.is_relative_to(self.project_root) else str(path)

    @staticmethod
    def _skip_reason(model):
        from autowork_core.runtime.tag_manager import SKIP_TAG_PRIORITY
        reason = str(getattr(model, "skip_reason", "") or "").strip()
        tags = {str(item).lstrip("@").casefold() for item in getattr(model, "effective_tags", ())}
        return reason or next((item for item in SKIP_TAG_PRIORITY if item in tags), "skipped")

    def _scenario_origin(self, scenario):
        source = scenario
        selected_steps = list(scenario.all_steps)
        plan = self._origin_plan
        if plan is not None:
            from behave.parser import Parser
            parsed = Parser().parse(self.record["sources"][self.current_feature["source"]]["text"])
            if getattr(plan, "mode", None) == "rule_subset":
                scenario_row = getattr(scenario, "_row", None)
                scenario_parent = getattr(scenario, "parent", None)
                scenario_template = scenario_parent if type(scenario_parent).__name__ == "ScenarioOutline" else scenario
                candidates = []
                for concrete in parsed.walk_scenarios():
                    parent = getattr(concrete, "parent", None)
                    template = parent if type(parent).__name__ == "ScenarioOutline" else concrete
                    rule = getattr(template, "parent", None)
                    if type(rule).__name__ != "Rule" or rule.name != plan.rule_name:
                        continue
                    if getattr(plan, "rule_line", None) is not None and rule.line != plan.rule_line:
                        continue
                    row = getattr(concrete, "_row", None)
                    row_matches = (
                        (scenario_row is None and row is None)
                        or (scenario_row is not None and row is not None and row.id == scenario_row.id)
                    )
                    if template.name == scenario_template.name and concrete.name == scenario.name and row_matches:
                        candidates.append(concrete)
                if len(candidates) != 1:
                    raise ValueError("Selected Rule Plan does not uniquely identify original Scenario/Example")
                source = candidates[0]
                selected_steps = list(source.all_steps)
            else:
                case = next((item for item in plan.cases if item.name == scenario.name), None) if hasattr(plan, "cases") else None
                if hasattr(plan, "cases") and case is None:
                    raise ValueError("Observed scenario is not in the selected subset Plan")
                candidates = []
                for concrete in parsed.walk_scenarios():
                    parent = getattr(concrete, "parent", None)
                    template = parent if type(parent).__name__ == "ScenarioOutline" else concrete
                    rule = getattr(template, "parent", None)
                    if getattr(plan, "rule_name", None) is not None:
                        if type(rule).__name__ != "Rule" or rule.name != plan.rule_name:
                            continue
                    if getattr(plan, "scenario_line", None) is not None and template.line != plan.scenario_line:
                        continue
                    row = getattr(concrete, "_row", None)
                    if case is not None:
                        name = template.name if row is None else f"{template.name} -- @{row.id}"
                        matches = template.name == plan.scenario_name and name == case.name
                    else:
                        matches = concrete.name == plan.scenario_name and (plan.example_id is None or (row is not None and row.id == plan.example_id))
                    if matches:
                        candidates.append(concrete)
                if len(candidates) != 1:
                    raise ValueError("Selected Plan does not uniquely identify original Scenario/Example")
                source = candidates[0]
                selected_steps = list(plan.background_steps) + list(case.steps) if case is not None else list(plan.steps)
        parent = getattr(source, "parent", None)
        template = parent if type(parent).__name__ == "ScenarioOutline" else source
        rule = getattr(template, "parent", None)
        original = {"name": template.name, "line": template.line, "concrete_name": source.name,
                    "rule": {"name": rule.name, "line": rule.line} if type(rule).__name__ == "Rule" else None,
                    "example": row_fact(getattr(source, "_row", None))}
        background = {(step.line, step.name) for step in getattr(source, "background_steps", ())}
        origins = [{"line": step.line, "name": step.name, "step_type": step.step_type,
                    "background": (step.line, step.name) in background} for step in selected_steps]
        if plan is None and self.current_feature["partial_execution"]:
            origins = []
        return original, origins


class UnavailableEvidence:
    def __init__(self, error):
        self.execution_id = uuid4().hex
        self.asset_directory = None
        self.directory = None
        self.active = False
        self.detailed = False
        self.current_step = None
        self.current_scenario = None
        self.current_feature = None
        self.recorder = None
        self._finalized = False
        self.deliveries = {}
        self._record = {
            "execution_id": self.execution_id, "started_at": utc_now(),
            "runner_outcome": "running", "capture_status": "unavailable",
            "snapshot_state": "incomplete", "features": [], "logs": [], "sources": {},
            "errors": [dict(stage="initialize", **RunEvidenceCollector.exception_record(error))],
        }

    def snapshot(self, scope=None):
        return plain_record(self._record)

    def finalize_run(self, outcome, error=None):
        self._record["runner_outcome"] = outcome
        self._finalized = True

    def __getattr__(self, name):
        if name in {"before_all", "before_feature", "after_feature", "before_scenario", "after_scenario",
                    "before_step", "after_step", "capture_failure", "record_exception", "record_error",
                    "start", "start_recording", "finish_recording", "close"}:
            return lambda *args, **kwargs: None
        raise AttributeError(name)