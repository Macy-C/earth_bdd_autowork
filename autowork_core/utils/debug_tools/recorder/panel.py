from __future__ import annotations

import os
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from autowork_core.utils.debug_tools.common import get_open_windows
from autowork_core.utils.debug_tools.recorder.annotations import (
    RecordingAnnotationRepository,
    step_business_context_text,
)
from autowork_core.utils.debug_tools.recorder.feature_plan import load_feature_plan
from autowork_core.utils.debug_tools.recorder.feature_workspace_query_service import (
    FeatureWorkspaceQueryService,
)
from autowork_core.utils.debug_tools.recorder.feature_delivery import (
    is_feature_delivery_package,
    preview_feature_delivery,
)
from autowork_core.utils.debug_tools.recorder.hotkeys import (
    RECORDER_HOTKEYS,
    VK_F7,
    VK_F9,
    VK_F10,
    VK_F11,
    VK_SHIFT,
    is_key_down,
    poll_hotkeys,
    reset_hotkeys,
)
from autowork_core.utils.debug_tools.recorder.library_panel import RecordingLibraryWindow
from autowork_core.utils.debug_tools.recorder.operation_coordinator import (
    OperationCoordinator,
)
from autowork_core.utils.debug_tools.recorder.query_service import (
    RecorderQueryService,
)
from autowork_core.utils.debug_tools.recorder.portability_service import (
    RecordingPortabilityService,
)
from autowork_core.utils.debug_tools.recorder.identity import stable_digest
from autowork_core.utils.debug_tools.recorder.run_retirement import (
    retire_recording_session,
)
from autowork_core.utils.debug_tools.recorder.review_panel import RecorderReviewWindow
from autowork_core.utils.debug_tools.recorder.session import (
    FeatureRecordingSession,
    RecordingSessionConfig,
)
from autowork_core.utils.debug_tools.recorder.status_overlay import RecordingStatusOverlay
from autowork_core.utils.debug_tools.recorder.target_highlight import (
    create_recording_target_highlight,
)
from autowork_core.utils.debug_tools.recorder.timeline_panel import (
    TimelineEditorWindow,
)
from autowork_core.utils.debug_tools.recorder.window_selector import (
    RecorderWindowSelector,
    format_window_selection_summary,
)
from autowork_core.utils.debug_tools.recorder.window_identity import (
    freeze_window_identity,
    is_recordable_window_handle,
)
from autowork_core.utils.debug_tools.recorder.workbench import RecorderWorkbench
from config.paths import Paths
from config.settings import settings


OBSERVATION_FOCUS_LABELS = {
    "自动判断": "auto",
    "显示文字": "text",
    "当前值": "value",
    "是否可见": "visible",
    "是否可用": "enabled",
    "窗口标题": "window_title",
}
OBSERVATION_RELATION_LABELS = {
    "自动": "auto",
    "等于": "equal",
    "包含": "contains",
    "不包含": "not_contains",
}
OBSERVATION_SOURCE_LABELS = {
    "自动匹配 Feature": "auto",
    "当前 Step 文本": "feature",
    "Examples 列": "examples",
    "Data Table 列": "data_table",
    "当前观察状态": "observed_state",
}
OBSERVATION_FOCUS_VALUES = {
    value: label for label, value in OBSERVATION_FOCUS_LABELS.items()
}
OBSERVATION_FOCUS_VALUES.update({
    "collection": "自动判断",
    "property": "自动判断",
    "region_text": "显示文字",
})
OBSERVATION_RELATION_VALUES = {
    value: label for label, value in OBSERVATION_RELATION_LABELS.items()
}
OBSERVATION_SOURCE_VALUES = {
    value: label for label, value in OBSERVATION_SOURCE_LABELS.items()
}


def _observation_receipt_summary(receipt):
    receipt = dict(receipt or {})
    target = receipt.get("target") or {}
    intent = receipt.get("observation_intent") or {}
    target_name = str(target.get("name") or "当前目标")
    focus = OBSERVATION_FOCUS_VALUES.get(
        str(intent.get("focus") or "auto"),
        "自动判断",
    )
    source = intent.get("expected_source") or {"kind": "auto"}
    source_label = OBSERVATION_SOURCE_VALUES.get(
        str(source.get("kind") or "auto"),
        "自动匹配 Feature",
    )
    reference = str(source.get("reference") or "").strip()
    if reference:
        source_label = f"{source_label} {reference}"
    prefix = (
        "已记录检查，证据需要复核"
        if receipt.get("status") == "warning"
        else "已记录检查"
    )
    return (
        f"{prefix}：{target_name}；{focus}；期望来自{source_label}。"
        "可继续录制，之后可在审阅中修改。"
    )


