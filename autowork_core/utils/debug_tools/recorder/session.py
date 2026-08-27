from __future__ import annotations

import atexit
import json
import os
import shutil
import tempfile
import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from functools import wraps
from pathlib import Path

import win32gui
import win32process
from mss import mss
from mss.tools import to_png

from autowork_core.utils.debug_tools.recorder.analysis import (
    build_locator_bundle,
    derive_actions,
)
from autowork_core.utils.debug_tools.recorder.annotations import (
    ANNOTATION_MODEL_VERSION,
    RecordingAnnotationRepository,
    SYSTEM_INFERRED_INTENT,
    USER_DECLARED_INTENT,
)
from autowork_core.utils.debug_tools.recorder.bundle_validator import validate_ai_bundle
from autowork_core.utils.debug_tools.recorder.capability import mark_capabilities_stale
from autowork_core.utils.debug_tools.recorder.capture_runtime import CaptureRuntime
from autowork_core.utils.debug_tools.recorder.input_capture import InputCaptureEngine
from autowork_core.utils.debug_tools.recorder.identity import (
    compact_feature_directory_name,
    compact_run_directory_name,
    compact_scenario_directory_name,
    compact_step_directory_name,
    feature_directory_name,
    minimal_feature_directory_name,
    minimal_scenario_directory_name,
    minimal_step_directory_name,
    run_directory_name,
    scenario_directory_name,
    stable_digest,
    step_directory_name,
)
from autowork_core.utils.debug_tools.recorder.models import (
    SCHEMA_VERSION,
    FeaturePlan,
    ScenarioPlan,
    StepPlan,
    StepTake,
    public_dict,
)
from autowork_core.utils.debug_tools.recorder.observation_providers import (
    default_observation_intent,
)
from autowork_core.utils.debug_tools.recorder.projection_store import (
    ProjectionStore,
)
from autowork_core.utils.debug_tools.recorder.session_projection import (
    SessionProjectionBuilder,
    SessionProjectionSource,
    find_recording_output_root,
)
from autowork_core.utils.debug_tools.recorder.tree_snapshot import (
    capture_tree_snapshot,
    diff_tree_snapshots,
)
from autowork_core.utils.debug_tools.recorder.timeline import TimelineStore
from autowork_core.utils.debug_tools.recorder.supplement_repository import (
    SupplementRepository,
)
from autowork_core.utils.debug_tools.recorder.run_lock import RunWriteLock
from autowork_core.utils.debug_tools.recorder.video import StepVideoRecorder
from autowork_core.utils.debug_tools.recorder.writer import RecordingSessionWriter
from autowork_core.utils.debug_tools.recorder.window_identity import (
    is_recordable_window_handle,
    list_top_level_windows,
    restore_window_handles,
    window_identity_for_handle,
)
from config.paths import Paths


WINDOWS_LEGACY_PATH_LIMIT = 259
MAX_REVIEW_TEXT_LENGTH = 2000


@dataclass(frozen=True)
class RecordingSessionConfig:
    backend: str = "uia"
    output_root: Path | None = None
    with_video: bool = True
    with_screenshots: bool = True
    with_tree: bool = True
    monitor_index: int = 1
    tree_max_depth: int = 12
    tree_max_nodes: int = 1200
    target_window_handle: int | None = None
    target_window_title: str | None = None
    target_window_handles: tuple[int, ...] = ()
    target_window_titles: tuple[str, ...] = ()
    target_window_identities: tuple[dict, ...] = ()
    capture_target_process_only: bool = True
    window_capture_mode: str = "strict"
    minimize_window: bool = True


