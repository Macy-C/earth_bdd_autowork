from __future__ import annotations

import hashlib
from io import BytesIO
import os
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk
import uuid

from PIL import Image, ImageDraw, ImageTk

from autowork_core.utils.debug_tools.recorder.request_service import (
    GenerationRequestService,
)
from autowork_core.utils.debug_tools.recorder.project_memory import (
    latest_transaction,
    record_transaction_feedback as save_transaction_feedback,
)
from autowork_core.utils.debug_tools.recorder.knowledge_store import (
    ensure_knowledge_store,
)
from autowork_core.utils.debug_tools.recorder.operation_coordinator import (
    OperationCoordinator,
)
from autowork_core.utils.debug_tools.recorder.query_service import (
    RecorderQueryService,
)
from config.paths import Paths


FEEDBACK_LABELS = {
    "可以使用": "accepted",
    "我已修改结果": "revised",
    "不采用": "rejected",
}


def _feedback_saved_message(label, event):
    payload = (event or {}).get("payload") or {}
    tier = payload.get("accepted_feedback_tier")
    if FEEDBACK_LABELS.get(label) == "accepted":
        if tier == "accepted_oracle_verified":
            detail = "已通过独立业务验证，可作为复用候选。"
        elif tier == "accepted_runtime_verified":
            detail = "已通过真实运行，可作为复用候选。"
        else:
            detail = "缺少真实运行或独立业务验证，仅作为建议性反馈。"
    else:
        detail = "它会作为后续生成的参考。"
    return f"结果确认已保存（{label}）。{detail}"