class RecorderToolMixin:
    def init_recorder_tool_state(self):
        self.recorder_workbench = None
        self.recorder_window = None
        self.recording_library_window = None
        self.recorder_review_window = None
        self.recorder_timeline_window = None
        self.recorder_timeline_return_view = "review"
        self.recorder_review_return_view = "capture"
        self.recorder_library_return_view = None
        self.recorder_status_overlay = None
        self.recorder_target_highlight = None
        self.recorder_session = None
        self.recorder_feature_plan = None
        self.recorder_feature_workspace = None
        self.recorder_feature_rows = {}
        self.recorder_feature_scenario_rows = {}
        self.recorder_scenario_map = {}
        self.recorder_step_map = {}
        self.recorder_target_window_map = {}
        self.recorder_selected_window_handles = ()
        self.recorder_primary_window_handle = None
        self.recorder_selected_step_ids = set()
        self.recorder_pending_step_contexts = {}
        self.recorder_output_dir = None
        self.recorder_task_busy = False
        self.recorder_pending_step_id = None
        self.recorder_operations = OperationCoordinator(
            max_workers=3,
            thread_name_prefix="recorder-workbench",
        )
        self.recorder_query_service = None
        self.recorder_workbench_context_operation_key = (
            f"workbench:{id(self)}:context"
        )
        self.recorder_workbench_context_sequence = 0
        self.recorder_portability_services = {}
        self.recorder_portability_active_kinds = {}
        self.recorder_poll_after_id = None
        self.recorder_start_after_id = None
        self.recorder_hotkey_down = dict.fromkeys(RECORDER_HOTKEYS, False)

        self.recorder_feature_path_var = tk.StringVar(value="")
        self.recorder_feature_detail_var = tk.StringVar(
            value="尚未选择 Feature。"
        )
        self.recorder_scenario_var = tk.StringVar(value="")
        self.recorder_window_summary_var = tk.StringVar(
            value="自动识别，无需提前选择"
        )
        self.recorder_output_root_var = tk.StringVar(
            value=str(Paths.ARTIFACTS_DIR / "recording_sessions")
        )
        self.recorder_backend_var = tk.StringVar(value=self.backend)
        self.recorder_video_var = tk.BooleanVar(value=True)
        self.recorder_screenshot_var = tk.BooleanVar(value=True)
        self.recorder_tree_var = tk.BooleanVar(value=True)
        self.recorder_minimize_var = tk.BooleanVar(value=True)
        self.recorder_target_process_only_var = tk.BooleanVar(value=True)
        self.recorder_window_mode_var = tk.StringVar(value="auto")
        self.recorder_tree_depth_var = tk.StringVar(value="8")
        self.recorder_tree_nodes_var = tk.StringVar(value="1200")
        self.recorder_status_var = tk.StringVar(value="请选择 Feature。")
        self.recorder_progress_var = tk.StringVar(value="0 / 0")
        self.recorder_privacy_warning_var = tk.StringVar(
            value=(
                "录制不会自动脱敏：请只使用测试数据，不要输入真实密码、"
                "令牌、患者或其他敏感信息。"
            )
        )
        self.recorder_take_summary_help_var = tk.StringVar(
            value=(
                "可选，仅随F10保存本次录制并供人工审阅；"
                "不会参与断言、binding或Plan推理。"
            )
        )
        self.recorder_step_business_context_var = tk.StringVar(value="")
        self.recorder_step_context_revision_var = tk.StringVar(value="版本 0")
        self.recorder_step_context_revision = 0
        self.recorder_step_context_step_id = None
        self.recorder_observation_capture_inflight_event_id = None

        self.recorder_feature_entry = None
        self.recorder_feature_tree = None
        self.recorder_materials_button = None
        self.recorder_materials_menu = None
        self.recorder_window_select_button = None
        self.recorder_window_summary_label = None
        self.recorder_step_tree = None
        self.recorder_take_summary_entry = None
        self.recorder_step_business_context_entry = None
        self.recorder_step_context_save_button = None
        self.recorder_create_button = None
        self.recorder_start_button = None
        self.recorder_pause_button = None
        self.recorder_observe_button = None
        self.recorder_finish_button = None
        self.recorder_cancel_button = None
        self.recorder_skip_button = None
        self.recorder_finalize_button = None
        self.recorder_open_output_button = None
        self.recorder_more_button = None
        self.recorder_more_menu = None
        self.recorder_source_frame = None
        self.recorder_options_frame = None
        self.recorder_advanced_button = None
        self.recorder_backend_combo = None
        self.recorder_window_mode_combo = None

    def open_recording_library(self):
        self.open_recorder_workbench("library")

    def open_recorder_workbench(self, initial_view="capture"):
        self._ensure_recorder_workbench()
        self._ensure_capture_view()
        self._ensure_library_view()
        self._sync_workbench_view_states()
        if not self.recorder_workbench.select(initial_view):
            self.recorder_workbench.select("capture")
        return self.recorder_workbench

    def _ensure_library_view(self):
        if self.recording_library_window is not None:
            try:
                if self.recording_library_window.window.winfo_exists():
                    return self.recording_library_window
            except tk.TclError:
                pass
        host = self.recorder_workbench.host("library")
        self.recorder_workbench.clear("library")
        self.recording_library_window = RecordingLibraryWindow(
            host,
            self.recorder_output_root_var.get().strip() or None,
            on_rerecord=self.resume_existing_recording,
            on_open_session=self._open_existing_session_in_workbench,
            on_retire_session=self._retire_recording_session,
            on_close=self._return_from_workbench_library,
            close_destroys=False,
        )
        return self.recording_library_window

    def _ensure_capture_view(self):
        if self.recorder_window is not None:
            try:
                if self.recorder_window.winfo_exists():
                    return self.recorder_window
            except tk.TclError:
                pass
        self.recorder_window = self.recorder_workbench.host("capture")
        self.recorder_workbench.clear("capture")
        if self.recorder_status_overlay is None:
            self.recorder_status_overlay = RecordingStatusOverlay(self.app)
        if self.recorder_target_highlight is None:
            self.recorder_target_highlight = create_recording_target_highlight(
                self.app
            )
        self._build_recorder_ui()
        self._restore_recorder_panel_state()
        self._schedule_recorder_poll()
        return self.recorder_window

    def _sync_workbench_view_states(self):
        if self.recorder_workbench is None:
            return
        self.recorder_workbench.set_view_enabled(
            "review",
            self.recorder_session is not None,
        )
        self.recorder_workbench.set_view_enabled(
            "timeline",
            self.recorder_timeline_window is not None,
        )

    def _on_workbench_view_selected(self, view):
        if view == "library":
            library = self._ensure_library_view()
            library.refresh()
        elif view == "capture":
            self._ensure_capture_view()
        elif view == "review" and self.recorder_session is not None:
            self.open_recorder_review()

    def _available_workbench_view(self):
        if self.recorder_workbench is None:
            return None
        current = self.recorder_workbench.selected_view()
        if current == "capture" and self.recorder_window is not None:
            return "capture"
        if current == "review" and self.recorder_review_window is not None:
            return "review"
        if current == "timeline" and self.recorder_timeline_window is not None:
            return "timeline"
        return None

    def _return_from_workbench_library(self):
        if self.recorder_workbench is not None:
            self.recorder_workbench.select("capture")

    def close_recording_library(self):
        if self.recording_library_window is not None:
            self.recording_library_window.dispose()
        self.recording_library_window = None
        self.recorder_library_return_view = None

    def _open_existing_session_in_workbench(self, session_dir):
        if self._recorder_is_active() or self.recorder_task_busy:
            if self.recording_library_window is not None:
                self.recording_library_window.status_var.set(
                    "当前仍在录制或保存，不能切换历史任务。"
                )
            return False
        session_dir = Path(session_dir).resolve()
        if (
            self.recorder_session is not None
            and self.recorder_session.session_dir == session_dir
        ):
            session = self.recorder_session
        else:
            session = FeatureRecordingSession.open_existing(session_dir)
        if (
                self.recorder_session is not None
                and self.recorder_session.session_dir != session.session_dir
        ):
            if not self._close_recorder_timeline():
                if self.recording_library_window is not None:
                    self.recording_library_window.status_var.set(
                        "补录仍在进行，完成或丢弃后才能切换历史任务。"
                    )
                return False
            self._close_recorder_review()
            self.recorder_session.close()
        self.recorder_session = session
        self.recorder_feature_plan = session.feature_plan
        self.recorder_selected_step_ids = set(session.step_states)
        self.recorder_output_dir = session.session_dir
        self.recorder_output_root_var.set(str(session.output_root))
        self.recorder_review_return_view = "library"
        self.open_recorder_review()
        return True

    def _retire_recording_session(self, session_dir, require_knowledge):
        session_dir = Path(session_dir).resolve()
        if self._recorder_is_active() or self.recorder_task_busy:
            raise RuntimeError("当前仍在录制或保存，不能退役 Run")
        if (
            self.recorder_session is not None
            and self.recorder_session.session_dir == session_dir
        ):
            if not self._close_recorder_timeline():
                raise RuntimeError("补录仍在进行，不能退役当前 Run")
            self._close_recorder_review()
            self.recorder_session.close()
            self.recorder_session = None
            self.recorder_output_dir = None
            self.recorder_pending_step_id = None
            self._hide_recorder_overlay()
            self._set_recorder_plan_locked(False)
            self._refresh_workbench_context()
        return retire_recording_session(
            session_dir,
            require_distilled_knowledge=bool(require_knowledge),
        )

    def _export_feature_delivery(
            self,
            feature_path,
            output_path,
            output_root,
        ):
        if self._recorder_is_active() or self.recorder_task_busy:
            raise RuntimeError("当前仍在录制或保存，不能导出Feature")
        output_root = Path(output_root).resolve()
        key_prefix = self._portability_key_prefix(output_root)
        if self.recorder_operations.list_active(key_prefix=key_prefix):
            raise RuntimeError("已有录屏包导入或导出任务正在执行")
        self.recorder_status_var.set("正在导出Feature录制资料...")
        self.recorder_portability_active_kinds[key_prefix] = "export"
        try:
            self.recorder_operations.submit(
                key_prefix + "feature-export",
                self._portability_service(output_root).export_feature,
                Path(feature_path),
                Path(output_path),
                context={
                    "kind": "feature_export",
                    "recording_root": str(output_root),
                },
            )
        except Exception:
            self.recorder_portability_active_kinds.pop(key_prefix, None)
            raise

    def _export_feature_scenarios(
            self,
            feature_path,
            scenario_ids,
            output_path,
            output_root,
    ):
        if self._recorder_is_active() or self.recorder_task_busy:
            raise RuntimeError("当前仍在录制或保存，不能导出场景")
        output_root = Path(output_root).resolve()
        key_prefix = self._portability_key_prefix(output_root)
        if self.recorder_operations.list_active(key_prefix=key_prefix):
            raise RuntimeError("已有录屏包导入或导出任务正在执行")
        self.recorder_status_var.set("正在导出当前场景录制资料...")
        self.recorder_portability_active_kinds[key_prefix] = "export"
        try:
            self.recorder_operations.submit(
                key_prefix + "scenario-export",
                self._portability_service(
                    output_root
                ).export_feature_scenarios,
                Path(feature_path),
                tuple(str(value) for value in scenario_ids),
                Path(output_path),
                context={
                    "kind": "feature_export",
                    "recording_root": str(output_root),
                },
            )
        except Exception:
            self.recorder_portability_active_kinds.pop(key_prefix, None)
            raise

    @staticmethod
    def _preview_feature_delivery(package_path):
        return preview_feature_delivery(package_path, Paths.BASE_DIR)

    def _import_feature_delivery(self, package_path, output_root):
        if self._recorder_is_active() or self.recorder_task_busy:
            raise RuntimeError("当前仍在录制或保存，不能导入Feature录制资料")
        output_root = Path(output_root).resolve()
        key_prefix = self._portability_key_prefix(output_root)
        if self.recorder_operations.list_active(key_prefix=key_prefix):
            raise RuntimeError("已有录屏包导入或导出任务正在执行")
        self.recorder_status_var.set("正在校验并导入Feature录制资料...")
        self.recorder_portability_active_kinds[key_prefix] = "import"
        try:
            self.recorder_operations.submit(
                key_prefix + "feature-import",
                self._portability_service(output_root).import_feature,
                Path(package_path),
                Paths.BASE_DIR,
                context={
                    "kind": "feature_import",
                    "recording_root": str(output_root),
                },
            )
        except Exception:
            self.recorder_portability_active_kinds.pop(key_prefix, None)
            raise

    def _portability_service(self, output_root):
        output_root = Path(output_root).resolve()
        key = str(output_root).casefold()
        service = self.recorder_portability_services.get(key)
        if service is None:
            service = RecordingPortabilityService(output_root)
            self.recorder_portability_services[key] = service
        return service

    @staticmethod
    def _portability_key_prefix(output_root):
        return (
            "portability:"
            + stable_digest(str(Path(output_root).resolve()), length=16)
            + ":"
        )

    def resume_existing_recording(self, session, step_id):
        if self._recorder_is_active() or self.recorder_task_busy:
            self.recorder_status_var.set("当前仍在录制或保存，不能切换历史任务。")
            return False
        if (
            self.recorder_session is not None
            and self.recorder_session.session_dir != session.session_dir
        ):
            self.recorder_session.close()
        session.reopen_for_recording()
        self.recorder_session = session
        self.recorder_feature_plan = session.feature_plan
        self.recorder_feature_path_var.set(str(session.feature_plan.source_path))
        self.recorder_scenario_map = {
            session.scenario_plan.id: session.scenario_plan
        }
        self.recorder_scenario_var.set(session.scenario_plan.id)
        self.recorder_selected_step_ids = set(session.step_states)
        self.recorder_output_dir = session.session_dir
        self.recorder_output_root_var.set(str(session.output_root))
        self.recorder_backend_var.set(session.config.backend)
        self.recorder_video_var.set(bool(session.config.with_video))
        self.recorder_screenshot_var.set(bool(session.config.with_screenshots))
        self.recorder_tree_var.set(bool(session.config.with_tree))
        self.recorder_tree_depth_var.set(str(session.config.tree_max_depth))
        self.recorder_tree_nodes_var.set(str(session.config.tree_max_nodes))
        self.recorder_target_process_only_var.set(
            bool(session.config.capture_target_process_only)
        )
        self.recorder_window_mode_var.set("auto")
        self.recorder_selected_window_handles = ()
        self.recorder_primary_window_handle = None
        self.open_recorder_tool()
        self._restore_recorder_panel_state()
        if step_id and self.recorder_step_tree.exists(step_id):
            self.recorder_step_tree.selection_set(step_id)
            self.recorder_step_tree.focus(step_id)
            self.recorder_step_tree.see(step_id)
        self.recorder_status_var.set(
            "历史任务已恢复。点击“录制当前 Step”会新增录制版本，"
            "旧版本不会删除。"
        )
        return True

    def open_recorder_tool(self):
        self.open_recorder_workbench("capture")

    def _ensure_recorder_workbench(self):
        if self.recorder_workbench is not None:
            try:
                if self.recorder_workbench.window.winfo_exists():
                    return self.recorder_workbench
            except tk.TclError:
                pass
        self.recorder_workbench = RecorderWorkbench(
            self.app,
            on_close=self.close_recorder_tool,
            on_view_selected=self._on_workbench_view_selected,
        )
        self.recorder_workbench.set_view_enabled("review", False)
        self.recorder_workbench.set_view_enabled("timeline", False)
        return self.recorder_workbench

    def _refresh_workbench_context(self, step_id=None):
        if self.recorder_workbench is None:
            return None
        self.recorder_workbench_context_sequence += 1
        sequence = self.recorder_workbench_context_sequence
        if self.recorder_session is None:
            self.recorder_operations.abandon_prefix(
                self.recorder_workbench_context_operation_key,
            )
            self.recorder_query_service = None
            self.recorder_workbench.set_context(None)
            self._sync_workbench_view_states()
            return None
        self.recorder_query_service = RecorderQueryService(
            self.recorder_session,
            operation_coordinator=self.recorder_operations,
        )
        resolved_step_id = step_id or self._selected_recorder_step_id()
        self.recorder_operations.submit(
            self.recorder_workbench_context_operation_key,
            self._query_workbench_context,
            self.recorder_query_service,
            resolved_step_id,
            context={
                "sequence": sequence,
                "session_dir": str(
                    Path(self.recorder_session.session_dir).resolve()
                ),
                "step_id": resolved_step_id,
            },
            pass_token=True,
        )
        self._schedule_recorder_poll()
        return None

    @staticmethod
    def _query_workbench_context(token, query_service, step_id):
        token.raise_if_cancelled()
        model = query_service.get_workbench(step_id)
        token.raise_if_cancelled()
        return model

    def _handle_workbench_context_result(self, task):
        context = dict(task.context or {})
        session = self.recorder_session
        if any((
                task.status in {"cancelled", "superseded"},
                self.recorder_workbench is None,
                session is None,
                context.get("sequence")
                != self.recorder_workbench_context_sequence,
                context.get("session_dir")
                != str(Path(session.session_dir).resolve()),
        )):
            return
        if task.error is not None:
            self.recorder_status_var.set(
                "工作台状态更新失败: "
                f"{type(task.error).__name__}: {task.error}"
            )
            return
        model = task.value
        if model is None:
            return
        resolved_step_id = context.get("step_id") or model.selected_step_id
        viewed_take_id = None
        review = getattr(self, "recorder_review_window", None)
        if (
                review is not None
                and review.selected_step_id() == resolved_step_id
        ):
            viewed_take = review.selected_take_entry()
            viewed_take_id = viewed_take.take_id if viewed_take else None
        self.recorder_workbench.set_context(
            model,
            step_id=resolved_step_id or model.selected_step_id,
            take_id=viewed_take_id,
        )
        self._sync_workbench_view_states()

    def _build_recorder_ui(self):
        window = self.recorder_window
        header = ttk.Frame(window)
        header.pack(fill="x", padx=12, pady=(10, 6))
        ttk.Label(
            header,
            text="Feature 与录制",
            font=("Microsoft YaHei UI", 14, "bold"),
            anchor="w",
        ).pack(side="left")
        ttk.Label(
            header,
            textvariable=self.recorder_progress_var,
            anchor="e",
        ).pack(side="right")

        workspace = ttk.Panedwindow(window, orient="horizontal")
        workspace.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        setup = ttk.Frame(workspace)
        work = ttk.Frame(workspace)
        workspace.add(setup, weight=2)
        workspace.add(work, weight=5)

        source = ttk.LabelFrame(setup, text="当前 Feature")
        self.recorder_source_frame = source
        source.pack(fill="x", pady=(0, 5))
        source.columnconfigure(1, weight=1)

        ttk.Label(source, text="Feature").grid(row=0, column=0, sticky="w", padx=8, pady=7)
        self.recorder_feature_entry = ttk.Entry(
            source,
            textvariable=self.recorder_feature_path_var,
            state="readonly",
        )
        self.recorder_feature_entry.grid(row=0, column=1, sticky="ew", padx=5, pady=7)
        ttk.Button(
            source,
            text="选择 Feature",
            command=self.choose_recorder_feature,
        ).grid(
            row=0, column=2, padx=4, pady=7
        )
        ttk.Button(
            source,
            text="重新加载",
            command=self.refresh_recorder_feature_workspace,
        ).grid(
            row=0, column=3, padx=(0, 8), pady=7
        )

        feature_frame = ttk.Frame(source)
        feature_frame.grid(
            row=1,
            column=0,
            columnspan=4,
            sticky="nsew",
            padx=8,
            pady=(0, 6),
        )
        feature_frame.rowconfigure(0, weight=1)
        feature_frame.columnconfigure(0, weight=1)
        self.recorder_feature_tree = ttk.Treeview(
            feature_frame,
            columns=("recording", "issues"),
            show="tree headings",
            selectmode="browse",
            height=12,
        )
        self.recorder_feature_tree.heading("#0", text="Feature / 场景")
        self.recorder_feature_tree.heading("recording", text="录制状态")
        self.recorder_feature_tree.heading("issues", text="问题")
        self.recorder_feature_tree.column("#0", width=280, minwidth=160)
        self.recorder_feature_tree.column(
            "recording", width=118, minwidth=90, stretch=False
        )
        self.recorder_feature_tree.column(
            "issues", width=70, minwidth=55, stretch=False
        )
        feature_scroll = ttk.Scrollbar(
            feature_frame,
            orient="vertical",
            command=self.recorder_feature_tree.yview,
        )
        self.recorder_feature_tree.configure(
            yscrollcommand=feature_scroll.set,
        )
        self.recorder_feature_tree.grid(row=0, column=0, sticky="nsew")
        feature_scroll.grid(row=0, column=1, sticky="ns")
        self.recorder_feature_tree.bind(
            "<<TreeviewSelect>>",
            self.on_recorder_feature_selected,
        )
        ttk.Label(
            source,
            textvariable=self.recorder_feature_detail_var,
            anchor="w",
            justify="left",
            wraplength=390,
        ).grid(
            row=2,
            column=0,
            columnspan=4,
            sticky="ew",
            padx=8,
            pady=(0, 5),
        )
        self.recorder_materials_button = ttk.Menubutton(
            source,
            text="录制资料",
        )
        self.recorder_materials_button.grid(
            row=3, column=0, sticky="w", padx=8, pady=(0, 8)
        )
        self.recorder_materials_menu = tk.Menu(
            self.recorder_materials_button,
            tearoff=False,
        )
        self.recorder_materials_menu.add_command(
            label="导入录制资料",
            command=self.import_feature_recordings,
        )
        self.recorder_materials_menu.add_command(
            label="导出当前 Feature",
            command=self.export_selected_recorder_feature,
        )
        self.recorder_materials_menu.add_command(
            label="导出当前场景",
            command=self.export_selected_recorder_scenario,
        )
        self.recorder_materials_button.configure(
            menu=self.recorder_materials_menu,
        )
        ttk.Label(source, text="窗口").grid(
            row=4,
            column=0,
            sticky="w",
            padx=8,
            pady=(0, 4),
        )
        self.recorder_window_summary_label = ttk.Label(
            source,
            textvariable=self.recorder_window_summary_var,
            anchor="w",
            justify="left",
            wraplength=280,
        )
        self.recorder_window_summary_label.grid(
            row=5,
            column=0,
            columnspan=4,
            sticky="ew",
            padx=8,
            pady=(0, 8),
        )
        self.recorder_window_select_button = ttk.Button(
            source,
            text="限制窗口...",
            command=self.limit_recorder_windows,
        )
        self.recorder_window_select_button.grid(
            row=4,
            column=1,
            columnspan=3,
            sticky="e",
            padx=(4, 8),
            pady=(0, 4),
        )
        source.bind(
            "<Configure>",
            self._update_recorder_window_summary_wrap,
            add="+",
        )

        steps_frame = ttk.LabelFrame(work, text="场景步骤")
        steps_frame.pack(fill="both", expand=True)
        steps_frame.rowconfigure(0, weight=1)
        steps_frame.columnconfigure(0, weight=1)
        self.recorder_step_tree = ttk.Treeview(
            steps_frame,
            columns=("status", "keyword", "line", "text", "takes"),
            show="headings",
            selectmode="browse",
        )
        headings = (
            ("status", "状态", 80),
            ("keyword", "关键字", 150),
            ("line", "行", 50),
            ("text", "步骤", 470),
            ("takes", "版本", 55),
        )
        for column, text, width in headings:
            self.recorder_step_tree.heading(column, text=text)
            self.recorder_step_tree.column(column, width=width, minwidth=40, stretch=column == "text")
        scroll = ttk.Scrollbar(steps_frame, orient="vertical", command=self.recorder_step_tree.yview)
        self.recorder_step_tree.configure(yscrollcommand=scroll.set)
        self.recorder_step_tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.recorder_step_tree.bind(
            "<<TreeviewSelect>>",
            self._on_recorder_step_selected,
        )

        step_context = ttk.LabelFrame(
            steps_frame,
            text="当前 Step 业务补充（可选）",
        )
        step_context.grid(
            row=1,
            column=0,
            columnspan=2,
            sticky="ew",
            padx=5,
            pady=(2, 5),
        )
        step_context.columnconfigure(1, weight=1)
        ttk.Label(step_context, text="补充事实").grid(
            row=0, column=0, sticky="w", padx=(8, 4), pady=6
        )
        self.recorder_step_business_context_entry = ttk.Entry(
            step_context,
            textvariable=self.recorder_step_business_context_var,
        )
        self.recorder_step_business_context_entry.grid(
            row=0,
            column=1,
            columnspan=3,
            sticky="ew",
            padx=(0, 8),
            pady=6,
        )
        self.recorder_step_context_save_button = ttk.Button(
            step_context,
            text="保存业务补充",
            command=self.save_selected_step_user_context,
        )
        self.recorder_step_context_save_button.grid(
            row=0, column=4, padx=(0, 6), pady=6
        )
        ttk.Label(
            step_context,
            textvariable=self.recorder_step_context_revision_var,
        ).grid(row=0, column=5, padx=(0, 8), pady=6)

        actions = ttk.LabelFrame(work, text="当前 Step 主操作")
        actions.pack(fill="x", pady=(8, 0))
        actions.columnconfigure(1, weight=1)
        ttk.Label(
            actions,
            textvariable=self.recorder_privacy_warning_var,
            foreground="#9a3412",
            anchor="w",
        ).grid(
            row=0,
            column=0,
            columnspan=7,
            sticky="ew",
            padx=8,
            pady=(7, 2),
        )
        ttk.Label(actions, text="本次录制说明（仅审阅）").grid(
            row=1,
            column=0,
            sticky="w",
            padx=8,
            pady=(2, 0),
        )
        self.recorder_take_summary_entry = ttk.Entry(actions)
        self.recorder_take_summary_entry.grid(
            row=1,
            column=1,
            columnspan=6,
            sticky="ew",
            padx=(5, 8),
            pady=(2, 0),
        )
        ttk.Label(
            actions,
            textvariable=self.recorder_take_summary_help_var,
            anchor="w",
        ).grid(
            row=2,
            column=0,
            columnspan=7,
            sticky="ew",
            padx=8,
            pady=(2, 6),
        )

        self.recorder_start_button = ttk.Button(
            actions,
            text="开始录制此场景",
            command=self.run_primary_recorder_action,
        )
        self.recorder_start_button.grid(row=3, column=0, padx=8, pady=(0, 8))
        self.recorder_pause_button = ttk.Button(
            actions,
            text="暂停录制 F7",
            command=self.toggle_current_step_pause,
        )
        self.recorder_pause_button.grid(row=3, column=1, sticky="w", padx=4, pady=(0, 8))
        self.recorder_more_button = ttk.Menubutton(actions, text="更多")
        self.recorder_more_button.grid(
            row=3,
            column=2,
            sticky="w",
            padx=4,
            pady=(0, 8),
        )
        self.recorder_more_menu = tk.Menu(
            self.recorder_more_button,
            tearoff=False,
        )
        self.recorder_more_menu.add_command(
            label="检查此处 F9",
            command=self.capture_current_observation,
        )
        self.recorder_more_menu.add_command(
            label="放弃本次录制 Shift+F11",
            command=self.cancel_current_step_recording,
        )
        self.recorder_more_menu.add_command(
            label="跳过当前 Step",
            command=self.skip_selected_step_recording,
        )
        self.recorder_more_menu.add_separator()
        self.recorder_more_menu.add_command(
            label="完成录制任务",
            command=self.finalize_recording_session,
        )
        self.recorder_more_menu.add_command(
            label="审阅并交给 Copilot",
            command=self.open_recorder_review,
        )
        self.recorder_more_menu.add_command(
            label="结束当前任务",
            command=self.reset_recording_session,
        )
        self.recorder_more_button.configure(menu=self.recorder_more_menu)

        ttk.Label(window, textvariable=self.recorder_status_var, anchor="w").pack(
            fill="x", padx=12, pady=(2, 10)
        )
        self._update_recorder_controls()

    def choose_recorder_feature(self):
        path = filedialog.askopenfilename(
            parent=self.recorder_window,
            title="选择 Feature",
            initialdir=str(Paths.TEST_FEATURES_DIR),
            filetypes=(("Gherkin Feature", "*.feature"), ("All files", "*.*")),
        )
        if path:
            self.recorder_feature_path_var.set(path)
            self.load_recorder_feature()

    def refresh_recorder_feature_workspace(self, *, announce=True):
        feature_path_var = getattr(self, "recorder_feature_path_var", None)
        if feature_path_var is None:
            return
        path = feature_path_var.get().strip()
        if not path:
            self.recorder_feature_workspace = None
            self._render_recorder_feature_workspace()
            if announce:
                self.recorder_status_var.set("请选择一个 Feature 文件。")
            return
        source_path = Path(path).resolve()
        try:
            source_path.relative_to(Paths.BASE_DIR.resolve())
            if not source_path.is_file() or source_path.suffix.casefold() != ".feature":
                raise FileNotFoundError(source_path)
            recording_root = Path(
                self.recorder_output_root_var.get().strip()
                or Paths.ARTIFACTS_DIR / "recording_sessions"
            ).resolve()
            feature, warnings = FeatureWorkspaceQueryService(
                source_path.parent,
                recording_root,
            ).get_feature(source_path)
        except Exception as error:
            self.recorder_status_var.set(
                f"Feature 加载失败: {type(error).__name__}: {error}"
            )
            return
        self.recorder_feature_path_var.set(str(source_path))
        plan = load_feature_plan(source_path)
        self.recorder_feature_plan = plan
        self.recorder_scenario_map = {
            scenario.id: scenario for scenario in plan.scenarios
        }
        self.recorder_feature_workspace = feature
        self._render_recorder_feature_workspace()
        if announce:
            message = (
                f"已打开 {feature.name}："
                f"{feature.recorded_scenario_count}/{feature.scenario_count} 个场景已录制。"
            )
            if warnings:
                message += " " + "；".join(warnings)
            self.recorder_status_var.set(message)

    def _render_recorder_feature_workspace(self):
        tree = self.recorder_feature_tree
        if tree is None:
            return
        selected_scenario_id = self._selected_recorder_scenario_id()
        tree.delete(*tree.get_children())
        self.recorder_feature_rows = {}
        self.recorder_feature_scenario_rows = {}
        feature = self.recorder_feature_workspace
        if feature is None:
            self.recorder_feature_detail_var.set("尚未选择 Feature。")
            self.recorder_progress_var.set("请选择 Feature")
            return
        row_id = "feature-" + stable_digest(
            feature.source_path,
            length=16,
        )
        tree.insert(
            "",
            "end",
            iid=row_id,
            text=feature.name,
            open=True,
            values=(
                feature.recording_label,
                f"{len(feature.issues)} 项" if feature.issues else "—",
            ),
            tags=(("issue",) if feature.issues else ()),
        )
        self.recorder_feature_rows[row_id] = feature
        selected_row = None
        for scenario in feature.scenarios:
            scenario_row_id = "scenario-row-" + stable_digest(
                feature.source_path,
                scenario.scenario_id,
                length=16,
            )
            tree.insert(
                row_id,
                "end",
                iid=scenario_row_id,
                text=scenario.name,
                values=(
                    scenario.recording_label,
                    "1 项" if scenario.issue else "—",
                ),
                tags=(("issue",) if scenario.issue else ()),
            )
            self.recorder_feature_scenario_rows[scenario_row_id] = (
                feature,
                scenario,
            )
            if scenario.scenario_id == selected_scenario_id:
                selected_row = scenario_row_id
        tree.tag_configure("issue", foreground="#9a3412")
        scenario_rows = tuple(self.recorder_feature_scenario_rows)
        selected_row = selected_row or (
            scenario_rows[0] if scenario_rows else row_id
        )
        if selected_row is not None:
            tree.selection_set(selected_row)
            tree.focus(selected_row)
            tree.see(selected_row)
            self.on_recorder_feature_selected()

    def on_recorder_feature_selected(self, event=None):
        feature = self.recorder_feature_workspace
        if feature is None:
            return
        selected_scenario = self._selected_recorder_feature_scenario()
        if selected_scenario is None:
            scenario_rows = tuple(self.recorder_feature_scenario_rows)
            if scenario_rows:
                self.recorder_feature_tree.selection_set(scenario_rows[0])
                self.recorder_feature_tree.focus(scenario_rows[0])
                selected_scenario = self._selected_recorder_feature_scenario()
        if selected_scenario is None:
            return
        detail = (
            f"{feature.name}\n{feature.source_relpath}\n"
            f"当前场景：{selected_scenario.name} · "
            f"{selected_scenario.recording_label}"
        )
        if feature.issues:
            detail += "\n问题：" + "；".join(feature.issues)
        if selected_scenario.issue:
            detail += f"\n场景问题：{selected_scenario.issue}"
        self.recorder_feature_detail_var.set(detail)
        if self.recorder_session is None:
            self.recorder_scenario_var.set(selected_scenario.scenario_id)
            scenario_plan = self._scenario_plan(selected_scenario.scenario_id)
            if scenario_plan is not None:
                self._render_recorder_steps(scenario_plan)
        self._update_recorder_controls()

    def _selected_recorder_features(self):
        return (
            (self.recorder_feature_workspace,)
            if self.recorder_feature_workspace is not None
            else ()
        )

    @staticmethod
    def _feature_has_exportable_recording(feature):
        return feature.exportable_recording_count > 0

    def _selected_recorder_feature_scenario(self):
        if self.recorder_feature_tree is None:
            return None
        selected = self.recorder_feature_tree.selection()
        if len(selected) != 1:
            return None
        value = self.recorder_feature_scenario_rows.get(selected[0])
        return value[1] if value is not None else None

    def _selected_recorder_scenario_id(self):
        if self.recorder_session is not None:
            return self.recorder_session.scenario_plan.id
        selected = self._selected_recorder_feature_scenario()
        return selected.scenario_id if selected is not None else None

    def _scenario_plan(self, scenario_id):
        return next((
            scenario
            for scenario in (
                self.recorder_feature_plan.scenarios
                if self.recorder_feature_plan is not None
                else ()
            )
            if scenario.id == scenario_id
        ), None)

    def export_selected_recorder_feature(self):
        features = self._selected_recorder_features()
        if not features or any(
            not self._feature_has_exportable_recording(feature)
            for feature in features
        ):
            self.recorder_status_var.set(
                "所选 Feature 没有可导出的当前录制资料。"
            )
            return
        feature = features[0]
        output = filedialog.asksaveasfilename(
            parent=self.recorder_window,
            title="导出 Feature 录制资料",
            defaultextension=".zip",
            filetypes=(("Feature 录制资料", "*.zip"),),
            initialfile=f"{Path(feature.source_path).stem}.delivery.zip",
        )
        if not output:
            return
        try:
            self._export_feature_delivery(
                feature.source_path,
                Path(output),
                self.recorder_output_root_var.get(),
            )
        except Exception as error:
            self.recorder_status_var.set(
                "启动Feature录制资料导出失败: "
                f"{type(error).__name__}: {error}"
            )

    def export_selected_recorder_scenario(self):
        feature = self.recorder_feature_workspace
        scenario = self._selected_recorder_feature_scenario()
        if feature is None or scenario is None or not scenario.exportable:
            self.recorder_status_var.set(
                "当前场景没有可导出的有效录制资料。"
            )
            return
        output = filedialog.asksaveasfilename(
            parent=self.recorder_window,
            title="导出当前场景录制资料",
            defaultextension=".zip",
            filetypes=(("Feature 录制资料", "*.zip"),),
            initialfile=(
                f"{Path(feature.source_path).stem}-"
                f"{stable_digest(scenario.scenario_id, length=8)}.delivery.zip"
            ),
        )
        if not output:
            return
        try:
            self._export_feature_scenarios(
                feature.source_path,
                (scenario.scenario_id,),
                Path(output),
                self.recorder_output_root_var.get(),
            )
        except Exception as error:
            self.recorder_status_var.set(
                "启动场景录制资料导出失败: "
                f"{type(error).__name__}: {error}"
            )

    def import_feature_recordings(self):
        package = filedialog.askopenfilename(
            parent=self.recorder_window,
            title="导入 Feature 录制资料",
            filetypes=(("Feature 录制资料", "*.zip"),),
        )
        if not package:
            return
        package_path = Path(package)
        try:
            if not is_feature_delivery_package(package_path):
                raise ValueError("所选文件不是Feature录制资料包")
            preview = self._preview_feature_delivery(package_path)
            feature = preview.get("feature") or {}
            target_status = str(preview.get("target_status") or "")
            if target_status == "conflict":
                detail = str(
                    preview.get("conflict_summary")
                    or "目标Feature与传入Feature内容不同。"
                )
                messagebox.showwarning(
                    "Feature内容冲突",
                    f"Feature：{feature.get('name') or feature.get('id')}\n"
                    f"目标：{preview.get('target_path')}\n\n"
                    f"{detail}\n\n"
                    "未执行导入。请先在项目中确认并合并Feature。",
                    parent=self.recorder_window,
                )
                self.recorder_status_var.set(
                    "目标Feature内容不同，未导入录制资料。"
                )
                return
            action = "新建" if target_status == "create" else "复用"
            total = int(feature.get("scenario_count") or 0)
            recorded = int(
                feature.get("recorded_scenario_count")
                or preview.get("run_count")
                or 0
            )
            if not messagebox.askyesno(
                "导入 Feature 录制资料",
                f"Feature：{feature.get('name') or feature.get('id')}\n"
                f"录制覆盖：{recorded}/{total} 个场景\n"
                f"目标：{preview.get('target_path')}\n"
                f"处理：{action}目标 Feature\n\n"
                "确认导入？",
                parent=self.recorder_window,
            ):
                self.recorder_status_var.set("已取消导入录制资料。")
                return
            self._import_feature_delivery(
                package_path,
                self.recorder_output_root_var.get(),
            )
        except Exception as error:
            self.recorder_status_var.set(
                "启动Feature录制资料导入失败: "
                f"{type(error).__name__}: {error}"
            )

    def load_recorder_feature(self):
        if self.recorder_session is not None:
            self.recorder_status_var.set("请先结束当前任务再更换 Feature。")
            return
        path = self.recorder_feature_path_var.get().strip()
        if not path:
            self.recorder_status_var.set("请选择 Feature 文件。")
            return
        try:
            plan = load_feature_plan(path, ensure_identity=True)
        except Exception as error:
            self.recorder_status_var.set(f"Feature 加载失败: {type(error).__name__}: {error}")
            return
        self.recorder_feature_plan = plan
        self.recorder_scenario_map = {
            scenario.id: scenario for scenario in plan.scenarios
        }
        self.refresh_recorder_feature_workspace(announce=True)
        self._update_recorder_controls()

    def refresh_recorder_target_windows(self):
        if self._recorder_is_active():
            return
        previous_handles = set(self.recorder_selected_window_handles)
        previous_primary = self.recorder_primary_window_handle
        backend = self.recorder_backend_var.get().strip() or self.backend
        windows = [
            item
            for item in get_open_windows(backend=backend)
            if item.get("process_id") != os.getpid()
            and str(item.get("class_name") or "").casefold()
            not in {"shell_traywnd", "shell_secondarytraywnd"}
            and is_recordable_window_handle(item.get("handle"))
        ]
        windows.sort(key=lambda item: (str(item.get("title") or "").casefold(), item.get("handle") or 0))
        self.recorder_target_window_map = {
            int(item["handle"]): item
            for item in windows
        }
        self.recorder_selected_window_handles = tuple(
            int(item["handle"])
            for item in windows
            if int(item["handle"]) in previous_handles
        )
        if previous_primary in self.recorder_selected_window_handles:
            self.recorder_primary_window_handle = previous_primary
        else:
            self.recorder_primary_window_handle = next(
                iter(self.recorder_selected_window_handles),
                None,
            )
        self._update_recorder_window_summary()
        if not windows:
            self.recorder_status_var.set("没有发现可录制的目标窗口，请先启动被测应用。")

    def open_recorder_window_selector(self):
        if self._recorder_is_active() or self.recorder_task_busy:
            self.recorder_status_var.set("录制或保存中不能更换窗口。")
            return
        RecorderWindowSelector(
            self.recorder_window,
            list(self.recorder_target_window_map.values()),
            self.recorder_selected_window_handles,
            self.recorder_primary_window_handle,
            self._apply_recorder_window_selection,
            self._refresh_recorder_windows_for_selector,
            allow_empty=self.recorder_window_mode_var.get() == "auto",
        ).show()

    def limit_recorder_windows(self):
        if self.recorder_window_mode_var.get() != "strict":
            self.recorder_window_mode_var.set("strict")
            self.on_recorder_window_mode_changed()
        self.open_recorder_window_selector()

    def on_recorder_window_mode_changed(self, event=None):
        if self.recorder_window_mode_var.get() == "strict":
            self.refresh_recorder_target_windows()
        elif not self.recorder_session:
            self.recorder_selected_window_handles = ()
            self.recorder_primary_window_handle = None
        self._update_recorder_window_summary()

    def _refresh_recorder_windows_for_selector(self):
        self.refresh_recorder_target_windows()
        return list(self.recorder_target_window_map.values())

    def _apply_recorder_window_selection(self, handles, primary_handle):
        self.recorder_selected_window_handles = tuple(handles)
        self.recorder_primary_window_handle = (
            int(primary_handle) if primary_handle is not None else None
        )
        self._update_recorder_window_summary()
        self.recorder_status_var.set(
            f"当前 Step 将录制 {len(handles)} 个窗口。"
        )

    def _update_recorder_window_summary(self):
        if self.recorder_window_mode_var.get() == "auto":
            self.recorder_window_summary_var.set(
                "自动识别，无需提前选择"
            )
            return
        self.recorder_window_summary_var.set(format_window_selection_summary(
            self.recorder_target_window_map,
            self.recorder_selected_window_handles,
            self.recorder_primary_window_handle,
            "strict",
        ))

    def _update_recorder_window_summary_wrap(self, event=None):
        label = self.recorder_window_summary_label
        if label is None:
            return
        width = int(
            getattr(event, "width", 0)
            or self.recorder_source_frame.winfo_width()
        ) - 16
        wraplength = max(180, width)
        if int(label.cget("wraplength") or 0) != wraplength:
            label.configure(wraplength=wraplength)

    def on_recorder_scenario_selected(self, event=None):
        if self.recorder_session is not None:
            return
        scenario = self.recorder_scenario_map.get(
            self.recorder_scenario_var.get()
        )
        if scenario is not None:
            self._render_recorder_steps(scenario)

    def _render_recorder_steps(self, scenario):
        tree = self.recorder_step_tree
        tree.delete(*tree.get_children())
        self.recorder_step_map = {step.id: step for step in scenario.steps}
        self.recorder_selected_step_ids = {step.id for step in scenario.steps}
        for step in scenario.steps:
            tree.insert("", "end", iid=step.id, values=(
                "待录制",
                f"Background {step.keyword}" if step.is_background else step.keyword,
                step.line,
                step.text,
                0,
            ))
        children = tree.get_children()
        if children:
            tree.selection_set(children[0])
            tree.focus(children[0])
        self._load_selected_step_user_context()
        self._update_progress()

    def _on_recorder_step_selected(self, event=None):
        self._load_selected_step_user_context()
        self._update_recorder_controls()

    def _load_selected_step_user_context(self):
        step_id = self._selected_recorder_step_id()
        self.recorder_step_context_step_id = step_id
        context = None
        if self.recorder_session is not None and step_id:
            try:
                context = RecordingAnnotationRepository(
                    self.recorder_session.session_dir
                ).current_step_context(step_id)
            except Exception as error:
                self.recorder_step_context_revision = 0
                self.recorder_step_context_revision_var.set("读取失败")
                self.recorder_status_var.set(
                    "读取Step业务说明失败: "
                    f"{type(error).__name__}: {error}"
                )
                return
        elif step_id:
            context = {
                "business_context": self.recorder_pending_step_contexts.get(
                    step_id,
                    "",
                ),
                "revision": 0,
            }
        self.recorder_step_business_context_var.set(
            step_business_context_text(context)
        )
        self.recorder_step_context_revision = int(
            (context or {}).get("revision") or 0
        )
        self.recorder_step_context_revision_var.set(
            f"版本 {self.recorder_step_context_revision}"
        )

    def save_selected_step_user_context(self):
        if self.recorder_task_busy or self._recorder_is_active():
            return
        step_id = self._selected_recorder_step_id()
        session = self.recorder_session
        valid_step_ids = (
            set(session.step_states)
            if session is not None
            else set(self.recorder_step_map)
        )
        if not step_id or step_id not in valid_step_ids:
            self.recorder_status_var.set("请选择当前场景中的 Step。")
            return
        if session is None:
            self.recorder_pending_step_contexts[step_id] = (
                self.recorder_step_business_context_var.get().strip()
            )
            self.recorder_step_context_revision_var.set("录制开始时保存")
            self.recorder_status_var.set(
                "业务补充已暂存，将在开始录制此场景时保存。"
            )
            return
        if self.recorder_step_context_step_id != step_id:
            self._load_selected_step_user_context()
        business_context = (
            self.recorder_step_business_context_var.get().strip()
        )
        self.recorder_task_busy = True
        self.recorder_status_var.set("正在保存Step业务说明...")
        self._update_recorder_controls()
        self.recorder_operations.submit(
            f"annotation:{session.run_id}:{step_id}",
            session.save_step_user_context,
            step_id,
            business_context=business_context,
            expected_revision=self.recorder_step_context_revision,
            context={"step_id": step_id},
        )

    def _restore_recorder_panel_state(self):
        if self.recorder_feature_plan is None:
            return
        if not self.recorder_scenario_map:
            self.recorder_scenario_map = {
                scenario.id: scenario
                for scenario in self.recorder_feature_plan.scenarios
            }
        scenario = None
        if self.recorder_session is not None:
            scenario = self.recorder_session.scenario_plan
            self.recorder_scenario_var.set(scenario.id)
        else:
            scenario = self.recorder_scenario_map.get(
                self.recorder_scenario_var.get()
            )
        if scenario is None and self.recorder_scenario_map:
            scenario = next(iter(self.recorder_scenario_map.values()))
            self.recorder_scenario_var.set(scenario.id)
        if scenario is None:
            return
        self._render_recorder_steps(scenario)
        if self.recorder_session is not None:
            self.recorder_selected_step_ids = set(self.recorder_session.step_states)
            self._set_recorder_plan_locked(True)
            self._refresh_recorder_step_states()
            self._select_next_pending_step()
        self._update_recorder_controls()

    def create_recording_session(self):
        if self.recorder_session is not None:
            return self.recorder_session
        scenario = self.recorder_scenario_map.get(
            self.recorder_scenario_var.get()
        )
        if self.recorder_feature_plan is None or scenario is None:
            self.recorder_status_var.set("请先加载 Feature 并选择场景。")
            return None
        self.recorder_selected_step_ids = {step.id for step in scenario.steps}
        scenario_status = self._selected_recorder_feature_scenario()
        if (
                scenario_status is not None
                and scenario_status.recording_state == "partial"
                and scenario_status.run_path
        ):
            recording_root = Path(
                self.recorder_output_root_var.get().strip()
            ).resolve()
            run_path = (recording_root / scenario_status.run_path).resolve()
            try:
                run_path.relative_to(recording_root)
                resumed = FeatureRecordingSession.open_existing(run_path)
                current_step_ids = {step.id for step in scenario.steps}
                if any((
                    resumed.scenario_plan.id != scenario.id,
                    set(resumed.step_states) != current_step_ids,
                )):
                    raise ValueError(
                        "未完成Run与当前场景或Step集合不一致"
                    )
                resumed.reopen_for_recording()
            except Exception as error:
                self.recorder_status_var.set(
                    "继续未完成场景失败: "
                    f"{type(error).__name__}: {error}"
                )
                return None
            self.recorder_session = resumed
            self.recorder_selected_step_ids = set(resumed.step_states)
            self.recorder_output_dir = resumed.session_dir
            self.recorder_scenario_var.set(resumed.scenario_plan.id)
            self._render_recorder_steps(resumed.scenario_plan)
            self._set_recorder_plan_locked(True)
            self._refresh_recorder_step_states()
            self._select_next_pending_step()
            self.recorder_status_var.set(
                "已继续当前场景的未完成录制。"
            )
            self._update_recorder_controls()
            self._refresh_workbench_context()
            return resumed
        target_window = self.recorder_target_window_map.get(
            self.recorder_primary_window_handle
        )
        window_mode = self.recorder_window_mode_var.get().strip() or "auto"
        if target_window is None and window_mode == "strict":
            self.recorder_status_var.set("请选择目标窗口；如果列表中没有，请点击“刷新窗口”。")
            return None
        try:
            depth = int(self.recorder_tree_depth_var.get().strip())
            nodes = int(self.recorder_tree_nodes_var.get().strip())
            if depth < 1 or nodes < 1:
                raise ValueError("树深度和节点上限必须大于 0")
            config = RecordingSessionConfig(
                backend=self.recorder_backend_var.get().strip() or self.backend,
                output_root=(
                    Path(self.recorder_output_root_var.get().strip())
                    if self.recorder_output_root_var.get().strip()
                    else None
                ),
                with_video=bool(self.recorder_video_var.get()),
                with_screenshots=bool(self.recorder_screenshot_var.get()),
                with_tree=bool(self.recorder_tree_var.get()),
                minimize_window=bool(self.recorder_minimize_var.get()),
                monitor_index=settings.record_monitor_index,
                tree_max_depth=depth,
                tree_max_nodes=nodes,
                target_window_handle=(
                    int(target_window["handle"])
                    if target_window is not None
                    else None
                ),
                target_window_title=(
                    str(target_window.get("title") or "")
                    if target_window is not None
                    else None
                ),
                target_window_handles=tuple(self.recorder_selected_window_handles),
                target_window_titles=tuple(
                    str(self.recorder_target_window_map[handle].get("title") or "")
                    for handle in self.recorder_selected_window_handles
                ),
                target_window_identities=tuple(
                    freeze_window_identity(
                        self.recorder_target_window_map[handle]
                    )
                    for handle in self.recorder_selected_window_handles
                ),
                capture_target_process_only=bool(
                    self.recorder_target_process_only_var.get()
                ),
                window_capture_mode=window_mode,
            )
            self.recorder_session = FeatureRecordingSession(
                self.recorder_feature_plan,
                scenario,
                self.recorder_selected_step_ids,
                config,
            )
            for step_id, business_context in self.recorder_pending_step_contexts.items():
                if step_id not in self.recorder_session.step_states:
                    continue
                self.recorder_session.save_step_user_context(
                    step_id,
                    business_context=business_context,
                    expected_revision=0,
                )
            self.recorder_pending_step_contexts.clear()
        except Exception as error:
            self.recorder_status_var.set(
                f"创建录制任务失败: {type(error).__name__}: {error}"
            )
            return None
        self.recorder_output_dir = self.recorder_session.session_dir
        self._set_recorder_plan_locked(True)
        self._refresh_recorder_step_states()
        self.refresh_recorder_feature_workspace(announce=False)
        self._select_next_pending_step()
        self.recorder_status_var.set(
            f"场景录制任务已创建：{len(self.recorder_session.selected_steps)} 个 Step；"
            + (
                f"主窗口：{target_window.get('title') or target_window.get('class_name')}"
                if target_window is not None
                else "窗口将在第一次业务操作时自动确定"
            )
        )
        self._update_recorder_controls()
        self._refresh_workbench_context()
        return self.recorder_session

    def start_selected_step_recording(self):
        if self.recorder_task_busy or self._recorder_is_active():
            return
        session = self.create_recording_session()
        if session is None:
            return
        step_id = self._selected_recorder_step_id()
        if step_id is None:
            step = session.next_pending_step()
            step_id = step.id if step else None
        if step_id not in session.step_states:
            self.recorder_status_var.set("请选择当前会话中的 Step。")
            return
        state = session.step_states[step_id]
        if state["status"] == "completed" and not messagebox.askyesno(
            "重新录制",
            "该 Step 已完成，是否创建新的录制版本？",
            parent=self.recorder_window,
        ):
            return
        self.recorder_pending_step_id = step_id
        self.recorder_task_busy = True
        self.recorder_status_var.set("准备录制，请保持目标窗口可见...")
        self._show_recorder_overlay("preparing", step_id)
        self._update_recorder_controls()
        if self.recorder_minimize_var.get():
            self._minimize_recorder_windows()
        self.recorder_start_after_id = self.app.after(1200, self._begin_pending_step_recording)

    def _begin_pending_step_recording(self):
        self.recorder_start_after_id = None
        step_id = self.recorder_pending_step_id
        if step_id is None or self.recorder_session is None:
            self.recorder_task_busy = False
            self._restore_recorder_windows()
            return
        self._submit_recorder_task(
            "start",
            self.recorder_session.start_step,
            step_id,
            self.recorder_selected_window_handles,
            self.recorder_primary_window_handle,
            self.recorder_window_mode_var.get().strip() or "auto",
            bool(getattr(
                self.recorder_target_highlight,
                "available",
                False,
            )),
            getattr(
                self.recorder_target_highlight,
                "post_notification",
                None,
            ),
        )

    def finish_current_step_recording(self):
        if self.recorder_task_busy or self.recorder_session is None or not self.recorder_session.is_recording:
            return
        if self.recorder_observation_capture_inflight_event_id is not None:
            self.recorder_status_var.set(
                "F9目标仍在采集中，请等待结果后再保存当前Step。"
            )
            return
        take_summary = (
            self.recorder_take_summary_entry.get().strip()
            if self.recorder_take_summary_entry
            else ""
        )
        self.recorder_task_busy = True
        self.recorder_status_var.set("正在保存 Step 录制产物...")
        self._show_recorder_overlay("saving")
        self._update_recorder_controls()
        self._submit_recorder_task(
            "finish",
            self.recorder_session.finish_step,
            take_summary,
        )

    def run_primary_recorder_action(self):
        session = self.recorder_session
        if session is not None and session.is_recording:
            self.finish_current_step_recording()
            return
        if session is not None and session.is_finalized:
            self.open_recorder_review()
            return
        if session is not None and not any(
            state["status"] == "pending"
            for state in session.step_states.values()
        ):
            self.finalize_recording_session()
            return
        self.start_selected_step_recording()

    def capture_current_observation(self):
        self._capture_current_observation()

    def _capture_current_observation(self):
        if self.recorder_task_busy or self.recorder_session is None or not self.recorder_session.is_recording:
            return
        if self.recorder_observation_capture_inflight_event_id is not None:
            self.recorder_status_var.set("上一次F9目标仍在采集中，请稍候。")
            return
        try:
            event_id = self.recorder_session.capture_observation(
                note="",
                provider=None,
            )
        except Exception as error:
            self.recorder_status_var.set(f"捕获目标失败: {type(error).__name__}: {error}")
            return
        self.recorder_status_var.set(f"F9 正在采集目标（{event_id}）...")
        self.recorder_observation_capture_inflight_event_id = event_id
        self._update_recorder_controls()
        if self.recorder_status_overlay is not None:
            self.recorder_status_overlay.show_observation_pending(event_id)

    def toggle_current_step_pause(self):
        if self.recorder_task_busy or self.recorder_session is None or not self.recorder_session.is_recording:
            return
        try:
            paused = self.recorder_session.toggle_pause(note="")
        except Exception as error:
            self.recorder_status_var.set(f"暂停切换失败: {type(error).__name__}: {error}")
            return
        if paused:
            self.recorder_status_var.set(
                "录制已暂停：准备操作不会生成业务动作；再次按 F7 继续。"
            )
            self._show_recorder_overlay("paused")
        else:
            self.recorder_status_var.set(
                "录制已继续：暂停区间的目标窗口状态差异已保存。"
            )
            self._show_recorder_overlay("recording")
        self._refresh_recorder_step_states()
        self._update_recorder_controls()

    def cancel_current_step_recording(self, reason=None, *, prompt_reason=True):
        if self.recorder_start_after_id is not None:
            self.app.after_cancel(self.recorder_start_after_id)
            self.recorder_start_after_id = None
            self.recorder_pending_step_id = None
            self.recorder_task_busy = False
            self._restore_recorder_windows()
            self._hide_recorder_overlay()
            self.recorder_status_var.set("已取消开始录制。")
            self._update_recorder_controls()
            return
        if self.recorder_task_busy or self.recorder_session is None or not self.recorder_session.is_recording:
            return
        if prompt_reason and reason is None:
            reason = simpledialog.askstring(
                "放弃本次录制",
                "可选：说明为什么丢弃本次录制。\n取消将返回录制。",
                parent=self.recorder_window,
            )
            if reason is None:
                return
        reason = str(reason or "").strip()
        self.recorder_task_busy = True
        self.recorder_status_var.set("正在放弃本次录制...")
        self._show_recorder_overlay("discarding")
        self._update_recorder_controls()
        self._submit_recorder_task(
            "cancel",
            self.recorder_session.cancel_step,
            reason,
        )

    def skip_selected_step_recording(self, reason=None):
        if self.recorder_task_busy or self._recorder_is_active() or self.recorder_session is None:
            return
        step_id = self._selected_recorder_step_id()
        if step_id not in self.recorder_session.step_states:
            self.recorder_status_var.set("请选择当前会话中的 Step。")
            return
        if self.recorder_session.step_states[step_id]["status"] == "completed":
            self.recorder_status_var.set(
                "已完成的 Step 不能跳过，可使用“开始 / 重录”创建新录制版本。"
            )
            return
        if reason is None:
            reason = simpledialog.askstring(
                "跳过当前 Step",
                "可选：说明为什么当前Step不录制。\n取消将返回录制任务。",
                parent=self.recorder_window,
            )
            if reason is None:
                return
        reason = str(reason or "").strip()
        try:
            self.recorder_session.skip_step(step_id, reason)
        except Exception as error:
            self.recorder_status_var.set(f"跳过失败: {type(error).__name__}: {error}")
            return
        self._refresh_recorder_step_states()
        self._select_next_pending_step()
        self.recorder_status_var.set("Step 已标记为跳过。")
        self._update_recorder_controls()

    def finalize_recording_session(self):
        if self.recorder_task_busy or self._recorder_is_active() or self.recorder_session is None:
            return
        pending = [state for state in self.recorder_session.step_states.values() if state["status"] == "pending"]
        if pending and not messagebox.askyesno(
            "完成录制任务", f"还有 {len(pending)} 个 Step 未录制，仍要完成任务吗？", parent=self.recorder_window,
        ):
            return
        try:
            output = self.recorder_session.finalize()
            self.recorder_session.close()
        except Exception as error:
            self.recorder_status_var.set(f"完成会话失败: {type(error).__name__}: {error}")
            return
        self.recorder_status_var.set(f"录制任务已完成，可交给 AI 分析: {output}")
        self.refresh_recorder_feature_workspace(announce=False)
        self._update_recorder_controls()
        self.open_recorder_review()

    def _on_timeline_change(
            self,
            take_dir,
            timeline_state,
            mutation_result=None,
            *,
            step_id=None,
        ):
        if mutation_result is None:
            readiness = self.recorder_session.refresh_after_timeline_edit(take_dir)
        else:
            readiness = mutation_result.get("readiness")
        self._refresh_recorder_step_states()
        self.refresh_recorder_feature_workspace(announce=False)
        self._update_recorder_controls()
        self._refresh_workbench_context(step_id)
        return readiness

    def reset_recording_session(self):
        if self.recorder_task_busy or self._recorder_is_active():
            self.recorder_status_var.set("录制中不能新建会话。")
            return
        if not self._close_recorder_timeline():
            self.recorder_status_var.set(
                "补录仍在进行，完成或丢弃后才能结束当前任务。"
            )
            return
        if self.recorder_session is not None:
            self.recorder_session.close()
        self.recorder_session = None
        self.recorder_output_dir = None
        self.recorder_pending_step_id = None
        self._close_recorder_review()
        self._hide_recorder_overlay()
        self._set_recorder_plan_locked(False)
        if self.recorder_feature_plan is not None:
            scenario = self.recorder_scenario_map.get(self.recorder_scenario_var.get())
            if scenario is not None:
                self._render_recorder_steps(scenario)
        self.recorder_status_var.set(
            "已退出当前任务，可以选择其他 Feature 或场景。"
        )
        self._update_recorder_controls()
        self._refresh_workbench_context()

    def _submit_recorder_task(self, task_name, function, *args):
        run_id = (
            self.recorder_session.run_id
            if self.recorder_session is not None
            else "pending"
        )
        self.recorder_operations.submit(
            f"capture:{run_id}",
            function,
            *args,
            context=task_name,
        )

    def _schedule_recorder_poll(self):
        if self.recorder_poll_after_id is None:
            self.recorder_poll_after_id = self.app.after(100, self.poll_recorder_state)

    def poll_recorder_state(self):
        self.recorder_poll_after_id = None
        try:
            if self._recorder_is_active():
                self._poll_recorder_hotkeys()
            for task in self.recorder_operations.drain(key_prefix="capture:"):
                if task.status in {"cancelled", "superseded"}:
                    continue
                self._handle_recorder_task_result(
                    task.context,
                    task.value,
                    task.error,
                )
            for task in self.recorder_operations.drain(
                    key_prefix="annotation:"
            ):
                if task.status in {"cancelled", "superseded"}:
                    continue
                self._handle_step_context_result(
                    task.context,
                    task.value,
                    task.error,
                )
            for task in self.recorder_operations.drain(
                    key_prefix="portability:"
            ):
                if task.status in {"cancelled", "superseded"}:
                    continue
                self._handle_portability_result(
                    task.context,
                    task.value,
                    task.error,
                )
            for task in self.recorder_operations.drain(
                    key=self.recorder_workbench_context_operation_key
            ):
                self._handle_workbench_context_result(task)
            self._consume_recorder_window_notifications()
            self._consume_recorder_observation_notifications()
        finally:
            if (
                    self.recorder_operations.list_active(
                        key=self.recorder_workbench_context_operation_key
                    )
                    or self.recorder_operations.has_results(
                        key=self.recorder_workbench_context_operation_key
                    )
            ):
                self._schedule_recorder_poll()
            if self.recorder_window is not None:
                try:
                    if self.recorder_window.winfo_exists():
                        self._schedule_recorder_poll()
                except Exception:
                    pass

    def _consume_recorder_window_notifications(self):
        session = self.recorder_session
        if session is None or not session.is_recording:
            return
        for window in session.drain_window_notifications():
            title = window.get("title") or window.get("class_name") or "新窗口"
            admission = window.get("admission")
            if admission == "provisional":
                message = f"发现待确认窗口：{title}；结束后在审阅中心确认。"
            else:
                message = f"已自动纳入窗口：{title}"
            self.recorder_status_var.set(message)
            if self.recorder_status_overlay is not None:
                self.recorder_status_overlay.show_window_notice(
                    title,
                    provisional=admission == "provisional",
                )

    def _consume_recorder_observation_notifications(self):
        session = self.recorder_session
        if session is None or not session.is_recording:
            return
        for receipt in session.drain_observation_notifications():
            status = receipt.get("status")
            event_id = str(receipt.get("event_id") or "")
            if event_id == self.recorder_observation_capture_inflight_event_id:
                self.recorder_observation_capture_inflight_event_id = None
            if status == "failed":
                message = (
                    "未记录这次检查："
                    f"{receipt.get('message') or '未识别到目标'}。"
                    "请把鼠标停在正确目标上后重试 F9。"
                )
            else:
                message = _observation_receipt_summary(receipt)
            self.recorder_status_var.set(message)
            if self.recorder_status_overlay is not None:
                self.recorder_status_overlay.show_observation_receipt(receipt)

    def _poll_recorder_hotkeys(self):
        poll_hotkeys(self.recorder_hotkey_down, (
            (VK_F7, self.toggle_current_step_pause),
            (VK_F9, self.capture_current_observation),
            (VK_F10, self.finish_current_step_recording),
            (VK_F11, self._discard_current_step_hotkey),
        ))

    def _discard_current_step_hotkey(self):
        if not is_key_down(VK_SHIFT):
            self.recorder_status_var.set(
                "为防止误触，请按 Shift+F11 放弃本次录制。"
            )
            return
        self.cancel_current_step_recording(
            "shift_f11_user_discard",
            prompt_reason=False,
        )

    def _handle_recorder_task_result(self, task_name, result, error):
        self.recorder_task_busy = False
        if error is not None:
            if task_name == "start":
                self.recorder_pending_step_id = None
            self.recorder_status_var.set(f"{task_name} 失败: {type(error).__name__}: {error}")
            self._hide_recorder_overlay()
            self._restore_recorder_windows()
            self._refresh_recorder_step_states()
            self._update_recorder_controls()
            return
        if task_name == "start":
            if self.recorder_take_summary_entry is not None:
                self.recorder_take_summary_entry.delete(0, "end")
            reset_hotkeys(self.recorder_hotkey_down)
            self.recorder_status_var.set(
                "录制中：F7 暂停，F9 捕获观察目标，F10 保存，Shift+F11 放弃。"
            )
            self._show_recorder_overlay("recording")
        elif task_name == "finish":
            self.recorder_pending_step_id = None
            self._restore_recorder_windows()
            self._hide_recorder_overlay()
            self._refresh_recorder_step_states()
            self.refresh_recorder_feature_workspace(announce=False)
            self._select_next_pending_step()
            self.recorder_status_var.set("Step 录制完成，产物已更新。")
            if self.recorder_take_summary_entry is not None:
                self.recorder_take_summary_entry.delete(0, "end")
        elif task_name == "cancel":
            self.recorder_pending_step_id = None
            self._restore_recorder_windows()
            self._hide_recorder_overlay()
            self._refresh_recorder_step_states()
            self.refresh_recorder_feature_workspace(announce=False)
            self.recorder_status_var.set("本次录制已放弃，Step 保持待录制。")
            if self.recorder_take_summary_entry is not None:
                self.recorder_take_summary_entry.delete(0, "end")
        self._update_recorder_controls()
        self._refresh_workbench_context()

    def _handle_step_context_result(self, task_context, result, error):
        self.recorder_task_busy = False
        step_id = str((task_context or {}).get("step_id") or "")
        if error is not None:
            self.recorder_status_var.set(
                "Step业务说明保存失败: "
                f"{type(error).__name__}: {error}"
            )
            if step_id == self._selected_recorder_step_id():
                self._load_selected_step_user_context()
            self._update_recorder_controls()
            return
        context = (result or {}).get("step_user_context") or {}
        if context.get("step_id") == self._selected_recorder_step_id():
            self.recorder_step_business_context_var.set(
                step_business_context_text(context)
            )
            self.recorder_step_context_step_id = context.get("step_id")
            self.recorder_step_context_revision = int(
                context.get("revision") or 0
            )
            self.recorder_step_context_revision_var.set(
                f"版本 {self.recorder_step_context_revision}"
            )
        self.recorder_status_var.set(
            "业务补充已保存；仅用于补充Feature未说明的业务事实。"
        )
        self._update_recorder_controls()
        self._refresh_workbench_context()

    def _handle_portability_result(self, task_context, result, error):
        task_context = dict(task_context or {})
        task_name = str(task_context.get("kind") or "unknown")
        task_root = Path(
            task_context.get("recording_root") or "."
        ).resolve()
        for key_prefix in tuple(self.recorder_portability_active_kinds):
            if not self.recorder_operations.list_active(key_prefix=key_prefix):
                self.recorder_portability_active_kinds.pop(key_prefix, None)
        if error is not None:
            message = (
                f"录制资料{('导出' if 'export' in task_name else '导入')}失败: "
                f"{type(error).__name__}: {error}"
            )
            self.recorder_status_var.set(message)
            return
        if task_name in {"export", "feature_export"}:
            message = (
                f"已导出 {result.get('package_count')} 个独立Feature录制资料包，"
                f"共 {result.get('run_count', 0)} 个 Run: "
                f"{result.get('package_path')}"
                if result.get("package_count") is not None
                else (
                    f"已导出 {result.get('run_count', 0)} 个 Run: "
                    f"{result.get('package_path')}"
                )
            )
            warnings = tuple(result.get("warnings") or ())
            if warnings:
                message += "；" + "；".join(warnings)
            self.recorder_status_var.set(message)
            if task_name == "feature_export":
                self.refresh_recorder_feature_workspace(announce=False)
            return
        if task_name == "feature_import" and result.get("target_path"):
            self.recorder_feature_path_var.set(str(result["target_path"]))
            self.load_recorder_feature()
        else:
            self.refresh_recorder_feature_workspace(announce=False)
        imported_runs = (
            result.get("imported_runs")
            if task_name == "feature_import"
            else result.get("runs")
        ) or ()
        ready_count = sum(
            bool(item.get("request_path"))
            for item in imported_runs
        )
        message = (
            f"已导入 {result.get('run_count', 0)} 个 Run；"
            f"{ready_count} 个已生成目标机器 Request。"
        )
        warnings = tuple(result.get("warnings") or ())
        if warnings:
            message += "；" + "；".join(warnings)
        self.recorder_status_var.set(message)

    def _set_recorder_plan_locked(self, locked):
        state = "disabled" if locked else "normal"
        for frame in (self.recorder_source_frame, self.recorder_options_frame):
            if frame is not None:
                self._set_recorder_widget_tree_state(frame, state)
        if not locked:
            if self.recorder_backend_combo is not None:
                self.recorder_backend_combo.configure(state="readonly")
        if self.recorder_window_select_button is not None:
            self.recorder_window_select_button.configure(state="normal")

    def _set_recorder_widget_tree_state(self, widget, state):
        for child in widget.winfo_children():
            try:
                child.configure(state=state)
            except tk.TclError:
                pass
            self._set_recorder_widget_tree_state(child, state)

    def _refresh_recorder_step_states(self):
        if self.recorder_session is None:
            return
        for step_id, state in self.recorder_session.step_states.items():
            if not self.recorder_step_tree.exists(step_id):
                continue
            values = list(self.recorder_step_tree.item(step_id, "values"))
            values[0] = {
                "pending": "待录制", "recording": "录制中", "completed": "已完成", "skipped": "已跳过",
            }.get(state["status"], state["status"])
            if (
                state["status"] == "recording"
                and self.recorder_session.is_paused
            ):
                values[0] = "已暂停"
            values[4] = len(state["takes"])
            self.recorder_step_tree.item(step_id, values=values)
        self._update_progress()

    def _select_next_pending_step(self):
        if self.recorder_session is None:
            return
        step = self.recorder_session.next_pending_step()
        if step and self.recorder_step_tree.exists(step.id):
            self.recorder_step_tree.selection_set(step.id)
            self.recorder_step_tree.focus(step.id)
            self.recorder_step_tree.see(step.id)

    def _selected_recorder_step_id(self):
        try:
            selected = (
                self.recorder_step_tree.selection()
                if self.recorder_step_tree
                else ()
            )
        except tk.TclError:
            selected = ()
        return selected[0] if selected else None

    def _update_progress(self):
        if self.recorder_session is None:
            self.recorder_progress_var.set(
                f"本场景 {len(self.recorder_step_map)} 个 Step"
                if self.recorder_step_map
                else "请选择场景"
            )
            return
        states = self.recorder_session.step_states.values()
        completed = sum(state["status"] == "completed" for state in states)
        skipped = sum(state["status"] == "skipped" for state in states)
        self.recorder_progress_var.set(f"完成 {completed} / {len(self.recorder_session.step_states)}，跳过 {skipped}")

    def _update_recorder_controls(self):
        session = self.recorder_session
        active = self._recorder_is_active()
        busy = self.recorder_task_busy
        selected_features = self._selected_recorder_features()
        can_manage_materials = not active and not busy
        if self.recorder_materials_button is not None:
            self.recorder_materials_button.configure(
                state="normal" if can_manage_materials else "disabled"
            )
        if self.recorder_materials_menu is not None:
            feature = selected_features[0] if selected_features else None
            scenario = self._selected_recorder_feature_scenario()
            self.recorder_materials_menu.entryconfigure(
                "导入录制资料",
                state="normal" if can_manage_materials else "disabled",
            )
            self.recorder_materials_menu.entryconfigure(
                "导出当前 Feature",
                state=(
                    "normal"
                    if can_manage_materials
                    and feature is not None
                    and self._feature_has_exportable_recording(feature)
                    else "disabled"
                ),
            )
            self.recorder_materials_menu.entryconfigure(
                "导出当前场景",
                state=(
                    "normal"
                    if can_manage_materials
                    and scenario is not None
                    and scenario.exportable
                    else "disabled"
                ),
            )
        has_plan = (
            self.recorder_feature_plan is not None
            and bool(self.recorder_selected_step_ids)
        )
        if self.recorder_create_button is not None:
            self.recorder_create_button.config(
                state=(
                    "normal"
                    if has_plan and session is None and not busy
                    else "disabled"
                )
            )
        pending = bool(
            session is not None
            and any(
                state["status"] == "pending"
                for state in session.step_states.values()
            )
        )
        if session is not None and session.is_recording:
            primary_text = "保存当前 Step F10"
            primary_enabled = not busy
        elif session is not None and session.is_finalized:
            primary_text = "审阅并交给 Copilot"
            primary_enabled = bool(self.recorder_output_dir) and not busy
        elif session is not None and not pending:
            primary_text = "完成录制并审阅"
            primary_enabled = not active and not busy
        else:
            primary_text = (
                "开始录制此场景"
                if session is None
                else "录制当前 Step"
            )
            primary_enabled = (
                has_plan
                and (session is None or not session.is_finalized)
                and not active
                and not busy
            )
        if self.recorder_start_button is not None:
            self.recorder_start_button.config(
                text=primary_text,
                state="normal" if primary_enabled else "disabled",
            )
        if self.recorder_pause_button is not None:
            self.recorder_pause_button.config(
                text=(
                    "继续录制 F7"
                    if session is not None and session.is_paused
                    else "暂停录制 F7"
                ),
                state=(
                    "normal"
                    if session is not None
                    and session.is_recording
                    and not busy
                    else "disabled"
                ),
            )
        if self.recorder_more_button is not None:
            self.recorder_more_button.configure(
                state=(
                    "normal"
                    if has_plan or session is not None
                    else "disabled"
                )
            )
        if self.recorder_step_context_save_button is not None:
            self.recorder_step_context_save_button.configure(
                state=(
                    "normal"
                    if has_plan and not active and not busy
                    else "disabled"
                )
            )
        if self.recorder_more_menu is not None:
            can_observe = bool(
                session is not None
                and session.is_recording
                and not session.is_paused
                and self.recorder_observation_capture_inflight_event_id
                is None
                and not busy
            )
            can_cancel = bool(
                self.recorder_start_after_id is not None
                or (session is not None and session.is_recording and not busy)
            )
            can_manage_idle = bool(
                session is not None
                and not session.is_finalized
                and not active
                and not busy
            )
            self.recorder_more_menu.entryconfigure(
                "检查此处 F9",
                state="normal" if can_observe else "disabled",
            )
            self.recorder_more_menu.entryconfigure(
                "放弃本次录制 Shift+F11",
                state="normal" if can_cancel else "disabled",
            )
            self.recorder_more_menu.entryconfigure(
                "跳过当前 Step",
                state="normal" if can_manage_idle else "disabled",
            )
            self.recorder_more_menu.entryconfigure(
                "完成录制任务",
                state="normal" if can_manage_idle else "disabled",
            )
            self.recorder_more_menu.entryconfigure(
                "审阅并交给 Copilot",
                state="normal" if self.recorder_output_dir else "disabled",
            )
            self.recorder_more_menu.entryconfigure(
                "结束当前任务",
                state="normal" if not active and not busy else "disabled",
            )

    def _recorder_is_active(self):
        return bool(
            self.recorder_start_after_id is not None
            or (self.recorder_session is not None and self.recorder_session.is_recording)
        )

    def _minimize_recorder_windows(self):
        if self.recorder_workbench is not None:
            self.recorder_workbench.minimize_for_capture()

    def _restore_recorder_windows(self):
        if not self.recorder_minimize_var.get():
            return
        if self.recorder_workbench is not None:
            self.recorder_workbench.restore_after_capture("capture")

    def open_recorder_review(self):
        if self.recorder_session is None:
            self.recorder_status_var.set("至少完成一次 Step 录制后才能审阅。")
            return
        if self.recorder_review_window is not None:
            try:
                same_session = (
                    self.recorder_review_window.session.session_dir
                    == self.recorder_session.session_dir
                )
                if (
                        same_session
                        and self.recorder_review_window.window.winfo_exists()
                ):
                    self.recorder_review_window.refresh()
                    self.recorder_review_window.show()
                    self.recorder_workbench.select("review")
                    return
            except tk.TclError:
                pass
            self._close_recorder_review()
        self.recorder_workbench.set_view_enabled("review", True)
        host = self.recorder_workbench.host("review")
        self.recorder_workbench.clear("review")
        self.recorder_review_window = RecorderReviewWindow(
            host,
            self.recorder_session,
            on_timeline_change=self._on_timeline_change,
            on_rerecord=self.resume_existing_recording,
            operation_coordinator=self.recorder_operations,
            on_close=self._return_from_workbench_review,
            on_open_timeline=self._open_workbench_timeline,
            on_context_change=self._refresh_workbench_context,
        )
        self.recorder_workbench.select("review")

    def _return_from_workbench_review(self):
        view = self.recorder_review_return_view
        self.recorder_review_window = None
        self.recorder_workbench.select(view)

    def _open_workbench_timeline(
            self,
            take_dir,
            *,
            focus_event_ids=None,
            on_change=None,
            step_id=None,
            owner_take_id=None,
        ):
        current_view = self.recorder_workbench.selected_view()
        if current_view in {"capture", "library", "review"}:
            self.recorder_timeline_return_view = current_view
        if self.recorder_timeline_window is not None:
            try:
                same_take = (
                    self.recorder_timeline_window.take_dir.resolve()
                    == Path(take_dir).resolve()
                )
                if same_take and self.recorder_timeline_window.window.winfo_exists():
                    if focus_event_ids:
                        self.recorder_timeline_window._focus_events(
                            set(focus_event_ids)
                        )
                    self.recorder_workbench.select("timeline")
                    return self.recorder_timeline_window
            except tk.TclError:
                pass
            if self.recorder_timeline_window.close() is False:
                self.recorder_workbench.select("timeline")
                return self.recorder_timeline_window
        host = self.recorder_workbench.host("timeline")
        self.recorder_workbench.clear("timeline")
        self.recorder_workbench.set_view_enabled("timeline", True)
        self.recorder_timeline_window = TimelineEditorWindow(
            host,
            take_dir,
            on_change=on_change,
            focus_event_ids=focus_event_ids,
            on_close=self._return_from_workbench_timeline,
            operation_coordinator=self.recorder_operations,
            capture_window_controller=self.recorder_workbench,
            mutation_handler=(
                self.recorder_session.apply_timeline_mutation
                if self.recorder_session is not None
                else None
            ),
            session=self.recorder_session,
            step_id=step_id,
            owner_take_id=owner_take_id,
        )
        self.recorder_workbench.select("timeline")
        return self.recorder_timeline_window

    def _return_from_workbench_timeline(self):
        self.recorder_timeline_window = None
        if self.recorder_workbench is not None:
            self.recorder_workbench.set_view_enabled("timeline", False)
            target = self.recorder_timeline_return_view
            if not self.recorder_workbench.view_enabled(target):
                target = "capture"
            self.recorder_workbench.select(target)

    def _show_recorder_overlay(self, state, step_id=None):
        target_highlight = getattr(self, "recorder_target_highlight", None)
        if (
                state != "recording"
            and target_highlight is not None
            ):
            target_highlight.clear(force=True)
        if self.recorder_status_overlay is None:
            return
        step_id = step_id or self.recorder_pending_step_id
        step = self.recorder_step_map.get(step_id)
        step_text = step.text if step is not None else ""
        self.recorder_status_overlay.show(state, step_text)

    def _hide_recorder_overlay(self):
        if self.recorder_status_overlay is not None:
            self.recorder_status_overlay.hide()
        if self.recorder_target_highlight is not None:
            self.recorder_target_highlight.clear(force=True)

    def _close_recorder_review(self):
        if self.recorder_review_window is not None:
            try:
                self.recorder_review_window.close()
            except tk.TclError:
                pass
        self.recorder_review_window = None

    def _close_recorder_timeline(self, *, force=False):
        if self.recorder_timeline_window is not None:
            try:
                close = (
                    self.recorder_timeline_window.force_close
                    if force
                    else self.recorder_timeline_window.close
                )
                if close() is False:
                    return False
            except tk.TclError:
                pass
        self.recorder_timeline_window = None
        if self.recorder_workbench is not None:
            self.recorder_workbench.set_view_enabled("timeline", False)
        return True

    def close_recorder_tool(self, force=False):
        if (
            not force
            and self.recorder_operations.list_active(key_prefix="portability:")
        ):
            if self.recording_library_window is not None:
                self.recording_library_window.status_var.set(
                    "录屏包导入或导出进行中，完成后才能关闭工作台。"
                )
            return False
        if not force and (self.recorder_task_busy or self._recorder_is_active()):
            self.recorder_status_var.set("录制或保存进行中，请先完成或取消。")
            return
        if self.recorder_start_after_id is not None:
            try:
                self.app.after_cancel(self.recorder_start_after_id)
            except Exception:
                pass
            self.recorder_start_after_id = None
            self.recorder_pending_step_id = None
        force_stop_capture = bool(
            force
            and (
                self.recorder_task_busy
                or self._recorder_is_active()
                or self.recorder_operations.list_active(
                    key_prefix="capture:"
                )
            )
        )
        if force_stop_capture:
            self.recorder_operations.abandon_prefix("capture:")
            session = self.recorder_session
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass
            self.recorder_task_busy = False
            self.recorder_pending_step_id = None
        if self.recorder_poll_after_id is not None:
            try:
                self.app.after_cancel(self.recorder_poll_after_id)
            except Exception:
                pass
            self.recorder_poll_after_id = None
        if not self._close_recorder_timeline(force=force) and not force:
            return False
        self._close_recorder_review()
        self.close_recording_library()
        if self.recorder_status_overlay is not None:
            self.recorder_status_overlay.destroy()
            self.recorder_status_overlay = None
        if self.recorder_target_highlight is not None:
            self.recorder_target_highlight.destroy()
            self.recorder_target_highlight = None
        self.recorder_window = None
        self.recorder_step_tree = None
        self.recorder_window_select_button = None
        self.recorder_window_summary_label = None
        self.recorder_feature_tree = None
        self.recorder_materials_button = None
        self.recorder_materials_menu = None
        self.recorder_start_button = None
        self.recorder_pause_button = None
        self.recorder_more_button = None
        self.recorder_more_menu = None
        self.recorder_workbench_context_sequence += 1
        self.recorder_operations.abandon_prefix(
            self.recorder_workbench_context_operation_key,
            wait=True,
        )
        self.recorder_query_service = None
        if self.recorder_workbench is not None:
            self.recorder_workbench.destroy()
            self.recorder_workbench = None
        return True

    def stop_recorder_on_close(self):
        self.close_recording_library()
        if self.recorder_start_after_id is not None:
            try:
                self.app.after_cancel(self.recorder_start_after_id)
            except Exception:
                pass
            self.recorder_start_after_id = None
        session = self.recorder_session
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
        self.recorder_operations.shutdown(wait=False)