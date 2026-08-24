from __future__ import annotations

import os
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from autowork_core.utils.debug_tools.recorder.library_query_service import (
    RecorderLibraryQueryService,
)
from config.paths import Paths


class RecordingLibraryWindow:
    def __init__(
            self,
            parent,
            output_root=None,
            on_rerecord=None,
            *,
            on_open_session=None,
            on_retire_session=None,
            on_close=None,
            close_destroys=True,
        ):
        self.parent = parent
        self.output_root = Path(
            output_root or (Paths.ARTIFACTS_DIR / "recording_sessions")
        ).resolve()
        self.entries = {}
        self.capabilities = {}
        self.retirement_inspections = {}
        self.query_service = RecorderLibraryQueryService(self.output_root)
        self.on_rerecord = on_rerecord
        self.on_open_session = on_open_session
        self.on_retire_session = on_retire_session
        self.on_close = on_close
        self.close_destroys = bool(close_destroys)

        self.window = ttk.Frame(parent)
        self.window.grid(row=0, column=0, sticky="nsew")

        self.search_var = tk.StringVar(value="")
        self.root_var = tk.StringVar(value=str(self.output_root))
        self.status_var = tk.StringVar(value="")
        self.retirement_var = tk.StringVar(value="")
        self.tree = None
        self.capability_tree = None
        self.notebook = None
        self.runs_tab = None
        self.capabilities_tab = None
        self.open_button = None
        self.open_directory_button = None
        self.open_capability_button = None
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
            text="历史与能力",
            font=("Microsoft YaHei UI", 14, "bold"),
        ).pack(side="left", padx=(0, 14))
        ttk.Label(header, text="录制根目录").pack(side="left")
        ttk.Entry(header, textvariable=self.root_var).pack(
            side="left", fill="x", expand=True, padx=6
        )
        ttk.Button(header, text="选择", command=self.choose_root).pack(side="left")
        ttk.Button(header, text="刷新", command=self.refresh).pack(side="left", padx=6)

        search = ttk.Frame(self.window)
        search.pack(fill="x", padx=12, pady=(0, 8))
        ttk.Label(search, text="搜索").pack(side="left")
        entry = ttk.Entry(search, textvariable=self.search_var)
        entry.pack(side="left", fill="x", expand=True, padx=6)
        entry.bind("<KeyRelease>", lambda event: self._render())
        ttk.Label(
            search,
            text="可搜索 Feature、Scenario、Step、路径或 Session ID",
        ).pack(side="left")

        ttk.Label(
            self.window,
            textvariable=self.retirement_var,
            anchor="w",
            wraplength=1100,
        ).pack(fill="x", padx=12, pady=(0, 6))

        notebook = ttk.Notebook(self.window)
        self.notebook = notebook
        notebook.pack(fill="both", expand=True, padx=12)
        runs_tab = ttk.Frame(notebook)
        capabilities_tab = ttk.Frame(notebook)
        self.runs_tab = runs_tab
        self.capabilities_tab = capabilities_tab
        notebook.add(runs_tab, text="历史 Run")
        notebook.add(capabilities_tab, text="业务能力")
        notebook.bind(
            "<<NotebookTabChanged>>",
            lambda event: self._update_controls(),
        )

        frame = ttk.Frame(runs_tab)
        frame.pack(fill="both", expand=True, padx=12)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        columns = (
            "feature",
            "scenario",
            "progress",
            "readiness",
            "updated",
            "path",
        )
        self.tree = ttk.Treeview(
            frame,
            columns=columns,
            show="headings",
            selectmode="browse",
        )
        headings = (
            ("feature", "Feature", 280),
            ("scenario", "Scenario / Examples", 220),
            ("progress", "Step", 80),
            ("readiness", "下一步", 130),
            ("updated", "更新时间", 145),
            ("path", "Run", 230),
        )
        for column, label, width in headings:
            self.tree.heading(column, text=label)
            self.tree.column(
                column,
                width=width,
                minwidth=55,
                stretch=column in ("feature", "scenario", "path"),
            )
        y_scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        x_scroll = ttk.Scrollbar(frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(
            yscrollcommand=y_scroll.set,
            xscrollcommand=x_scroll.set,
        )
        self.tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        self.tree.bind("<Double-1>", lambda event: self.open_selected())
        self.tree.bind("<<TreeviewSelect>>", lambda event: self._update_controls())

        capabilities_tab.rowconfigure(0, weight=1)
        capabilities_tab.columnconfigure(0, weight=1)
        capability_columns = (
            "status",
            "feature",
            "scenario",
            "step",
            "published",
            "path",
        )
        self.capability_tree = ttk.Treeview(
            capabilities_tab,
            columns=capability_columns,
            show="headings",
            selectmode="browse",
        )
        for column, label, width in (
            ("status", "状态", 80),
            ("feature", "Feature", 260),
            ("scenario", "Scenario", 180),
            ("step", "已确认业务能力", 340),
            ("published", "确认时间", 145),
            ("path", "能力文件", 240),
        ):
            self.capability_tree.heading(column, text=label)
            self.capability_tree.column(
                column,
                width=width,
                minwidth=60,
                stretch=column in ("feature", "step", "path"),
            )
        capability_scroll = ttk.Scrollbar(
            capabilities_tab,
            orient="vertical",
            command=self.capability_tree.yview,
        )
        self.capability_tree.configure(yscrollcommand=capability_scroll.set)
        self.capability_tree.grid(row=0, column=0, sticky="nsew")
        capability_scroll.grid(row=0, column=1, sticky="ns")
        self.capability_tree.bind(
            "<Double-1>",
            lambda event: self.open_capability(),
        )
        self.capability_tree.bind(
            "<<TreeviewSelect>>",
            lambda event: self._update_controls(),
        )

        actions = ttk.Frame(self.window)
        actions.pack(fill="x", padx=12, pady=8)
        self.open_button = ttk.Button(
            actions,
            text="打开审阅",
            command=self.open_selected,
        )
        self.open_button.pack(side="left")
        self.open_directory_button = ttk.Button(
            actions,
            text="打开目录",
            command=self.open_directory,
        )
        self.open_directory_button.pack(side="left", padx=6)
        self.open_capability_button = ttk.Button(
            actions,
            text="打开能力文件",
            command=self.open_capability,
        )
        self.open_capability_button.pack(side="left", padx=6)
        self.retire_button = ttk.Button(
            actions,
            text="退役 Run",
            command=self.retire_selected,
        )
        self.retire_button.pack(side="left", padx=6)
        ttk.Button(
            actions,
            text="关闭" if self.close_destroys else "返回录制",
            command=self.close,
        ).pack(side="right")

        ttk.Label(
            self.window,
            textvariable=self.status_var,
            anchor="w",
            wraplength=1100,
        ).pack(fill="x", padx=12, pady=(0, 12))

    def choose_root(self):
        path = filedialog.askdirectory(
            parent=self.window,
            title="选择 recording_sessions 目录",
            initialdir=str(self.output_root),
        )
        if path:
            self.root_var.set(path)
            self.refresh()

    def refresh(self):
        try:
            self.output_root = self._entered_root()
        except Exception as error:
            self.status_var.set(
                f"录制根目录无效: {type(error).__name__}: {error}"
            )
            return
        self.query_service = RecorderLibraryQueryService(self.output_root)
        model = self.query_service.get_library()
        self.capabilities = {
            entry.capability_id: entry
            for entry in model.capabilities
        }
        self.entries = {
            entry.session_id: entry
            for entry in model.runs
        }
        self.retirement_inspections = {}
        self._render()
        self.status_var.set(
            f"已加载 {len(self.entries)} 个历史 Run、"
            f"{len(model.capabilities)} 个能力记录。"
            "双击 Run 可直接审阅，不会启动录制。"
        )

    def _render(self):
        selected = self.selected_session_id()
        self.tree.delete(*self.tree.get_children())
        query = self.search_var.get().strip().casefold()
        for session_id, entry in self.entries.items():
            if query and query not in entry.search_text:
                continue
            self.tree.insert(
                "",
                "end",
                iid=session_id,
                values=(
                    entry.feature_name,
                    entry.scenario_name,
                    entry.progress,
                    entry.next_action,
                    entry.updated_at,
                    entry.path,
                ),
            )
        if selected and self.tree.exists(selected):
            self.tree.selection_set(selected)
        elif self.tree.get_children():
            self.tree.selection_set(self.tree.get_children()[0])
        self._update_controls()
        self._render_capabilities(query)

    def _render_capabilities(self, query):
        self.capability_tree.delete(*self.capability_tree.get_children())
        for capability_id, entry in self.capabilities.items():
            if query and query not in entry.search_text:
                continue
            self.capability_tree.insert(
                "",
                "end",
                iid=capability_id,
                values=(
                    entry.status_label,
                    entry.feature_name,
                    entry.scenario_name,
                    entry.step_text,
                    entry.published_at,
                    entry.path,
                ),
            )

    def selected_session_id(self):
        if self._active_tab() != "runs":
            return None
        selected = self.tree.selection() if self.tree is not None else ()
        return selected[0] if selected else None

    def selected_capability(self):
        if self._active_tab() != "capabilities":
            return None
        selected = (
            self.capability_tree.selection()
            if self.capability_tree is not None
            else ()
        )
        return self.capabilities.get(selected[0]) if selected else None

    def selected_entry(self):
        session_id = self.selected_session_id()
        return self.entries.get(session_id)

    def selected_session_dir(self):
        entry = self.selected_entry()
        return (
            Path(entry.directory_path)
            if entry is not None and entry.directory_path is not None
            else None
        )

    def open_selected(self):
        session_dir = self.selected_session_dir()
        if session_dir is None:
            self.status_var.set("请选择一个历史 Run。")
            return
        try:
            if self.on_open_session is None:
                raise RuntimeError("录制库未连接 Recorder 工作台")
            if self.on_open_session(session_dir) is False:
                return
        except Exception as error:
            self.status_var.set(
                f"打开历史 Run 失败: {type(error).__name__}: {error}"
            )
            return
        self.status_var.set(
            f"已打开：{self.selected_entry().feature_name} / "
            f"{self.selected_entry().scenario_name}"
        )

    def open_directory(self):
        session_dir = self.selected_session_dir()
        if session_dir is None:
            self.status_var.set("请选择一个历史 Run。")
            return
        try:
            os.startfile(session_dir)
        except Exception as error:
            self.status_var.set(
                f"打开目录失败: {type(error).__name__}: {error}"
            )

    def open_capability(self):
        entry = self.selected_capability()
        if entry is None:
            self.status_var.set("请在“已确认能力”中选择一项。")
            return
        path = Path(entry.detail_path) if entry.detail_path else None
        if path is None:
            self.status_var.set("能力文件无效或已不存在。")
            return
        try:
            os.startfile(path)
        except Exception as error:
            self.status_var.set(
                f"打开能力文件失败: {type(error).__name__}: {error}"
            )

    def retire_selected(self):
        session_dir = self.selected_session_dir()
        if session_dir is None:
            self.status_var.set("请选择一个历史 Run。")
            return
        if self.on_retire_session is None:
            self.status_var.set("录制库未连接 Run 退役服务。")
            return
        if not messagebox.askyesno(
            "退役 Run",
            "保留已提炼的 AI 经验，并永久删除此 Run 的录屏、图片、UI tree 和事务工件？",
            parent=self.window,
        ):
            return
        try:
            result = self.on_retire_session(session_dir, True)
            if result is None:
                return
        except Exception as error:
            from autowork_core.utils.debug_tools.recorder.run_retirement import (
                RunKnowledgeRequiredError,
            )

            if not isinstance(error, RunKnowledgeRequiredError):
                self.status_var.set(
                    f"退役 Run 失败: {type(error).__name__}: {error}"
                )
                return
            if not messagebox.askyesno(
                "没有可提炼经验",
                "此 Run 没有确认经验或成功生成结果。是否不保留经验并直接丢弃？此操作不可恢复。",
                parent=self.window,
            ):
                self.status_var.set("已取消；Run 保持不变。")
                return
            try:
                result = self.on_retire_session(session_dir, False)
                if result is None:
                    return
            except Exception as discard_error:
                self.status_var.set(
                    "丢弃 Run 失败: "
                    f"{type(discard_error).__name__}: {discard_error}"
                )
                return
        self.refresh()
        self.status_var.set(
            "Run 已退役，AI 经验保存在 Bdd/ai/knowledge。"
            if result.get("mode") == "distilled"
            else "Run 已丢弃，未保留 AI 经验。"
        )

    def _entered_root(self):
        value = self.root_var.get().strip()
        return Path(value or self.output_root).resolve()

    def _update_controls(self):
        active_tab = self._active_tab()
        session_id = self.selected_session_id()
        entry = self.selected_entry()
        inspection = self.retirement_inspections.get(session_id)
        if entry is not None and inspection is None:
            inspection = self.query_service.retirement_status(entry)
            self.retirement_inspections[session_id] = inspection
        state = (
            "normal"
            if active_tab == "runs" and entry is not None
            else "disabled"
        )
        self.open_button.configure(state=state)
        self.open_directory_button.configure(state=state)
        capability = self.selected_capability()
        self.open_capability_button.configure(
            state=(
                "normal"
                if capability is not None
                and capability.detail_path is not None
                else "disabled"
            )
        )
        retirement_state = (
            "normal"
            if entry is not None
            and inspection is not None
            and inspection.eligible
            else "disabled"
        )
        self.retire_button.configure(state=retirement_state)
        self.retirement_var.set(
            (
                "退役：仅适用于历史 Run"
                if active_tab != "runs"
                else inspection.detail
                if inspection
                else "退役：请选择历史 Run"
            )
        )

    def _active_tab(self):
        if self.notebook is None:
            return "runs"
        selected = self.notebook.select()
        if selected == str(self.capabilities_tab):
            return "capabilities"
        return "runs"

    def close(self):
        if self.close_destroys:
            self.window.destroy()
        if self.on_close is not None:
            self.on_close()

    def dispose(self):
        try:
            self.window.destroy()
        except tk.TclError:
            pass


__all__ = ["RecordingLibraryWindow"]