def _session_locked(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._state_lock:
            return method(self, *args, **kwargs)

    return wrapped


class FeatureRecordingSession:
    def __init__(self, feature_plan, scenario_plan, selected_step_ids, config=None):
        self.feature_plan = feature_plan
        self.scenario_plan = scenario_plan
        self.config = config or RecordingSessionConfig()
        self._state_lock = threading.RLock()
        selected = set(selected_step_ids)
        self.selected_steps = tuple(step for step in scenario_plan.steps if step.id in selected)
        if not self.selected_steps:
            raise ValueError("至少选择一个 Step")
        unknown = selected - {step.id for step in scenario_plan.steps}
        if unknown:
            raise KeyError(f"选择了不存在的 Step: {sorted(unknown)}")

        output_root = Path(self.config.output_root or (Paths.ARTIFACTS_DIR / "recording_sessions"))
        self.output_root = output_root.resolve()
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        self.run_id = f"run_{timestamp}"
        self.path_mode = "readable"
        self.session_dir = _unique_session_dir(
            self.output_root
            / feature_directory_name(
                feature_plan.name,
                feature_id=feature_plan.id,
            )
            / scenario_directory_name(
                scenario_plan.name,
                scenario_plan.example_id,
                scenario_id=scenario_plan.id,
            )
            / run_directory_name(timestamp)
        )
        if _session_paths_too_long(
                self.session_dir,
                self.selected_steps,
                mode=self.path_mode,
        ):
            self.path_mode = "compact"
            self.session_dir = _unique_session_dir(
                self.output_root
                / compact_feature_directory_name(
                    feature_plan.name,
                    feature_plan.id,
                )
                / compact_scenario_directory_name(
                    scenario_plan.name,
                    scenario_plan.id,
                    scenario_plan.example_id,
                )
                / compact_run_directory_name(timestamp)
            )
        if _session_paths_too_long(
                self.session_dir,
                self.selected_steps,
                mode=self.path_mode,
        ):
            self.path_mode = "minimal"
            self.session_dir = _unique_session_dir(
                self.output_root
                / minimal_feature_directory_name(feature_plan.id)
                / minimal_scenario_directory_name(
                    scenario_plan.id,
                    scenario_plan.example_id,
                )
                / compact_run_directory_name(timestamp)
            )
        self.compact_paths = self.path_mode != "readable"
        self._existing_session = False
        self.annotation_model_version = ANNOTATION_MODEL_VERSION
        self.run_id = "run-" + stable_digest(
            self.session_dir.relative_to(self.output_root).as_posix(),
            length=12,
        )
        _validate_session_paths(
            self.session_dir,
            self.selected_steps,
            mode=self.path_mode,
        )
        self.writer = RecordingSessionWriter(self.session_dir)
        self._run_lock = RunWriteLock(self.session_dir).acquire()
        self.created_at = datetime.now().isoformat(timespec="seconds")
        self.updated_at = self.created_at
        self.finalized_at = None
        self.closed_at = None
        self.step_states = {
            step.id: {
                "status": "pending",
                "takes": [],
                "selected_take": None,
                "take_summary": "",
                "skip_reason": "",
            }
            for step in self.selected_steps
        }
        self.active = None
        self.latest_readiness = None
        self._atexit_callback = self.close
        atexit.register(self._atexit_callback)
        try:
            self.writer.initialize(
                feature_plan,
                scenario_plan,
                self.selected_steps,
                public_dict(self.config),
            )
            self._write_outputs()
        except Exception:
            try:
                atexit.unregister(self._atexit_callback)
            except Exception:
                pass
            self._atexit_callback = None
            self._run_lock.release()
            self._run_lock = None
            raise

    @classmethod
    def open_existing(cls, session_dir):
        session_dir = Path(session_dir).resolve()
        manifest_path = session_dir / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"录制会话 manifest.json 不存在: {manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(
                f"当前 Recorder 只支持 schema {SCHEMA_VERSION}；旧 Run "
                "需要使用旧版本或独立离线迁移工具"
            )
        if manifest.get("annotation_model_version") != ANNOTATION_MODEL_VERSION:
            raise ValueError(
                f"当前 Recorder 只支持 Annotation Model "
                f"{ANNOTATION_MODEL_VERSION}；旧 Run 需要使用旧版本或"
                "独立离线迁移工具"
            )
        scenario_data = manifest.get("scenario") or {}
        manifest_steps = manifest.get("steps") or []
        step_data = [entry.get("plan") or {} for entry in manifest_steps]
        selected_steps = tuple(
            _step_plan_from_dict(item) for item in step_data
        )
        scenario_steps = tuple(
            _step_plan_from_dict(item)
            for item in (
                scenario_data.get("steps")
                or step_data
            )
        )
        scenario = ScenarioPlan(
            id=str(scenario_data["id"]),
            key=str(scenario_data["key"]),
            logical_template_id=str(
                scenario_data.get("logical_template_id")
                or scenario_data["id"]
            ),
            name=str(scenario_data.get("name") or ""),
            line=int(scenario_data.get("line") or 0),
            kind=str(scenario_data.get("kind") or "scenario"),
            example_id=scenario_data.get("example_id"),
            example_values=dict(scenario_data.get("example_values") or {}),
            tags=tuple(scenario_data.get("tags") or ()),
            steps=scenario_steps,
            specification=deepcopy(
                scenario_data.get("specification") or {}
            ),
        )
        feature_data = manifest.get("feature") or {}
        source_path = session_dir / str(manifest.get("source_feature") or "source.feature")
        feature = FeaturePlan(
            id=str(feature_data["id"]),
            key=str(feature_data["key"]),
            source_path=source_path,
            source_relpath=str(feature_data.get("source_relpath") or source_path),
            source_hash=str(manifest.get("source_hash") or ""),
            name=str(feature_data.get("name") or ""),
            line=int(feature_data.get("line") or 0),
            tags=tuple(feature_data.get("tags") or ()),
            scenarios=(scenario,),
            description=tuple(feature_data.get("description") or ()),
        )
        config = _recording_config_from_manifest(manifest)

        self = cls.__new__(cls)
        self.feature_plan = feature
        self.scenario_plan = scenario
        self.config = config
        self._state_lock = threading.RLock()
        self.selected_steps = selected_steps
        self.session_dir = session_dir
        self.output_root = find_recording_output_root(session_dir, config.output_root)
        self.run_id = str(manifest["session_id"])
        self.path_mode = "existing"
        self.compact_paths = False
        self.writer = RecordingSessionWriter(session_dir)
        self.created_at = manifest.get("created_at")
        self.updated_at = manifest.get("updated_at")
        self.finalized_at = manifest.get("finalized_at")
        self.closed_at = manifest.get("closed_at")
        self._recorded_environment = deepcopy(manifest.get("environment") or {})
        self.annotation_model_version = manifest.get(
            "annotation_model_version"
        )
        self.step_states = {}
        for entry in manifest_steps:
            state = {
                key: deepcopy(entry.get(key))
                for key in ("status", "takes", "selected_take")
            }
            state["take_summary"] = str(
                entry.get("take_summary") or ""
            )
            state["skip_reason"] = str(
                entry.get("skip_reason") or ""
            )
            self.step_states[entry["plan"]["id"]] = state
        self.active = None
        self.latest_readiness = None
        self._atexit_callback = None
        self._run_lock = None
        self._existing_session = True
        return self

    @property
    def is_recording(self):
        return self.active is not None

    @property
    def is_paused(self):
        return bool(
            self.active is not None
            and self.active["runtime"].is_paused
        )

    @property
    def is_finalized(self):
        return self.finalized_at is not None

    @property
    def is_closed(self):
        return self.closed_at is not None

    def selected_take_entry(self, step_id, *, require_directory=False):
        state = self.step_states.get(step_id) or {}
        selected_take_id = state.get("selected_take")
        take = next(
            (
                item
                for item in state.get("takes") or ()
                if item.get("id") == selected_take_id
                and item.get("status") == "completed"
            ),
            None,
        )
        if take is None:
            return None
        if require_directory and not (
                self.session_dir / str(take.get("path") or "")
        ).is_dir():
            return None
        return take

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    @_session_locked
    def start_step(
            self,
            step_id,
            target_window_handles=None,
            primary_window_handle=None,
            window_capture_mode=None,
            target_hover=False,
            hover_notification=None,
        ):
        if self.is_closed:
            raise RuntimeError("会话已经关闭")
        if self.is_finalized:
            raise RuntimeError("录制任务已经完成，请重新选择范围后继续录制")
        if self.active is not None:
            raise RuntimeError("已有 Step 正在录制，请先完成或取消")
        step = self._step(step_id)
        state = self.step_states[step_id]
        step_dir = self.step_dir(step)
        take_number = len(state["takes"]) + 1
        while (step_dir / "takes" / f"take-{take_number:03d}").exists():
            take_number += 1
        take_dir = step_dir / "takes" / f"take-{take_number:03d}"
        _validate_take_path(take_dir)
        take_dir.mkdir(parents=True, exist_ok=True)
        started_monotonic = time.monotonic()
        take = StepTake(
            id=f"{step.id}-take-{take_number:03d}",
            step_id=step.id,
            take_number=take_number,
            started_at=datetime.now().isoformat(timespec="milliseconds"),
            started_monotonic=started_monotonic,
            directory=take_dir,
        )
        capture_mode = str(
            window_capture_mode or self.config.window_capture_mode or "strict"
        ).strip().lower()
        if capture_mode not in {"auto", "strict"}:
            raise ValueError(f"未知窗口采集模式: {capture_mode}")
        configured_handles = tuple(
            self.config.target_window_handles
            if target_window_handles is None
            else target_window_handles
        )
        if configured_handles or capture_mode == "strict":
            window_handles, window_handle = self._resolve_target_window_handles(
                configured_handles,
                primary_window_handle,
            )
        else:
            window_handles, window_handle = (), None
        target_windows = [_window_identity(handle) for handle in window_handles]
        target_window = next(
            (
                item
                for item in target_windows
                if item.get("handle") == window_handle
            ),
            {},
        )
        runtime = None
        try:
            window_evidence = self._capture_window_evidence_before(
                take_dir,
                target_windows,
                window_handle,
            )
            before_tree = next(
                (
                    item["before_tree_data"]
                    for item in window_evidence
                    if item["primary"]
                ),
                _empty_tree_snapshot(None, "waiting_for_first_business_window"),
            )
            active = {
                "step": step,
                "take": take,
                "window_handle": window_handle,
                "window_handles": tuple(window_handles),
                "discovered_window_handles": list(window_handles),
                "target_window": target_window,
                "target_windows": target_windows,
                "window_evidence": window_evidence,
                "before_tree": before_tree,
                "runtime": None,
                "previous_status": state["status"],
                "pauses": [],
                "active_pause": None,
                "observation_receipts": {},
                "observation_intent_errors": [],
                "window_capture_mode": capture_mode,
            }
            runtime = CaptureRuntime(
                take_dir,
                backend=self.config.backend,
                with_video=self.config.with_video,
                with_screenshots=self.config.with_screenshots,
                monitor_index=self.config.monitor_index,
                allowed_process_ids=(
                    tuple({
                        window.get("process_id")
                        for window in target_windows
                        if window.get("process_id") is not None
                    })
                    if self.config.capture_target_process_only
                    else ()
                ),
                selected_window_handles=window_handles,
                window_capture_mode=capture_mode,
                on_window_discovered=lambda lifecycle: (
                    self._capture_discovered_window(active, lifecycle)
                ),
                process_filter_enabled=bool(
                    self.config.capture_target_process_only
                ),
                video_title=self.feature_plan.name,
                video_label=f"{step.ordinal:03d}_{step.text}",
                capture_factory=InputCaptureEngine,
                video_factory=StepVideoRecorder,
                target_hover=bool(target_hover),
                hover_notification=hover_notification,
            )
            active["runtime"] = runtime
            self.active = active
            runtime.start()
        except Exception:
            if runtime is not None:
                runtime.abort()
            self.active = None
            raise
        state["status"] = "recording"
        self._touch()
        self._write_outputs()
        return take

    @_session_locked
    def finish_step(self, take_summary=""):
        take_summary = _bounded_review_text(
            take_summary,
            "Take总结",
        )
        return self._close_active(
            "completed",
            take_summary=take_summary,
        )

    @_session_locked
    def cancel_step(self, reason=""):
        reason = _bounded_review_text(reason, "丢弃原因")
        return self._close_active(
            "discarded",
            discard_reason=reason,
        )

    @_session_locked
    def capture_observation(self, point=None, note="", provider=None):
        if self.active is None:
            raise RuntimeError("当前没有正在录制的 Step")
        return self.active["runtime"].record_observation(
            point=point,
            note=note,
            provider=provider,
        )

    @_session_locked
    def save_step_user_context(
            self,
            step_id,
            *,
            business_context=None,
            purpose=None,
            constraints=None,
            expected_revision,
        ):
        if self.active is not None:
            raise RuntimeError("录制进行中不能修改Step业务说明")
        self._step(step_id)
        transient_lock = None
        if self._run_lock is None:
            transient_lock = RunWriteLock(self.session_dir).acquire()
        try:
            running = self._step_workflow_request_ids(
                step_id,
                status="running",
            )
            if running:
                raise RuntimeError(
                    "当前Step已有生成事务运行，不能修改业务说明: "
                    + ", ".join(running)
                )
            context = RecordingAnnotationRepository(
                self.session_dir
            ).append_step_user_context(
                step_id,
                business_context=business_context,
                purpose=purpose,
                constraints=constraints,
                expected_revision=expected_revision,
            )
            self._mark_step_generation_requests_stale(
                step_id,
                annotation_revision=context["revision"],
            )
            state = self.step_states[step_id]
            selected_take = next((
                take
                for take in state.get("takes") or ()
                if take.get("id") == state.get("selected_take")
            ), None)
            if selected_take is not None:
                mark_capabilities_stale(
                    self.output_root,
                    self.run_id,
                    step_id,
                    reason="step_user_context_changed",
                    current_take_id=selected_take.get("id"),
                    current_timeline_revision=selected_take.get(
                        "timeline_revision"
                    ),
                )
            self._touch()
            readiness = self._write_outputs()
            return {
                "step_user_context": context,
                "readiness": readiness,
            }
        finally:
            if transient_lock is not None:
                transient_lock.release()

    def drain_window_notifications(self):
        return self._drain_capture_notifications("drain_window_notifications")

    @_session_locked
    def drain_observation_notifications(self):
        receipts = self._drain_capture_notifications(
            "drain_observation_notifications"
        )
        return self._record_observation_receipts(receipts)

    @_session_locked
    def save_observation_intent(
            self,
            event_id,
            *,
            focus="auto",
            relation="auto",
            expected_source=None,
            property_name=None,
            business_meaning="",
            expected_revision,
        ):
        if self.active is None:
            raise RuntimeError("当前没有正在录制的Step")
        event_id = str(event_id or "").strip()
        receipt = self.active["observation_receipts"].get(event_id)
        if not receipt or receipt.get("status") not in {"captured", "warning"}:
            raise ValueError("ObservationIntent只能绑定已完成的F9目标采集")
        step = self.active["step"]
        take = self.active["take"]
        return RecordingAnnotationRepository(
            self.session_dir
        ).append_observation_intent(
            step.id,
            take.id,
            event_id,
            focus=focus,
            relation=relation,
            expected_source=expected_source,
            property_name=property_name,
            business_meaning=business_meaning,
            expected_revision=expected_revision,
        )

    @_session_locked
    def revise_observation_intent(
            self,
            step_id,
            owner_take_id,
            take_id,
            event_id,
            *,
            focus,
            relation,
            expected_source,
            property_name=None,
            business_meaning="",
            expected_revision,
        ):
        if self.active is not None:
            raise RuntimeError("录制进行中不能修改已保存的F9观察")
        transient_lock = None
        if self._run_lock is None:
            transient_lock = RunWriteLock(self.session_dir).acquire()
        try:
            self._step(step_id)
            running = self._step_workflow_request_ids(
                step_id,
                status="running",
            )
            if running:
                raise RuntimeError(
                    "当前Step已有生成事务运行，不能修改F9观察: "
                    + ", ".join(running)
                )
            state = self.step_states[step_id]
            take = next((
                item
                for item in state.get("takes") or ()
                if str(item.get("id") or "") == str(owner_take_id)
                and item.get("status") == "completed"
            ), None)
            if take is None:
                raise ValueError("F9观察所属Take不存在或未完成")
            take_dir = (
                self.session_dir / str(take.get("path") or "")
            ).resolve()
            try:
                take_dir.relative_to(self.session_dir)
            except ValueError as error:
                raise ValueError("F9观察所属Take路径越界") from error
            event = next((
                item
                for item in TimelineStore(take_dir).effective_events()
                if str(item.get("id") or "") == str(event_id)
                and item.get("event_type") == "observation"
            ), None)
            if event is None:
                raise ValueError("只能修改当前有效时间线中的F9观察")
            supplement_id = str(
                ((event.get("details") or {}).get("supplement") or {}).get(
                    "supplement_id"
                )
                or ""
            )
            if supplement_id:
                if str(take_id) != supplement_id:
                    raise ValueError("F9观察Supplement scope不一致")
                annotation_directory = SupplementRepository(
                    take_dir
                ).path_for(supplement_id)
            else:
                if str(take_id) != str(owner_take_id):
                    raise ValueError("F9观察Take scope不一致")
                annotation_directory = self.session_dir
            intent = RecordingAnnotationRepository(
                annotation_directory
            ).append_observation_intent(
                step_id,
                take_id,
                event_id,
                focus=focus,
                relation=relation,
                expected_source=expected_source,
                property_name=property_name,
                business_meaning=business_meaning,
                authority=USER_DECLARED_INTENT,
                expected_revision=expected_revision,
            )
            self._mark_generation_requests_stale(
                "observation_intent_changed",
                str(take.get("path") or ""),
                take.get("timeline_revision"),
            )
            mark_capabilities_stale(
                self.output_root,
                self.run_id,
                step_id,
                reason="observation_intent_changed",
                affected_take_id=owner_take_id,
                affected_timeline_revision=take.get("timeline_revision"),
                current_take_id=owner_take_id,
                current_timeline_revision=take.get("timeline_revision"),
            )
            self._touch()
            readiness = self._write_outputs()
            return {
                "observation_intent": intent,
                "readiness": readiness,
            }
        finally:
            if transient_lock is not None:
                transient_lock.release()

    def _drain_capture_notifications(self, method_name):
        active = self.active
        runtime = active.get("runtime") if active else None
        drain = getattr(runtime, method_name, None)
        return drain() if callable(drain) else []

    def _record_observation_receipts(self, receipts):
        active = self.active
        if active is None:
            return list(receipts or ())
        repository = RecordingAnnotationRepository(self.session_dir)
        result = []
        for source in receipts or ():
            receipt = dict(source or {})
            event_id = str(receipt.get("event_id") or "").strip()
            if event_id:
                active["observation_receipts"][event_id] = receipt
            if receipt.get("status") in {"captured", "warning"} and event_id:
                try:
                    intent = repository.current_observation_intent(
                        active["step"].id,
                        active["take"].id,
                        event_id,
                    )
                    if intent is None:
                        structured = receipt.get("structured_observation") or {}
                        defaults = default_observation_intent(
                            structured.get("provider")
                        )
                        intent = repository.append_observation_intent(
                            active["step"].id,
                            active["take"].id,
                            event_id,
                            focus=defaults["focus"],
                            relation=defaults["relation"],
                            expected_source=defaults["expected_source"],
                            authority=SYSTEM_INFERRED_INTENT,
                            expected_revision=0,
                        )
                    receipt["observation_intent"] = intent
                    active["observation_receipts"][event_id] = receipt
                except Exception as error:
                    message = f"{type(error).__name__}: {error}"
                    receipt["observation_intent_error"] = message
                    active["observation_intent_errors"].append({
                        "event_id": event_id,
                        "error": message,
                    })
            result.append(receipt)
        return result

    @_session_locked
    def apply_timeline_mutation(
            self,
            take_dir,
            expected_revision,
            mutation,
        ):
        if self.active is not None:
            raise RuntimeError("录制进行中不能校正已有 Take")
        if not callable(mutation):
            raise TypeError("Timeline mutation 必须可调用")
        take_dir = Path(take_dir).resolve()
        try:
            relative_take = take_dir.relative_to(
                self.session_dir
            ).as_posix()
        except ValueError as error:
            raise ValueError(f"Take 不属于当前录制任务: {take_dir}") from error

        transient_lock = None
        if self._run_lock is None:
            transient_lock = RunWriteLock(self.session_dir).acquire()
        try:
            step_id = self._step_id_for_take_path(relative_take)
            if step_id is None:
                raise KeyError(
                    f"录制任务状态中找不到 Take: {relative_take}"
                )
            self._require_step_generation_idle(
                step_id,
                "校正 Timeline",
            )
            timeline = TimelineStore(take_dir)
            timeline.require_revision(expected_revision)
            state = mutation(timeline)
            try:
                readiness = self.refresh_after_timeline_edit(take_dir)
            except Exception as error:
                readiness = None
                refresh_error = error
            else:
                refresh_error = None
            return {
                "state": state,
                "readiness": readiness,
                "refresh_error": refresh_error,
            }
        finally:
            if transient_lock is not None:
                transient_lock.release()

    @_session_locked
    def refresh_after_timeline_edit(self, take_dir):
        if self.active is not None:
            raise RuntimeError("录制进行中不能校正已有 Take")
        take_dir = Path(take_dir).resolve()
        try:
            relative_take = take_dir.relative_to(self.session_dir).as_posix()
        except ValueError as error:
            raise ValueError(f"Take 不属于当前录制任务: {take_dir}") from error
        timeline = TimelineStore(take_dir)
        timeline_state_path = timeline.state_path
        if not timeline_state_path.exists():
            raise FileNotFoundError(f"timeline-state.json 不存在: {timeline_state_path}")
        timeline_state = json.loads(timeline_state_path.read_text(encoding="utf-8"))
        effective_actions = timeline.effective_actions()
        take_path = take_dir / "take.json"
        take_metadata = json.loads(take_path.read_text(encoding="utf-8"))
        previous_timeline_revision = None
        take_metadata["timeline_revision"] = timeline_state.get("timeline_revision")
        take_metadata["effective_action_count"] = len(effective_actions)
        self.writer._write_json_path(take_path, take_metadata)
        tree_diff = json.loads(
            (take_dir / "ui" / "tree-diff.json").read_text(encoding="utf-8")
        )
        import yaml

        effective_locator = yaml.safe_load(
            timeline.locator_effective_path.read_text(
                encoding="utf-8"
            )
        ) or {}
        self.writer.write_take_summary(
            take_dir,
            take_metadata,
            effective_actions,
            tree_diff,
            effective_locator,
        )
        found = False
        for state in self.step_states.values():
            for take in state["takes"]:
                if take.get("path") != relative_take:
                    continue
                previous_timeline_revision = take.get("timeline_revision")
                take["effective_action_count"] = len(effective_actions)
                take["timeline_revision"] = timeline_state.get("timeline_revision")
                found = True
        if not found:
            raise KeyError(f"录制任务状态中找不到 Take: {relative_take}")
        self._mark_generation_requests_stale(
            reason="timeline_changed",
            take_path=relative_take,
            timeline_revision=timeline_state.get("timeline_revision"),
        )
        step_id = take_metadata.get("step", {}).get("id")
        if step_id:
            mark_capabilities_stale(
                self.output_root,
                self.run_id,
                step_id,
                reason="timeline_changed",
                affected_take_id=take_metadata.get("id"),
                affected_timeline_revision=previous_timeline_revision,
                current_take_id=take_metadata.get("id"),
                current_timeline_revision=timeline_state.get("timeline_revision"),
            )
        self._touch()
        return self._write_outputs()

    @_session_locked
    def toggle_pause(self, note=""):
        if self.active is None:
            raise RuntimeError("当前没有正在录制的 Step")
        active = self.active
        runtime = active["runtime"]
        if runtime.is_paused:
            self._resume_active_pause(active, note="")
            return False
        self._start_active_pause(active, note="")
        return True

    @_session_locked
    def skip_step(self, step_id, reason=""):
        if self.is_closed:
            raise RuntimeError("会话已经关闭")
        if self.is_finalized:
            raise RuntimeError("会话已经完成")
        if self.active is not None:
            raise RuntimeError("录制中不能跳过其他 Step")
        reason = _bounded_review_text(reason, "跳过原因")
        self._step(step_id)
        state = self.step_states[step_id]
        if state["status"] == "completed":
            raise ValueError("已完成的 Step 不能直接跳过；请保留当前 Take 或重新录制")
        state["status"] = "skipped"
        state["selected_take"] = None
        state["take_summary"] = ""
        state["skip_reason"] = str(reason or "")
        self._touch()
        self._write_outputs()

    @_session_locked
    def select_take(self, step_id, take_id):
        if self.active is not None:
            raise RuntimeError("录制进行中不能切换 selected Take")
        transient_lock = None
        if self._run_lock is None:
            transient_lock = RunWriteLock(self.session_dir).acquire()
        try:
            self._step(step_id)
            state = self.step_states[step_id]
            take = next(
                (
                    item
                    for item in state.get("takes", [])
                    if item.get("id") == take_id
                    and item.get("status") == "completed"
                ),
                None,
            )
            if take is None:
                raise KeyError(f"Step 中不存在可用完成 Take: {take_id}")
            previous_take_id = state.get("selected_take")
            if previous_take_id == take_id:
                return self.latest_readiness or validate_ai_bundle(
                    self.session_dir
                )
            self._require_step_generation_idle(
                step_id,
                "切换 selected Take",
            )
            previous_take = next(
                (
                    item
                    for item in state.get("takes", [])
                    if item.get("id") == previous_take_id
                ),
                None,
            )
            state["selected_take"] = take_id
            state["status"] = "completed"
            state["take_summary"] = str(take.get("take_summary") or "")
            state["skip_reason"] = ""
            self._mark_generation_requests_stale(
                reason="selected_take_changed",
                take_path=(previous_take or take).get("path"),
                timeline_revision=take.get("timeline_revision"),
            )
            mark_capabilities_stale(
                self.output_root,
                self.run_id,
                step_id,
                reason="selected_take_changed",
                affected_take_id=previous_take_id,
                affected_timeline_revision=(
                    (previous_take or {}).get("timeline_revision")
                ),
                current_take_id=take_id,
                current_timeline_revision=take.get("timeline_revision"),
            )
            self._touch()
            return self._write_outputs()
        finally:
            if transient_lock is not None:
                transient_lock.release()

    @_session_locked
    def reopen_for_recording(self):
        if self.active is not None:
            raise RuntimeError("会话仍在录制中")
        if self._run_lock is None:
            self._run_lock = RunWriteLock(self.session_dir).acquire()
        self.closed_at = None
        self.finalized_at = None
        if self._atexit_callback is None:
            self._atexit_callback = self.close
            atexit.register(self._atexit_callback)
        self._existing_session = False
        self._touch()
        self._write_outputs()
        return self

    @_session_locked
    def next_pending_step(self):
        for step in self.selected_steps:
            if self.step_states[step.id]["status"] == "pending":
                return step
        return None

    @_session_locked
    def finalize(self):
        if self.is_closed:
            raise RuntimeError("会话已经关闭")
        if self.active is not None:
            raise RuntimeError("仍有 Step 正在录制")
        self.finalized_at = datetime.now().isoformat(timespec="seconds")
        self._touch()
        try:
            self._write_outputs()
        finally:
            if self._run_lock is not None:
                self._run_lock.release()
                self._run_lock = None
        return self.session_dir

    @_session_locked
    def close(self):
        if self.is_closed:
            return self.session_dir
        if self._existing_session and self._run_lock is None:
            return self.session_dir
        if self.active is not None:
            try:
                self.cancel_step("recording session closed")
            except Exception:
                self._force_stop_active()
        transient_lock = None
        if self._run_lock is None:
            transient_lock = RunWriteLock(self.session_dir).acquire()
        self.closed_at = datetime.now().isoformat(timespec="seconds")
        self._touch()
        try:
            self._write_outputs()
        finally:
            if self._atexit_callback is not None:
                try:
                    atexit.unregister(self._atexit_callback)
                except Exception:
                    pass
                self._atexit_callback = None
            if self._run_lock is not None:
                self._run_lock.release()
                self._run_lock = None
            if transient_lock is not None:
                transient_lock.release()
        return self.session_dir

    def _close_active(
            self,
            status,
            *,
            take_summary="",
            discard_reason="",
        ):
        if self.active is None:
            raise RuntimeError("当前没有正在录制的 Step")
        active = self.active
        step = active["step"]
        take = active["take"]
        runtime = active["runtime"]
        state = self.step_states[step.id]
        previous_state = deepcopy(state)
        previous_state["status"] = active["previous_status"]
        previous_selected_take_id = state.get("selected_take")
        previous_selected_take = next(
            (
                item
                for item in state.get("takes", [])
                if item.get("id") == previous_selected_take_id
            ),
            None,
        )
        capture_errors = []
        events = []
        video_path = None
        try:
            if runtime.is_paused:
                self._resume_active_pause(active, note="")
            capture_result = runtime.stop()
            events = capture_result.events
            capture_errors.extend(capture_result.errors)
            self._record_observation_receipts(
                runtime.drain_observation_notifications()
            )
            capture_errors.extend(
                "ObservationIntent保存失败: "
                f"{item['event_id']}: {item['error']}"
                for item in active["observation_intent_errors"]
            )
            window_lifecycle = capture_result.window_lifecycle
            self._merge_window_lifecycle(active, window_lifecycle)
            video_path = capture_result.video_path
            window_evidence = self._capture_window_evidence_after(
                active,
                capture_errors,
            )
            tree_snapshots = [
                item.get("before_tree_data")
                for item in active["window_evidence"]
                if item.get("before_tree_data")
            ]
            tree_snapshots.extend(
                item.get("after_tree_data")
                for item in window_evidence
                if item.get("after_tree_data")
            )
            primary_evidence = next(
                (item for item in window_evidence if item["primary"]),
                None,
            )
            before_tree = active["before_tree"]
            if primary_evidence is None:
                after_tree = _empty_tree_snapshot(
                    None,
                    "no_business_window_observed",
                )
                tree_diff = diff_tree_snapshots(before_tree, after_tree)
            else:
                after_tree = primary_evidence.pop("after_tree_data")
                tree_diff = primary_evidence.pop("tree_diff_data")
            for item in window_evidence:
                item.pop("after_tree_data", None)
                item.pop("tree_diff_data", None)
                item.pop("business_tree_captured", None)
            actions = derive_actions(events)
            locator_bundle = build_locator_bundle(
                events,
                tree_snapshots=tree_snapshots,
            )
            take.events = events
            take.ended_at = datetime.now().isoformat(timespec="milliseconds")
            take.ended_monotonic = time.monotonic()
            take.video_path = video_path
            take.error = "; ".join(capture_errors) or None
            metadata = {
                "schema_version": SCHEMA_VERSION,
                "annotation_model_version": ANNOTATION_MODEL_VERSION,
                "id": take.id,
                "status": status,
                "take_number": take.take_number,
                "step": step,
                "started_at": take.started_at,
                "ended_at": take.ended_at,
                "duration_ms": int((take.ended_monotonic - take.started_monotonic) * 1000),
                "event_count": len(events),
                "action_count": len(actions),
                "video": "step.mp4" if video_path else None,
                "screenshots": {
                    "before": "screenshots/before.png" if self.config.with_screenshots else None,
                    "after": "screenshots/after.png" if self.config.with_screenshots else None,
                },
                "capture_error": take.error,
                **(
                    {"capture_integrity": capture_result.capture_integrity}
                    if capture_result.capture_integrity.get("status")
                    == "complete"
                    else {}
                ),
                "target_window": active["target_window"],
                "target_windows": active["target_windows"],
                "window_evidence": window_evidence,
                "window_lifecycle": window_lifecycle,
                "pauses": active["pauses"],
                "timeline": capture_result.timeline_metadata(
                    "input_capture_start"
                ),
            }
            self.writer.write_take(
                take.directory,
                metadata,
                events,
                actions,
                before_tree,
                after_tree,
                tree_diff,
                locator_bundle,
            )
            timeline_state = TimelineStore(take.directory).initialize(actions)
            relative_take = take.directory.relative_to(self.session_dir).as_posix()
            state["takes"].append({
                "id": take.id,
                "status": status,
                "path": relative_take,
                "event_count": len(events),
                "action_count": len(actions),
                "unresolved_count": len(locator_bundle["unresolved"]),
                "window_count": len(active["target_windows"]),
                "target_windows": active["target_windows"],
                "window_lifecycle": window_lifecycle,
                "take_summary": str(take_summary or ""),
                "discard_reason": str(discard_reason or ""),
            })
            state["status"] = "completed" if status == "completed" else active["previous_status"]
            if status == "completed":
                state["selected_take"] = take.id
                state["take_summary"] = str(take_summary or "")
                state["skip_reason"] = ""
                if previous_selected_take is not None:
                    self._mark_generation_requests_stale(
                        reason="selected_take_changed",
                        take_path=previous_selected_take.get("path"),
                        timeline_revision=timeline_state.get("timeline_revision"),
                    )
                    mark_capabilities_stale(
                        self.output_root,
                        self.run_id,
                        step.id,
                        reason="selected_take_changed",
                        affected_take_id=previous_selected_take.get("id"),
                        affected_timeline_revision=(
                            previous_selected_take.get("timeline_revision")
                        ),
                        current_take_id=take.id,
                        current_timeline_revision=timeline_state.get(
                            "timeline_revision"
                        ),
                    )
        except Exception:
            state.clear()
            state.update(previous_state)
            raise
        finally:
            self.active = None
            self._touch()
            try:
                self._write_outputs()
            except Exception:
                if state["status"] in ("completed", "skipped"):
                    raise
        return take

    def _start_active_pause(self, active, note=""):
        runtime = active["runtime"]
        pause_index = len(active["pauses"]) + 1
        relative_dir = Path("pauses") / f"pause-{pause_index:03d}"
        pause_dir = active["take"].directory / relative_dir
        pause_dir.mkdir(parents=True, exist_ok=True)
        start_ms = runtime.pause(note=note)
        window_states = self._capture_pause_window_starts(
            active,
            pause_dir,
            relative_dir,
        )
        primary = next(
            (item for item in window_states if item["public"]["primary"]),
            None,
        )
        pause = {
            "id": f"pause-{pause_index:03d}",
            "path": relative_dir.as_posix(),
            "note": str(note or ""),
            "start_note": str(note or ""),
            "end_note": None,
            "start_ms": start_ms,
            "end_ms": None,
            "duration_ms": None,
            "start_tree": primary["public"]["start_tree"] if primary else None,
            "end_tree": None,
            "tree_diff": None,
            "start_screenshot": (
                primary["public"]["start_screenshot"] if primary else None
            ),
            "end_screenshot": None,
            "windows": [item["public"] for item in window_states],
            "state_diff_summary": {},
        }
        active["active_pause"] = {
            "public": pause,
            "window_states": window_states,
            "directory": pause_dir,
            "relative_dir": relative_dir,
        }

    def _resume_active_pause(self, active, note=""):
        pause_state = active.get("active_pause")
        if pause_state is None:
            active["runtime"].resume(note=note)
            return
        runtime = active["runtime"]
        pause = pause_state["public"]
        window_evidence = self._capture_pause_window_ends(
            pause_state["window_states"],
            active["take"].directory,
        )
        primary = next(
            (item for item in window_evidence if item["primary"]),
            None,
        )
        pause["end_ms"] = runtime.resume(note=note)
        pause["end_note"] = str(note or "")
        pause["duration_ms"] = (
            pause["end_ms"] - pause["start_ms"]
            if pause["end_ms"] is not None and pause["start_ms"] is not None
            else None
        )
        pause["end_tree"] = primary["end_tree"] if primary else None
        pause["tree_diff"] = primary["tree_diff"] if primary else None
        pause["end_screenshot"] = primary["end_screenshot"] if primary else None
        pause["windows"] = window_evidence
        pause["state_changed"] = any(
            item.get("state_changed") for item in window_evidence
        )
        pause["state_diff_summary"] = _pause_state_diff_summary(
            window_evidence
        )
        active["pauses"].append(pause)
        active["active_pause"] = None

    def _capture_pause_window_starts(
            self,
            active,
            pause_dir,
            relative_dir,
    ):
        result = []
        for index, window in enumerate(active["target_windows"], start=1):
            handle = int(window["handle"])
            primary = handle == active["window_handle"]
            directory = (
                relative_dir
                if primary
                else relative_dir / "windows" / f"window-{index:03d}"
            )
            absolute_dir = active["take"].directory / directory
            start_tree = self._capture_tree(handle)
            self.writer._write_json_path(
                absolute_dir / "start-tree.json",
                start_tree,
            )
            screenshot = self._capture_screenshot(
                absolute_dir / "start.png",
                window_handle=handle,
            )
            result.append({
                "public": {
                    "window": window,
                    "primary": primary,
                    "start_tree": (directory / "start-tree.json").as_posix(),
                    "end_tree": None,
                    "tree_diff": None,
                    "start_screenshot": (
                        (directory / "start.png").as_posix()
                        if screenshot is not None
                        else None
                    ),
                    "end_screenshot": None,
                    "state_changed": False,
                },
                "start_tree_data": start_tree,
                "directory": directory,
            })
        return result

    def _capture_pause_window_ends(self, window_states, take_dir):
        result = []
        for state in window_states:
            item = dict(state["public"])
            window = item["window"]
            handle = int(window["handle"])
            directory = state["directory"]
            absolute_dir = take_dir / directory
            end_tree = self._capture_tree(handle)
            tree_diff = diff_tree_snapshots(state["start_tree_data"], end_tree)
            self.writer._write_json_path(
                absolute_dir / "end-tree.json",
                end_tree,
            )
            self.writer._write_json_path(
                absolute_dir / "tree-diff.json",
                tree_diff,
            )
            screenshot = self._capture_screenshot(
                absolute_dir / "end.png",
                window_handle=handle,
            )
            item["end_tree"] = (directory / "end-tree.json").as_posix()
            item["tree_diff"] = (directory / "tree-diff.json").as_posix()
            item["end_screenshot"] = (
                (directory / "end.png").as_posix()
                if screenshot is not None
                else None
            )
            item["state_changed"] = bool(
                tree_diff.get("summary", {}).get("added_count")
                or tree_diff.get("summary", {}).get("removed_count")
                or tree_diff.get("summary", {}).get("changed_count")
            )
            item["state_diff_summary"] = dict(
                tree_diff.get("summary") or {}
            )
            result.append(item)
        return result

    def _capture_tree(self, window_handle):
        if not self.config.with_tree:
            return {
                "schema_version": SCHEMA_VERSION,
                "nodes": [],
                "disabled": True,
                "error": None,
            }
        return capture_tree_snapshot(
            backend=self.config.backend,
            window_handle=window_handle,
            max_depth=self.config.tree_max_depth,
            max_nodes=self.config.tree_max_nodes,
        )

    def _capture_window_evidence_before(
            self,
            take_dir,
            target_windows,
            primary_window_handle,
    ):
        evidence = []
        for index, window in enumerate(target_windows, start=1):
            handle = int(window["handle"])
            primary = handle == primary_window_handle
            directory = Path("ui") if primary else Path("windows") / f"window-{index:03d}"
            before_tree = self._capture_tree(handle)
            if not primary:
                self.writer._write_json_path(
                    take_dir / directory / "before-tree.json",
                    before_tree,
                )
            screenshot_path = (
                Path("screenshots") / "before.png"
                if primary
                else directory / "before.png"
            )
            screenshot = self._capture_screenshot(
                take_dir / screenshot_path,
                window_handle=handle,
            )
            evidence.append({
                "window": window,
                "primary": primary,
                "before_tree": (directory / "before-tree.json").as_posix(),
                "after_tree": (directory / "after-tree.json").as_posix(),
                "tree_diff": (directory / "tree-diff.json").as_posix(),
                "before_screenshot": screenshot_path.as_posix() if screenshot else None,
                "after_screenshot": None,
                "before_tree_data": before_tree,
                "business_tree_captured": True,
            })
        return evidence

    def _capture_discovered_window(self, active, lifecycle):
        is_cancelled = lifecycle.get("_capture_cancelled") or (lambda: False)
        commit = lifecycle.get("_capture_commit") or (
            lambda callback: (callback(), True)[1]
        )
        handle = int(lifecycle["handle"])
        existing = next(
            (
                item
                for item in active["window_evidence"]
                if int((item.get("window") or {}).get("handle") or 0) == handle
            ),
            None,
        )
        if existing is not None:
            before_tree = None
            promotion_screenshot = None
            promotion_screenshot_error = None
            if (
                lifecycle.get("has_business_event")
                and not existing.get("business_tree_captured")
            ):
                before_tree = self._capture_tree(handle)
            promote = bool(
                lifecycle.get("has_business_event")
                and active.get("window_handle") is None
            )
            if promote:
                try:
                    promotion_screenshot = self._capture_window_screenshot_temp(
                        window_handle=handle,
                    )
                except Exception as error:
                    promotion_screenshot_error = (
                        f"{type(error).__name__}: {error}"
                    )
            if is_cancelled():
                if promotion_screenshot is not None:
                    Path(promotion_screenshot).unlink(missing_ok=True)
                return {}

            def publish_existing():
                if before_tree is not None:
                    self._publish_business_window_tree(
                        active,
                        existing,
                        before_tree,
                    )
                if promote:
                    self._promote_discovered_window(
                        active,
                        existing,
                        screenshot_temp=promotion_screenshot,
                        screenshot_error=promotion_screenshot_error,
                    )

            if not commit(publish_existing):
                if promotion_screenshot is not None:
                    Path(promotion_screenshot).unlink(missing_ok=True)
                return {}
            return {
                key: existing.get(key)
                for key in ("before_tree", "before_screenshot")
            }

        window = _window_identity(handle)
        primary = bool(
            active.get("window_handle") is None
            and lifecycle.get("has_business_event")
        )
        index = len(active["target_windows"]) + 1
        directory = Path("ui") if primary else Path("windows") / f"window-{index:03d}"
        before_tree = (
            self._capture_tree(handle)
            if lifecycle.get("has_business_event")
            else _empty_tree_snapshot(
                handle,
                "waiting_for_first_business_event",
            )
        )
        screenshot_path = (
            Path("screenshots") / "before.png"
            if primary
            else directory / "before.png"
        )
        screenshot_error = None
        screenshot_temp = None
        try:
            screenshot_temp = self._capture_window_screenshot_temp(
                window_handle=handle,
            )
            screenshot = screenshot_temp
        except Exception as error:
            screenshot = None
            screenshot_error = f"{type(error).__name__}: {error}"
        evidence = {
            "window": window,
            "primary": primary,
            "admission": lifecycle.get("admission"),
            "process_relation": lifecycle.get("process_relation"),
            "opened_during_take": lifecycle.get("opened_during_take"),
            "first_seen_ms": lifecycle.get("first_seen_ms"),
            "before_tree": (directory / "before-tree.json").as_posix(),
            "after_tree": (directory / "after-tree.json").as_posix(),
            "tree_diff": (directory / "tree-diff.json").as_posix(),
            "before_screenshot": screenshot_path.as_posix() if screenshot else None,
            "first_seen_capture_error": screenshot_error,
            "after_screenshot": None,
            "before_tree_data": before_tree,
            "business_tree_captured": bool(
                lifecycle.get("has_business_event")
            ),
        }
        if is_cancelled():
            if screenshot_temp is not None:
                Path(screenshot_temp).unlink(missing_ok=True)
            return {}

        def publish_new():
            if primary:
                active["window_handle"] = handle
                active["target_window"] = window
            active["discovered_window_handles"].append(handle)
            active["window_handles"] = tuple(
                active["discovered_window_handles"]
            )
            active["target_windows"].append(window)
            active["window_evidence"].append(evidence)
            self.writer._write_json_path(
                active["take"].directory / evidence["before_tree"],
                before_tree,
            )
            if screenshot_temp is not None:
                self._publish_temp_file(
                    screenshot_temp,
                    active["take"].directory / screenshot_path,
                )
            if primary:
                active["before_tree"] = before_tree

        if not commit(publish_new):
            if screenshot_temp is not None:
                Path(screenshot_temp).unlink(missing_ok=True)
            return {}
        return {
            "before_tree": evidence["before_tree"],
            "before_screenshot": evidence["before_screenshot"],
            "capture_error": screenshot_error,
        }

    def _publish_business_window_tree(self, active, evidence, before_tree):
        evidence["before_tree_data"] = before_tree
        evidence["business_tree_captured"] = True
        self.writer._write_json_path(
            active["take"].directory / evidence["before_tree"],
            before_tree,
        )
        if evidence.get("primary"):
            active["before_tree"] = before_tree

    def _capture_window_screenshot_temp(self, window_handle):
        if not self.config.with_screenshots:
            return None
        file_descriptor, name = tempfile.mkstemp(suffix=".png")
        os.close(file_descriptor)
        path = Path(name)
        try:
            self._capture_screenshot(path, window_handle=window_handle)
            return path
        except Exception:
            path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _publish_temp_file(source, destination):
        source = Path(source)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))

    def _promote_discovered_window(
            self,
            active,
            evidence,
            *,
            screenshot_temp=None,
            screenshot_error=None,
    ):
        handle = int(evidence["window"]["handle"])
        active["window_handle"] = handle
        active["target_window"] = evidence["window"]
        evidence["primary"] = True
        active["before_tree"] = evidence["before_tree_data"]
        self.writer._write_json_path(
            active["take"].directory / "ui" / "before-tree.json",
            evidence["before_tree_data"],
        )
        if screenshot_temp is not None:
            self._publish_temp_file(
                screenshot_temp,
                active["take"].directory / "screenshots" / "before.png",
            )
        evidence.update({
            "before_tree": "ui/before-tree.json",
            "after_tree": "ui/after-tree.json",
            "tree_diff": "ui/tree-diff.json",
            "before_screenshot": (
                "screenshots/before.png"
                if screenshot_temp is not None
                else None
            ),
            "first_business_capture_error": screenshot_error,
        })

    def _capture_window_evidence_after(self, active, capture_errors):
        result = []
        take_dir = active["take"].directory
        for evidence in active["window_evidence"]:
            item = dict(evidence)
            before_tree = item.pop("before_tree_data")
            window = item["window"]
            handle = int(window["handle"])
            directory = Path(item["after_tree"]).parent
            alive = bool(win32gui.IsWindow(handle))
            item["alive_at_end"] = alive
            item["closed_during_take"] = not alive
            after_tree = (
                self._capture_tree(handle)
                if alive
                else _empty_tree_snapshot(handle, "window_closed_during_take")
            )
            tree_diff = diff_tree_snapshots(before_tree, after_tree)
            if not item["primary"]:
                self.writer._write_json_path(
                    take_dir / item["after_tree"],
                    after_tree,
                )
                self.writer._write_json_path(
                    take_dir / item["tree_diff"],
                    tree_diff,
                )
            screenshot_path = (
                Path("screenshots") / "after.png"
                if item["primary"]
                else directory / "after.png"
            )
            try:
                if not alive:
                    raise _ExpectedWindowClosed()
                screenshot = self._capture_screenshot(
                    take_dir / screenshot_path,
                    window_handle=handle,
                )
                item["after_screenshot"] = (
                    screenshot_path.as_posix() if screenshot else None
                )
            except _ExpectedWindowClosed:
                item["after_screenshot"] = None
            except Exception as error:
                if not win32gui.IsWindow(handle):
                    alive = False
                    item["alive_at_end"] = False
                    item["closed_during_take"] = True
                    after_tree = _empty_tree_snapshot(
                        handle,
                        "window_closed_during_take",
                    )
                    tree_diff = diff_tree_snapshots(before_tree, after_tree)
                    item["after_screenshot"] = None
                    if not item["primary"]:
                        self.writer._write_json_path(
                            take_dir / item["after_tree"],
                            after_tree,
                        )
                        self.writer._write_json_path(
                            take_dir / item["tree_diff"],
                            tree_diff,
                        )
                else:
                    capture_errors.append(
                        "after screenshot "
                        f"window={handle}: {type(error).__name__}: {error}"
                    )
            item["comparable"] = tree_diff.get("comparable")
            item["after_tree_data"] = after_tree
            item["tree_diff_data"] = tree_diff
            result.append(item)
        return result

    @staticmethod
    def _merge_window_lifecycle(active, lifecycle_entries):
        by_handle = {
            int(item["handle"]): item
            for item in lifecycle_entries
            if item.get("handle") is not None
        }
        for evidence in active["window_evidence"]:
            handle = int((evidence.get("window") or {}).get("handle") or 0)
            lifecycle = by_handle.get(handle)
            if lifecycle is None:
                evidence.setdefault("admission", "selected")
                evidence.setdefault("process_relation", "selected_window")
                continue
            for key in (
                "admission",
                "process_relation",
                "first_seen_ms",
                "last_seen_ms",
                "opened_during_take",
                "closed_during_take",
                "alive_at_end",
                "event_ids",
            ):
                evidence[key] = deepcopy(lifecycle.get(key))

    def _capture_screenshot(self, destination, window_handle=None):
        if not self.config.with_screenshots:
            return None
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with mss() as screen:
            if window_handle:
                left, top, right, bottom = win32gui.GetWindowRect(int(window_handle))
                width = int(right - left)
                height = int(bottom - top)
                if width <= 0 or height <= 0:
                    raise ValueError(
                        f"目标窗口区域无效: handle={window_handle}, rect={(left, top, right, bottom)}"
                    )
                image = screen.grab({
                    "left": int(left),
                    "top": int(top),
                    "width": width,
                    "height": height,
                })
                to_png(image.rgb, image.size, output=str(destination))
                return destination
            monitor_index = min(max(1, self.config.monitor_index), len(screen.monitors) - 1)
            screen.shot(mon=monitor_index, output=str(destination))
        return destination

    def _resolve_target_window_handle(self):
        handle = self.config.target_window_handle
        if handle is None:
            handle = win32gui.GetForegroundWindow()
        return self._resolve_target_window_handles(
            (handle,),
            handle,
        )[1]

    def _resolve_target_window_handles(
            self,
            target_window_handles=None,
            primary_window_handle=None,
    ):
        configured = tuple(target_window_handles or self.config.target_window_handles)
        if not configured:
            primary = self._resolve_target_window_handle()
            return (primary,), primary

        primary_recorded = int(
            primary_window_handle
            or self.config.target_window_handle
            or configured[0]
        )
        identities_by_handle = {
            int(item["handle"]): dict(item)
            for item in self.config.target_window_identities
            if item.get("handle") is not None
        }
        recorded = [
            identities_by_handle.get(int(value))
            for value in configured
        ]
        has_frozen_identities = all(item is not None for item in recorded)

        try:
            handles = []
            for value in configured:
                handle = self._validate_target_window_handle(value)
                if handle not in handles:
                    handles.append(handle)
            if has_frozen_identities:
                verified = restore_window_handles(
                    recorded,
                    [window_identity_for_handle(handle) for handle in handles],
                )
                if len(verified) != len(recorded):
                    raise ValueError(
                        "目标窗口句柄仍存在，但冻结身份已不匹配"
                    )
                handles = list(verified)
        except ValueError as original_error:
            if not has_frozen_identities:
                raise original_error
            restored = restore_window_handles(
                recorded,
                list_top_level_windows(self.config.backend),
            )
            if len(restored) != len(recorded):
                raise ValueError(
                    "目标窗口已重建，但无法按冻结身份唯一恢复；请刷新并重新选择目标窗口"
                ) from original_error
            if not all(is_recordable_window_handle(handle) for handle in restored):
                raise ValueError(
                    "恢复的目标窗口没有可录制的可视区域；"
                    "请刷新并重新选择实际业务窗口"
                ) from original_error
            handles = list(restored)

        recorded_to_current = dict(zip(
            (int(value) for value in configured),
            handles,
        ))
        primary = recorded_to_current.get(primary_recorded)
        if primary is None:
            raise ValueError("主窗口必须包含在当前 Step 的录制窗口中")
        return tuple(handles), primary

    @staticmethod
    def _validate_target_window_handle(value):
        handle = int(value or 0)
        if not handle or not win32gui.IsWindow(handle):
            raise ValueError(
                f"目标窗口不存在，请刷新并重新选择目标窗口: {handle}"
            )
        if not is_recordable_window_handle(handle):
            raise ValueError(
                "目标窗口没有可录制的可视区域；"
                "请在“限制窗口”中选择实际业务窗口"
            )
        _, process_id = win32process.GetWindowThreadProcessId(handle)
        class_name = str(win32gui.GetClassName(handle) or "")
        if int(process_id) == os.getpid():
            raise ValueError("目标窗口不能是录制工具自身窗口")
        if class_name.casefold() in {"shell_traywnd", "shell_secondarytraywnd"}:
            raise ValueError("目标窗口不能是任务栏")
        return handle

    def _force_stop_active(self):
        active = self.active
        if active is None:
            return
        runtime = active.get("runtime")
        if runtime is not None:
            runtime.abort(timeout=2)
        state = self.step_states.get(active["step"].id)
        if state is not None:
            state["status"] = active.get("previous_status", "pending")
        self.active = None

    def _mark_generation_requests_stale(self, reason, take_path, timeline_revision):
        from autowork_core.utils.debug_tools.recorder.workflow_state import (
            load_workflow_state,
            transition_workflow,
        )

        request_dir = self.session_dir / "ai" / "requests"
        if not request_dir.exists():
            return
        for request_path in request_dir.glob("request_*.json"):
            try:
                request = json.loads(request_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            evidence_paths = {
                (entry.get("artifacts") or {}).get("take")
                for entry in request.get("evidence") or []
            }
            if take_path not in evidence_paths:
                continue
            state = load_workflow_state(
                self.session_dir,
                request.get("request_id"),
            )
            if not state:
                continue
            transition_workflow(
                self.session_dir,
                request["request_id"],
                status="stale",
                result={
                    "status": "stale",
                    "reason": reason,
                    "take_path": take_path,
                    "current_timeline_revision": timeline_revision,
                },
            )

    def _step_workflow_request_ids(self, step_id, *, status=None):
        from autowork_core.utils.debug_tools.recorder.workflow_state import (
            load_workflow_state,
        )

        request_dir = self.session_dir / "ai" / "requests"
        if not request_dir.exists():
            return []
        result = []
        for request_path in request_dir.glob("request_*.json"):
            try:
                request = json.loads(request_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            target_step_ids = {
                str(item.get("id") or "")
                for item in (request.get("target") or {}).get("steps") or ()
            }
            if str(step_id) not in target_step_ids:
                continue
            state = load_workflow_state(
                self.session_dir,
                request.get("request_id"),
            )
            if not state or (status and state.get("status") != status):
                continue
            result.append(str(request.get("request_id") or ""))
        return sorted(item for item in result if item)

    def _step_id_for_take_path(self, relative_take):
        relative_take = str(relative_take or "")
        return next((
            str(step_id)
            for step_id, state in self.step_states.items()
            for take in state.get("takes") or ()
            if str(take.get("path") or "") == relative_take
        ), None)

    def _require_step_generation_idle(self, step_id, operation):
        running = self._step_workflow_request_ids(
            step_id,
            status="running",
        )
        if running:
            raise RuntimeError(
                f"当前Step已有生成事务运行，不能{operation}: "
                + ", ".join(running)
            )

    def _mark_step_generation_requests_stale(
            self,
            step_id,
            *,
            annotation_revision,
        ):
        from autowork_core.utils.debug_tools.recorder.workflow_state import (
            transition_workflow,
        )

        for request_id in self._step_workflow_request_ids(step_id):
            transition_workflow(
                self.session_dir,
                request_id,
                status="stale",
                result={
                    "status": "stale",
                    "reason": "step_user_context_changed",
                    "step_id": str(step_id),
                    "current_annotation_revision": annotation_revision,
                },
            )

    def _write_outputs(self):
        source = SessionProjectionSource(
            run_id=self.run_id,
            created_at=self.created_at,
            updated_at=self.updated_at,
            finalized_at=self.finalized_at,
            closed_at=self.closed_at,
            is_recording=self.is_recording,
            feature_plan=self.feature_plan,
            scenario_plan=self.scenario_plan,
            config=self.config,
            selected_steps=self.selected_steps,
            step_states=self.step_states,
            annotation_model_version=self.annotation_model_version,
            environment=getattr(self, "_recorded_environment", None),
        )
        readiness = SessionProjectionBuilder(
            self.session_dir,
            self.output_root,
            writer=self.writer,
        ).write_source(source)
        self.latest_readiness = readiness
        return readiness

    def _step(self, step_id):
        for step in self.selected_steps:
            if step.id == step_id:
                return step
        raise KeyError(f"Step 不在当前录制范围: {step_id}")

    def step_dir(self, step_or_id):
        step = (
            self._step(step_or_id)
            if isinstance(step_or_id, str)
            else step_or_id
        )
        directory = (
            step_directory_name(step)
            if self.path_mode == "readable"
            else compact_step_directory_name(step)
            if self.path_mode == "compact"
            else minimal_step_directory_name(step)
        )
        return self.writer.steps_dir / directory

    def _touch(self):
        self.updated_at = datetime.now().isoformat(timespec="seconds")
        self.latest_readiness = None


class _ExpectedWindowClosed(Exception):
    pass


def _bounded_review_text(value, label):
    text = str(value or "").strip()
    if len(text) > MAX_REVIEW_TEXT_LENGTH:
        raise ValueError(f"{label}超过{MAX_REVIEW_TEXT_LENGTH}字符")
    return text


def _pause_state_diff_summary(window_evidence):
    result = {
        "added_count": 0,
        "removed_count": 0,
        "changed_count": 0,
        "changed_window_count": 0,
    }
    for item in window_evidence or ():
        summary = item.get("state_diff_summary") or {}
        for key in ("added_count", "removed_count", "changed_count"):
            value = summary.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                result[key] += max(0, value)
        if item.get("state_changed"):
            result["changed_window_count"] += 1
    return result


def _empty_tree_snapshot(window_handle, reason):
    return {
        "schema_version": SCHEMA_VERSION,
        "window_handle": window_handle,
        "nodes": [],
        "disabled": False,
        "error": str(reason),
    }


def _step_plan_from_dict(value):
    return StepPlan(
        id=str(value["id"]),
        key=str(value["key"]),
        ordinal=int(value.get("ordinal") or 0),
        keyword=str(value.get("keyword") or ""),
        text=str(value.get("text") or ""),
        line=int(value.get("line") or 0),
        semantic_type=str(value.get("semantic_type") or ""),
        is_background=bool(value.get("is_background")),
        selected=bool(value.get("selected", True)),
        text_block=value.get("text_block"),
        table=deepcopy(value.get("table")),
    )


def _recording_config_from_manifest(manifest):
    value = dict(manifest.get("capture_config") or {})
    allowed = RecordingSessionConfig.__dataclass_fields__
    value = {key: item for key, item in value.items() if key in allowed}
    if value.get("output_root"):
        value["output_root"] = Path(value["output_root"])
    for key in (
        "target_window_handles",
        "target_window_titles",
        "target_window_identities",
    ):
        if key in value:
            value[key] = tuple(value[key] or ())
    return RecordingSessionConfig(**value)


def _unique_session_dir(path):
    path = Path(path)
    if not path.exists():
        return path
    index = 2
    while True:
        candidate = path.with_name(f"{path.name}_{index}")
        if not candidate.exists():
            return candidate
        index += 1


def _validate_session_paths(session_dir, steps, mode="readable"):
    if os.name != "nt":
        return
    if _session_paths_too_long(session_dir, steps, mode=mode):
        longest = _longest_session_path(session_dir, steps, mode=mode)
        raise ValueError(
            "录制输出根目录过长，请选择更短的输出目录: "
            f"length={len(str(longest))}, preview={longest}"
        )


def _validate_take_path(take_dir):
    if os.name != "nt":
        return
    longest = ProjectionStore.longest_write_path(take_dir)
    if len(str(longest)) > WINDOWS_LEGACY_PATH_LIMIT:
        raise ValueError(
            "当前录制会话路径过长，无法安全保存投影；请新建录制会话并选择"
            "更短的输出目录（例如 D:\\rec）: "
            f"length={len(str(longest))}, preview={longest}"
        )


def _session_paths_too_long(session_dir, steps, mode="readable"):
    return (
        os.name == "nt"
        and len(str(_longest_session_path(session_dir, steps, mode=mode)))
        > WINDOWS_LEGACY_PATH_LIMIT
    )


def _longest_session_path(session_dir, steps, mode="readable"):
    take_dirs = (
        (
            session_dir
            / "steps"
            / (
                step_directory_name(step)
                if mode == "readable"
                else compact_step_directory_name(step)
                if mode == "compact"
                else minimal_step_directory_name(step)
            )
            / "takes"
            / "take-999"
        )
        for step in steps
    )
    return max(
        ProjectionStore.longest_write_path(take_dir)
        for take_dir in take_dirs
    )


def _window_identity(handle):
    try:
        return window_identity_for_handle(handle)
    except Exception as error:
        return {
            "handle": int(handle) if handle else None,
            "error": f"{type(error).__name__}: {error}",
        }