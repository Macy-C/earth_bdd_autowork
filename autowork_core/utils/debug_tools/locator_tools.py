import tkinter as tk
from tkinter import ttk

import yaml
from pywinauto.findwindows import ElementAmbiguousError

from autowork_core.common.compile import CHILD_FILTER_KEYS, compile_locator, data_map
from autowork_core.common.locator import _find_by_child_window, _find_by_pos
from autowork_core.utils.debug_tools.common import (
    get_element_rect,
    get_tree_values,
    make_element_key,
    to_wrapper,
)
from autowork_core.utils.visual_marker import mark_visual_target
from config.settings import settings


CHILD_ROOT_CURRENT_WINDOW = "当前窗口"
CHILD_ROOT_SELECTED_ELEMENT = "控件树选中项"


def validate_child_locator(root, locator, timeout=1.0, wait_type="exists"):
    compiled = compile_locator(locator)
    if compiled.prefix != "child":
        raise ValueError(f"Child 验证只接受 by: child，当前为: {compiled.prefix}")

    root_wrapper = to_wrapper(root)
    matches = _find_by_child_window(
        root_wrapper,
        compiled.criteria,
        first_only=False,
        control_type=None,
        timeout=timeout,
        wait_type="none",
    )
    matches = [to_wrapper(item) for item in (matches or [])]
    if not matches:
        raise LookupError(f"Child locator 未找到目标: {compiled.criteria}")
    if len(matches) > 1:
        error = ElementAmbiguousError(
            f"Child locator 匹配到 {len(matches)} 个元素，不唯一: {compiled.criteria}"
        )
        error.elements = matches
        raise error

    target = _find_by_child_window(
        root_wrapper,
        compiled.criteria,
        first_only=True,
        control_type=None,
        timeout=timeout,
        wait_type=wait_type,
    )
    if target is None:
        raise LookupError(f"Child locator 唯一候选未满足 {wait_type}: {compiled.criteria}")
    return to_wrapper(target), compiled


def resolve_pos_locator(locator):
    compiled = compile_locator(locator)
    if compiled.prefix != "pos":
        raise ValueError(f"坐标验证只接受 by: pos，当前为: {compiled.prefix}")
    return _find_by_pos(compiled.criteria), compiled