class RecorderReviewWindow:
    def __init__(
            self,
            parent,
            session,
            on_timeline_change=None,
            on_rerecord=None,
            operation_coordinator=None,
            on_close=None,
            on_open_timeline=None,
            on_context_change=None,
    ):
        self.parent = parent
        self.session = session
        self.on_timeline_change = on_timeline_change
        self.on_rerecord = on_rerecord
        self.on_close = on_close
        self.on_open_timeline = on_open_timeline
        self.on_context_change = on_context_change
        self.request_service = GenerationRequestService(session.session_dir)
        self.query_service = RecorderQueryService(
            session,
            operation_coordinator=operation_coordinator,
        )
        self.model = None
        self.last_request_path = None
        self.request_refresh_after_id = None
        self.request_refresh_poll_after_id = None
        self.request_refresh_running = False
        self.request_refresh_error = None
        self.request_refresh_sequence = 0
        self.job_operation_after_id = None
        self.operations = operation_coordinator or OperationCoordinator(
            max_workers=2,
            thread_name_prefix="recorder-review",
        )
        self._owns_operations = operation_coordinator is None
        self.request_operation_prefix = (
            f"request:{session.run_id}:{uuid.uuid4().hex}:"
        )
        self.job_operation_key = f"{self.request_operation_prefix}job"
        self.closed = False
        self.take_map = {}
        self.diagnostics = []

        self.window = ttk.Frame(parent)
        self.window.grid(row=0, column=0, sticky="nsew")

        self.status_var = tk.StringVar(
            value="完成当前场景的目标 Step；Copilot 任务会随录制修改自动更新。"
        )
        self.summary_var = tk.StringVar(value="")
        self.capture_summary_var = tk.StringVar(value="")
        self.evidence_summary_var = tk.StringVar(value="")
        self.generation_summary_var = tk.StringVar(value="")
        self.take_var = tk.StringVar(value="")
        self.next_action_var = tk.StringVar(value="")
        self.generation_profile_var = tk.StringVar(value="generation_first")
        self.question_option_var = tk.StringVar(value="")
        self.feedback_var = tk.StringVar(value="可以使用")
        self.feedback_note_var = tk.StringVar(value="")
        self.tree = None
        self.timeline_button = None
        self.take_combo = None
        self.select_take_button = None
        self.next_action_button = None
        self.tools_button = None
        self.tools_menu = None
        self.feedback_frame = None
        self.result_frame = None
        self.result_summary_var = tk.StringVar(value="")
        self.runtime_summary_var = tk.StringVar(value="")
        self.result_tree = None
        self.detail_notebook = None
        self.issues_frame = None
        self.issue_tree = None
        self.issue_detail = None
        self.locate_button = None
        self.repair_button = None
        self.question_frame = None
        self.question_tree = None
        self.question_detail = None
        self.question_image = None
        self.question_image_ref = None
        self.question_open_button = None
        self.question_options_frame = None
        self.question_submit_button = None
        self.profile_buttons = []
        self.decision_questions = []
        self.decision_selections = {}
        self.decision_request_id = None
        self.question_preview_path = None
        self.question_preview_sha256 = None
        self.question_preview_size = None

        self._build_ui()
        self.refresh()

    def show(self):
        self.window.grid(row=0, column=0, sticky="nsew")
        self.window.tkraise()
        return self

    def _build_ui(self):
        header = ttk.Frame(self.window)
        header.pack(fill="x", padx=12, pady=(12, 6))
        ttk.Label(
            header,
            text="录制与生成",
            font=("Microsoft YaHei UI", 14, "bold"),
            anchor="w",
        ).pack(side="left")
        ttk.Button(header, text="关闭", command=self.close).pack(side="right")

        status = ttk.Frame(self.window)
        status.pack(fill="x", padx=12, pady=(0, 6))
        ttk.Label(
            status,
            textvariable=self.summary_var,
            anchor="w",
        ).pack(fill="x")
        ttk.Label(
            status,
            textvariable=self.capture_summary_var,
            anchor="w",
        ).pack(fill="x", pady=(3, 0))
        ttk.Label(
            status,
            textvariable=self.evidence_summary_var,
            anchor="w",
        ).pack(fill="x", pady=(3, 0))
        ttk.Label(
            status,
            textvariable=self.generation_summary_var,
            anchor="w",
            wraplength=1180,
        ).pack(fill="x", pady=(3, 0))

        actions = ttk.Frame(self.window)
        actions.pack(fill="x", padx=12, pady=(0, 8))
        self.next_action_button = ttk.Button(
            actions,
            text="下一步",
            command=self.run_recommended_action,
        )
        self.next_action_button.pack(side="left")
        self.timeline_button = ttk.Button(
            actions,
            text="校正与补录",
            command=self.open_timeline,
        )
        self.timeline_button.pack(side="left", padx=(6, 0))
        self.tools_button = ttk.Menubutton(actions, text="更多")
        self.tools_button.pack(side="left", padx=6)
        self.tools_menu = tk.Menu(self.tools_button, tearoff=False)
        self.tools_menu.add_command(
            label="查看项目经验",
            command=self.open_project_memory,
        )
        self.tools_menu.add_command(
            label="打开任务目录",
            command=self.open_session_dir,
        )
        self.tools_menu.add_separator()
        self.tools_menu.add_command(
            label="重试生成",
            command=self.retry_generation_job,
        )
        self.tools_menu.add_command(
            label="终止未开始的生成任务",
            command=self.retire_generation_job,
        )
        self.tools_button.configure(menu=self.tools_menu)
        profile_frame = ttk.Frame(actions)
        profile_frame.pack(side="left", padx=(8, 0))
        ttk.Label(profile_frame, text="生成方式").pack(side="left")
        for profile_id, label, enabled in (
            ("generation_first", "专心生成", True),
        ):
            button = ttk.Radiobutton(
                profile_frame,
                text=label,
                value=profile_id,
                variable=self.generation_profile_var,
            )
            button.pack(side="left", padx=(6, 0))
            if not enabled:
                button.configure(state="disabled")
            self.profile_buttons.append((button, enabled))
        ttk.Label(
            actions,
            textvariable=self.next_action_var,
            anchor="w",
        ).pack(side="left", fill="x", expand=True, padx=(12, 0))

        workspace = ttk.Panedwindow(self.window, orient="horizontal")
        workspace.pack(fill="both", expand=True, padx=12, pady=(0, 8))

        left = ttk.Frame(workspace)
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        right = ttk.Frame(workspace)
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)
        workspace.add(left, weight=5)
        workspace.add(right, weight=4)

        frame = ttk.LabelFrame(left, text="Scenario Steps")
        frame.grid(row=0, column=0, sticky="nsew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        columns = (
            "capture",
            "ordinal",
            "keyword",
            "step",
            "takes",
            "windows",
            "evidence",
            "generation",
        )
        self.tree = ttk.Treeview(
            frame,
            columns=columns,
            show="headings",
            selectmode="browse",
        )
        headings = (
            ("capture", "录制", 80),
            ("ordinal", "序号", 55),
            ("keyword", "关键字", 100),
            ("step", "Step", 390),
            ("takes", "版本", 55),
            ("windows", "窗口", 55),
            ("evidence", "录制质量", 90),
            ("generation", "下一步", 120),
        )
        for column, label, width in headings:
            self.tree.heading(column, text=label)
            self.tree.column(
                column,
                width=width,
                minwidth=40,
                stretch=column == "step",
            )
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", self._on_step_selected)

        take_bar = ttk.Frame(left)
        take_bar.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(take_bar, text="查看录制版本").pack(side="left")
        self.take_combo = ttk.Combobox(
            take_bar,
            state="readonly",
            width=54,
            textvariable=self.take_var,
        )
        self.take_combo.pack(side="left", padx=6)
        self.take_combo.bind(
            "<<ComboboxSelected>>",
            self._on_take_selected,
        )
        self.select_take_button = ttk.Button(
            take_bar,
            text="用于生成",
            command=self.select_current_take,
        )
        self.select_take_button.pack(side="left")
        ttk.Label(
            take_bar,
            text="切换不会删除其他录制版本",
        ).pack(side="left", padx=10)

        self.detail_notebook = ttk.Notebook(right)
        self.detail_notebook.grid(row=0, column=0, sticky="nsew")

        issues = ttk.Frame(self.detail_notebook)
        self.issues_frame = issues
        self.detail_notebook.add(issues, text="问题与处理")
        issues.rowconfigure(0, weight=1)
        issues.columnconfigure(0, weight=1)
        self.issue_tree = ttk.Treeview(
            issues,
            columns=("problem", "location", "repair"),
            show="headings",
            selectmode="browse",
            height=7,
        )
        for column, label, width in (
            ("problem", "问题", 200),
            ("location", "具体位置", 260),
            ("repair", "修复方式", 90),
        ):
            self.issue_tree.heading(column, text=label)
            self.issue_tree.column(
                column,
                width=width,
                minwidth=60,
                stretch=column in ("problem", "location"),
            )
        self.issue_tree.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        self.issue_tree.bind(
            "<<TreeviewSelect>>",
            lambda event: self._show_selected_diagnostic(),
        )
        detail_frame = ttk.Frame(issues)
        detail_frame.grid(row=1, column=0, sticky="nsew", padx=6)
        detail_frame.rowconfigure(0, weight=1)
        detail_frame.columnconfigure(0, weight=1)
        self.issue_detail = tk.Text(
            detail_frame,
            wrap="word",
            height=8,
            font=("Microsoft YaHei UI", 9),
        )
        detail_scroll = ttk.Scrollbar(
            detail_frame,
            orient="vertical",
            command=self.issue_detail.yview,
        )
        self.issue_detail.configure(yscrollcommand=detail_scroll.set)
        self.issue_detail.grid(row=0, column=0, sticky="nsew")
        detail_scroll.grid(row=0, column=1, sticky="ns")
        issue_actions = ttk.Frame(issues)
        issue_actions.grid(row=2, column=0, sticky="ew", padx=6, pady=6)
        self.locate_button = ttk.Button(
            issue_actions,
            text="定位到录制内容",
            command=self.locate_selected_diagnostic,
        )
        self.locate_button.pack(side="left")
        ttk.Button(
            issue_actions,
            text="打开证据",
            command=self.open_selected_evidence,
        ).pack(side="left", padx=6)
        self.repair_button = ttk.Button(
            issue_actions,
            text="执行建议修复",
            command=self.repair_selected_diagnostic,
        )
        self.repair_button.pack(side="left")

        questions = ttk.Frame(self.detail_notebook)
        self.question_frame = questions
        self.detail_notebook.add(questions, text="业务确认")
        questions.rowconfigure(3, weight=1)
        questions.columnconfigure(0, weight=1)
        self.question_tree = ttk.Treeview(
            questions,
            columns=("step", "question"),
            show="headings",
            selectmode="browse",
            height=4,
        )
        self.question_tree.heading("step", text="Step")
        self.question_tree.heading("question", text="需要确认")
        self.question_tree.column("step", width=180, stretch=False)
        self.question_tree.column("question", width=360, stretch=True)
        self.question_tree.grid(
            row=0,
            column=0,
            sticky="nsew",
            padx=8,
            pady=(8, 4),
        )
        self.question_tree.bind(
            "<<TreeviewSelect>>",
            lambda event: self._show_selected_question(),
        )
        self.question_detail = tk.Text(
            questions,
            wrap="word",
            height=9,
            font=("Microsoft YaHei UI", 9),
        )
        self.question_detail.grid(
            row=1,
            column=0,
            sticky="ew",
            padx=8,
            pady=4,
        )
        self.question_detail.configure(state="disabled")
        self.question_options_frame = ttk.LabelFrame(
            questions,
            text="选择",
        )
        self.question_options_frame.grid(
            row=2,
            column=0,
            sticky="ew",
            padx=8,
            pady=4,
        )
        self.question_image = ttk.Label(
            questions,
            text="选择问题后显示相关操作现场。",
            anchor="center",
        )
        self.question_image.grid(
            row=3,
            column=0,
            sticky="nsew",
            padx=8,
            pady=4,
        )
        question_actions = ttk.Frame(questions)
        question_actions.grid(
            row=4,
            column=0,
            sticky="ew",
            padx=8,
            pady=(4, 8),
        )
        self.question_open_button = ttk.Button(
            question_actions,
            text="打开现场原图",
            command=self.open_selected_question_media,
        )
        self.question_open_button.pack(side="left")
        self.question_submit_button = ttk.Button(
            question_actions,
            text="提交全部回答",
            command=self.submit_decision_batch,
        )
        self.question_submit_button.pack(side="left", padx=(8, 0))
        ttk.Label(
            question_actions,
            text="现场仅帮助理解问题；选择仍以真实业务规则为准。",
        ).pack(side="left", padx=10)

        result = ttk.Frame(self.detail_notebook)
        self.result_frame = result
        self.detail_notebook.add(result, text="本次生成")
        ttk.Label(
            result,
            textvariable=self.result_summary_var,
            anchor="w",
            wraplength=540,
        ).pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(
            result,
            textvariable=self.runtime_summary_var,
            anchor="w",
            wraplength=540,
        ).pack(fill="x", padx=8, pady=(0, 6))
        result_table = ttk.Frame(result)
        result_table.pack(fill="both", expand=True, padx=8)
        result_table.rowconfigure(0, weight=1)
        result_table.columnconfigure(0, weight=1)
        self.result_tree = ttk.Treeview(
            result_table,
            columns=("path", "state"),
            show="headings",
            selectmode="browse",
            height=8,
        )
        self.result_tree.heading("path", text="修改文件")
        self.result_tree.heading("state", text="状态")
        self.result_tree.column("path", width=390, stretch=True)
        self.result_tree.column("state", width=100, stretch=False)
        self.result_tree.grid(row=0, column=0, sticky="nsew")
        result_actions = ttk.Frame(result)
        result_actions.pack(fill="x", padx=8, pady=8)
        ttk.Button(
            result_actions,
            text="查看详细报告",
            command=self.open_generation_report,
        ).pack(side="left")
        ttk.Button(
            result_actions,
            text="打开所选文件",
            command=self.open_selected_generated_file,
        ).pack(side="left", padx=6)
        ttk.Button(
            result_actions,
            text="交给 Copilot 继续处理",
            command=self.copy_generation_command,
        ).pack(side="left")
        ttk.Button(
            result_actions,
            text="打开运行报告",
            command=self.open_runtime_report,
        ).pack(side="left", padx=(12, 6))
        ttk.Button(
            result_actions,
            text="打开失败附件",
            command=self.open_runtime_attachment,
        ).pack(side="left")

        feedback = ttk.Frame(self.detail_notebook)
        self.feedback_frame = feedback
        self.detail_notebook.add(feedback, text="确认结果")
        ttk.Label(feedback, text="这次结果").grid(
            row=0, column=0, sticky="w", padx=(8, 4), pady=(10, 6)
        )
        ttk.Combobox(
            feedback,
            state="readonly",
            width=15,
            values=tuple(FEEDBACK_LABELS),
            textvariable=self.feedback_var,
        ).grid(row=0, column=1, sticky="w", pady=(10, 6))
        ttk.Label(feedback, text="补充说明（可选）").grid(
            row=1, column=0, sticky="w", padx=(8, 4), pady=6,
        )
        feedback.columnconfigure(1, weight=1)
        ttk.Entry(
            feedback,
            textvariable=self.feedback_note_var,
        ).grid(row=1, column=1, sticky="ew", pady=6, padx=(0, 8))
        feedback_actions = ttk.Frame(feedback)
        feedback_actions.grid(
            row=2, column=0, columnspan=2, sticky="ew", padx=8, pady=8
        )
        ttk.Button(
            feedback_actions,
            text="保存确认",
            command=self.record_transaction_feedback,
        ).pack(side="left")

        ttk.Label(
            self.window,
            textvariable=self.status_var,
            anchor="w",
            wraplength=1180,
        ).pack(fill="x", padx=12, pady=(0, 10))

    def refresh(self):
        selected = self.selected_step_id()
        self.model = self.query_service.get_workbench(selected)
        self.tree.delete(*self.tree.get_children())
        for step in self.model.steps:
            selected_take = next(
                (take for take in step.takes if take.selected),
                None,
            )
            self.tree.insert(
                "",
                "end",
                iid=step.step_id,
                values=(
                    _status_label(step.capture_status),
                    step.ordinal,
                    step.keyword,
                    step.text,
                    len(step.takes),
                    selected_take.window_count if selected_take else 0,
                    _evidence_status_label(step.evidence_status),
                    _generation_status_label(step.generation_status),
                ),
            )
        if selected and self.tree.exists(selected):
            self.tree.selection_set(selected)
        elif self.tree.get_children():
            self.tree.selection_set(self.tree.get_children()[0])
        self.summary_var.set(
            f"Feature: {self.model.feature_name}    "
            f"Scenario: {self.model.scenario_name}    "
            f"范围: {self.model.scope.label}"
        )
        self.capture_summary_var.set(_capture_summary_text(self.model))
        self.generation_summary_var.set(
            _generation_summary_text(self.model.generation)
        )
        self._refresh_generation_result()
        self._refresh_decision_questions()
        self._sync_result_tabs()
        self._refresh_diagnostics()
        self._refresh_take_selector()
        self._update_controls()
        self._restore_or_schedule_request()

    def _on_step_selected(self, event=None):
        self.last_request_path = None
        self._refresh_take_selector()
        self._refresh_diagnostics()
        self._update_controls()
        self._restore_or_schedule_request()
        self._notify_context_change()

    def _on_take_selected(self, event=None):
        self._update_controls()
        self._notify_context_change()

    def selected_step_id(self):
        selected = self.tree.selection() if self.tree is not None else ()
        return selected[0] if selected else None

    def scenario_step_ids(self):
        model = getattr(self, "model", None)
        return (
            model.scope.selected_step_ids
            if model is not None and model.scope is not None
            else ()
        )

    def _scenario_scope_complete(self):
        model = getattr(self, "model", None)
        return bool(
            model is not None
            and model.scope is not None
            and model.scope.complete
        )

    def _scenario_scope_label(self):
        model = getattr(self, "model", None)
        return (
            model.scope.label
            if model is not None and model.scope is not None
            else "当前场景"
        )

    @staticmethod
    def _normalize_step_scope(step_ids):
        return (step_ids,) if isinstance(step_ids, str) else tuple(step_ids)

    def _scenario_incomplete_steps(self):
        model = getattr(self, "model", None)
        if model is None or model.scope is None:
            return ()
        incomplete = set(model.scope.incomplete_step_ids)
        return tuple(
            step
            for step in model.steps
            if step.step_id in incomplete
        )

    def _capture_generation_candidate(self):
        model = getattr(self, "model", None)
        return bool(
            model is not None
            and model.scope is not None
            and getattr(
                model.scope,
                "capture_generation_candidate",
                False,
            )
        )

    def _scenario_generation_ready(self):
        return self._capture_generation_candidate()

    def selected_take_entry(self):
        step = self._selected_step_workspace()
        if step is None:
            return None
        take_id = (
            self.take_map.get(self.take_var.get())
            or step.selected_take_id
        )
        return next(
            (
                take
                for take in step.takes
                if take.take_id == take_id
                and take.status == "completed"
                and take.directory_path is not None
            ),
            None,
        )

    def selected_take_dir(self):
        take = self.selected_take_entry()
        return Path(take.directory_path) if take else None

    def _selected_step_workspace(self):
        step_id = self.selected_step_id()
        return next(
            (
                step
                for step in (self.model.steps if self.model else ())
                if step.step_id == step_id
            ),
            None,
        )

    def _refresh_take_selector(self):
        step = self._selected_step_workspace()
        takes = step.takes if step is not None else ()
        self.take_map = {}
        selected_label = ""
        for take in takes:
            marker = "用于生成" if take.selected else "历史版本"
            label = (
                f"第 {take.take_number} 次录制 | {marker} | "
                f"操作 {take.action_count} | 窗口 {take.window_count or 1}"
            )
            if take.review_text:
                label += f" | 说明={take.review_text[:60]}"
            self.take_map[label] = take.take_id
            if take.selected:
                selected_label = label
        values = list(self.take_map)
        self.take_combo.configure(values=values)
        self.take_var.set(selected_label or (values[0] if values else ""))

    def select_current_take(self):
        step_id = self.selected_step_id()
        take_id = self.take_map.get(self.take_var.get())
        if not step_id or not take_id:
            self.status_var.set("请选择 Step 和录制版本。")
            return
        try:
            self.session.select_take(step_id, take_id)
        except Exception as error:
            self.status_var.set(
                f"切换录制版本失败: {type(error).__name__}: {error}"
            )
            return
        self.last_request_path = None
        self.status_var.set("用于生成的录制版本已更新，正在更新 Copilot 任务。")
        self.refresh()
        self._notify_context_change()

    def _notify_context_change(self):
        if self.on_context_change is not None:
            self.on_context_change(self.selected_step_id())

    def open_timeline(self, focus_event_ids=None):
        take = self.selected_take_entry()
        take_dir = self.selected_take_dir()
        step_id = self.selected_step_id()
        if (
            take_dir is None
            or take is None
            or take.status != "completed"
        ):
            self._resume_current_step_recording()
            return
        try:
            if self.on_open_timeline is None:
                raise RuntimeError("审阅视图未连接 Recorder 工作台")
            self.on_open_timeline(
                take_dir,
                focus_event_ids=focus_event_ids,
                on_change=lambda path, state, result=None: (
                    self._timeline_changed(
                        path,
                        state,
                        result,
                        step_id=step_id,
                    )
                ),
                step_id=step_id,
                owner_take_id=take.take_id,
            )
        except Exception as error:
            self.status_var.set(
                f"打开录制内容失败: {type(error).__name__}: {error}"
            )

    def _resume_current_step_recording(self):
        step_id = self.selected_step_id()
        if step_id is None:
            self.status_var.set("请先选择需要录制的 Step。")
            return
        if self.on_rerecord is None:
            message = "当前 Step 没有可用录制，请先回到录制工具完成一次录制。"
            self.status_var.set(message)
            messagebox.showinfo("需要可用录制", message, parent=self.window)
            return
        try:
            resumed = self.on_rerecord(self.session, step_id)
        except Exception as error:
            detail = f"{type(error).__name__}: {error}"
            self.status_var.set(f"恢复录制失败: {detail}")
            messagebox.showerror("无法恢复录制", detail, parent=self.window)
            return
        if resumed is False:
            self.status_var.set("当前存在未完成的录制或保存任务，暂时无法切换。")
            return
        self.status_var.set(
            "已切换到录制工具；完成当前 Step 后即可补上缺失操作。"
        )

    def _timeline_changed(
            self,
            take_dir,
            timeline_state,
            mutation_result=None,
            *,
            step_id=None,
        ):
        self.last_request_path = None
        if self.on_timeline_change is not None:
            if mutation_result is None:
                readiness = self.on_timeline_change(
                    take_dir,
                    timeline_state,
                    step_id=step_id,
                )
            else:
                readiness = self.on_timeline_change(
                    take_dir,
                    timeline_state,
                    mutation_result,
                    step_id=step_id,
                )
        elif mutation_result is not None:
            readiness = mutation_result.get("readiness")
        else:
            readiness = self.session.refresh_after_timeline_edit(take_dir)
        self.refresh()
        return readiness

    def _restore_or_schedule_request(self):
        step_ids = self.scenario_step_ids()
        if not self._capture_generation_candidate():
            self.last_request_path = None
            if self.request_refresh_error is not None:
                self.request_refresh_error = None
                self._update_controls()
            return
        request = self.request_service.latest(step_ids)
        if request is not None:
            self.last_request_path = self.session.session_dir / request["request_path"]
            workflow = self.request_service.workflow_state(step_ids)
            if workflow.get("status") != "stale":
                self._update_controls()
                return
        self.last_request_path = None
        self._schedule_request_refresh(step_ids)

    def _schedule_request_refresh(self, step_ids, delay_ms=450):
        step_ids = self._normalize_step_scope(step_ids)
        self.request_refresh_error = None
        self.request_refresh_sequence += 1
        sequence = self.request_refresh_sequence
        if self.request_refresh_after_id is not None:
            try:
                self.window.after_cancel(self.request_refresh_after_id)
            except tk.TclError:
                pass
        self.status_var.set("录制内容已变化，正在更新 Copilot 任务...")
        self.request_refresh_after_id = self.window.after(
            delay_ms,
            lambda: self._auto_refresh_request(step_ids, sequence),
        )

    def _auto_refresh_request(self, step_ids, sequence=None):
        step_ids = self._normalize_step_scope(step_ids)
        self.request_refresh_after_id = None
        if sequence is None:
            self.request_refresh_sequence += 1
            sequence = self.request_refresh_sequence
        if sequence != self.request_refresh_sequence or self.closed:
            return
        self.request_refresh_running = True
        self.operations.submit(
            f"{self.request_operation_prefix}scenario",
            self._ensure_latest_request,
            step_ids,
            context=(sequence, step_ids),
            pass_token=True,
        )
        self._schedule_request_result_poll()

    def _ensure_latest_request(self, token, step_ids):
        token.raise_if_cancelled()
        request = self.request_service.ensure_latest(step_ids, repair=True)
        token.raise_if_cancelled()
        return request

    def _schedule_request_result_poll(self):
        if self.closed or self.request_refresh_poll_after_id is not None:
            return
        self.request_refresh_poll_after_id = self.window.after(
            40,
            self._poll_request_refresh_result,
        )

    def _poll_request_refresh_result(self):
        self.request_refresh_poll_after_id = None
        results = self.operations.drain(
            key_prefix=self.request_operation_prefix,
        )
        if not results:
            if self.operations.list_active(
                    key_prefix=self.request_operation_prefix,
            ):
                self._schedule_request_result_poll()
            return
        for task in results:
            sequence, step_ids = task.context
            request = task.value
            error = task.error
            is_current = (
                not self.closed
                and sequence == self.request_refresh_sequence
                and tuple(step_ids) == self.scenario_step_ids()
                and self._capture_generation_candidate()
                and task.status not in {"cancelled", "superseded"}
            )
            if is_current and error is None:
                self.request_refresh_error = None
                request_path = self.session.session_dir / request["request_path"]
                current = self.request_service.latest(step_ids)
                if current and current.get("request_id") == request.get("request_id"):
                    self.last_request_path = request_path
                    workflow = self.request_service.workflow_state(step_ids)
                    self._reload_model()
                    self.status_var.set(
                        f"{self._scenario_scope_label()} Copilot 任务已自动更新"
                        f"（{_request_status_label(workflow)}）: "
                        f"{request_path}"
                    )
            elif is_current:
                self.last_request_path = None
                self.request_refresh_error = error
                self.status_var.set(
                    f"自动准备 Copilot 任务失败，可点击“重试准备”: "
                    f"{type(error).__name__}: {error}"
                )
        self.request_refresh_running = bool(self.operations.list_active(
            key_prefix=self.request_operation_prefix,
        ))
        if not self.closed:
            self._update_controls()
        if self.request_refresh_running or self.operations.has_results(
                key_prefix=self.request_operation_prefix,
        ):
            self._schedule_request_result_poll()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.request_refresh_sequence += 1
        for after_id in (
            self.request_refresh_after_id,
            self.request_refresh_poll_after_id,
            self.job_operation_after_id,
        ):
            if after_id is None:
                continue
            try:
                self.window.after_cancel(after_id)
            except tk.TclError:
                pass
        self.request_refresh_after_id = None
        self.request_refresh_poll_after_id = None
        self.job_operation_after_id = None
        self.operations.abandon_prefix(
            self.request_operation_prefix,
            wait=True,
        )
        if self._owns_operations:
            self.operations.shutdown(wait=False)
        try:
            self.window.destroy()
        except tk.TclError:
            pass
        if self.on_close is not None:
            self.on_close()
        return True

    def prepare_request(self):
        step_ids = self.scenario_step_ids()
        if not step_ids:
            self.status_var.set("当前范围没有可生成 Step；请恢复或选择至少一个 Step。")
            return
        incomplete = self._scenario_incomplete_steps()
        if incomplete:
            labels = "、".join(
                f"Step {step.ordinal}" for step in incomplete
            )
            self.status_var.set(f"当前场景尚未完成：{labels}。")
            return
        try:
            request = self.request_service.ensure_latest(step_ids, repair=True)
            self.last_request_path = (
                self.session.session_dir / request["request_path"]
            )
            workflow = self.request_service.workflow_state(step_ids)
            self._reload_model()
        except Exception as error:
            self.request_refresh_error = error
            self.last_request_path = None
            self.status_var.set(
                f"准备 Copilot 任务失败: {type(error).__name__}: {error}"
            )
            self._update_controls()
            return
        self.request_refresh_error = None
        status = _request_status_label(workflow)
        self.status_var.set(
            f"{self._scenario_scope_label()} Copilot 任务已准备"
            f"（{status}，{len(step_ids)} 个 Step）: "
            f"{self.last_request_path}"
        )
        self._update_controls()

    def copy_generation_command(self):
        step_ids = self.scenario_step_ids()
        if not step_ids:
            self.status_var.set("当前范围没有可生成 Step；请恢复或选择至少一个 Step。")
            return
        incomplete = self._scenario_incomplete_steps()
        if incomplete:
            labels = "、".join(
                f"Step {step.ordinal}" for step in incomplete
            )
            self.status_var.set(f"当前场景尚未完成：{labels}。")
            return
        try:
            profile_var = getattr(self, "generation_profile_var", None)
            profile_id = (
                profile_var.get()
                if profile_var is not None
                else "generation_first"
            )
            command = self.request_service.generation_command(
                step_ids,
                profile_id=profile_id,
            )
            request = self.request_service.latest(step_ids)
        except Exception as error:
            self.status_var.set(
                f"准备 Copilot 任务失败: {type(error).__name__}: {error}"
            )
            return
        self.last_request_path = (
            self.session.session_dir / request["request_path"]
            if request is not None
            else None
        )
        self.parent.clipboard_clear()
        self.parent.clipboard_append(command)
        self.status_var.set(
            f"已创建{self._scenario_scope_label()}的 Generation Job"
            f"（{len(step_ids)} 个 Step）；"
            "已复制 /recorder-generate Job 命令，粘贴到 Copilot Chat 即可继续。"
        )
        self._reload_model()
        self._refresh_decision_questions()
        self._update_controls()

    def run_recommended_action(self):
        model = getattr(self, "model", None)
        task = getattr(model, "user_task", None) if model else None
        action, _label, _detail = self._recommended_action()
        target_view = getattr(task, "target_view", "review")
        if target_view == "timeline":
            self.open_timeline()
            return
        if target_view == "capture":
            if self.on_rerecord is not None:
                self.on_rerecord(
                    self.session,
                    task.target_step_id or self.selected_step_id(),
                )
            else:
                self.status_var.set(
                    "当前证据缺少可恢复事实，请回到录制工具重录此 Step。"
                )
            return
        if action == "v3_adjust":
            if self.question_frame is not None:
                self.detail_notebook.select(self.question_frame)
            self.status_var.set(
                "请一次完成全部业务确认；提交后才能交给 Copilot。"
            )
            return
        if action in {
            "generate",
            "v3_fast",
            "v3_plan",
            "v3_forensic",
        }:
            self.copy_generation_command()
        elif action in {"refresh_request", "retry_request"}:
            self.prepare_request()
        elif action in {"review_result", "review_failed_result"}:
            self.show_generation_result()
        else:
            self.status_var.set("当前 Step 尚未完成录制。")

    def retry_generation_job(self):
        generation = self.model.generation if self.model else None
        history = getattr(generation, "job_history", ()) if generation else ()
        terminal = next((item for item in history if not item.is_current), None)
        if terminal is None:
            self.status_var.set("当前没有可重试的已结束生成任务。")
            return
        self._run_job_lifecycle(
            "retry",
            terminal.job_id,
            terminal.profile_id,
        )

    def retire_generation_job(self):
        generation = self.model.generation if self.model else None
        if generation is None or not generation.job_id or generation.job_phase != "ready":
            self.status_var.set("只有尚未开始的生成任务可以在此终止。")
            return
        self._run_job_lifecycle("retire", generation.job_id, None)

    def _run_job_lifecycle(self, action, job_id, profile_id):
        history = (
            self.model.generation.job_history
            if self.model and self.model.generation else ()
        )
        job = next((item for item in history if item.job_id == job_id), None)
        if job is None:
            self.status_var.set("Generation Job历史已变化，请刷新后重试。")
            return
        if not job.job_path:
            self.status_var.set("无法解析Generation Job路径。")
            return
        workflow = self.request_service.workflow_state(self.scenario_step_ids())
        epoch = (workflow.get("job_execution") or {}).get("epoch")
        if action == "retire" and epoch is None:
            self.status_var.set("Generation Job状态已变化，请刷新后重试。")
            return
        try:
            self.operations.submit(
                self.job_operation_key,
                self._execute_job_lifecycle,
                action,
                job.job_path,
                profile_id,
                epoch,
                context=action,
            )
        except Exception as error:
            self.status_var.set(f"Generation Job操作失败: {type(error).__name__}: {error}")
            return
        self.status_var.set("正在更新 Generation Job...")
        self._schedule_job_operation_poll()

    def _execute_job_lifecycle(
            self,
            action,
            job_path,
            profile_id,
            epoch,
        ):
        if action == "retry":
            return self.request_service.retry_job(job_path, profile_id=profile_id)
        return self.request_service.retire_job(
            job_path,
            expected_epoch=epoch,
            reason="operator_retired_from_workbench",
        )

    def _schedule_job_operation_poll(self):
        if self.closed or self.job_operation_after_id is not None:
            return
        self.job_operation_after_id = self.window.after(
            40,
            self._poll_job_operation,
        )

    def _poll_job_operation(self):
        self.job_operation_after_id = None
        result = self.operations.next_result(key=self.job_operation_key)
        if result is None:
            if self.operations.list_active(key=self.job_operation_key):
                self._schedule_job_operation_poll()
            return
        if result.status == "completed" and result.error is None:
            self._reload_model()
            self._update_controls()
            self.status_var.set(
                "已创建新的 Generation Job。"
                if result.context == "retry"
                else "未开始的 Generation Job 已终止。"
            )
        else:
            error = result.error
            self.status_var.set(
                "Generation Job操作失败: "
                f"{type(error).__name__}: {error}"
                if error is not None
                else "Generation Job操作未完成。"
            )

    def record_transaction_feedback(self):
        step_ids = self.scenario_step_ids()
        if not step_ids:
            self.status_var.set("当前场景没有目标 Step。")
            return
        request = self.request_service.latest(step_ids)
        if request is None:
            self.status_var.set(
                "当前场景的 Copilot 任务仍在更新，请等待完成后再反馈。"
            )
            return
        latest = latest_transaction(
            self.session.session_dir,
            request_id=request.get("request_id"),
            completed_only=True,
        )
        if latest is None:
            self.status_var.set(
                "当前场景还没有完成的 Copilot 生成报告；"
                "生成完成后重新打开审阅即可反馈。"
            )
            return
        report_path, report = latest
        status = FEEDBACK_LABELS.get(self.feedback_var.get())
        try:
            event = save_transaction_feedback(
                report_path,
                status,
                self.feedback_note_var.get().strip(),
            )
        except Exception as error:
            self.status_var.set(
                f"保存结果确认失败: {type(error).__name__}: {error}"
            )
            return
        self.feedback_note_var.set("")
        self.status_var.set(_feedback_saved_message(
            self.feedback_var.get(),
            event,
        ))

    def open_project_memory(self):
        memory_dir = (
            ensure_knowledge_store(self.session.output_root)
            / "project-memory"
        )
        memory_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(memory_dir)
        except Exception as error:
            self.status_var.set(
                f"打开项目经验目录失败: {type(error).__name__}: {error}"
            )

    def show_generation_result(self):
        result = (self.model.generation.result if self.model else None)
        if result is None:
            self.status_var.set("当前 Scenario 还没有可审阅的生成报告。")
            return
        self._refresh_generation_result()
        self.detail_notebook.select(self.result_frame)

    def open_generation_report(self):
        result = (self.model.generation.result if self.model else None)
        if result is None:
            self.status_var.set("当前没有生成事务报告。")
            return
        self._open_project_path(result.report_path, "详细报告")

    def open_selected_generated_file(self):
        selected = self.result_tree.selection() if self.result_tree else ()
        if not selected:
            self.status_var.set("请选择一个生成文件。")
            return
        relative = self.result_tree.item(selected[0], "values")[0]
        path = (Paths.BASE_DIR / str(relative)).resolve()
        try:
            path.relative_to(Paths.BASE_DIR.resolve())
        except ValueError:
            self.status_var.set("生成文件路径越界，已拒绝打开。")
            return
        try:
            os.startfile(path)
        except Exception as error:
            self.status_var.set(
                f"打开生成文件失败: {type(error).__name__}: {error}"
            )

    def open_runtime_report(self):
        runtime = self.model.runtime_result if self.model else None
        if runtime is None or not runtime.report_path:
            self.status_var.set("当前 Scenario 没有匹配的真实运行报告。")
            return
        self._open_project_path(
            runtime.report_path,
            "运行报告",
            runtime.report_sha256,
            runtime.report_size,
        )

    def open_runtime_attachment(self):
        runtime = self.model.runtime_result if self.model else None
        if runtime is None or not runtime.first_attachment_path:
            self.status_var.set("当前 Scenario 没有可打开的失败附件。")
            return
        self._open_project_path(
            runtime.first_attachment_path,
            "失败附件",
            runtime.first_attachment_sha256,
            runtime.first_attachment_size,
        )

    def _refresh_decision_questions(self):
        generation = self.model.generation if self.model else None
        decision = generation.decision if generation is not None else None
        self.decision_questions = list(
            decision.questions if decision is not None else ()
        )
        request_id = (
            generation.request_id if generation is not None else None
        )
        if request_id != self.decision_request_id:
            self.decision_selections = {}
            self.decision_request_id = request_id
        valid_options = {
            question.question_id: {
                option.option_id for option in question.options
            }
            for question in self.decision_questions
        }
        self.decision_selections = {
            question_id: option_id
            for question_id, option_id in self.decision_selections.items()
            if option_id in valid_options.get(question_id, set())
        }
        self.question_tree.delete(*self.question_tree.get_children())
        for index, question in enumerate(self.decision_questions):
            self.question_tree.insert(
                "",
                "end",
                iid=f"question-{index}",
                values=(
                    question.step_text or question.step_id or "当前场景",
                    question.title or question.prompt,
                ),
            )
        if self.question_tree.get_children():
            first = self.question_tree.get_children()[0]
            self.question_tree.selection_set(first)
        self._show_selected_question()

    def selected_decision_question(self):
        selected = self.question_tree.selection() if self.question_tree else ()
        if not selected:
            return None
        try:
            index = int(selected[0].rsplit("-", 1)[-1])
            return self.decision_questions[index]
        except (IndexError, ValueError):
            return None

    def _show_selected_question(self):
        question = self.selected_decision_question()
        detail = _decision_question_text(question)
        self.question_detail.configure(state="normal")
        self.question_detail.delete("1.0", "end")
        self.question_detail.insert("1.0", detail)
        self.question_detail.configure(state="disabled")
        preview = _decision_question_preview_reference(question)
        self.question_preview_path = preview[0] if preview else None
        self.question_preview_sha256 = preview[1] if preview else None
        self.question_preview_size = preview[2] if preview else None
        self.question_open_button.configure(
            state="normal" if self.question_preview_path else "disabled",
        )
        self._render_question_options(question)
        self._render_question_media(question)

    def _render_question_options(self, question):
        frame = self.question_options_frame
        if frame is None:
            return
        for child in frame.winfo_children():
            child.destroy()
        selected = (
            self.decision_selections.get(question.question_id, "")
            if question is not None
            else ""
        )
        self.question_option_var.set(selected)
        if question is None or not question.options:
            ttk.Label(frame, text="当前问题没有可提交选项。").pack(
                anchor="w",
                padx=6,
                pady=4,
            )
        else:
            for option in question.options:
                ttk.Radiobutton(
                    frame,
                    text=option.label,
                    value=option.option_id,
                    variable=self.question_option_var,
                    command=self._record_selected_question_option,
                ).pack(anchor="w", padx=6, pady=2)
        self._update_decision_submit_state()

    def _record_selected_question_option(self):
        question = self.selected_decision_question()
        option_id = self.question_option_var.get()
        if question is not None and option_id:
            self.decision_selections[question.question_id] = option_id
        self._update_decision_submit_state()

    def _update_decision_submit_state(self):
        button = self.question_submit_button
        if button is None:
            return
        blocking = [
            question
            for question in self.decision_questions
            if question.blocking
        ]
        complete = bool(blocking) and all(
            self.decision_selections.get(question.question_id)
            in {option.option_id for option in question.options}
            for question in blocking
        )
        button.configure(state="normal" if complete else "disabled")

    def submit_decision_batch(self):
        step_ids = self.scenario_step_ids()
        if not step_ids:
            self.status_var.set("当前范围没有可回答的业务问题。")
            return
        try:
            self.request_service.answer_decision_batch(
                step_ids,
                self.decision_selections,
            )
        except Exception as error:
            self.status_var.set(
                f"提交业务确认失败: {type(error).__name__}: {error}"
            )
            return
        self.decision_selections = {}
        self.status_var.set("业务确认已提交，生成任务正在重新检查。")
        self.refresh()

    def _render_question_media(self, question):
        self.question_image_ref = None
        self.question_image.configure(image="")
        if question is None or not question.media:
            self.question_image.configure(
                text="此问题没有对应界面现场，请根据上方规格或业务说明判断。"
            )
            return
        media = question.media[0]
        frames = []
        for label, relative, highlight_box, sha256, size in (
            (
                "操作前",
                media.before_path,
                media.before_highlight_box,
                media.before_sha256,
                media.before_size,
            ),
            (
                "操作后",
                media.after_path or media.context_path,
                (
                    media.after_highlight_box
                    if media.after_path
                    else media.context_highlight_box
                ),
                (
                    media.after_sha256
                    if media.after_path
                    else media.context_sha256
                ),
                (
                    media.after_size
                    if media.after_path
                    else media.context_size
                ),
            ),
        ):
            image = _load_project_preview_image(
                relative,
                highlight_box,
                label,
                sha256,
                size,
            )
            if image is not None:
                frames.append(image)
        if not frames:
            self.question_image.configure(text="相关截图不可用或已移动。")
            return
        width = sum(image.width for image in frames) + 8 * (len(frames) - 1)
        height = max(image.height for image in frames)
        preview = Image.new("RGB", (width, height), "white")
        left = 0
        for image in frames:
            preview.paste(image, (left, 0))
            left += image.width + 8
        self.question_image_ref = ImageTk.PhotoImage(preview)
        self.question_image.configure(
            image=self.question_image_ref,
            text="",
        )

    def open_selected_question_media(self):
        if not self.question_preview_path:
            self.status_var.set("当前问题没有可打开的现场截图。")
            return
        self._open_project_path(
            self.question_preview_path,
            "问题现场",
            self.question_preview_sha256,
            self.question_preview_size,
        )

    def _open_project_path(
            self,
            value,
            label,
            expected_sha256=None,
            expected_size=None,
        ):
        path = (Paths.BASE_DIR / str(value)).resolve()
        try:
            path.relative_to(Paths.BASE_DIR.resolve())
        except ValueError:
            self.status_var.set(f"{label}路径越界，已拒绝打开。")
            return
        if expected_sha256 is not None or expected_size is not None:
            if _verified_project_file_bytes(
                value,
                expected_sha256,
                expected_size,
            ) is None:
                self.status_var.set(f"{label}内容已变化，已拒绝打开。")
                return
        try:
            os.startfile(path)
        except Exception as error:
            self.status_var.set(
                f"打开{label}失败: {type(error).__name__}: {error}"
            )

    def _refresh_generation_result(self):
        result = (self.model.generation.result if self.model else None)
        runtime = self.model.runtime_result if self.model else None
        self.result_tree.delete(*self.result_tree.get_children())
        if runtime is None:
            self.runtime_summary_var.set("真实运行：尚无匹配结果。")
        else:
            error = f"；首个错误：{runtime.first_error}" if runtime.first_error else ""
            diagnostic = (
                f"；排障：{_runtime_diagnostic_text(runtime.first_diagnostic)}"
                if runtime.first_diagnostic is not None
                else ""
            )
            self.runtime_summary_var.set(
                f"真实运行：{runtime.status}；失败 Step "
                f"{runtime.failed_step_count}；附件 "
                f"{runtime.attachment_count}{diagnostic}{error}"
            )
        if result is None:
            self.result_summary_var.set("当前 Scenario 尚无生成结果。")
            return
        self.result_summary_var.set(
            f"验证层级：{(self.model.generation.verification.label if self.model.generation.verification else '未知')}；"
            f"生成状态：{_result_status_label(result.status)}；"
            f"修改文件 {len(result.changed_files)}；"
            f"未通过检查 {len(result.failed_checks)}；"
            f"下一步：{result.recommended_label}"
        )
        for index, path in enumerate(result.changed_files):
            self.result_tree.insert(
                "",
                "end",
                iid=f"generated-file-{index}",
                values=(
                    path,
                    (
                        "静态检查通过"
                        if self.model.generation.verification
                        and self.model.generation.verification.implementation_validated
                        else "需检查"
                    ),
                ),
            )

    def _sync_result_tabs(self):
        generation = self.model.generation if self.model else None
        result = generation.result if generation is not None else None
        verification = (
            generation.verification if generation is not None else None
        )
        runtime = self.model.runtime_result if self.model else None
        question_frame = getattr(self, "question_frame", None)
        decision_questions = getattr(self, "decision_questions", ())
        if question_frame is not None:
            self._set_detail_tab_visible(
                question_frame,
                "业务确认",
                bool(decision_questions),
            )
        self._set_detail_tab_visible(
            self.result_frame,
            "本次生成",
            result is not None or runtime is not None,
        )
        self._set_detail_tab_visible(
            self.feedback_frame,
            "确认结果",
            bool(
                result is not None
                and result.status in {
                    "completed",
                    "completed_no_changes",
                }
                and not result.failed_checks
                and verification is not None
                and verification.implementation_validated
            ),
        )
        if (
            generation is not None
            and getattr(generation, "workflow_status", None)
            == "needs_adjustment"
            and decision_questions
            and question_frame is not None
        ):
            self.detail_notebook.select(question_frame)

    def _set_detail_tab_visible(self, frame, label, visible):
        frame_id = str(frame)
        tabs = set(self.detail_notebook.tabs())
        if visible:
            if frame_id not in tabs:
                self.detail_notebook.add(frame, text=label)
            else:
                self.detail_notebook.add(frame)
                self.detail_notebook.tab(frame, text=label)
        elif not visible and frame_id in tabs:
            if self.detail_notebook.select() == frame_id:
                self.detail_notebook.select(self.issues_frame)
            self.detail_notebook.hide(frame)

    def toggle_feedback(self):
        self.detail_notebook.select(self.feedback_frame)

    def open_session_dir(self):
        try:
            os.startfile(self.session.session_dir)
        except Exception as error:
            self.status_var.set(
                f"打开目录失败: {type(error).__name__}: {error}"
            )

    def _refresh_diagnostics(self):
        step_id = self.selected_step_id()
        step = next(
            (
                item
                for item in (self.model.steps if self.model else ())
                if item.step_id == step_id
            ),
            None,
        )
        self.diagnostics = list(step.issues) if step is not None else []
        self.diagnostic_step_id = step_id
        self.issue_tree.delete(*self.issue_tree.get_children())
        for index, diagnostic in enumerate(self.diagnostics):
            self.issue_tree.insert(
                "",
                "end",
                iid=f"diagnostic-{index}",
                values=(
                    diagnostic.title,
                    diagnostic.location or "当前录制",
                    _repair_text(diagnostic.repair),
                ),
            )
        if self.issue_tree.get_children():
            first = self.issue_tree.get_children()[0]
            self.issue_tree.selection_set(first)
        self._show_selected_diagnostic()

    def selected_diagnostic(self):
        selected = self.issue_tree.selection() if self.issue_tree else ()
        if not selected:
            return None
        try:
            index = int(selected[0].rsplit("-", 1)[-1])
            return self.diagnostics[index]
        except (IndexError, ValueError):
            return None

    def _show_selected_diagnostic(self):
        diagnostic = self.selected_diagnostic()
        text = (
            diagnostic.detail
            if diagnostic is not None
            else "当前 Step 没有需要你处理的问题，可以交给 Copilot。"
        )
        self.issue_detail.configure(state="normal")
        self.issue_detail.delete("1.0", "end")
        self.issue_detail.insert("1.0", text)
        self.issue_detail.configure(state="disabled")
        event_ids = diagnostic.event_ids if diagnostic is not None else ()
        self.locate_button.configure(
            state="normal" if event_ids else "disabled",
        )
        repair = diagnostic.repair if diagnostic is not None else None
        repair_labels = {
            "timeline": "去校正",
            "rerecord": "补录此 Step",
        }
        actionable = repair in {"timeline", "rerecord"}
        self.repair_button.configure(
            text=repair_labels.get(repair, "Copilot 会自动处理"),
            state="normal" if actionable else "disabled",
        )

    def locate_selected_diagnostic(self):
        diagnostic = self.selected_diagnostic()
        if diagnostic is None:
            self.status_var.set("当前没有可定位的问题。")
            return
        event_ids = (
            diagnostic.get("event_ids") or ()
            if isinstance(diagnostic, dict)
            else diagnostic.event_ids
        )
        if not event_ids:
            self.status_var.set(
                "该问题涉及整段录制，无法定位到单个操作。"
            )
            return
        self.open_timeline(focus_event_ids=event_ids)

    def open_selected_evidence(self):
        diagnostic = self.selected_diagnostic()
        path = diagnostic.evidence_path if diagnostic is not None else None
        if path is None:
            self.status_var.set("当前问题没有可打开的证据文件。")
            return
        self._open_project_path(path, "证据")

    def repair_selected_diagnostic(self):
        diagnostic = self.selected_diagnostic()
        if diagnostic is None:
            self.status_var.set("当前没有待修复问题。")
            return
        repair = diagnostic.repair
        if repair == "timeline":
            self.locate_selected_diagnostic()
        elif repair == "rerecord":
            if self.on_rerecord is not None:
                self.on_rerecord(self.session, self.selected_step_id())
            else:
                self.status_var.set(
                    "请从录制库打开此任务，再点击“补录当前 Step”。"
                )

    def _update_controls(self):
        step_id = self.selected_step_id()
        viewed_take = self.selected_take_entry()
        has_editable_take = bool(
            viewed_take is not None
            and viewed_take.status == "completed"
        )
        step = self._selected_step_workspace()
        selected_take_id = step.selected_take_id if step else None
        self.timeline_button.configure(
            text="检查与补录" if has_editable_take else "录制当前 Step",
            state="normal" if step_id is not None else "disabled",
        )
        self.select_take_button.configure(
            state="normal"
            if viewed_take is not None
            and viewed_take.status == "completed"
            and viewed_take.take_id != selected_take_id
            else "disabled"
        )
        action, label, detail = self._recommended_action()
        self.next_action_button.configure(
            text=(
                "交给 Copilot"
                if action in {
                    "generate",
                    "v3_fast",
                    "v3_plan",
                    "v3_forensic",
                }
                else label
            ),
            state="normal" if action != "pending" else "disabled",
        )
        self.next_action_var.set(detail)
        generation = self.model.generation if self.model else None
        job_id = getattr(generation, "job_id", None)
        profile_id = getattr(generation, "generation_profile_id", None)
        if profile_id:
            self.generation_profile_var.set(profile_id)
        for button, enabled in self.profile_buttons:
            button.configure(
                state=(
                    "normal"
                    if enabled and not job_id
                    else "disabled"
                )
            )
        self._update_evidence_summary()
        self._refresh_diagnostics_if_step_changed()

    def _update_evidence_summary(self):
        take = self.selected_take_entry()
        if take is None:
            self.evidence_summary_var.set("录制质量：请选择一个已完成的录制版本。")
            return
        evidence = take.evidence_summary
        if evidence is None:
            self.evidence_summary_var.set(
                "录制质量：正在检查本次录制内容。"
            )
            return
        generation = self.model.generation if self.model else None
        task_status = ""
        if generation is not None:
            if generation.workflow_status == "stale":
                task_status = "；正在根据最新录制重新准备"
            elif generation.request_path:
                task_status = "；生成任务已准备"
        self.evidence_summary_var.set(
            "录制证据："
            f"已关联事件 {evidence.linked_event_count}/"
            f"{evidence.event_count}；完整动作 "
            f"{evidence.complete_action_count}/{evidence.action_count}"
            f"{task_status}"
        )

    def _recommended_action(self):
        if getattr(self, "request_refresh_error", None) is not None:
            return (
                "retry_request",
                "重试准备",
                "Copilot 任务自动准备失败，请重试。",
            )
        task = getattr(self.model, "user_task", None) if self.model else None
        if task is None:
            return "pending", "正在准备", "证据与生成状态正在更新。"
        return task.action, task.action_label, task.reason

    def _refresh_diagnostics_if_step_changed(self):
        current_step = self.selected_step_id()
        if current_step != getattr(self, "diagnostic_step_id", None):
            self._refresh_diagnostics()

    def _reload_model(self):
        self.model = self.query_service.get_workbench(
            self.selected_step_id()
        )
        self.summary_var.set(
            f"Feature: {self.model.feature_name}    "
            f"Scenario: {self.model.scenario_name}    "
            f"范围: {self.model.scope.label}"
        )
        self.capture_summary_var.set(_capture_summary_text(self.model))
        self.generation_summary_var.set(
            _generation_summary_text(self.model.generation)
        )
        self._refresh_generation_result()
        self._sync_result_tabs()


