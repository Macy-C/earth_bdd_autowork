from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from autowork_core.utils.debug_tools.action_runner_catalog import (
    ActionDescriptor,
    ActionParameterDescriptor,
    PageViewCatalog,
    PageViewDescriptor,
    action_descriptors,
    discover_page_view_catalog,
    render_call_source_snippet,
)
from autowork_core.utils.debug_tools.action_runner_runtime import (
    ActionRunRequest,
    ActionRunResult,
    run_action,
)


class ActionRunnerWindow:
    def __init__(
            self,
            parent,
            *,
            catalog: PageViewCatalog | None = None,
            actions: tuple[ActionDescriptor, ...] | None = None,
            on_close=None,
    ):
        self.parent = parent
        self.on_close = on_close
        self.catalog = catalog or discover_page_view_catalog()
        self.targets = tuple(self.catalog.targets)
        self.actions = tuple(actions or action_descriptors())
        self.target_by_label = {target.label: target for target in self.targets}
        self.action_by_name = {action.name: action for action in self.actions}
        self.call_block_expanded = True

        self.window = tk.Toplevel(parent)
        self.window.title("BDD Autowork 运行调试器")
        self.window.geometry("960x720+120+70")
        self.window.minsize(760, 560)
        self.window.attributes("-topmost", True)
        self.window.protocol("WM_DELETE_WINDOW", self.close)

        self.target_var = tk.StringVar(value="")
        self.action_var = tk.StringVar(value="")
        self.signature_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="请选择 Page/View 和 action。")
        self.catalog_status_var = tk.StringVar(value="")
        self.sequence_status_var = tk.StringVar(
            value="多 action 编排预留，当前仅运行一个 action。"
        )
        self.current_snippet = ""

        self._build_ui()
        self._select_defaults()
        self.refresh_snippet()

    def _build_ui(self):
        self.window.columnconfigure(0, weight=1)
        self.window.rowconfigure(2, weight=1)

        header = ttk.Frame(self.window)
        header.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 6))
        header.columnconfigure(0, weight=1)
        ttk.Label(
            header,
            text="运行调试器",
            font=("Microsoft YaHei UI", 13, "bold"),
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(
            header,
            text="关闭",
            command=self.close,
        ).grid(row=0, column=1, sticky="e")

        selectors = ttk.LabelFrame(self.window, text="目标和动作")
        selectors.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 8))
        selectors.columnconfigure(1, weight=1)
        ttk.Label(selectors, text="Page/View").grid(
            row=0,
            column=0,
            sticky="w",
            padx=(8, 4),
            pady=(8, 4),
        )
        self.target_combo = ttk.Combobox(
            selectors,
            state="readonly" if self.targets else "disabled",
            values=[target.label for target in self.targets],
            textvariable=self.target_var,
        )
        self.target_combo.grid(
            row=0,
            column=1,
            sticky="ew",
            padx=(4, 8),
            pady=(8, 4),
        )
        self.target_combo.bind("<<ComboboxSelected>>", self.on_selection_changed)

        ttk.Label(selectors, text="Action").grid(
            row=1,
            column=0,
            sticky="w",
            padx=(8, 4),
            pady=(4, 8),
        )
        self.action_combo = ttk.Combobox(
            selectors,
            state="readonly" if self.actions else "disabled",
            values=[action.name for action in self.actions],
            textvariable=self.action_var,
        )
        self.action_combo.grid(
            row=1,
            column=1,
            sticky="ew",
            padx=(4, 8),
            pady=(4, 8),
        )
        self.action_combo.bind("<<ComboboxSelected>>", self.on_action_selected)
        ttk.Label(
            selectors,
            textvariable=self.signature_var,
            font=("Consolas", 9),
            foreground="#334155",
        ).grid(row=2, column=1, sticky="w", padx=(4, 8), pady=(0, 8))
        ttk.Label(
            selectors,
            textvariable=self.catalog_status_var,
            foreground="#92400e",
            wraplength=720,
        ).grid(row=3, column=1, sticky="ew", padx=(4, 8), pady=(0, 8))

        body = ttk.Panedwindow(self.window, orient="vertical")
        self.body_paned = body
        body.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 8))

        self.steps_outer = ttk.LabelFrame(body, text="Action 调用")
        self.steps_outer.columnconfigure(0, weight=1)
        self.steps_outer.rowconfigure(0, weight=1)

        self.parameters_outer = ttk.LabelFrame(self.steps_outer, text="步骤 1")
        self.parameters_outer.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        self.parameters_outer.columnconfigure(0, weight=1)
        self.parameters_outer.rowconfigure(1, weight=1)

        parameters_header = ttk.Frame(self.parameters_outer)
        parameters_header.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 6))
        parameters_header.columnconfigure(2, weight=1)
        self.call_toggle_button = ttk.Button(
            parameters_header,
            text="收起",
            width=6,
            command=self.toggle_call_block,
        )
        self.call_toggle_button.grid(row=0, column=0, sticky="w")
        self.call_title_var = tk.StringVar(value="调用代码")
        ttk.Label(
            parameters_header,
            textvariable=self.call_title_var,
            font=("Microsoft YaHei UI", 10, "bold"),
        ).grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.call_summary_var = tk.StringVar(value="")
        ttk.Label(
            parameters_header,
            textvariable=self.call_summary_var,
            font=("Consolas", 10),
            foreground="#0f172a",
            anchor="e",
        ).grid(row=0, column=2, sticky="ew", padx=(12, 0))

        self.parameters_body = ttk.Frame(self.parameters_outer)
        self.parameters_body.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
        self.parameters_body.columnconfigure(0, weight=1)
        self.parameters_body.rowconfigure(1, weight=1)
        ttk.Label(
            self.parameters_body,
            text="调用代码",
        ).grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.call_text = tk.Text(
            self.parameters_body,
            height=12,
            font=("Consolas", 10),
            wrap="none",
        )
        self.call_text.grid(row=1, column=0, sticky="nsew", pady=(0, 8))
        self.call_text.bind("<KeyRelease>", self.on_call_text_changed)

        body.add(self.steps_outer, weight=3)

        controls = ttk.Frame(self.window)
        controls.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 12))
        controls.columnconfigure(0, weight=1)
        ttk.Label(
            controls,
            textvariable=self.status_var,
            anchor="w",
        ).grid(row=0, column=0, sticky="ew")
        ttk.Button(
            controls,
            text="运行",
            command=self.run_selected_action,
        ).grid(row=0, column=1, sticky="e", padx=(8, 0))
        self.add_next_button = ttk.Button(
            controls,
            text="添加下一步",
            state="disabled",
        )
        self.add_next_button.grid(row=0, column=2, sticky="e", padx=(6, 0))
        ttk.Label(
            controls,
            textvariable=self.sequence_status_var,
            foreground="#64748b",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))

        output = ttk.LabelFrame(body, text="运行结果")
        output.columnconfigure(0, weight=1)
        output.rowconfigure(0, weight=1)
        self.output_notebook = ttk.Notebook(output)
        self.output_notebook.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        result_tab = ttk.Frame(self.output_notebook)
        result_tab.columnconfigure(0, weight=1)
        result_tab.rowconfigure(0, weight=1)
        diagnostic_tab = ttk.Frame(self.output_notebook)
        diagnostic_tab.columnconfigure(0, weight=1)
        diagnostic_tab.rowconfigure(0, weight=1)
        snippet_tab = ttk.Frame(self.output_notebook)
        snippet_tab.columnconfigure(0, weight=1)
        snippet_tab.rowconfigure(0, weight=1)
        self.output_notebook.add(result_tab, text="摘要")
        self.output_notebook.add(diagnostic_tab, text="诊断")
        self.output_notebook.add(snippet_tab, text="Step 代码片段")
        self.result_text = tk.Text(
            result_tab,
            height=8,
            font=("Consolas", 10),
            wrap="word",
        )
        self.result_text.grid(row=0, column=0, sticky="nsew")
        self.diagnostic_text = tk.Text(
            diagnostic_tab,
            height=8,
            font=("Consolas", 10),
            wrap="word",
        )
        self.diagnostic_text.grid(row=0, column=0, sticky="nsew")
        self.snippet_text = tk.Text(
            snippet_tab,
            height=8,
            font=("Consolas", 10),
            wrap="none",
        )
        self.snippet_text.grid(row=0, column=0, sticky="nsew")
        body.add(output, weight=2)

    def _select_defaults(self):
        if self.targets:
            self.target_combo.current(0)
        if self.actions:
            self.action_combo.current(0)
            self.rebuild_action_call()
        if self.catalog.import_errors:
            self.status_var.set(
                "部分 Page/View 导入失败；已显示可加载目标。"
            )
            self.catalog_status_var.set(_catalog_issue_summary(self.catalog))
        elif self.catalog.warnings:
            self.catalog_status_var.set(_catalog_issue_summary(self.catalog))
        elif not self.targets:
            self.status_var.set("未发现可运行的 WindowPage/WindowView。")

    def on_selection_changed(self, _event=None):
        self.refresh_snippet()

    def on_action_selected(self, _event=None):
        self.rebuild_action_call()
        self.refresh_snippet()

    def on_call_text_changed(self, _event=None):
        self._refresh_call_summary()
        self.refresh_snippet()

    def rebuild_action_call(self):
        action = self.selected_action()
        if action is None:
            self.signature_var.set("")
            self.call_title_var.set("调用代码")
            self._set_call("")
            return
        self.signature_var.set(f"{action.name}{action.signature}")
        self.call_title_var.set(f"{action.name} 调用代码")
        target = self.selected_target()
        receiver_name = "view" if target is not None and target.kind == "view" else "page"
        self._set_call(_default_call_source(receiver_name, action))

    def selected_target(self) -> PageViewDescriptor | None:
        return self.target_by_label.get(self.target_var.get())

    def selected_action(self) -> ActionDescriptor | None:
        return self.action_by_name.get(self.action_var.get())

    def toggle_call_block(self):
        if self.call_block_expanded:
            self.parameters_body.grid_remove()
            self.call_toggle_button.configure(text="展开")
            self.call_block_expanded = False
        else:
            self.parameters_body.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
            self.call_toggle_button.configure(text="收起")
            self.call_block_expanded = True
        self._refresh_call_summary()

    def refresh_snippet(self):
        target = self.selected_target()
        action = self.selected_action()
        if target is None or action is None:
            self.current_snippet = ""
            self._set_snippet("")
            return
        call_source = self._current_call_source()
        snippet = render_call_source_snippet(target, call_source) if call_source else ""
        self.current_snippet = snippet
        self._set_snippet(snippet)
        self._refresh_call_summary()
        if not call_source:
            self.status_var.set("请填写调用代码。")
        else:
            self.status_var.set("准备就绪。请确认应用界面已由用户准备好后运行。")

    def run_selected_action(self):
        target = self.selected_target()
        action = self.selected_action()
        if target is None or action is None:
            self.status_var.set("请先选择 Page/View 和 action。")
            return
        call_source = self._current_call_source()
        if not call_source:
            self.status_var.set("请填写调用代码。")
            return
        self.status_var.set("正在运行 action...")
        self.window.update_idletasks()
        result = run_action(ActionRunRequest(
            target=target,
            action=action,
            values={},
            call_source=call_source,
        ))
        self.show_result(result)

    def show_result(self, result: ActionRunResult):
        summary_lines = []
        if result.success:
            self.status_var.set("运行成功。")
            summary_lines.append("状态: 成功")
            if result.return_value is not None:
                summary_lines.append(f"返回值: {result.return_value!r}")
        else:
            self.status_var.set("运行失败。")
            summary_lines.append("状态: 失败")
            summary_lines.append(f"错误: {result.error_type}: {result.error_message}")
            if result.diagnostic_summary:
                summary_lines.append(f"诊断: {result.diagnostic_summary}")
        if result.stage:
            summary_lines.append(f"阶段: {result.stage}")
        if result.target_label:
            summary_lines.append(f"目标: {result.target_label}")
        if result.action_name:
            summary_lines.append(f"Action: {result.action_name}")
        if result.call_source:
            summary_lines.append("调用:")
            summary_lines.append(result.call_source)
        if result.cleanup_error:
            summary_lines.append(f"清理: {result.cleanup_error}")
        self.result_text.delete("1.0", "end")
        self.result_text.insert("1.0", "\n".join(summary_lines))
        self.diagnostic_text.delete("1.0", "end")
        self.diagnostic_text.insert("1.0", _diagnostic_text(result))
        if result.snippet:
            self.current_snippet = result.snippet
            self._set_snippet(result.snippet)

    def _set_call(self, value):
        self.call_text.delete("1.0", "end")
        if value:
            self.call_text.insert("1.0", value)
        self._refresh_call_summary()

    def _refresh_call_summary(self):
        call_source = self._current_call_source()
        summary = _compact_call_source(call_source) if not self.call_block_expanded else ""
        self.call_summary_var.set(summary)

    def _current_call_source(self):
        return self.call_text.get("1.0", "end").strip()

    def _set_snippet(self, value):
        self.snippet_text.delete("1.0", "end")
        if value:
            self.snippet_text.insert("1.0", value)

    def close(self):
        try:
            self.window.destroy()
        finally:
            if self.on_close is not None:
                self.on_close()


