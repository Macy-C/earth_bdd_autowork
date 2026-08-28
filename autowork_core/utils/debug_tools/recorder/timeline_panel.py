from __future__ import annotations

from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

from PIL import Image, ImageChops, ImageEnhance, ImageTk

from autowork_core.utils.debug_tools.recorder.media import extract_video_frame
from autowork_core.utils.debug_tools.recorder.query_service import (
    RecorderQueryService,
)
from autowork_core.utils.debug_tools.recorder.supplement_window import (
    SupplementRecordingWindow,
)
from autowork_core.utils.debug_tools.recorder.timeline import (
    TimelineStore,
)
from autowork_core.utils.debug_tools.recorder.take_query_service import (
    TakeQueryService,
)


VIEW_LABELS = {
    "before": "操作前",
    "after": "操作后",
    "diff": "差异",
    "video": "视频帧",
}
VIEW_VALUES = {label: value for value, label in VIEW_LABELS.items()}
OBSERVATION_FOCUS_LABELS = {
    "自动核对业务描述": "auto",
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
    "自动核对 Feature": "auto",
    "Feature 文案": "feature",
    "Examples 列": "examples",
    "Data Table 列": "data_table",
    "当前看到的结果就是期望": "observed_state",
}


class TimelineEditorWindow:
    def __init__(
            self,
            parent,
            take_dir,
            on_change=None,
            focus_event_ids=None,
            *,
            on_close=None,
            operation_coordinator=None,
            capture_window_controller=None,
            mutation_handler=None,
            session=None,
            step_id=None,
            owner_take_id=None,
        ):
        self.parent = parent
        self.take_dir = Path(take_dir)
        self.query = TakeQueryService(self.take_dir)
        self.on_change = on_change
        self.focus_event_ids = set(focus_event_ids or ())
        self.on_close = on_close
        self.operations = operation_coordinator
        self.capture_window_controller = capture_window_controller
        self.mutation_handler = mutation_handler
        self.session = session
        self.step_id = str(step_id) if step_id else None
        self.owner_take_id = (
            str(owner_take_id) if owner_take_id else None
        )
        self.recorder_query = (
            RecorderQueryService(
                session,
                operation_coordinator=operation_coordinator,
            )
            if session is not None and self.step_id is not None
            else None
        )
        self.store = TimelineStore(self.take_dir)
        self.media_index, self.action_media = self.query.media_bundle()
        self.action_media_map = {
            item.get("action_id"): item
            for item in self.action_media.get("actions", [])
        }
        self.event_media = {
            item.get("event_id"): item
            for item in self.media_index.get("events", [])
        }
        self.photo = None

        self.window = ttk.Frame(parent)
        self.window.grid(row=0, column=0, sticky="nsew")

        self.status_var = tk.StringVar(value="检查录制内容：忽略误录，或补上缺失动作。")
        self.zoom_var = tk.StringVar(value="适应")
        self.view_var = tk.StringVar(value=VIEW_LABELS["after"])
        self.preview_canvas = None
        self.preview_image_id = None
        self.source_image = None
        self.current_image_path = None
        self.current_action = None
        self.current_action_media = None
        self.zoom_factor = None
        self.tree = None
        self.undo_button = None
        self.redo_button = None
        self.supplement_window = None
        self.pending_supplement_before_action_id = None
        self.timeline_revision = None
        self.review_action_map = {}
        self.keyboard_event_rows = {}
        self.busy = False
        self.closed = False
        self.mutation_controls = []
        self.mutation_control_states = {}
        self.mutation_operation_key = f"timeline:{id(self)}:mutation"
        self.mutation_poll_after_id = None
        self.busy_after_id = None
        self.busy_frame = None
        self.busy_progress = None
        self.status_label = None
        self.simple_ignore_button = None
        self.detail_notebook = None
        self.observations_frame = None
        self.observation_tree = None
        self.observations = []
        self.observation_focus_var = tk.StringVar(value="")
        self.observation_relation_var = tk.StringVar(value="")
        self.observation_source_var = tk.StringVar(value="")
        self.observation_reference_var = tk.StringVar(value="")
        self.observation_meaning_var = tk.StringVar(value="")
        self.observation_focus_combo = None
        self.observation_relation_combo = None
        self.observation_source_combo = None
        self.observation_reference_combo = None
        self.observation_save_button = None
        self.observation_operation_prefix = (
            f"timeline:{id(self)}:observation:"
        )
        self.observation_poll_after_id = None
        self.preview_operation_key = f"timeline:{id(self)}:preview"
        self.preview_poll_after_id = None
        self.preview_sequence = 0

        self._build_ui()
        self.refresh(notify=False)
        self._focus_events(self.focus_event_ids)

    def show(self):
        self.window.grid(row=0, column=0, sticky="nsew")
        self.window.tkraise()
        return self

    def _build_ui(self):
        toolbar = ttk.Frame(self.window)
        toolbar.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(
            toolbar,
            text="本次录制内容",
            font=("Microsoft YaHei UI", 13, "bold"),
        ).pack(side="left", padx=(2, 14))
        self.undo_button = ttk.Button(toolbar, text="撤销", command=self._undo)
        self.undo_button.pack(side="left")
        self.redo_button = ttk.Button(toolbar, text="重做", command=self._redo)
        self.redo_button.pack(side="left", padx=(5, 12))
        self.mutation_controls.extend((self.undo_button, self.redo_button))
        ttk.Separator(toolbar, orient="vertical").pack(
            side="left",
            fill="y",
            padx=3,
        )
        supplement_button = ttk.Button(
            toolbar,
            text="补录缺失操作",
            command=self.open_supplement_window,
        )
        supplement_button.pack(side="left", padx=(8, 5))
        self.simple_ignore_button = ttk.Button(
            toolbar,
            text="忽略错误动作",
            command=self._toggle_simple_ignored,
        )
        self.simple_ignore_button.pack(side="left", padx=5)
        self.mutation_controls.append(self.simple_ignore_button)
        self.mutation_controls.append(supplement_button)
        ttk.Button(toolbar, text="完成校正", command=self.close).pack(side="right")

        body = ttk.Panedwindow(self.window, orient="horizontal")
        body.pack(fill="both", expand=True, padx=10, pady=5)
        left = ttk.Frame(body, width=600)
        right = ttk.Frame(body, width=400)
        left.grid_propagate(False)
        right.grid_propagate(False)
        body.add(left, weight=3)
        body.add(right, weight=2)

        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        columns = (
            "included",
            "ordinal",
            "summary",
        )
        self.tree = ttk.Treeview(
            left,
            columns=columns,
            show="tree headings",
            selectmode="browse",
        )
        self.tree.heading("#0", text="")
        self.tree.column("#0", width=24, minwidth=24, stretch=False)
        headings = (
            ("included", "保留", 45),
            ("ordinal", "序号", 50),
            ("summary", "录制内容", 390),
        )
        for column, label, width in headings:
            self.tree.heading(column, text=label)
            self.tree.column(
                column,
                width=width,
                minwidth=40,
                stretch=column == "summary",
            )
        y_scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        x_scroll = ttk.Scrollbar(left, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<<TreeviewSelect>>", self._on_selection)
        self.tree.tag_configure("supplement_action", foreground="#0b5f45")

        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)
        self.detail_notebook = ttk.Notebook(right)
        self.detail_notebook.grid(
            row=0,
            column=0,
            sticky="nsew",
            padx=(8, 0),
        )
        preview = ttk.Frame(self.detail_notebook)
        self.detail_notebook.add(preview, text="录制画面")
        preview.rowconfigure(1, weight=1)
        preview.columnconfigure(0, weight=1)
        zoom_bar = ttk.Frame(preview)
        zoom_bar.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 0))
        ttk.Button(zoom_bar, text="−", width=3, command=self.zoom_out).pack(side="left")
        ttk.Button(zoom_bar, text="+", width=3, command=self.zoom_in).pack(side="left", padx=4)
        ttk.Button(zoom_bar, text="适应", command=self.zoom_fit).pack(side="left")
        ttk.Button(zoom_bar, text="100%", command=self.zoom_actual).pack(side="left", padx=4)
        ttk.Label(zoom_bar, textvariable=self.zoom_var).pack(side="left", padx=8)
        ttk.Label(zoom_bar, text="视图").pack(side="left", padx=(12, 4))
        view_combo = ttk.Combobox(
            zoom_bar,
            state="readonly",
            width=9,
            values=tuple(VIEW_LABELS.values()),
            textvariable=self.view_var,
        )
        view_combo.pack(side="left")
        view_combo.bind("<<ComboboxSelected>>", self._on_view_changed)

        canvas_frame = ttk.Frame(preview)
        canvas_frame.grid(row=1, column=0, sticky="nsew", padx=6, pady=6)
        canvas_frame.rowconfigure(0, weight=1)
        canvas_frame.columnconfigure(0, weight=1)
        self.preview_canvas = tk.Canvas(
            canvas_frame,
            background="#202020",
            highlightthickness=0,
        )
        preview_x = ttk.Scrollbar(
            canvas_frame,
            orient="horizontal",
            command=self.preview_canvas.xview,
        )
        preview_y = ttk.Scrollbar(
            canvas_frame,
            orient="vertical",
            command=self.preview_canvas.yview,
        )
        self.preview_canvas.configure(
            xscrollcommand=preview_x.set,
            yscrollcommand=preview_y.set,
        )
        self.preview_canvas.grid(row=0, column=0, sticky="nsew")
        preview_y.grid(row=0, column=1, sticky="ns")
        preview_x.grid(row=1, column=0, sticky="ew")
        self.preview_canvas.bind("<Configure>", self._on_preview_resize)
        self.preview_canvas.bind("<Control-MouseWheel>", self._on_zoom_wheel)
        self._show_preview_message("选择一个动作查看证据")

        self.observations_frame = ttk.Frame(self.detail_notebook)
        self.detail_notebook.add(
            self.observations_frame,
            text="F9 检查",
        )
        self._build_observation_editor(self.observations_frame)
        self.detail_notebook.hide(self.observations_frame)

        self.busy_frame = ttk.Frame(self.window)
        ttk.Label(
            self.busy_frame,
            text="正在更新录制内容与 AI 结果...",
        ).pack(side="left")
        self.busy_progress = ttk.Progressbar(
            self.busy_frame,
            mode="indeterminate",
            length=180,
        )
        self.busy_progress.pack(side="left", padx=(10, 0))

        self.status_label = ttk.Label(
            self.window,
            textvariable=self.status_var,
            anchor="w",
        )
        self.status_label.pack(fill="x", padx=12, pady=(0, 10))

    def _build_observation_editor(self, parent):
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)
        self.observation_tree = ttk.Treeview(
            parent,
            columns=("operation", "target", "check", "state"),
            show="headings",
            selectmode="browse",
            height=5,
        )
        for column, label, width in (
            ("operation", "操作", 72),
            ("target", "检查目标", 120),
            ("check", "已记录的检查", 230),
            ("state", "状态", 80),
        ):
            self.observation_tree.heading(column, text=label)
            self.observation_tree.column(
                column,
                width=width,
                minwidth=70,
                stretch=column == "check",
            )
        self.observation_tree.grid(
            row=0,
            column=0,
            sticky="nsew",
            padx=6,
            pady=6,
        )
        self.observation_tree.bind(
            "<<TreeviewSelect>>",
            lambda event: self._load_selected_observation(),
        )
        editor = ttk.Frame(parent)
        editor.grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 6))
        editor.columnconfigure(1, weight=1)
        editor.columnconfigure(3, weight=1)
        ttk.Label(editor, text="检查").grid(
            row=0, column=0, sticky="w", padx=(0, 4), pady=3
        )
        self.observation_focus_combo = ttk.Combobox(
            editor,
            state="readonly",
            values=tuple(OBSERVATION_FOCUS_LABELS),
            textvariable=self.observation_focus_var,
            width=16,
        )
        self.observation_focus_combo.grid(
            row=0, column=1, sticky="ew", padx=(0, 6), pady=3
        )
        self.observation_focus_combo.bind(
            "<<ComboboxSelected>>",
            lambda event: self._update_observation_editor_controls(),
        )
        ttk.Label(editor, text="关系").grid(
            row=0, column=2, sticky="w", padx=(0, 4), pady=3
        )
        self.observation_relation_combo = ttk.Combobox(
            editor,
            state="readonly",
            values=tuple(OBSERVATION_RELATION_LABELS),
            textvariable=self.observation_relation_var,
            width=10,
        )
        self.observation_relation_combo.grid(
            row=0, column=3, sticky="ew", pady=3
        )
        ttk.Label(editor, text="期望依据").grid(
            row=1, column=0, sticky="w", padx=(0, 4), pady=3
        )
        self.observation_source_combo = ttk.Combobox(
            editor,
            state="readonly",
            values=tuple(OBSERVATION_SOURCE_LABELS),
            textvariable=self.observation_source_var,
            width=20,
        )
        self.observation_source_combo.grid(
            row=1, column=1, sticky="ew", padx=(0, 6), pady=3
        )
        self.observation_source_combo.bind(
            "<<ComboboxSelected>>",
            lambda event: self._update_observation_editor_controls(),
        )
        ttk.Label(editor, text="参考项").grid(
            row=1, column=2, sticky="w", padx=(0, 4), pady=3
        )
        self.observation_reference_combo = ttk.Combobox(
            editor,
            state="readonly",
            textvariable=self.observation_reference_var,
            width=14,
        )
        self.observation_reference_combo.grid(
            row=1, column=3, sticky="ew", pady=3
        )
        ttk.Label(editor, text="业务说明").grid(
            row=2, column=0, sticky="w", padx=(0, 4), pady=3
        )
        ttk.Entry(
            editor,
            textvariable=self.observation_meaning_var,
        ).grid(
            row=2,
            column=1,
            columnspan=3,
            sticky="ew",
            pady=3,
        )
        actions = ttk.Frame(editor)
        actions.grid(
            row=3,
            column=0,
            columnspan=4,
            sticky="ew",
            pady=(3, 0),
        )
        self.observation_save_button = ttk.Button(
            actions,
            text="保存检查修改",
            command=self.save_selected_observation,
        )
        self.observation_save_button.pack(side="left")
        self.mutation_controls.append(self.observation_save_button)
        ttk.Button(
            actions,
            text="定位到对应动作",
            command=self.ignore_selected_observation,
        ).pack(side="left", padx=6)

    def _toggle_keyboard_event(
            self,
            keyboard_event,
            *,
            action_id=None,
            event_row_id=None,
        ):
        action_id = action_id or (self.current_action or {}).get("id")
        event_id = str(keyboard_event.get("event_id") or "")
        if not action_id or not event_id:
            self.status_var.set("当前键盘操作无可校正的原始事件。")
            return
        include = not bool(keyboard_event.get("included"))
        self._apply(
            lambda store, _ids: store.set_keyboard_event_included(
                action_id,
                event_id,
                include,
            ),
            [],
            select_ids=[event_row_id or action_id],
            completion_message=(
                "已恢复键盘事件。"
                if include
                else "已忽略键盘事件；原始录制仍然保留。"
            ),
        )

    def _refresh_observations(self, select_event_id=None):
        if self.observation_tree is None:
            return
        self.observations = []
        if self.recorder_query is not None:
            try:
                workspace = self.recorder_query.get_step_workspace(
                    self.step_id
                )
                self.observations = [
                    item
                    for item in workspace.observations
                    if item.owner_take_id == self.owner_take_id
                ]
            except Exception as error:
                self.status_var.set(
                    "F9 检查记录暂时无法加载: "
                    f"{type(error).__name__}: {error}"
                )
        self.observation_tree.delete(
            *self.observation_tree.get_children()
        )
        selected_item = None
        for index, observation in enumerate(self.observations):
            item_id = f"observation-{index}"
            self.observation_tree.insert(
                "",
                "end",
                iid=item_id,
                values=(
                    (
                        f"操作 {observation.action_ordinal}"
                        if observation.action_ordinal is not None
                        else "操作未知"
                    ),
                    observation.target_name,
                    observation.summary,
                    (
                        "需要确认"
                        if observation.needs_business_confirmation
                        else "已记录"
                    ),
                ),
            )
            if observation.event_id == select_event_id:
                selected_item = item_id
        self._set_observation_tab_visible(bool(self.observations))
        if not self.observations:
            return
        selected_item = selected_item or self.observation_tree.get_children()[0]
        self.observation_tree.selection_set(selected_item)
        self._load_selected_observation()

    def _set_observation_tab_visible(self, visible):
        if self.observations_frame is None:
            return
        frame_id = str(self.observations_frame)
        tabs = set(self.detail_notebook.tabs())
        if visible:
            if frame_id not in tabs:
                self.detail_notebook.add(
                    self.observations_frame,
                    text="F9 检查",
                )
            else:
                self.detail_notebook.add(self.observations_frame)
                self.detail_notebook.tab(
                    self.observations_frame,
                    text="F9 检查",
                )
        elif frame_id in tabs:
            if self.detail_notebook.select() == frame_id:
                self.detail_notebook.select(0)
            self.detail_notebook.hide(self.observations_frame)

    def selected_observation(self):
        selected = (
            self.observation_tree.selection()
            if self.observation_tree is not None
            else ()
        )
        if not selected:
            return None
        try:
            return self.observations[
                int(selected[0].rsplit("-", 1)[-1])
            ]
        except (IndexError, ValueError):
            return None

    def _load_selected_observation(self):
        observation = self.selected_observation()
        if observation is None:
            return
        self.observation_focus_var.set(
            _observation_focus_label(observation.focus)
        )
        self.observation_relation_var.set(
            _observation_relation_label(observation.relation)
        )
        self.observation_source_var.set(
            _observation_source_label(
                observation.expected_source_kind
            )
        )
        self.observation_reference_combo.configure(
            values=observation.reference_options,
        )
        self.observation_reference_var.set(
            observation.expected_source_reference or ""
        )
        self.observation_meaning_var.set(observation.business_meaning)
        self._update_observation_editor_controls()

    def _update_observation_editor_controls(self):
        focus_label = self.observation_focus_var.get()
        focus = OBSERVATION_FOCUS_LABELS.get(
            focus_label,
            "auto",
        )
        observation = self.selected_observation()
        if (
            observation is not None
            and focus_label == _observation_focus_label(observation.focus)
        ):
            focus = observation.focus
        if focus == "auto":
            relation_values = ("自动",)
            source_values = ("自动核对 Feature",)
        elif focus in {"visible", "enabled", "property", "collection"}:
            relation_values = ("自动",)
            source_values = ("当前看到的结果就是期望",)
        else:
            relation_values = tuple(OBSERVATION_RELATION_LABELS)
            source_values = (
                "自动核对 Feature",
                "Feature 文案",
                "Examples 列",
                "Data Table 列",
            )
        self.observation_relation_combo.configure(values=relation_values)
        if self.observation_relation_var.get() not in relation_values:
            self.observation_relation_var.set(relation_values[0])
        self.observation_source_combo.configure(values=source_values)
        if self.observation_source_var.get() not in source_values:
            self.observation_source_var.set(source_values[0])
        source_kind = OBSERVATION_SOURCE_LABELS.get(
            self.observation_source_var.get(),
            "auto",
        )
        values = tuple(self.observation_reference_combo.cget("values"))
        enabled = source_kind in {"examples", "data_table"} and values
        self.observation_reference_combo.configure(
            state="readonly" if enabled else "disabled",
        )
        if not enabled:
            self.observation_reference_var.set("")
        elif self.observation_reference_var.get() not in values:
            self.observation_reference_var.set(values[0])

    def save_selected_observation(self):
        observation = self.selected_observation()
        if observation is None:
            self.status_var.set("请选择一条 F9 检查记录。")
            return
        if self.session is None or self.step_id is None:
            self.status_var.set(
                "当前录制内容暂时无法修改 F9 检查，请返回审阅页后重试。"
            )
            return
        if self.busy:
            self.status_var.set("录制修改正在保存，请等待当前操作完成。")
            return
        focus_label = self.observation_focus_var.get()
        focus = OBSERVATION_FOCUS_LABELS.get(focus_label, "auto")
        if focus_label == _observation_focus_label(observation.focus):
            focus = observation.focus
        relation = OBSERVATION_RELATION_LABELS.get(
            self.observation_relation_var.get(),
            "auto",
        )
        source_kind = OBSERVATION_SOURCE_LABELS.get(
            self.observation_source_var.get(),
            "auto",
        )
        if focus == "auto" and (
            relation != "auto" or source_kind != "auto"
        ):
            self.status_var.set("自动核对时不需要选择关系或期望来源。")
            return
        if focus in {"visible", "enabled", "property", "collection"} and (
            relation != "auto" or source_kind != "observed_state"
        ):
            self.status_var.set("该检查只需确认当前状态是否就是业务期望。")
            return
        reference = self.observation_reference_var.get().strip()
        if source_kind in {"examples", "data_table"} and not reference:
            self.status_var.set("请选择期望值所在的列。")
            return
        if source_kind == "observed_state" and focus not in {
            "visible",
            "enabled",
            "property",
            "collection",
        }:
            self.status_var.set(
                "文字和值不能把当前看到的内容直接当作业务期望；"
                "请选择 Feature、Examples 或 Data Table。"
            )
            return
        arguments = (
            self.step_id,
            observation.owner_take_id,
            observation.take_id,
            observation.event_id,
        )
        options = {
            "focus": focus,
            "relation": relation,
            "expected_source": {
                "kind": source_kind,
                "reference": (
                    reference
                    if source_kind in {"examples", "data_table"}
                    else None
                ),
            },
            "property_name": (
                observation.property_name
                if focus == "property"
                else None
            ),
            "business_meaning": self.observation_meaning_var.get().strip(),
            "expected_revision": observation.revision,
        }
        self.status_var.set("正在保存 F9 检查修改...")
        if self.operations is None:
            try:
                result = self.session.revise_observation_intent(
                    *arguments,
                    **options,
                )
                self._complete_observation_revision(
                    result,
                    observation.event_id,
                )
            except Exception as error:
                self.status_var.set(
                    "保存 F9 检查修改失败: "
                    f"{type(error).__name__}: {error}"
                )
            return
        self._set_busy(True)
        self.operations.submit(
            f"{self.observation_operation_prefix}{observation.event_id}",
            self.session.revise_observation_intent,
            *arguments,
            **options,
            context={"event_id": observation.event_id},
        )
        self._schedule_observation_result_poll()

    def _schedule_observation_result_poll(self):
        if self.closed or self.observation_poll_after_id is not None:
            return
        self.observation_poll_after_id = self.window.after(
            40,
            self._poll_observation_result,
        )

    def _poll_observation_result(self):
        self.observation_poll_after_id = None
        if self.closed:
            return
        results = self.operations.drain(
            key_prefix=self.observation_operation_prefix,
        )
        if not results:
            if self.operations.list_active(
                key_prefix=self.observation_operation_prefix,
            ):
                self._schedule_observation_result_poll()
            return
        result = results[-1]
        self._set_busy(False)
        if result.status != "completed" or result.error is not None:
            error = result.error or RuntimeError(
                f"任务状态异常: {result.status}"
            )
            self.status_var.set(
                "保存 F9 检查修改失败: "
                f"{type(error).__name__}: {error}"
            )
            return
        self._complete_observation_revision(
            result.value,
            (result.context or {}).get("event_id"),
        )

    def _complete_observation_revision(self, result, event_id):
        self._refresh_observations(select_event_id=event_id)
        if self.on_change is not None:
            self.on_change(
                self.take_dir,
                self.store.materialize(),
                {
                    "readiness": (result or {}).get("readiness"),
                    "refresh_error": None,
                },
            )
        self.status_var.set(
            "F9 检查修改已保存；正在更新 Copilot 任务。"
        )

    def ignore_selected_observation(self):
        observation = self.selected_observation()
        if observation is None:
            self.status_var.set("请选择一条 F9 检查记录。")
            return
        if observation.action_id and self.tree.exists(
                observation.action_id
        ):
            self.tree.selection_set(observation.action_id)
            self.tree.focus(observation.action_id)
            self.tree.see(observation.action_id)
            self._on_selection()
        else:
            self._focus_events({observation.event_id})
        self.detail_notebook.select(0)
        self.status_var.set(
            "已定位到这次检查；确认不需要时点击“忽略错误动作”。"
        )

    def refresh(
            self,
            select_ids=None,
            notify=True,
            *,
            state=None,
            media_bundle=None,
        ):
        tree_view = self._capture_tree_view_state()
        state = state or self.store.materialize()
        self.timeline_revision = state.get("timeline_revision")
        self.review_action_map = {
            action.get("id"): action
            for action in state.get("actions", [])
            if action.get("id")
        }
        self.keyboard_event_rows = {}
        current_selection = set(select_ids or tree_view["selection"])
        self.tree.delete(*self.tree.get_children())
        for action in state.get("actions", []):
            action_id = action["id"]
            tags = (
                ("supplement_action",)
                if (action.get("source") or {}).get("kind") == "supplement"
                else ()
            )
            self.tree.insert(
                "",
                "end",
                iid=action_id,
                tags=tags,
                open=action_id in tree_view["expanded"],
                values=(
                    "✓" if action.get("included", True) else "×",
                    action.get("ordinal"),
                    _action_summary_label(action),
                ),
            )
            if action.get("type") == "keyboard":
                for keyboard_event in self.store.keyboard_events(action_id):
                    event_row_id = (
                        f"{action_id}:event:{keyboard_event['event_id']}"
                    )
                    self.keyboard_event_rows[event_row_id] = {
                        "action_id": action_id,
                        "event": keyboard_event,
                    }
                    self.tree.insert(
                        action_id,
                        "end",
                        iid=event_row_id,
                        values=(
                            "✓" if keyboard_event.get("included") else "×",
                            "",
                            _keyboard_event_label(keyboard_event),
                        ),
                    )
        existing = [action_id for action_id in current_selection if self.tree.exists(action_id)]
        if existing:
            self.tree.selection_set(existing)
        elif self.tree.get_children():
            self.tree.selection_set(self.tree.get_children()[0])
        self._restore_tree_view_state(tree_view, existing)
        self.undo_button.configure(state="normal" if state.get("can_undo") else "disabled")
        self.redo_button.configure(state="normal" if state.get("can_redo") else "disabled")
        if media_bundle is not None:
            self._set_media_bundle(*media_bundle)
        elif notify:
            self._notify_change(state)
        else:
            self._reload_media()
        self._on_selection()
        self._refresh_observations()

    def _capture_tree_view_state(self):
        expanded = {
            item
            for item in self.tree.get_children()
            if bool(self.tree.item(item, "open"))
        }
        yview = self.tree.yview()
        return {
            "expanded": expanded,
            "selection": tuple(self.tree.selection()),
            "focus": self.tree.focus(),
            "yview": yview[0] if yview else 0.0,
        }

    def _restore_tree_view_state(self, tree_view, selected):
        focus = tree_view.get("focus")
        if focus and self.tree.exists(focus):
            self.tree.focus(focus)
        elif selected:
            self.tree.focus(selected[0])
        try:
            self.tree.yview_moveto(float(tree_view.get("yview") or 0.0))
        except tk.TclError:
            pass

    def _selected_ids(self):
        action_ids = list(self.tree.selection())
        if not action_ids:
            raise ValueError("请先选择动作")
        return action_ids

    def _simple_edit(self, operation):
        self._apply(
            lambda store, ids: store.apply_edit(operation, ids),
            self._selected_ids(),
        )

    def _toggle_simple_ignored(self):
        try:
            action_ids = self._selected_ids()
        except ValueError as error:
            self.status_var.set(str(error))
            return
        if len(action_ids) != 1:
            self.status_var.set("忽略或恢复时请只选择一个动作。")
            return
        keyboard_event = self.keyboard_event_rows.get(action_ids[0])
        if keyboard_event is not None:
            self._toggle_keyboard_event(
                keyboard_event["event"],
                action_id=keyboard_event["action_id"],
                event_row_id=action_ids[0],
            )
            return
        action = self.review_action_map.get(action_ids[0]) or {}
        ignored = not action.get("included", True)
        if not ignored:
            if action.get("type") == "keyboard":
                confirmed = messagebox.askyesno(
                    "忽略整段输入",
                    "这会移除整段键盘输入的有效证据。"
                    "原始录制仍会保留，是否继续？",
                    parent=self.window.winfo_toplevel(),
                )
                if not confirmed:
                    self.status_var.set("已保留整段键盘输入。")
                    return
            if action.get("type") == "keyboard":
                operation = lambda store, ids: store.apply_edit(
                    "exclude",
                    ids,
                    {"confirmed_keyboard_exclusion": True},
                )
            else:
                operation = lambda store, ids: store.apply_edit(
                    "exclude",
                    ids,
                )
            self._apply(
                operation,
                action_ids,
                completion_message="已忽略该动作；原始证据仍然保留。",
            )
            return
        operation = lambda store, ids: store.apply_edit("include", ids)
        self._apply(
            operation,
            action_ids,
            completion_message="已恢复该动作。",
        )

    def _on_tree_click(self, event):
        if self.tree.identify_column(event.x) != "#1":
            return None
        action_id = self.tree.identify_row(event.y)
        if not action_id:
            return "break"
        if self.busy:
            self.status_var.set("录制修改正在保存，请等待当前操作完成。")
            return "break"
        keyboard_event = self.keyboard_event_rows.get(action_id)
        action = self.review_action_map.get(
            keyboard_event["action_id"]
            if keyboard_event is not None else action_id
        )
        if action is None:
            return "break"
        selected_ids = list(self.tree.selection())
        if action_id not in selected_ids:
            self.tree.selection_set(action_id)
        self.tree.focus(action_id)
        self._toggle_simple_ignored()
        return "break"

    def _undo(self):
        self._apply(lambda store, ids: store.undo(), [])

    def _redo(self):
        self._apply(lambda store, ids: store.redo(), [])

    def _apply(
            self,
            operation,
            selected_ids,
            *,
            select_ids=None,
            completion_message=None,
            stale_message=None,
        ):
        if self.busy:
            self.status_var.set("录制修改正在保存，请等待当前操作完成。")
            return
        context = {
            "selected_ids": list(selected_ids),
            "select_ids": select_ids,
            "completion_message": completion_message,
            "stale_message": stale_message,
        }
        try:
            expected_revision = self.timeline_revision
            if self.operations is None or self.mutation_handler is None:
                result = self._execute_mutation(
                    operation,
                    context["selected_ids"],
                    expected_revision,
                )
                self._complete_mutation(result, context)
                return
            self._set_busy(True)
            self.operations.submit(
                self.mutation_operation_key,
                self._execute_mutation,
                operation,
                context["selected_ids"],
                expected_revision,
                context=context,
            )
            self._schedule_mutation_poll()
        except Exception as error:
            self._set_busy(False)
            self._reload_materialized_state_after_failure()
            if stale_message and hasattr(error, "current_revision"):
                self.status_var.set(stale_message)
            else:
                self.status_var.set(
                    f"修改失败，已重新载入当前录制内容: "
                    f"{type(error).__name__}: {error}"
                )

    def _execute_mutation(self, operation, selected_ids, expected_revision):
        if self.mutation_handler is not None:
            result = self.mutation_handler(
                self.take_dir,
                expected_revision,
                lambda store: operation(store, selected_ids),
            )
        else:
            self.store.require_revision(expected_revision)
            state = operation(self.store, selected_ids)
            readiness = (
                self.on_change(self.take_dir, state)
                if self.on_change is not None
                else None
            )
            result = {
                "state": state,
                "readiness": readiness,
                "refresh_error": None,
            }
        result = dict(result)
        result["media_bundle"] = self.query.media_bundle()
        return result

    def _schedule_mutation_poll(self):
        if self.closed or self.mutation_poll_after_id is not None:
            return
        self.mutation_poll_after_id = self.window.after(
            40,
            self._poll_mutation_result,
        )

    def _poll_mutation_result(self):
        self.mutation_poll_after_id = None
        if self.closed:
            return
        results = self.operations.drain(key=self.mutation_operation_key)
        if not results:
            if self.busy:
                self._schedule_mutation_poll()
            return
        result = results[-1]
        if result.status != "completed" or result.error is not None:
            self._set_busy(False)
            error = result.error or RuntimeError(f"任务状态异常: {result.status}")
            reloaded = self._reload_materialized_state_after_failure()
            if hasattr(error, "current_revision") and reloaded:
                self.status_var.set(
                    (result.context or {}).get("stale_message")
                    or "录制内容已由其他窗口更新，已重新载入，请重试。"
                )
            else:
                self.status_var.set(
                    f"修改失败，已重新载入当前录制内容: "
                    f"{type(error).__name__}: {error}"
                )
            return
        try:
            self._complete_mutation(result.value, result.context or {})
        finally:
            self._set_busy(False)

    def _complete_mutation(self, result, context):
        state = result["state"]
        select_ids = context.get("select_ids")
        if callable(select_ids):
            select_ids = select_ids(state)
        if select_ids is None:
            select_ids = context.get("selected_ids")
        self.refresh(
            select_ids=select_ids,
            notify=False,
            state=state,
            media_bundle=result.get("media_bundle"),
        )
        if self.mutation_handler is not None:
            self._notify_change(state, mutation_result=result)
        else:
            self._set_completion_status(result.get("readiness"), result)
        message = context.get("completion_message")
        if message and result.get("refresh_error") is None:
            self.status_var.set(message)

    def _notify_change(self, state, mutation_result=None):
        if self.on_change is None:
            if mutation_result is not None:
                self._set_completion_status(
                    mutation_result.get("readiness"),
                    mutation_result,
                )
            return
        try:
            if mutation_result is None:
                readiness = self.on_change(self.take_dir, state)
                mutation_result = {"refresh_error": None}
            else:
                readiness = self.on_change(
                    self.take_dir,
                    state,
                    mutation_result,
                )
            self._set_completion_status(readiness, mutation_result)
        except Exception as error:
            self.status_var.set(
                f"录制修改已保存，但刷新审阅界面失败: {type(error).__name__}: {error}"
            )

    def _set_completion_status(self, readiness, result):
        refresh_error = result.get("refresh_error")
        if refresh_error is not None:
            self.status_var.set(
                "录制修改已保存，但更新 AI 结果失败: "
                f"{type(refresh_error).__name__}: {refresh_error}"
            )
            return
        review_count = len((readiness or {}).get("review_required") or [])
        if review_count:
            self.status_var.set(
                f"录制修改已保存；当前仍有 {review_count} 项需要检查。"
            )
        else:
            self.status_var.set("录制修改已保存；没有需要人工检查的问题。")

    def _reload_materialized_state_after_failure(self):
        try:
            selected_ids = list(self.tree.selection())
            self.refresh(
                select_ids=selected_ids,
                notify=False,
                state=self.store.materialize(),
            )
            return True
        except Exception:
            self._on_selection()
            return False

    def _set_busy(self, busy):
        self.busy = bool(busy)
        if self.busy:
            self.mutation_control_states = {}
            for control in self.mutation_controls:
                try:
                    self.mutation_control_states[control] = control.cget("state")
                    control.configure(state="disabled")
                except tk.TclError:
                    continue
            self.busy_after_id = self.window.after(200, self._show_busy)
            return
        for control, state in self.mutation_control_states.items():
            try:
                control.configure(state=state)
            except tk.TclError:
                continue
        self.mutation_control_states = {}
        if self.busy_after_id is not None:
            try:
                self.window.after_cancel(self.busy_after_id)
            except tk.TclError:
                pass
            self.busy_after_id = None
        if self.busy_progress is not None:
            self.busy_progress.stop()
        if self.busy_frame is not None:
            self.busy_frame.pack_forget()

    def _show_busy(self):
        self.busy_after_id = None
        if not self.busy or self.closed:
            return
        self.busy_frame.pack(
            fill="x",
            padx=12,
            pady=(0, 5),
            before=self.status_label,
        )
        self.busy_progress.start(12)
        self.status_var.set("正在保存录制修改并更新 AI 结果...")

    def _reload_media(self):
        self._set_media_bundle(*self.query.media_bundle())

    def _set_media_bundle(self, media_index, action_media):
        self.media_index = media_index
        self.action_media = action_media
        self.action_media_map = {
            item.get("action_id"): item
            for item in self.action_media.get("actions", [])
            if item.get("action_id")
        }
        self.event_media = {
            item.get("event_id"): item
            for item in self.media_index.get("events", [])
            if item.get("event_id")
        }

    def open_supplement_window(self):
        try:
            if (
                    self.supplement_window is not None
                    and not self.supplement_window.closed
            ):
                self.supplement_window.show()
                return
            self.pending_supplement_before_action_id = None
            selected = list(self.tree.selection())
            if selected:
                before_selected = messagebox.askyesnocancel(
                    "选择补录位置",
                    "选择“是”：把本次补录插到当前所选操作之前。\n"
                    "选择“否”：把本次补录追加到本次录制末尾。\n"
                    "选择“取消”：暂不补录。",
                    parent=self.window,
                )
                if before_selected is None:
                    return
                if before_selected:
                    self.pending_supplement_before_action_id = selected[0]
            backend = self.query.capture_backend()
            self.supplement_window = SupplementRecordingWindow(
                self.window,
                self.take_dir,
                backend=backend,
                on_completed=self._on_supplement_completed,
                operation_coordinator=self.operations,
                window_controller=self.capture_window_controller,
            ).show()
        except Exception as error:
            self.supplement_window = None
            self.pending_supplement_before_action_id = None
            detail = f"{type(error).__name__}: {error}"
            self.status_var.set(f"打开补录失败: {detail}")
            messagebox.showerror(
                "无法打开补录",
                detail,
                parent=self.window,
            )

    def _on_supplement_completed(self, artifact):
        self.supplement_window = None
        before_action_id = self.pending_supplement_before_action_id
        self.pending_supplement_before_action_id = None
        supplement_id = artifact.get("supplement_id")
        action_ids = [
            action.get("id")
            for action in artifact.get("actions") or []
            if action.get("id")
        ]
        if action_ids:
            location = (
                f"操作 {self.review_action_map.get(before_action_id, {}).get('ordinal', '?')} 前"
                if before_action_id
                else "本次录制末尾"
            )
            if messagebox.askyesno(
                    "确认插入本次补录",
                    f"本次补录记录了 {len(action_ids)} 个操作。\n"
                    f"它们将按录制顺序插入到{location}。\n"
                    "确认后才会更新本次录制和 Copilot 任务。",
                    parent=self.window,
            ):
                self._insert_supplement_actions(
                    supplement_id,
                    action_ids,
                    before_action_id,
                )
                return
            self.status_var.set(
                "本次补录已保存但尚未插入，不会改变当前录制内容。"
            )
            return
        self.status_var.set(
            f"本次补录已保存，记录了 {artifact.get('action_count', 0)} 个操作。"
        )

    def _insert_supplement_actions(
            self,
            supplement_id,
            action_ids,
            before_action_id,
            *,
            reason="",
        ):
        action_ids = list(action_ids)
        existing_ids = set(self.tree.get_children())
        self._apply(
            lambda store, ids: store.insert_supplement(
                supplement_id,
                action_ids,
                before_action_id=before_action_id,
                reason=reason,
            ),
            [],
            select_ids=lambda state: [
                action["id"]
                for action in state.get("actions") or []
                if action.get("id") not in existing_ids
            ][:1],
            completion_message=(
                f"已按录制顺序插入 {len(action_ids)} 个补录操作。"
                if before_action_id
                else f"已按录制顺序追加 {len(action_ids)} 个补录操作。"
            ),
            stale_message=(
                "录制内容已由其他窗口更新；本次补录内容已保留但没有插入。"
                "请重新选择插入位置。"
            ),
        )

    def close(self):
        if self.closed:
            return True
        if self.busy:
            self.status_var.set("录制修改正在保存，请等待当前操作完成后再关闭。")
            return False
        supplement = self.supplement_window
        if supplement is not None and not supplement.closed:
            if supplement.busy:
                self.status_var.set("补录正在启动或保存，请完成后再关闭录制内容。")
                return False
            if supplement.recording:
                supplement.close()
                if not supplement.closed:
                    self.status_var.set("请先完成或丢弃正在进行的补录。")
                    return False
            else:
                supplement.close()
        self.closed = True
        self._cancel_pending_callbacks()
        self.window.destroy()
        if self.on_close is not None:
            self.on_close()
        return True

    def force_close(self):
        if self.closed:
            return True
        self.closed = True
        self._cancel_pending_callbacks()
        if self.operations is not None:
            self.operations.abandon_prefix(self.mutation_operation_key)
            self.operations.abandon_prefix(self.observation_operation_prefix)
            self.operations.abandon_prefix(self.preview_operation_key)
        supplement = self.supplement_window
        if supplement is not None and not supplement.closed:
            supplement.force_close()
        self.window.destroy()
        return True

    def _cancel_pending_callbacks(self):
        for after_id in (
            self.mutation_poll_after_id,
            self.observation_poll_after_id,
            self.preview_poll_after_id,
            self.busy_after_id,
        ):
            if after_id is None:
                continue
            try:
                self.window.after_cancel(after_id)
            except tk.TclError:
                pass
        self.mutation_poll_after_id = None
        self.observation_poll_after_id = None
        self.preview_poll_after_id = None
        self.busy_after_id = None

    def _on_selection(self, event=None):
        selected = list(self.tree.selection())
        if not selected:
            return
        keyboard_event = self.keyboard_event_rows.get(selected[0])
        action = self.review_action_map.get(
            keyboard_event["action_id"]
            if keyboard_event is not None else selected[0]
        )
        if action is None:
            return
        if keyboard_event is not None:
            self.simple_ignore_button.configure(
                text=(
                    "恢复键盘事件"
                    if not keyboard_event["event"].get("included")
                    else "忽略键盘事件"
                ),
                state="normal",
            )
            self._show_action(action)
            return
        role = action.get("role") or "business"
        ignored = (
            not action.get("included", True)
            or role == "noise"
        )
        self.simple_ignore_button.configure(
            text="恢复动作" if ignored else "忽略错误动作",
            state="normal",
        )
        self._show_action(action)

    def _show_action(self, action):
        media = _first_media(
            self.event_media,
            action.get("media_event_ids") or action.get("event_ids") or [],
        )
        self.current_action = action
        self.current_action_media = self.action_media_map.get(action.get("id"))
        self._render_action_view()

    def _show_image(self, path):
        with Image.open(path) as source:
            self.source_image = source.convert("RGB").copy()
        self.current_image_path = Path(path)
        if self.zoom_factor is None:
            self.zoom_fit()
        else:
            self._render_preview()

    def _show_image_object(self, image, source_label):
        self.source_image = image.convert("RGB").copy()
        self.current_image_path = source_label
        if self.zoom_factor is None:
            self.zoom_fit()
        else:
            self._render_preview()

    def _render_action_view(self):
        view = VIEW_VALUES.get(self.view_var.get(), "after")
        media = self.current_action_media or {}
        if view == "before":
            self._show_media_frame(media.get("before"), "该动作没有操作前截图。")
        elif view == "after":
            self._show_media_frame(
                media.get("after"),
                "该动作没有操作后截图；可切换到视频帧。",
            )
        elif view == "diff":
            self._show_action_diff(media)
        else:
            self._show_action_video_frame(media)

    def _show_media_frame(self, frame, missing_text):
        path = self.take_dir / frame["path"] if frame and frame.get("path") else None
        if path is not None and path.exists():
            self._show_image(path)
            return
        fallback = _first_media(
            self.event_media,
            (self.current_action or {}).get("media_event_ids")
            or (self.current_action or {}).get("event_ids")
            or [],
        )
        fallback_path = (
            self.take_dir / fallback["screenshot"]
            if fallback and fallback.get("screenshot")
            else None
        )
        if fallback_path is not None and fallback_path.exists():
            self._show_image(fallback_path)
            self.status_var.set("当前为旧录制中的备用画面，可能不是操作后的最终状态。")
            return
        self._clear_preview(missing_text)

    def _show_action_diff(self, media):
        before = media.get("before") or {}
        after = media.get("after") or {}
        before_path = self.take_dir / before.get("path", "")
        after_path = self.take_dir / after.get("path", "")
        if not before_path.exists() or not after_path.exists():
            self._clear_preview("需要同时存在操作前和操作后截图才能显示差异。")
            return
        try:
            with Image.open(before_path) as first, Image.open(after_path) as second:
                first_image = first.convert("RGB")
                second_image = second.convert("RGB")
            width = min(first_image.width, second_image.width)
            height = min(first_image.height, second_image.height)
            diff = ImageChops.difference(
                first_image.crop((0, 0, width, height)),
                second_image.crop((0, 0, width, height)),
            )
            diff = ImageEnhance.Contrast(diff).enhance(2.5)
            self._show_image_object(diff, "memory://action-diff")
        except Exception as error:
            self._clear_preview(f"生成差异图失败: {type(error).__name__}: {error}")

    def _show_action_video_frame(self, media):
        after = media.get("after") or {}
        commit = media.get("commit") or {}
        video_ms = after.get("video_ms")
        if video_ms is None:
            video_ms = commit.get("video_ms")
        if video_ms is None:
            self._clear_preview("该动作没有可用视频时间。")
            return
        action_id = (self.current_action or {}).get("id") or "action"
        output = self.take_dir / "extracted_frames" / f"{action_id}-view.png"
        video_path = (media.get("video") or {}).get("path")
        if self.operations is not None:
            self.preview_sequence += 1
            sequence = self.preview_sequence
            self.status_var.set("正在提取动作视频帧...")
            self.operations.submit(
                self.preview_operation_key,
                extract_video_frame,
                self.take_dir,
                video_ms=video_ms,
                video_path=video_path,
                output_path=output,
                context={"sequence": sequence, "action_id": action_id},
            )
            self._schedule_preview_poll()
            return
        try:
            frame = extract_video_frame(
                self.take_dir,
                video_ms=video_ms,
                video_path=video_path,
                output_path=output,
            )
            self._show_image(frame)
            self.status_var.set(f"已显示动作视频帧: {video_ms} ms")
        except Exception as error:
            self._clear_preview(f"抽取视频帧失败: {type(error).__name__}: {error}")

    def _schedule_preview_poll(self):
        if self.closed or self.preview_poll_after_id is not None:
            return
        self.preview_poll_after_id = self.window.after(
            40,
            self._poll_preview_result,
        )

    def _poll_preview_result(self):
        self.preview_poll_after_id = None
        if self.closed:
            return
        results = self.operations.drain(key=self.preview_operation_key)
        if not results:
            if self.operations.list_active(key=self.preview_operation_key):
                self._schedule_preview_poll()
            return
        current_action_id = (self.current_action or {}).get("id") or ""
        current_result = next((
            result
            for result in reversed(results)
            if (
                (result.context or {}).get("sequence")
                == self.preview_sequence
                and (result.context or {}).get("action_id")
                == current_action_id
            )
        ), None)
        if current_result is None:
            if self.operations.list_active(key=self.preview_operation_key):
                self._schedule_preview_poll()
            return
        if current_result.status != "completed" or current_result.error is not None:
            error = current_result.error
            self._clear_preview(
                f"抽取视频帧失败: "
                f"{type(error).__name__}: {error}"
                if error is not None
                else "任务未完成。"
            )
            return
        self._show_image(current_result.value)
        self.status_var.set("已显示动作视频帧。")

    def _clear_preview(self, text):
        self.photo = None
        self.source_image = None
        self.current_image_path = None
        self._show_preview_message(text)

    def _on_view_changed(self, event=None):
        if self.current_action is not None:
            self._render_action_view()

    def zoom_in(self):
        self._set_zoom((self.zoom_factor or self._fit_factor()) * 1.25)

    def zoom_out(self):
        self._set_zoom((self.zoom_factor or self._fit_factor()) / 1.25)

    def zoom_fit(self):
        self.zoom_factor = None
        self._render_preview()

    def zoom_actual(self):
        self._set_zoom(1.0)

    def _set_zoom(self, factor):
        self.zoom_factor = min(4.0, max(0.25, float(factor)))
        self._render_preview()

    def _fit_factor(self):
        if self.source_image is None:
            return 1.0
        self.preview_canvas.update_idletasks()
        width = max(1, self.preview_canvas.winfo_width() - 12)
        height = max(1, self.preview_canvas.winfo_height() - 12)
        return min(
            1.0,
            width / self.source_image.width,
            height / self.source_image.height,
        )

    def _render_preview(self):
        if self.source_image is None:
            return
        factor = self.zoom_factor if self.zoom_factor is not None else self._fit_factor()
        width = max(1, int(self.source_image.width * factor))
        height = max(1, int(self.source_image.height * factor))
        image = self.source_image.resize((width, height), Image.Resampling.LANCZOS)
        self.photo = ImageTk.PhotoImage(image)
        self.preview_canvas.delete("all")
        self.preview_image_id = self.preview_canvas.create_image(
            0,
            0,
            image=self.photo,
            anchor="nw",
        )
        self.preview_canvas.configure(scrollregion=(0, 0, width, height))
        self.zoom_var.set(
            "适应" if self.zoom_factor is None else f"{int(factor * 100)}%"
        )

    def _show_preview_message(self, text):
        self.preview_canvas.delete("all")
        self.preview_canvas.create_text(
            12,
            12,
            text=text,
            fill="#f0f0f0",
            anchor="nw",
            width=420,
        )
        self.preview_canvas.configure(scrollregion=(0, 0, 440, 80))

    def _on_preview_resize(self, event=None):
        if self.source_image is not None and self.zoom_factor is None:
            self._render_preview()

    def _on_zoom_wheel(self, event):
        if event.delta > 0:
            self.zoom_in()
        else:
            self.zoom_out()
        return "break"

    def _focus_events(self, event_ids):
        event_ids = set(event_ids or ())
        if not event_ids:
            return
        actions = self.review_action_map.values()
        action_ids = [
            action["id"]
            for action in actions
            if event_ids & set(action.get("event_ids") or ())
        ]
        if action_ids:
            self.tree.selection_set(action_ids)
            self.tree.focus(action_ids[0])
            self.tree.see(action_ids[0])
            self._on_selection()