def _status_label(status):
    return {
        "pending": "待录制",
        "recording": "录制中",
        "completed": "已完成",
        "skipped": "已跳过",
    }.get(status, status)


def _evidence_status_label(status):
    return {
        "unavailable": "无证据",
        "clean": "已就绪",
        "needs_review": "待复核",
        "broken": "需修复",
    }.get(str(status), str(status or "未知"))


def _generation_status_label(status):
    return {
        "unavailable": "不可用",
        "scenario_incomplete": "待完成",
        "updating": "准备中",
        "ready": "可交给 Copilot",
        "needs_input": "需确认业务问题",
        "blocked": "需补充证据",
        "running": "正在生成",
        "completed": "已完成",
        "failed": "生成未完成",
    }.get(str(status), str(status or "未知"))


def _result_status_label(status):
    return {
        "running": "进行中",
        "completed": "已完成",
        "completed_no_changes": "已完成，无需修改",
        "failed": "未完成",
        "aborted": "已取消",
        "aborting": "正在取消",
    }.get(str(status), "待确认")


def _decision_question_text(question):
    if question is None:
        return "当前没有需要确认的业务问题。"
    lines = [question.prompt or question.title]
    if question.observed:
        lines.extend(("", f"现场与事实：{question.observed}"))
    if question.uncertainty:
        lines.append(f"需要你判断：{question.uncertainty}")
    if question.options:
        lines.extend(("", "可选答案："))
        for index, option in enumerate(question.options, start=1):
            effect = f"；影响：{option.effect}" if option.effect else ""
            lines.append(f"{index}. {option.label}{effect}")
    return "\n".join(lines)