def _default_parameter_code(parameter: ActionParameterDescriptor):
    if parameter.required:
        if "loc" in parameter.reference_kinds:
            return '"$loc:"'
        if "data" in parameter.reference_kinds:
            return '"$data:"'
        if "visual" in parameter.reference_kinds:
            return '"$data:"'
    if parameter.default_code is None:
        return '""'
    return parameter.default_code


def _default_call_source(receiver_name: str, action: ActionDescriptor):
    arguments = []
    for parameter in action.parameters:
        value_code = _default_parameter_code(parameter)
        if parameter.kind == "POSITIONAL_ONLY":
            arguments.append(value_code)
        elif parameter.kind in {"POSITIONAL_OR_KEYWORD", "KEYWORD_ONLY"}:
            arguments.append(f"{parameter.name}={value_code}")
        else:
            continue
    if not arguments:
        return f"{receiver_name}.{action.name}()"
    joined = ",\n    ".join(arguments)
    return f"{receiver_name}.{action.name}(\n    {joined},\n)"


def _compact_call_source(call_source: str):
    compact = " ".join(call_source.split())
    if len(compact) <= 96:
        return compact
    return compact[:93].rstrip() + "..."


def _diagnostic_text(result: ActionRunResult):
    lines = []
    if result.stage:
        lines.append(f"stage              {result.stage}")
    if result.error_type or result.error_message:
        lines.append(f"error_type         {result.error_type or ''}")
        lines.append(f"error_message      {result.error_message or ''}")
    if result.cleanup_error:
        lines.append(f"cleanup_error      {result.cleanup_error}")
    diagnostic = result.diagnostic or {}
    if diagnostic:
        for key in (
                "code",
                "category",
                "stage",
                "summary",
                "backend",
                "entry_point",
                "locator_name",
                "locator_kind",
                "root_name",
                "root_state",
                "wait_type",
                "timeout_seconds",
                "interval_seconds",
                "probe_count",
                "candidate_count",
                "last_state",
                "cause_type",
                "cause_message",
                "artifacts",
                "diagnostic_version",
        ):
            value = diagnostic.get(key)
            if value in (None, "", []):
                continue
            lines.append(f"{key:<18} {_diagnostic_value_text(value)}")
    elif not result.success:
        lines.append("未收到 RuntimeDiagnostic。")
        lines.append("失败可能发生在调用代码解析、Page/View 初始化或非 locator/action 代码中。")
    if not lines:
        lines.append("本次运行未产生诊断详情。")
    return "\n".join(lines)


def _diagnostic_value_text(value):
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    return str(value)


def _catalog_issue_summary(catalog: PageViewCatalog):
    messages = []
    if catalog.import_errors:
        messages.append(f"导入失败 {len(catalog.import_errors)} 项")
    if catalog.warnings:
        messages.append(f"跳过未绑定 View {len(catalog.warnings)} 项")
    return "；".join(messages)