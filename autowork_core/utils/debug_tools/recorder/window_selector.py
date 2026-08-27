from __future__ import annotations

import tkinter as tk
from tkinter import ttk


def format_window_selection_summary(
        window_map,
        selected_handles,
        primary_handle,
        window_capture_mode,
    ):
    primary = window_map.get(primary_handle)
    if primary is None:
        if str(window_capture_mode or "strict").strip().lower() == "auto":
            return "自动跟随：第一次业务操作确定主窗口"
        return "未选择窗口"
    name = primary.get("title") or primary.get("class_name") or "无标题窗口"
    return f"主窗口：{name}（共 {len(selected_handles)} 个）"


class RecorderWindowSelector:
    def __init__(
            self,
            parent,
            windows,
            selected_handles,
            primary_handle,
            on_apply,
            on_refresh,
            allow_empty=False,
    ):
        self.parent = parent
        self.windows = list(windows)
        self.window_map = {
            int(item["handle"]): item
            for item in self.windows
        }
        self.selected_handles = {
            int(handle)
            for handle in selected_handles
            if int(handle) in self.window_map
        }
        self.primary_handle = (
            int(primary_handle)
            if primary_handle is not None
            and int(primary_handle) in self.window_map
            else None
        )
        self.on_apply = on_apply
        self.on_refresh = on_refresh
        self.allow_empty = bool(allow_empty)
        if self.primary_handle not in self.selected_handles:
            self.primary_handle = next(iter(self.selected_handles), None)

        self.dialog = tk.Toplevel(parent)
        self.dialog.title("选择当前 Step 的录制窗口")
        self.dialog.geometry("820x500+150+100")
        self.dialog.minsize(680, 420)
        self.dialog.transient(parent)
        self.dialog.attributes("-topmost", True)
        self.dialog.grab_set()

        self.summary_var = tk.StringVar(value="")
        self.tree = None
        self._build_ui()
        self._render()

    def show(self):
        self.dialog.deiconify()
        self.dialog.lift()
        self.dialog.focus_force()
        return self

    def _build_ui(self):
        ttk.Label(
            self.dialog,
            text=(
                "勾选本 Step 可能操作的窗口；主窗口用于前后截图和主要控件树。"
                "同一进程的新弹窗会自动纳入采集。"
            ),
            anchor="w",
            wraplength=780,
        ).pack(fill="x", padx=12, pady=(12, 8))

        frame = ttk.Frame(self.dialog)
        frame.pack(fill="both", expand=True, padx=12)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        columns = ("selected", "primary", "title", "process", "class", "handle")
        self.tree = ttk.Treeview(
            frame,
            columns=columns,
            show="headings",
            selectmode="browse",
        )
        headings = (
            ("selected", "录制", 55),
            ("primary", "主窗口", 65),
            ("title", "窗口标题", 310),
            ("process", "PID", 70),
            ("class", "窗口类", 160),
            ("handle", "HWND", 90),
        )
        for column, label, width in headings:
            self.tree.heading(column, text=label)
            self.tree.column(
                column,
                width=width,
                minwidth=45,
                stretch=column == "title",
            )
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<Double-1>", lambda event: self.set_primary())

        actions = ttk.Frame(self.dialog)
        actions.pack(fill="x", padx=12, pady=8)
        ttk.Button(actions, text="设为主窗口", command=self.set_primary).pack(side="left")
        if self.allow_empty:
            ttk.Button(
                actions,
                text="自动确定",
                command=self.clear_selection,
            ).pack(side="left", padx=6)
        ttk.Button(actions, text="刷新窗口", command=self.refresh).pack(side="left", padx=6)
        ttk.Label(actions, textvariable=self.summary_var).pack(side="left", padx=12)
        ttk.Button(actions, text="取消", command=self.dialog.destroy).pack(side="right")
        ttk.Button(actions, text="应用", command=self.apply).pack(side="right", padx=6)

    def _render(self):
        focused = self._focused_handle()
        self.tree.delete(*self.tree.get_children())
        for item in self.windows:
            handle = int(item["handle"])
            self.tree.insert(
                "",
                "end",
                iid=str(handle),
                values=(
                    "✓" if handle in self.selected_handles else "",
                    "●" if handle == self.primary_handle else "",
                    item.get("title") or "（无标题）",
                    item.get("process_id") or "",
                    item.get("class_name") or "",
                    handle,
                ),
            )
        if focused is not None and self.tree.exists(str(focused)):
            self.tree.selection_set(str(focused))
        elif self.tree.get_children():
            self.tree.selection_set(self.tree.get_children()[0])
        self.summary_var.set(
            f"已选 {len(self.selected_handles)} 个窗口"
            + ("，请选择主窗口" if self.primary_handle is None else "")
        )

    def _on_click(self, event):
        if self.tree.identify_column(event.x) != "#1":
            return
        item_id = self.tree.identify_row(event.y)
        if not item_id:
            return
        handle = int(item_id)
        if handle in self.selected_handles:
            if len(self.selected_handles) == 1 and not self.allow_empty:
                self.summary_var.set("至少保留一个录制窗口。")
                return "break"
            self.selected_handles.remove(handle)
            if self.primary_handle == handle:
                self.primary_handle = next(iter(self.selected_handles), None)
        else:
            self.selected_handles.add(handle)
            if self.primary_handle is None:
                self.primary_handle = handle
        self._render()
        return "break"

    def clear_selection(self):
        if not self.allow_empty:
            return
        self.selected_handles.clear()
        self.primary_handle = None
        self._render()

    def set_primary(self):
        handle = self._focused_handle()
        if handle is None:
            return
        self.selected_handles.add(handle)
        self.primary_handle = handle
        self._render()

    def refresh(self):
        windows = list(self.on_refresh() or ())
        self.windows = windows
        self.window_map = {int(item["handle"]): item for item in windows}
        self.selected_handles &= set(self.window_map)
        if self.primary_handle not in self.selected_handles:
            self.primary_handle = next(iter(self.selected_handles), None)
        self._render()

    def apply(self):
        if not self.selected_handles and self.allow_empty:
            self.on_apply((), None)
            self.dialog.destroy()
            return
        if not self.selected_handles or self.primary_handle is None:
            self.summary_var.set("至少选择一个窗口并指定主窗口。")
            return
        ordered = [
            int(item["handle"])
            for item in self.windows
            if int(item["handle"]) in self.selected_handles
        ]
        self.on_apply(tuple(ordered), int(self.primary_handle))
        self.dialog.destroy()

    def _focused_handle(self):
        selected = self.tree.selection() if self.tree is not None else ()
        return int(selected[0]) if selected else None