def _decision_question_preview_path(question):
    reference = _decision_question_preview_reference(question)
    return reference[0] if reference else None


def _decision_question_preview_reference(question):
    if question is None:
        return None
    for media in question.media:
        for reference in (
            (media.after_path, media.after_sha256, media.after_size),
            (media.context_path, media.context_sha256, media.context_size),
            (media.before_path, media.before_sha256, media.before_size),
        ):
            if reference[0]:
                return reference
    return None


def _runtime_diagnostic_text(diagnostic):
    if diagnostic is None:
        return ""
    parts = [diagnostic.summary]
    target = diagnostic.locator_name or ""
    if target:
        parts.append(f"目标={target}")
    if diagnostic.root_name:
        parts.append(f"窗口={diagnostic.root_name}")
    if diagnostic.wait_type:
        parts.append(f"等待={diagnostic.wait_type}")
    if diagnostic.probe_count is not None:
        parts.append(f"检查={diagnostic.probe_count}次")
    if diagnostic.candidate_count is not None:
        parts.append(f"候选={diagnostic.candidate_count}")
    if diagnostic.cause_type:
        cause = diagnostic.cause_type
        if diagnostic.cause_message:
            cause += f": {diagnostic.cause_message}"
        parts.append(f"底层={cause}")
    return "；".join(parts)


