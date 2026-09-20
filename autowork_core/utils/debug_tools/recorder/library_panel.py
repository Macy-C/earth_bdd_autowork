from __future__ import annotations

import os
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from autowork_core.utils.debug_tools.recorder.identity import stable_digest
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
            on_import_feature=None,
            on_export_feature=None,
            on_export_scenario=None,
            on_open_session=None,
            on_retire_session=None,
            on_cleanup_legacy_generation=None,
            on_close=None,
            close_destroys=True,
        ):
        self.parent = parent
        self.output_root = Path(
            output_root or (Paths.ARTIFACTS_DIR / "recording_sessions")
        ).resolve()
        self.entries = {}
        self.retirement_inspections = {}
        self.query_service = RecorderLibraryQueryService(self.output_root)
        self.on_rerecord = on_rerecord
        self.on_import_feature = on_import_feature
        self.on_export_feature = on_export_feature
        self.on_export_scenario = on_export_scenario
        self.on_open_session = on_open_session
        self.on_retire_session = on_retire_session
        self.on_cleanup_legacy_generation = on_cleanup_legacy_generation
        self.on_close = on_close
        self.close_destroys = bool(close_destroys)

        self.window = ttk.Frame(parent)
        self.window.grid(row=0, column=0, sticky="nsew")

        self.search_var = tk.StringVar(value="")
        self.root_var = tk.StringVar(value=str(self.output_root))
        self.status_var = tk.StringVar(value="")
        self.retirement_var = tk.StringVar(value="")
        self.tree = None
        self.runs_tab = None
        self.open_button = None
        self.open_directory_button = None
        self.import_button = None
        self.export_button = None
        self.export_menu = None
        self.retire_button = None
        self.cleanup_button = None
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
            text="录制任务",
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

        runs_tab = ttk.Frame(self.window)
        self.runs_tab = runs_tab
        runs_tab.pack(fill="both", expand=True, padx=12)

        frame = ttk.Frame(runs_tab)
        frame.pack(fill="both", expand=True)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        columns = ("progress", "readiness", "updated", "path")
        self.tree = ttk.Treeview(
            frame,
            columns=columns,
            show="tree headings",
            selectmode="browse",
        )
        self.tree.heading("#0", text="Feature / Scenario / Run")
        self.tree.column("#0", width=420, minwidth=220, stretch=True)
        headings = (
            ("progress", "Step", 80),
            ("readiness", "下一步", 130),
            ("updated", "更新时间", 145),
            ("path", "Run", 280),
        )
        for column, label, width in headings:
            self.tree.heading(column, text=label)
            self.tree.column(
                column,
                width=width,
                minwidth=55,
                stretch=column == "path",
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
        self.tree.bind("<Double-1>", self._on_tree_double_click)
        self.tree.bind("<<TreeviewSelect>>", lambda event: self._update_controls())

        ttk.Label(
            self.window,
            textvariable=self.retirement_var,
            anchor="w",
            wraplength=1100,
        ).pack(fill="x", padx=12, pady=(6, 0))

        actions = ttk.Frame(self.window)
        actions.pack(fill="x", padx=12, pady=8)
        self.import_button = ttk.Button(
            actions,
            text="导入录制资料",
            command=self.import_recording_material,
        )
        self.import_button.pack(side="left")
        self.export_button = ttk.Menubutton(actions, text="导出录制资料")
        self.export_button.pack(side="left", padx=(6, 0))
        self.export_menu = tk.Menu(self.export_button, tearoff=False)
        self.export_menu.add_command(
            label="导出当前场景",
            command=self.export_selected_scenario,
        )
        self.export_menu.add_command(
            label="导出当前 Feature",
            command=self.export_selected_feature,
        )
        self.export_button.configure(menu=self.export_menu)
        self.open_button = ttk.Button(
            actions,
            text="打开处理",
            command=self.run_primary_action,
        )
        self.open_button.pack(side="left", padx=(6, 0))
        self.retire_button = ttk.Button(
            actions,
            text="删除录制资料",
            command=self.retire_selected,
        )
        self.retire_button.pack(side="left", padx=6)
        self.cleanup_button = ttk.Menubutton(
            actions,
            text="诊断",
        )
        self.cleanup_menu = tk.Menu(self.cleanup_button, tearoff=False)
        self.cleanup_menu.add_command(
            label="清理旧生成状态",
            command=self.cleanup_selected_legacy_generation,
        )
        self.cleanup_button.configure(menu=self.cleanup_menu)
        self.cleanup_button.pack(side="left")
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

    def import_recording_material(self):
        if self.on_import_feature is None:
            self.status_var.set("当前资料库未连接导入服务。")
            return False
        try:
            self.output_root = self._entered_root()
        except Exception as error:
            self.status_var.set(
                f"录制任务目录无效: {type(error).__name__}: {error}"
            )
            return False
        try:
            started = self.on_import_feature(
                output_root=self.output_root,
                parent=self.window,
            )
        except Exception as error:
            self.status_var.set(
                f"启动录制资料导入失败: {type(error).__name__}: {error}"
            )
            return False
        if started:
            self.status_var.set("正在校验并导入录制资料...")
        return bool(started)

    def export_selected_feature(self):
        return self._export_selected("feature")

    def export_selected_scenario(self):
        return self._export_selected("scenario")

    def _export_selected(self, scope):
        entry = self.selected_entry()
        if entry is None:
            self.status_var.set("请选择一条录制任务。")
            return False
        callback = (
            self.on_export_feature
            if scope == "feature"
            else self.on_export_scenario
        )
        if callback is None:
            self.status_var.set("当前工作台未连接导出服务。")
            return False
        output = filedialog.asksaveasfilename(
            parent=self.window,
            title=(
                "导出 Feature 录制资料"
                if scope == "feature"
                else "导出当前场景录制资料"
            ),
            defaultextension=".zip",
            filetypes=(("Feature 录制资料", "*.zip"),),
            initialfile=(
                f"{stable_digest(entry.feature_name, entry.scenario_name, scope, length=8)}.delivery.zip"
            ),
        )
        if not output:
            return False
        try:
            self.output_root = self._entered_root()
            callback(entry, Path(output), self.output_root)
        except Exception as error:
            self.status_var.set(
                f"启动录制资料导出失败: {type(error).__name__}: {error}"
            )
            return False
        self.status_var.set("正在导出录制资料...")
        return True

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
            f"已加载 {len(self.entries)} 条录制任务。"
            "按 Feature / Scenario 管理；双击 Run 打开审阅。"
        )

    def _on_tree_double_click(self, event=None):
        if self.selected_entry() is None:
            selected = self.tree.selection() if self.tree is not None else ()
            if selected:
                row = selected[0]
                self.tree.item(row, open=not bool(self.tree.item(row, "open")))
            return
        self.open_selected()

    def _render(self):
        selected = self.selected_session_id()
        self.tree.delete(*self.tree.get_children())
        query = self.search_var.get().strip().casefold()
        feature_rows = {}
        scenario_rows = {}
        first_run_id = None
        for session_id, entry in self.entries.items():
            if query and query not in entry.search_text:
                continue
            feature_key = "feature-" + stable_digest(
                entry.feature_name,
                getattr(entry, "feature_source_relpath", ""),
                length=16,
            )
            if feature_key not in feature_rows:
                self.tree.insert(
                    "",
                    "end",
                    iid=feature_key,
                    text=entry.feature_name or "未命名 Feature",
                    open=True,
                    values=("", "", "", ""),
                )
                feature_rows[feature_key] = entry.feature_name
            scenario_key = feature_key + "-scenario-" + stable_digest(
                entry.scenario_name,
                getattr(entry, "scenario_id", ""),
                length=16,
            )
            if scenario_key not in scenario_rows:
                self.tree.insert(
                    feature_key,
                    "end",
                    iid=scenario_key,
                    text=entry.scenario_name or "未命名场景",
                    open=True,
                    values=("", "", "", ""),
                )
                scenario_rows[scenario_key] = entry.scenario_name
            self.tree.insert(
                scenario_key,
                "end",
                iid=session_id,
                text=_run_label(entry),
                values=(
                    entry.progress,
                    entry.next_action,
                    entry.updated_at,
                    entry.path,
                ),
            )
            if first_run_id is None:
                first_run_id = session_id
        if selected and self.tree.exists(selected):
            self.tree.selection_set(selected)
            self.tree.focus(selected)
        elif first_run_id and self.tree.exists(first_run_id):
            self.tree.selection_set(first_run_id)
            self.tree.focus(first_run_id)
        self._update_controls()

    def selected_session_id(self):
        selected = self.tree.selection() if self.tree is not None else ()
        session_id = selected[0] if selected else None
        return session_id if session_id in self.entries else None

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

    def run_primary_action(self):
        entry = self.selected_entry()
        if entry is None:
            self.status_var.set("请选择一条录制任务。")
            return
        if (
            entry.next_action in {"可继续录制", "继续录制"}
            and self.on_rerecord is not None
        ):
            self.resume_selected()
            return
        self.open_selected()

    def resume_selected(self):
        session_dir = self.selected_session_dir()
        if session_dir is None:
            self.status_var.set("请选择一条录制任务。")
            return
        if self.on_rerecord is None:
            self.status_var.set("当前工作台未连接继续录制服务。")
            return
        try:
            from autowork_core.utils.debug_tools.recorder.session import (
                FeatureRecordingSession,
            )

            session = FeatureRecordingSession.open_existing(session_dir)
            step = session.next_recordable_step()
            if step is None:
                self.status_var.set("当前录制任务没有待录制的 Step。")
                session.close()
                return
            if self.on_rerecord(session, step.id) is False:
                return
        except Exception as error:
            self.status_var.set(
                f"继续录制失败: {type(error).__name__}: {error}"
            )
            return
        self.status_var.set("已切换到录制页，请继续当前 Step。")

    def open_selected(self):
        session_dir = self.selected_session_dir()
        if session_dir is None:
            self.status_var.set("请选择一条录制资料。")
            return
        try:
            if self.on_open_session is None:
                raise RuntimeError("录制库未连接 Recorder 工作台")
            if self.on_open_session(session_dir) is False:
                return
        except Exception as error:
            self.status_var.set(
                f"打开录制资料失败: {type(error).__name__}: {error}"
            )
            return
        self.status_var.set(
            f"已打开：{self.selected_entry().feature_name} / "
            f"{self.selected_entry().scenario_name}"
        )

    def open_directory(self):
        session_dir = self.selected_session_dir()
        if session_dir is None:
            self.status_var.set("请选择一条录制资料。")
            return
        try:
            os.startfile(session_dir)
        except Exception as error:
            self.status_var.set(
                f"打开目录失败: {type(error).__name__}: {error}"
            )

    def retire_selected(self):
        session_dir = self.selected_session_dir()
        if session_dir is None:
            self.status_var.set("请选择一条录制任务。")
            return
        if self.on_retire_session is None:
            self.status_var.set("当前工作台未连接删除服务。")
            return
        if not messagebox.askyesno(
            "删除录制资料",
            "将删除这条录制任务的录屏、截图和生成中间文件。"
            "已生成到项目中的脚本不会删除。是否继续？",
            parent=self.window,
        ):
            return
        try:
            result = self.on_retire_session(session_dir, False)
            if result is None:
                return
        except Exception as error:
            self.status_var.set(
                f"删除录制资料失败: {type(error).__name__}: {error}"
            )
            return
        self.refresh()
        self.status_var.set(
            "已删除录制资料。生成到项目中的文件不受影响。"
        )

    def cleanup_selected_legacy_generation(self):
        session_dir = self.selected_session_dir()
        if session_dir is None:
            self.status_var.set("请选择一条录制任务。")
            return False
        if self.on_cleanup_legacy_generation is None:
            self.status_var.set("当前工作台未连接遗留状态清理服务。")
            return False
        if not messagebox.askyesno(
            "清理遗留生成状态",
            "仅清理已卡住的历史生成状态，不删除录制资料，"
            "也不会删除已生成到项目中的脚本。是否继续？",
            parent=self.window,
        ):
            return False
        try:
            result = self.on_cleanup_legacy_generation(session_dir)
        except Exception as error:
            self.status_var.set(
                f"清理遗留生成状态失败: {type(error).__name__}: {error}"
            )
            return False
        self.refresh()
        cleaned = len((result or {}).get("cleaned_workflows") or ())
        self.status_var.set(
            f"已清理 {cleaned} 个遗留生成状态；请重新检查是否可删除。"
        )
        return True

    def _entered_root(self):
        value = self.root_var.get().strip()
        return Path(value or self.output_root).resolve()

    def _update_controls(self):
        session_id = self.selected_session_id()
        entry = self.selected_entry()
        inspection = self.retirement_inspections.get(session_id)
        if entry is not None and inspection is None:
            inspection = self.query_service.retirement_status(entry)
            self.retirement_inspections[session_id] = inspection
        state = "normal" if entry is not None else "disabled"
        self.import_button.configure(
            state="normal" if self.on_import_feature is not None else "disabled"
        )
        self.open_button.configure(text=_primary_action_label(entry))
        self.open_button.configure(state=state)
        export_state = (
            "normal"
            if entry is not None
            and (
                self.on_export_feature is not None
                or self.on_export_scenario is not None
            )
            else "disabled"
        )
        self.export_button.configure(state=export_state)
        self.export_menu.entryconfigure(
            "导出当前场景",
            state=(
                "normal"
                if entry is not None and self.on_export_scenario is not None
                else "disabled"
            ),
        )
        self.export_menu.entryconfigure(
            "导出当前 Feature",
            state=(
                "normal"
                if entry is not None and self.on_export_feature is not None
                else "disabled"
            ),
        )
        retirement_state = (
            "normal"
            if entry is not None
            and inspection is not None
            and inspection.eligible
            else "disabled"
        )
        self.retire_button.configure(state=retirement_state)
        cleanup_state = (
            "normal"
            if entry is not None
            and inspection is not None
            and _can_cleanup_legacy_generation(inspection)
            and self.on_cleanup_legacy_generation is not None
            else "disabled"
        )
        self.cleanup_button.configure(state=cleanup_state)
        self.retirement_var.set(
            _delete_status_text(inspection)
            if inspection
            else "请选择一条录制任务。"
        )

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


def _primary_action_label(entry):
    if entry is None:
        return "打开处理"
    if entry.next_action in {"可继续录制", "继续录制"}:
        return "继续录制"
    if entry.next_action in {"打开检查", "待 AI 理解", "交给 Copilot"}:
        return "打开生成"
    if entry.next_action in {"需要审阅", "证据损坏"}:
        return "打开处理"
    return "打开处理"


def _run_label(entry):
    return entry.updated_at.replace("T", " ")[:16] or entry.session_id


def _delete_status_text(inspection):
    if inspection is None:
        return "请选择一条录制任务。"
    if not inspection.eligible:
        detail = str(inspection.detail or "")
        if detail.startswith("退役："):
            detail = detail.removeprefix("退役：")
        if "已阻塞" in detail:
            detail = detail.split("已阻塞；", 1)[-1]
        return "暂时不能删除：" + detail
    return "可删除。已生成到项目中的脚本不会删除。"


def _can_cleanup_legacy_generation(inspection):
    detail = str(getattr(inspection, "detail", "") or "")
    return "正在生成或上次生成未正常结束" in detail


__all__ = ["RecordingLibraryWindow"]
