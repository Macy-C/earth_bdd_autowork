from __future__ import annotations

import tkinter as tk
from tkinter import ttk


WORKBENCH_VIEWS = ("capture", "library", "review", "timeline")
_BASE_LABELS = {
    "capture": "Feature与录制",
    "library": "历史",
    "review": "修复与生成",
    "timeline": "录制内容",
}


class RecorderWorkbench:
    """Single top-level host for Recorder task views."""

    def __init__(self, parent, *, on_close, on_view_selected=None):
        self.parent = parent
        self.on_close = on_close
        self.on_view_selected = on_view_selected
        self._selecting = False
        self.window = tk.Toplevel(parent)
        self.window.title("Recorder 工作台")
        self.window.geometry("1320x860+45+30")
        self.window.minsize(1040, 700)
        self.window.attributes("-topmost", True)
        self.window.protocol("WM_DELETE_WINDOW", self.on_close)

        self.context_var = tk.StringVar(value="尚未打开录制任务")
        self.scope_var = tk.StringVar(value="")
        self.operation_var = tk.StringVar(value="")
        self._tabs = {}
        self._build_ui()

    def _build_ui(self):
        header = ttk.Frame(self.window)
        header.pack(fill="x", padx=12, pady=(10, 6))
        ttk.Label(
            header,
            text="Recorder 工作台",
            font=("Microsoft YaHei UI", 14, "bold"),
        ).pack(side="left")
        ttk.Label(
            header,
            textvariable=self.context_var,
            anchor="w",
        ).pack(side="left", fill="x", expand=True, padx=18)
        ttk.Label(
            header,
            textvariable=self.scope_var,
            anchor="e",
        ).pack(side="right", padx=(8, 0))
        ttk.Label(
            header,
            textvariable=self.operation_var,
            anchor="e",
        ).pack(side="right", padx=(8, 0))

        self.notebook = ttk.Notebook(self.window)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        for key, label in (
            ("capture", "Feature与录制"),
            ("library", "历史"),
            ("review", "修复与生成"),
            ("timeline", "录制内容"),
        ):
            frame = ttk.Frame(self.notebook)
            frame.rowconfigure(0, weight=1)
            frame.columnconfigure(0, weight=1)
            self.notebook.add(frame, text=label)
            self._tabs[key] = frame
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

    def host(self, view):
        if view not in self._tabs:
            raise KeyError(f"未知工作台视图: {view}")
        return self._tabs[view]

    def clear(self, view):
        host = self.host(view)
        for child in host.winfo_children():
            child.destroy()

    def select(self, view):
        host = self.host(view)
        if str(self.notebook.tab(host, "state")) == "disabled":
            return False
        self._selecting = True
        try:
            self.notebook.select(host)
        finally:
            self._selecting = False
        self.show()
        return True

    def set_view_enabled(self, view, enabled):
        host = self.host(view)
        self.notebook.tab(host, state="normal" if enabled else "disabled")

    def view_enabled(self, view):
        return str(self.notebook.tab(self.host(view), "state")) != "disabled"

    def selected_view(self):
        selected = self.notebook.select()
        for key, host in self._tabs.items():
            if str(host) == selected:
                return key
        return None

    def _on_tab_changed(self, event=None):
        if self._selecting or self.on_view_selected is None:
            return
        view = self.selected_view()
        if view is not None:
            self.on_view_selected(view)

    def set_context(self, model=None, *, step_id=None, take_id=None):
        if model is None:
            self.context_var.set("尚未打开录制任务")
            self.scope_var.set("")
            self._set_tab_labels()
            return
        step = next(
            (item for item in model.steps if item.step_id == step_id),
            None,
        )
        if step is None and model.steps:
            step = model.steps[0]
        take_id = take_id or (step.selected_take_id if step else None)
        parts = [model.feature_name, model.scenario_name]
        if step is not None:
            parts.append(f"Step {step.ordinal}: {step.text}")
        if take_id:
            take = next(
                (
                    item
                    for item in (step.takes if step is not None else ())
                    if item.take_id == take_id
                ),
                None,
            )
            if take is not None:
                parts.append(f"第 {take.take_number} 次录制")
        self.context_var.set("  /  ".join(parts))
        self.scope_var.set(
            model.scope.label
            if getattr(model, "scope", None) is not None
            else ""
        )
        count = int(model.active_operation_count or 0)
        self.operation_var.set(f"后台任务 {count}" if count else "")
        self._set_tab_labels(
            step,
            generation=getattr(model, "generation", None),
        )

    def _set_tab_labels(self, step=None, *, generation=None):
        labels = dict(_BASE_LABELS)
        if step is not None:
            labels["capture"] = (
                f"Feature与录制 · {_capture_label(step.capture_status)}"
            )
            labels["timeline"] = (
                f"录制内容 · {_evidence_label(step.evidence_status)}"
            )
            labels["review"] = (
                f"修复与生成 · {step.next_action_label}"
            )
        if generation is not None:
            labels["review"] = (
                f"修复与生成 · "
                f"{_generation_label(generation.display_status)}"
            )
        for key, label in labels.items():
            self.notebook.tab(self._tabs[key], text=label)

    def show(self):
        self.window.deiconify()
        self.window.lift()
        self.window.focus_force()
        return self

    def minimize_for_capture(self):
        for window in (self.window, self.parent):
            try:
                window.iconify()
            except (AttributeError, tk.TclError):
                pass

    def restore_after_capture(self, view):
        try:
            self.parent.deiconify()
        except (AttributeError, tk.TclError):
            pass
        self.select(view)

    def destroy(self):
        try:
            self.window.destroy()
        except tk.TclError:
            pass


def _capture_label(status):
    return {
        "pending": "待录制",
        "recording": "录制中",
        "completed": "已完成",
        "skipped": "已跳过",
    }.get(str(status), str(status or "未知"))


def _evidence_label(status):
    return {
        "unavailable": "无证据",
        "clean": "已就绪",
        "needs_review": "待复核",
        "broken": "需修复",
        "updating": "更新中",
    }.get(str(status), str(status or "未知"))


def _generation_label(status):
    return {
        "scenario_incomplete": "待完成",
        "updating": "更新中",
        "ready": "可生成",
        "needs_input": "需决策",
        "blocked": "已阻塞",
        "running": "生成中",
        "completed": "已完成",
        "failed": "失败",
        "unavailable": "不可用",
    }.get(str(status), str(status or "未知"))