def _load_project_preview_image(
        relative,
        highlight_box,
        label,
        expected_sha256=None,
        expected_size=None,
    ):
    if not relative:
        return None
    content = _verified_project_file_bytes(
        relative,
        expected_sha256,
        expected_size,
    )
    if content is None:
        return None
    try:
        with Image.open(BytesIO(content)) as source:
            image = source.convert("RGB")
    except (OSError, ValueError):
        return None
    if highlight_box is not None:
        left, top, right, bottom = highlight_box
        clipped = (
            max(0, min(image.width - 1, left)),
            max(0, min(image.height - 1, top)),
            max(0, min(image.width - 1, right)),
            max(0, min(image.height - 1, bottom)),
        )
        if clipped[2] > clipped[0] and clipped[3] > clipped[1]:
            ImageDraw.Draw(image).rectangle(
                clipped,
                outline="#e23d28",
                width=5,
            )
    image.thumbnail((340, 250), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (image.width, image.height + 24), "white")
    canvas.paste(image, (0, 24))
    ImageDraw.Draw(canvas).text((6, 5), label, fill="#202020")
    return canvas


def _verified_project_file_bytes(
        relative,
        expected_sha256=None,
        expected_size=None,
    ):
    project_root = Path(Paths.BASE_DIR).resolve()
    path = (project_root / str(relative)).resolve()
    try:
        path.relative_to(project_root)
        content = path.read_bytes()
    except (OSError, ValueError):
        return None
    if expected_sha256 is None and expected_size is None:
        return content
    try:
        if any((
            expected_sha256 is None,
            expected_size is None,
            len(content) != int(expected_size),
            hashlib.sha256(content).hexdigest() != str(expected_sha256),
        )):
            return None
    except (TypeError, ValueError):
        return None
    return content