def _observation_focus_label(focus):
    return {
        "auto": "自动核对业务描述",
        "text": "显示文字",
        "value": "当前值",
        "visible": "是否可见",
        "enabled": "是否可用",
        "window_title": "窗口标题",
        "region_text": "显示文字",
        "collection": "自动核对业务描述",
        "property": "自动核对业务描述",
    }.get(str(focus or "auto"), "自动核对业务描述")


def _observation_relation_label(relation):
    values = {
        value: label
        for label, value in OBSERVATION_RELATION_LABELS.items()
    }
    return values.get(str(relation or "auto"), "自动")


def _observation_source_label(source_kind):
    values = {
        value: label
        for label, value in OBSERVATION_SOURCE_LABELS.items()
    }
    return values.get(str(source_kind or "auto"), "自动核对 Feature")


def _target_label(action):
    target = action.get("target") or {}
    element = target.get("element") or {}
    return (
        element.get("auto_id")
        or element.get("name")
        or element.get("class_name")
        or target.get("root_name")
        or ""
    )


def _action_summary_label(action):
    target = _target_label(action) or "当前目标"
    action_type = str(action.get("type") or "")
    if action_type == "observe" or action.get("role") == "assertion":
        text = f"检查「{target}」的结果"
    elif action_type in {"keyboard", "input_text"}:
        text = f"在「{target}」中输入内容"
    elif action_type == "double_click":
        text = f"双击「{target}」"
    elif action_type == "right_click":
        text = f"右键点击「{target}」"
    elif action_type == "scroll":
        direction = (action.get("parameters") or {}).get("direction")
        text = f"在「{target}」中向{'上' if direction == 'up' else '下'}滚动"
    elif action_type == "drag":
        text = f"拖动「{target}」"
    elif action_type == "focus":
        text = f"将焦点移到「{target}」"
    else:
        text = f"点击「{target}」"
    if not action.get("included", True) or action.get("role") == "noise":
        return "已忽略 · " + text
    return text


def _keyboard_event_label(keyboard_event):
    key = (keyboard_event or {}).get("key") or {}
    name = str(key.get("name") or "")
    event_type = str(keyboard_event.get("event_type") or "keyboard")
    return f"{event_type} · {_keyboard_key_label(name)}"


def _keyboard_key_label(name):
    return {
        "Left": "左方向键",
        "Right": "右方向键",
        "Up": "上方向键",
        "Down": "下方向键",
        "Back": "退格键",
        "Delete": "删除键",
        "Enter": "回车键",
        "Tab": "Tab 键",
        "Escape": "Esc 键",
    }.get(name, f"按键 {name}")


def _first_media(event_media, event_ids):
    for event_id in event_ids:
        media = event_media.get(event_id)
        if media is not None:
            return media
    return None