class LocatorToolMixin:
    def init_locator_tool_state(self):
        desktop_width, desktop_height = settings.desktop_size

        self.child_window = None
        self.child_root_mode_var = tk.StringVar(value=CHILD_ROOT_CURRENT_WINDOW)
        self.child_control_type_var = tk.StringVar(value="")
        self.child_auto_id_var = tk.StringVar(value="")
        self.child_title_var = tk.StringVar(value="")
        self.child_class_name_var = tk.StringVar(value="")
        self.child_wait_type_var = tk.StringVar(value="exists")
        self.child_timeout_var = tk.StringVar(value="1.0")
        self.child_result_var = tk.StringVar(value="")
        self.child_extra_text = None

        self.pos_window = None
        self.pos_x_var = tk.StringVar(value="")
        self.pos_y_var = tk.StringVar(value="")
        self.pos_source_width_var = tk.StringVar(value=str(desktop_width))
        self.pos_source_height_var = tk.StringVar(value=str(desktop_height))
        self.pos_result_var = tk.StringVar(value="")

    def open_child_tool(self):
        if self.child_window is not None:
            try:
                if self.child_window.winfo_exists():
                    self.child_window.lift()
                    self.child_window.focus_force()
                    return
            except Exception:
                pass

        self.child_window = tk.Toplevel(self.app)
        self.child_window.title("Child Locator Validator")
        self.child_window.geometry("700x520+170+90")
        self.child_window.minsize(650, 480)
        self.child_window.attributes("-topmost", True)
        self.child_window.protocol("WM_DELETE_WINDOW", self.close_child_tool)

        form = tk.Frame(self.child_window)
        form.pack(fill="x", padx=10, pady=10)
        form.columnconfigure(1, weight=1)
        form.columnconfigure(3, weight=1)

        tk.Label(form, text="Root：").grid(row=0, column=0, sticky="e")
        ttk.Combobox(
            form,
            state="readonly",
            values=(CHILD_ROOT_CURRENT_WINDOW, CHILD_ROOT_SELECTED_ELEMENT),
            textvariable=self.child_root_mode_var,
        ).grid(row=0, column=1, columnspan=3, sticky="ew", padx=4)

        fields = (
            ("control_type：", self.child_control_type_var, "auto_id：", self.child_auto_id_var),
            ("title：", self.child_title_var, "class_name：", self.child_class_name_var),
        )
        for row, (left_label, left_var, right_label, right_var) in enumerate(fields, start=1):
            tk.Label(form, text=left_label).grid(row=row, column=0, sticky="e", pady=(7, 0))
            tk.Entry(form, textvariable=left_var).grid(
                row=row,
                column=1,
                sticky="ew",
                padx=4,
                pady=(7, 0),
            )
            tk.Label(form, text=right_label).grid(row=row, column=2, sticky="e", pady=(7, 0))
            tk.Entry(form, textvariable=right_var).grid(
                row=row,
                column=3,
                sticky="ew",
                padx=4,
                pady=(7, 0),
            )

        tk.Label(form, text="等待状态：").grid(row=3, column=0, sticky="e", pady=(7, 0))
        ttk.Combobox(
            form,
            state="readonly",
            values=("exists", "visible", "enabled", "ready", "none"),
            textvariable=self.child_wait_type_var,
        ).grid(row=3, column=1, sticky="ew", padx=4, pady=(7, 0))

        tk.Label(form, text="超时秒：").grid(row=3, column=2, sticky="e", pady=(7, 0))
        tk.Entry(form, textvariable=self.child_timeout_var).grid(
            row=3,
            column=3,
            sticky="ew",
            padx=4,
            pady=(7, 0),
        )

        tk.Label(form, text="其他条件（YAML）：").grid(row=4, column=0, sticky="ne", pady=(7, 0))
        self.child_extra_text = tk.Text(form, height=6, font=("Consolas", 9))
        self.child_extra_text.grid(
            row=4,
            column=1,
            columnspan=3,
            sticky="nsew",
            padx=4,
            pady=(7, 0),
        )
        self.child_extra_text.insert(
            "1.0",
            "# 可选: title_re, auto_id_re, class_name_re, process, handle, depth\n"
            "# framework_id, visible, enabled",
        )

        buttons = tk.Frame(self.child_window)
        buttons.pack(fill="x", padx=10, pady=(2, 8))
        tk.Button(buttons, text="验证并框选", command=self.validate_child_from_gui).pack(side="left")
        tk.Button(buttons, text="填入选中控件", command=self.fill_child_from_selection).pack(side="left", padx=6)
        tk.Button(buttons, text="复制 Locator", command=self.copy_child_locator).pack(side="left")
        tk.Button(buttons, text="关闭", command=self.close_child_tool).pack(side="right")

        tk.Label(
            self.child_window,
            textvariable=self.child_result_var,
            anchor="nw",
            justify="left",
            wraplength=620,
        ).pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.child_result_var.set(
            "验证会先统计全部匹配；只有恰好 1 个且满足等待状态时才通过。"
        )

    def _child_locator_from_form(self):
        locator = {"by": "child"}
        if self.child_extra_text is not None:
            extra_text = self.child_extra_text.get("1.0", "end").strip()
            if extra_text:
                extra = yaml.safe_load(extra_text)
                if extra is not None and not isinstance(extra, dict):
                    raise ValueError("其他 Child 条件必须是 YAML mapping")
                if extra:
                    extra = data_map({"criteria": extra})["criteria"]
                    unknown = sorted(set(extra) - CHILD_FILTER_KEYS)
                    if unknown:
                        raise ValueError(f"不支持的 Child 条件: {', '.join(unknown)}")
                    locator.update(extra)

        values = (
            ("control_type", self.child_control_type_var.get()),
            ("auto_id", self.child_auto_id_var.get()),
            ("title", self.child_title_var.get()),
            ("class_name", self.child_class_name_var.get()),
        )
        for key, value in values:
            value = value.strip()
            if value:
                locator[key] = value
        if len(locator) == 1:
            raise ValueError("至少填写一个 Child 定位条件")
        return locator

    def _selected_child_root(self):
        if self.child_root_mode_var.get() == CHILD_ROOT_SELECTED_ELEMENT:
            selected = [
                item_id
                for item_id in self.tree.selection()
                if item_id in self.tree_id_to_element
            ]
            if not selected:
                raise ValueError("请先在控件树中选择一个 root 节点")
            return self.tree_id_to_element[selected[0]], CHILD_ROOT_SELECTED_ELEMENT

        if self.root_window is None:
            raise ValueError("请先选择并切换一个窗口")
        return self.root_window, CHILD_ROOT_CURRENT_WINDOW

    def fill_child_from_selection(self):
        selected = [
            item_id
            for item_id in self.tree.selection()
            if item_id in self.tree_id_to_element
        ]
        if not selected:
            self.child_result_var.set("请先在控件树中选择目标控件")
            return

        values = get_tree_values(self.tree_id_to_element[selected[0]])
        self.child_control_type_var.set(values["control_type"] or "")
        self.child_auto_id_var.set(values["auto_id"] or "")
        self.child_title_var.set(values["name"] or "")
        self.child_class_name_var.set(values["class_name"] or "")
        self.child_result_var.set("已填入控件树选中项的 Child 条件")

    def validate_child_from_gui(self):
        try:
            locator = self._child_locator_from_form()
            root, root_label = self._selected_child_root()
            timeout = float(self.child_timeout_var.get().strip())
            if timeout < 0:
                raise ValueError("超时秒不能小于 0")

            target, compiled = validate_child_locator(
                root,
                locator,
                timeout=timeout,
                wait_type=self.child_wait_type_var.get(),
            )
            self._highlight_child_targets([target])
            values = get_tree_values(target)
            self.child_result_var.set(
                "Child 验证通过：匹配结果唯一\n"
                f"Root: {root_label}\n"
                f"Criteria: {compiled.criteria}\n"
                f"control_type: {values['control_type']}\n"
                f"auto_id: {values['auto_id']}\n"
                f"name: {values['name']}\n"
                f"class_name: {values['class_name']}\n"
                f"rect: {values['rect']}"
            )
        except ElementAmbiguousError as error:
            matches = list(getattr(error, "elements", ()) or ())
            self._highlight_child_targets(matches)
            details = [
                self._child_candidate_summary(index, target)
                for index, target in enumerate(matches)
            ]
            self.child_result_var.set(
                f"Child 验证失败：匹配到 {len(matches)} 个元素，定位不唯一。\n"
                "请增加 Child 条件；确实需要按序号定位时改用 XPath [N]。\n"
                + "\n".join(details)
            )
        except Exception as error:
            self.overlay_manager.clear()
            self.child_result_var.set(f"Child 匹配失败: {type(error).__name__}: {error}")

    def _highlight_child_targets(self, targets):
        self.overlay_manager.clear()
        tree_ids = []
        for target in targets:
            try:
                self.overlay_manager.show_rect(get_element_rect(target))
            except Exception:
                pass
            try:
                tree_id = self.element_key_to_tree_id.get(make_element_key(target))
            except Exception:
                tree_id = None
            if tree_id:
                tree_ids.append(tree_id)

        if tree_ids:
            self.tree.selection_set(tree_ids)
            self.open_tree_parents(tree_ids[0])
            self.tree.see(tree_ids[0])

    @staticmethod
    def _child_candidate_summary(index, target):
        values = get_tree_values(target)
        return (
            f"[{index}] type={values['control_type']!r}, "
            f"auto_id={values['auto_id']!r}, name={values['name']!r}, "
            f"class={values['class_name']!r}, rect={values['rect']}"
        )

    def copy_child_locator(self):
        try:
            locator = self._child_locator_from_form()
            compiled = compile_locator(locator)
            text = yaml.safe_dump(
                {"by": "child", **compiled.criteria},
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
            ).rstrip()
            self.app.clipboard_clear()
            self.app.clipboard_append(text)
            self.child_result_var.set(f"已复制 Locator:\n{text}")
        except Exception as error:
            self.child_result_var.set(f"复制失败: {type(error).__name__}: {error}")

    def close_child_tool(self):
        if self.child_window is not None:
            try:
                self.child_window.destroy()
            except Exception:
                pass
        self.child_window = None
        self.child_extra_text = None

    def open_pos_tool(self):
        if self.pos_window is not None:
            try:
                if self.pos_window.winfo_exists():
                    self.pos_window.lift()
                    self.pos_window.focus_force()
                    return
            except Exception:
                pass

        self.pos_window = tk.Toplevel(self.app)
        self.pos_window.title("Coordinate Locator Validator")
        self.pos_window.geometry("560x260+210+150")
        self.pos_window.minsize(520, 240)
        self.pos_window.attributes("-topmost", True)
        self.pos_window.protocol("WM_DELETE_WINDOW", self.close_pos_tool)

        form = tk.Frame(self.pos_window)
        form.pack(fill="x", padx=10, pady=10)
        for column in (1, 3):
            form.columnconfigure(column, weight=1)

        fields = (
            ("x：", self.pos_x_var, "y：", self.pos_y_var),
            ("来源宽度：", self.pos_source_width_var, "来源高度：", self.pos_source_height_var),
        )
        for row, (left_label, left_var, right_label, right_var) in enumerate(fields):
            tk.Label(form, text=left_label).grid(row=row, column=0, sticky="e", pady=(6, 0))
            tk.Entry(form, textvariable=left_var).grid(row=row, column=1, sticky="ew", padx=4, pady=(6, 0))
            tk.Label(form, text=right_label).grid(row=row, column=2, sticky="e", pady=(6, 0))
            tk.Entry(form, textvariable=right_var).grid(row=row, column=3, sticky="ew", padx=4, pady=(6, 0))

        buttons = tk.Frame(self.pos_window)
        buttons.pack(fill="x", padx=10, pady=(2, 8))
        tk.Button(buttons, text="读取鼠标(F4)", command=self.capture_pos_from_cursor).pack(side="left")
        tk.Button(buttons, text="验证并标记", command=self.validate_pos_from_gui).pack(side="left", padx=6)
        tk.Button(buttons, text="复制 Locator", command=self.copy_pos_locator).pack(side="left")
        tk.Button(buttons, text="关闭", command=self.close_pos_tool).pack(side="right")

        tk.Label(
            self.pos_window,
            textvariable=self.pos_result_var,
            anchor="nw",
            justify="left",
            wraplength=530,
        ).pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.pos_result_var.set("移动鼠标到目标位置后按 F4，或点击“读取鼠标(F4)”。")

    def _pos_locator_from_form(self):
        return {
            "by": "pos",
            "coords": [
                int(self.pos_x_var.get().strip()),
                int(self.pos_y_var.get().strip()),
                int(self.pos_source_width_var.get().strip()),
                int(self.pos_source_height_var.get().strip()),
            ],
        }

    def capture_pos_from_cursor(self):
        x, y = self._cursor_position()
        self.pos_x_var.set(str(x))
        self.pos_y_var.set(str(y))
        self.pos_result_var.set(f"已读取当前鼠标坐标: ({x}, {y})")

    def capture_pos_hotkey(self):
        if self.pos_window is None:
            return
        try:
            if not self.pos_window.winfo_exists():
                return
        except Exception:
            return
        self.capture_pos_from_cursor()

    def validate_pos_from_gui(self):
        try:
            locator = self._pos_locator_from_form()
            target, compiled = resolve_pos_locator(locator)
            self.overlay_manager.clear()
            marked = mark_visual_target(target, duration=1.0)
            run_x, run_y = target[0]
            run_width, run_height = settings.desktop_size
            self.pos_result_var.set(
                "坐标验证成功\n"
                f"Criteria: {compiled.criteria}\n"
                f"运行桌面: {run_width} x {run_height}\n"
                f"换算坐标: ({run_x}, {run_y})\n"
                f"屏幕标记: {'成功' if marked else '失败'}"
            )
        except Exception as error:
            self.pos_result_var.set(f"坐标验证失败: {type(error).__name__}: {error}")

    def copy_pos_locator(self):
        try:
            locator = self._pos_locator_from_form()
            compiled = compile_locator(locator)
            x, y, width, height = compiled.criteria.split(",")
            text = f"by: pos\ncoords: [{x}, {y}, {width}, {height}]"
            self.app.clipboard_clear()
            self.app.clipboard_append(text)
            self.pos_result_var.set(f"已复制 Locator:\n{text}")
        except Exception as error:
            self.pos_result_var.set(f"复制失败: {type(error).__name__}: {error}")

    def close_pos_tool(self):
        if self.pos_window is not None:
            try:
                self.pos_window.destroy()
            except Exception:
                pass
        self.pos_window = None

    def close_locator_tools(self):
        self.close_child_tool()
        self.close_pos_tool()


__all__ = [
    "LocatorToolMixin",
    "resolve_pos_locator",
    "validate_child_locator",
]