def _capture_summary_text(model):
    completed = sum(
        step.capture_status == "completed"
        for step in model.steps
    )
    skipped = sum(
        step.capture_status == "skipped"
        for step in model.steps
    )
    selected = next(
        (
            step
            for step in model.steps
            if step.step_id == model.selected_step_id
        ),
        None,
    )
    take = next(
        (item for item in selected.takes if item.selected),
        None,
    ) if selected is not None else None
    current = (
        f"当前录制：事件 {take.event_count} / 动作 {take.action_count} / "
        f"窗口 {take.window_count}"
        if take is not None
        else "当前 Step 尚无可用于生成的录制"
    )
    return (
        f"录制：完成 {completed}/{len(model.steps)}，跳过 {skipped}；"
        f"{current}"
    )


def _generation_summary_text(generation):
    if generation is None:
        return "阶段：尚无生成状态"
    decision = generation.decision
    if generation.workflow_status == "needs_adjustment":
        return (
            f"生成：有业务问题需要确认；共 "
            f"{generation.pending_user_ambiguities or decision.blocking_count}"
        )
    if generation.workflow_status == "forensic":
        return (
            "生成：有录制事实需要 Copilot 自动核对；"
            f"共 {generation.pending_ai_ambiguities} 项"
        )
    if generation.workflow_status == "draft":
        return "生成：录制证据已准备，可以交给 Copilot"
    if generation.workflow_status == "ready":
        return "生成：任务已准备，可以交给 Copilot"
    if generation.workflow_status == "blocked":
        return "生成：缺少必要证据，请按右侧提示校正或补录"
    if generation.workflow_status == "stale":
        return "生成：录制已变化，正在自动准备最新任务"
    history = getattr(generation, "job_history", ())
    feedback = getattr(generation, "feedback_history", ())
    history_text = f"；任务历史 {len(history)}" if history else ""
    effective_feedback = [item for item in feedback if item.is_effective]
    tier_counts = {
        tier: sum(item.tier == tier for item in effective_feedback)
        for tier in (
            "accepted_static_only",
            "accepted_runtime_verified",
            "accepted_oracle_verified",
        )
    }
    feedback_text = (
        "；反馈 静态/运行/Oracle "
        f"{tier_counts['accepted_static_only']}/"
        f"{tier_counts['accepted_runtime_verified']}/"
        f"{tier_counts['accepted_oracle_verified']}"
        if effective_feedback else ""
    )
    if generation.result is not None:
        result = generation.result
        verification = generation.verification
        return (
            f"生成：{(verification.label if verification else _generation_status_label(generation.display_status))}；"
            f"修改文件 {len(result.changed_files)}，"
            f"失败检查 {len(result.failed_checks)}"
            f"{history_text}{feedback_text}"
        )
    return (
        f"生成：{_generation_status_label(generation.display_status)}"
        f"{history_text}{feedback_text}"
    )


def _request_status_label(workflow):
    return {
        "draft": "可以交给 Copilot",
        "ready": "任务已准备",
        "needs_adjustment": "需要确认业务问题",
        "forensic": "需要 Copilot 核对录制事实",
        "blocked": "需要校正或补录",
        "stale": "正在根据最新录制重新准备",
        "running": "正在生成",
        "completed": "生成已完成",
        "failed": "生成未完成",
    }.get((workflow or {}).get("status"), "正在准备")


def _repair_text(repair):
    return {
        "timeline": "检查录制",
        "reconcile": "交给 Copilot",
        "rerecord": "补录当前 Step",
    }.get(repair, str(repair or "检查证据"))


__all__ = ["RecorderReviewWindow"]
