import ctypes
import sys
import time
from pathlib import Path
import tkinter as tk
from tkinter import ttk
# =========================================================
# 项目根目录处理
# =========================================================
PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from autowork_core.common.winauto_xpath import find_by_xpath
from autowork_core.utils.debug_tools.recorder.inspector import _wrapper_from_point
from autowork_core.utils.debug_tools.common import (
    get_all_element_properties,
    get_element_rect,
    get_open_windows,
    get_tree_values,
    iter_ordered_properties,
    iter_runtime_descendants,
    iter_tree_children,
    make_element_key,
    make_xpath_suggestion,
    safe_parent,
    safe_get_element_rect,
    to_wrapper,
)
from autowork_core.utils.overlay import OverlayManager
from autowork_core.utils.debug_tools.locator_tools import LocatorToolMixin
from autowork_core.utils.debug_tools.recorder.panel import RecorderToolMixin
from autowork_core.utils.debug_tools.visual_tools import VisualToolMixin


BACKEND = "uia"

VK_ESCAPE = 0x1B
VK_F2 = 0x71
VK_F4 = 0x73
VK_F8 = 0x77
VK_F9 = 0x78


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


user32 = ctypes.windll.user32

# =========================================================
# 主程序
# =========================================================
class XPathDebuggerApp(LocatorToolMixin, RecorderToolMixin, VisualToolMixin):
    def __init__(self):
        self.backend = BACKEND

        self.app = tk.Tk()
        self.app.title("BDD Autowork Locator 工作台")
        self.app.geometry("1180x760+40+40")
        self.app.minsize(900, 620)
        self.app.attributes("-topmost", True)

        self.overlay_manager = OverlayManager()

        self.after_id = None
        self.last_xpath = None

        self.windows = []
        self.root_window = None

        # 控件节点缓存
        self.element_key_to_tree_id = {}
        self.tree_id_to_element = {}
        self.tree_id_to_rect = {}
        self.tree_id_to_key = {}
        self.hit_test_items = []

        # 属性行状态
        self.property_expanded_items = set()
        self.property_row_items = {}

        self.selected_cell_value = tk.StringVar(value="")
        self.tree_expanded = False
        self.tree_toggle_button = None
        self.locator_workspace = None
        self.locator_notebook = None
        self.locator_workspace_initialized = False
        self.recorder_button = None

        # 点选控件模式
        self.pick_mode = False
        self.pick_overlay = None
        self.last_hover_key = None
        self.pick_current_element = None
        self.pick_current_tree_id = None
        self.pick_root_rect = None
        self.pick_mouse_down = False
        self.last_pick_motion_time = 0

        # 全局热键捕获模式：不抢下拉框焦点，不拦截鼠标点击。
        self.hotkey_capture_mode = False
        self.hotkey_poll_after_id = None
        self.hotkey_down = {
            VK_F2: False,
            VK_F4: False,
            VK_F8: False,
            VK_F9: False,
            VK_ESCAPE: False,
        }
        self.captured_tree_id = None

        # 双击恢复展开状态
        self.last_pressed_item = None
        self.last_pressed_open_state = None

        self.init_visual_tool_state()

        self.init_recorder_tool_state()

        self.init_locator_tool_state()

        self.build_ui()
        self.refresh_window_list()
        self.start_hotkey_polling()

    # =====================================================
    # UI
    # =====================================================
    def build_ui(self):
        header = ttk.Frame(self.app)
        header.pack(fill="x", padx=12, pady=(10, 6))
        ttk.Label(
            header,
            text="Locator 工作台",
            font=("Microsoft YaHei UI", 14, "bold"),
        ).pack(side="left")
        self.recorder_button = ttk.Button(
            header,
            text="Recorder 工作台",
            command=self.open_recorder_workbench,
        )
        self.recorder_button.pack(side="right")

        target = ttk.LabelFrame(self.app, text="目标窗口")
        target.pack(fill="x", padx=12, pady=(0, 8))
        target.columnconfigure(1, weight=1)
        ttk.Label(target, text="窗口").grid(
            row=0, column=0, sticky="w", padx=(8, 4), pady=7
        )
        self.window_combo = ttk.Combobox(target, state="readonly")
        self.window_combo.grid(row=0, column=1, sticky="ew", padx=4, pady=7)
        self.window_combo.bind("<<ComboboxSelected>>", self.on_window_selected)
        ttk.Button(
            target,
            text="刷新列表",
            command=self.refresh_window_list,
        ).grid(row=0, column=2, padx=4, pady=7)
        ttk.Button(
            target,
            text="切换窗口",
            command=self.switch_to_selected_window,
        ).grid(row=0, column=3, padx=(4, 8), pady=7)

        workspace = ttk.Panedwindow(self.app, orient="horizontal")
        self.locator_workspace = workspace
        workspace.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        workspace.bind("<Map>", self.initialize_locator_workspace, add="+")

        tree_panel = ttk.Frame(workspace)
        inspector_panel = ttk.Frame(workspace)
        workspace.add(tree_panel, weight=3)
        workspace.add(inspector_panel, weight=2)

        tree_panel.rowconfigure(1, weight=1)
        tree_panel.columnconfigure(0, weight=1)
        tree_toolbar = ttk.Frame(tree_panel)
        tree_toolbar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        ttk.Button(
            tree_toolbar,
            text="刷新树 F2",
            command=self.refresh_tree,
        ).pack(side="left")
        self.tree_toggle_button = ttk.Button(
            tree_toolbar,
            text="展开全部",
            command=self.toggle_tree_expansion,
        )
        self.tree_toggle_button.pack(side="left", padx=6)
        ttk.Button(
            tree_toolbar,
            text="点选控件",
            command=self.start_pick_mode,
        ).pack(side="left")
        ttk.Button(
            tree_toolbar,
            text="捕获 F8",
            command=self.toggle_hotkey_capture_mode,
        ).pack(side="left", padx=6)
        ttk.Button(
            tree_toolbar,
            text="清除框选",
            command=self.clear_overlay,
        ).pack(side="left")

        tree_frame = ttk.LabelFrame(tree_panel, text="控件树")
        tree_frame.grid(row=1, column=0, columnspan=2, sticky="nsew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        self.tree = ttk.Treeview(
            tree_frame,
            columns=("type", "props", "name", "value", "class", "rect"),
            show="tree headings",
            selectmode="extended"
        )

        self.tree.heading("#0", text="控件树")
        self.tree.heading("type", text="type")
        self.tree.heading("props", text="属性")
        self.tree.heading("name", text="name / key")
        self.tree.heading("value", text="value / auto_id")
        self.tree.heading("class", text="class")
        self.tree.heading("rect", text="rect")

        self.tree.column("#0", width=300, minwidth=180)
        self.tree.column("type", width=90, minwidth=60)
        self.tree.column("props", width=80, minwidth=60)
        self.tree.column("name", width=260, minwidth=120)
        self.tree.column("value", width=360, minwidth=160)
        self.tree.column("class", width=120, minwidth=80)
        self.tree.column("rect", width=150, minwidth=90)

        y_scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        x_scroll = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)

        self.tree.configure(
            yscrollcommand=y_scroll.set,
            xscrollcommand=x_scroll.set
        )
        self.tree.tag_configure("captured", background="#dbeafe", foreground="#123c69")
        self.tree.tag_configure("runtime", background="#eef2ff")

        self.tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")

        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        self.tree.bind("<ButtonPress-1>", self.on_tree_button_press)
        self.tree.bind("<<TreeviewSelect>>", self.on_tree_select)
        self.tree.bind("<ButtonRelease-1>", self.on_tree_cell_click)
        self.tree.bind("<Double-1>", self.on_tree_double_click)

        self.locator_notebook = ttk.Notebook(inspector_panel)
        self.locator_notebook.pack(fill="both", expand=True)
        xpath_page = ttk.Frame(self.locator_notebook)
        tools_page = ttk.Frame(self.locator_notebook)
        self.locator_notebook.add(xpath_page, text="XPath")
        self.locator_notebook.add(tools_page, text="验证工具")

        xpath_box = ttk.LabelFrame(xpath_page, text="XPath 定位")
        xpath_box.pack(fill="x", padx=8, pady=8)
        for column in range(4):
            xpath_box.columnconfigure(column, weight=1)
        self.entry = ttk.Entry(xpath_box, font=("Consolas", 10))
        self.entry.grid(row=0, column=0, columnspan=4, sticky="ew", padx=8, pady=8)
        self.entry.bind("<KeyRelease>", self.on_xpath_change)
        ttk.Button(
            xpath_box,
            text="前兄弟",
            command=lambda: self.generate_sibling_xpath("prev"),
        ).grid(row=1, column=0, sticky="ew", padx=(8, 3), pady=(0, 6))
        ttk.Button(
            xpath_box,
            text="后兄弟",
            command=lambda: self.generate_sibling_xpath("next"),
        ).grid(row=1, column=1, sticky="ew", padx=3, pady=(0, 6))
        ttk.Button(
            xpath_box,
            text="前任意",
            command=lambda: self.generate_sibling_xpath("prev", wildcard=True),
        ).grid(row=1, column=2, sticky="ew", padx=3, pady=(0, 6))
        ttk.Button(
            xpath_box,
            text="后任意",
            command=lambda: self.generate_sibling_xpath("next", wildcard=True),
        ).grid(row=1, column=3, sticky="ew", padx=(3, 8), pady=(0, 6))
        ttk.Button(
            xpath_box,
            text="清空 XPath",
            command=self.clear_input,
        ).grid(row=2, column=0, columnspan=4, sticky="w", padx=8, pady=(0, 8))

        selected_box = ttk.LabelFrame(xpath_page, text="选中属性")
        selected_box.pack(fill="x", padx=8, pady=(0, 8))
        selected_box.columnconfigure(0, weight=1)
        self.selected_cell_entry = ttk.Entry(
            selected_box,
            textvariable=self.selected_cell_value,
            font=("Consolas", 9),
        )
        self.selected_cell_entry.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        ttk.Button(
            selected_box,
            text="复制属性",
            command=self.copy_selected_cell,
        ).grid(row=0, column=1, padx=(0, 8), pady=8)

        structural = ttk.LabelFrame(tools_page, text="结构定位")
        structural.pack(fill="x", padx=8, pady=8)
        ttk.Button(
            structural,
            text="Child 验证",
            command=self.open_child_tool,
        ).pack(side="left", padx=8, pady=8)
        ttk.Button(
            structural,
            text="坐标验证",
            command=self.open_pos_tool,
        ).pack(side="left", padx=(0, 8), pady=8)

        visual = ttk.LabelFrame(tools_page, text="视觉定位")
        visual.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(
            visual,
            text="OCR 验证",
            command=self.open_ocr_tool,
        ).pack(side="left", padx=8, pady=8)
        ttk.Button(
            visual,
            text="图片验证",
            command=self.open_pic_tool,
        ).pack(side="left", padx=(0, 8), pady=8)

        self.status_label = ttk.Label(
            self.app,
            text="请选择一个已打开窗口进行调试。",
            anchor="w",
        )
        self.status_label.pack(fill="x", padx=12, pady=(0, 10))

        self.entry.focus_set()

    def initialize_locator_workspace(self, _event=None):
        if self.locator_workspace_initialized:
            return
        width = self.locator_workspace.winfo_width()
        if width <= 1:
            self.app.after_idle(self.initialize_locator_workspace)
            return
        self.locator_workspace.sashpos(0, round(width * 0.62))
        self.locator_workspace_initialized = True

    # =====================================================
    # 全局热键：F2 刷新树，F4 读取坐标，F8 开关捕获，F9 捕获控件，Esc 退出捕获
    # =====================================================
    def start_hotkey_polling(self):
        if self.hotkey_poll_after_id is None:
            self.hotkey_poll_after_id = self.app.after(80, self.poll_hotkeys)

    def poll_hotkeys(self):
        self.hotkey_poll_after_id = None
        try:
            self._handle_hotkey_edge(VK_F2, self.on_f2_refresh_tree)
            self._handle_hotkey_edge(VK_F4, self.capture_pos_hotkey)
            self._handle_hotkey_edge(VK_F8, self.toggle_hotkey_capture_mode)
            if not self._recorder_is_active():
                self._handle_hotkey_edge(VK_F9, self.capture_current_mouse_element)
            self._handle_hotkey_edge(VK_ESCAPE, self.exit_hotkey_capture_mode)
        finally:
            try:
                self.hotkey_poll_after_id = self.app.after(80, self.poll_hotkeys)
            except Exception:
                self.hotkey_poll_after_id = None

    def _handle_hotkey_edge(self, vk, callback):
        pressed = bool(user32.GetAsyncKeyState(vk) & 0x8000)
        was_pressed = self.hotkey_down.get(vk, False)
        self.hotkey_down[vk] = pressed
        if pressed and not was_pressed:
            callback()

    @staticmethod
    def _cursor_position():
        point = POINT()
        user32.GetCursorPos(ctypes.byref(point))
        return int(point.x), int(point.y)

    def toggle_hotkey_capture_mode(self):
        if self.hotkey_capture_mode:
            self.exit_hotkey_capture_mode()
            return
        self.hotkey_capture_mode = True
        self.set_status("捕获模式已开启：展开下拉框后按 F2 刷新树，移动鼠标到目标按 F9 捕获；按 Esc 或 F8 退出。")

    def exit_hotkey_capture_mode(self):
        if not self.hotkey_capture_mode:
            return
        self.hotkey_capture_mode = False
        self.set_status("捕获模式已关闭。")

    def capture_current_mouse_element(self):
        if not self.hotkey_capture_mode:
            return

        try:
            if self.root_window is not None:
                self.load_control_tree()
            x, y = self._cursor_position()
            element = _wrapper_from_point(x, y, self.backend)
            self.select_captured_element(element, x, y)
        except Exception as e:
            self.set_status(f"捕获鼠标点控件失败: {type(e).__name__}: {e}")

    def select_captured_element(self, element, x=None, y=None):
        tree_id = None
        xpath = make_xpath_suggestion(element)

        try:
            key = make_element_key(element)
            tree_id = self.element_key_to_tree_id.get(key)
        except Exception:
            key = None

        try:
            xpath = self.make_unique_xpath_for_element(element, xpath)
        except Exception:
            pass

        try:
            rect = get_element_rect(element)
            self.overlay_manager.clear()
            self.overlay_manager.show_rect(rect)
        except Exception:
            pass

        if tree_id:
            self.tree.selection_set(tree_id)
            self.tree.focus(tree_id)
            self.mark_captured_tree_item(tree_id)
            self.open_tree_parents(tree_id)
            self.tree.see(tree_id)
        else:
            self.clear_tree_selection()
            self.clear_captured_tree_mark()

        self.entry.delete(0, "end")
        self.entry.insert(0, xpath)
        self.entry.icursor("end")

        self.app.clipboard_clear()
        self.app.clipboard_append(xpath)
        self.last_xpath = None

        point_text = f" @ {x},{y}" if x is not None and y is not None else ""
        scope_text = "已匹配当前控件树" if tree_id else "未匹配当前控件树"
        self.set_status(f"已捕获鼠标点控件{point_text}，{scope_text}，已复制 XPath: {xpath}")

    def clear_captured_tree_mark(self):
        if not self.captured_tree_id:
            return
        try:
            tags = tuple(tag for tag in self.tree.item(self.captured_tree_id, "tags") if tag != "captured")
            self.tree.item(self.captured_tree_id, tags=tags)
        except Exception:
            pass
        self.captured_tree_id = None

    def mark_captured_tree_item(self, tree_id):
        self.clear_captured_tree_mark()
        try:
            tags = tuple(self.tree.item(tree_id, "tags") or ())
            if "captured" not in tags:
                self.tree.item(tree_id, tags=tags + ("captured",))
            self.captured_tree_id = tree_id
        except Exception:
            self.captured_tree_id = None

    # =====================================================
    # TreeView
    # =====================================================
    def on_tree_button_press(self, event=None):
        if event is None:
            return

        item_id = self.tree.identify_row(event.y)
        self.last_pressed_item = item_id

        if item_id:
            try:
                self.last_pressed_open_state = self.tree.item(item_id, "open")
            except Exception:
                self.last_pressed_open_state = None
        else:
            self.last_pressed_open_state = None

    def on_tree_cell_click(self, event=None):
        if event is None:
            return

        row_id = self.tree.identify_row(event.y)
        column_id = self.tree.identify_column(event.x)

        if not row_id:
            return

        # #1 type
        # #2 props
        # #3 name / key
        # #4 value / auto_id
        # #5 class
        # #6 rect
        if column_id == "#2":
            self.toggle_properties_for_control(row_id)
            return "break"

        values = self.tree.item(row_id, "values")

        if not values:
            return

        if column_id == "#0":
            value = self.tree.item(row_id, "text")
        else:
            try:
                index = int(column_id.replace("#", "")) - 1
                value = values[index]
            except Exception:
                value = ""

        self.selected_cell_value.set(value)

    def copy_selected_cell(self):
        value = self.selected_cell_value.get()

        if not value:
            self.set_status("没有可复制的属性值。")
            return

        self.app.clipboard_clear()
        self.app.clipboard_append(value)
        self.set_status(f"已复制属性值: {value}")

    def on_tree_select(self, event=None):
        if self.pick_mode:
            return

        selected_items = self.tree.selection()

        if not selected_items:
            return

        self.overlay_manager.clear()

        count = 0

        for item_id in selected_items:
            element = self.tree_id_to_element.get(item_id)

            if element is None:
                continue

            try:
                rect = self.tree_id_to_rect.get(item_id)

                if rect is None:
                    rect = get_element_rect(element)
                    self.tree_id_to_rect[item_id] = rect

                self.overlay_manager.show_rect(rect=rect)
                count += 1

            except Exception as e:
                self.set_status(f"框选失败: {type(e).__name__}: {e}")

        if count:
            self.set_status(f"已框选控件树选中元素: {count} 个。")

    def on_tree_double_click(self, event=None):
        if event is None:
            return "break"

        item_id = self.tree.identify_row(event.y)

        if not item_id:
            return "break"

        element = self.tree_id_to_element.get(item_id)

        if element is None:
            self.set_status("当前行不是控件节点，无法生成 XPath。")
            return "break"

        if self.last_pressed_item == item_id and self.last_pressed_open_state is not None:
            try:
                self.tree.item(item_id, open=self.last_pressed_open_state)
            except Exception:
                pass

        xpath = self.make_unique_xpath_for_element(
            element,
            make_xpath_suggestion(element)
        )

        if self.after_id:
            try:
                self.app.after_cancel(self.after_id)
            except Exception:
                pass
            self.after_id = None

        self.tree.selection_set(item_id)
        self.tree.focus(item_id)
        self.tree.see(item_id)

        self.entry.delete(0, "end")
        self.entry.insert(0, xpath)
        self.entry.icursor("end")
        self.entry.update_idletasks()

        self.app.clipboard_clear()
        self.app.clipboard_append(xpath)
        self.app.update_idletasks()

        self.last_xpath = None
        self.run_xpath(force=True)

        self.set_status(f"已生成并复制 XPath: {xpath}")

        return "break"

    def make_unique_xpath_for_element(self, element, xpath):
        if self.root_window is None:
            return xpath

        try:
            elements = find_by_xpath(
                root=self.root_window,
                xpath=xpath,
                first_only=False
            )

            if not isinstance(elements, list):
                elements = [elements]

            if len(elements) <= 1:
                return xpath

            target_key = make_element_key(element)

            for index, candidate in enumerate(elements):
                if make_element_key(candidate) == target_key:
                    return f"{xpath}[{index}]"

        except Exception:
            pass

        return xpath

    def generate_sibling_xpath(self, direction, wildcard=False):
        selected_items = [
            item_id for item_id in self.tree.selection()
            if item_id in self.tree_id_to_element
        ]

        if not selected_items:
            self.set_status("请先选择一个控件节点。")
            return

        item_id = selected_items[0]
        element = self.tree_id_to_element.get(item_id)

        if element is None:
            self.set_status("当前行不是控件节点，无法生成兄弟 XPath。")
            return

        try:
            base_xpath = self.make_unique_xpath_for_element(
                element,
                make_xpath_suggestion(element)
            )
            sibling_type = "*" if wildcard else self.get_adjacent_sibling_type(item_id, direction)
            xpath = f"{base_xpath}/{direction}::{sibling_type}[0]"

            if self.after_id:
                try:
                    self.app.after_cancel(self.after_id)
                except Exception:
                    pass
                self.after_id = None

            self.entry.delete(0, "end")
            self.entry.insert(0, xpath)
            self.entry.icursor("end")

            self.app.clipboard_clear()
            self.app.clipboard_append(xpath)

            self.last_xpath = None
            self.run_xpath(force=True)

            label = "前兄弟" if direction == "prev" else "后兄弟"
            if wildcard:
                label = "前任意兄弟" if direction == "prev" else "后任意兄弟"
            self.set_status(f"已生成并复制{label} XPath: {xpath}")

        except Exception as e:
            self.set_status(f"生成兄弟 XPath 失败: {type(e).__name__}: {e}")

    def get_adjacent_sibling_type(self, item_id, direction):
        parent_item = self.tree.parent(item_id)
        siblings = [
            child_id for child_id in self.tree.get_children(parent_item)
            if child_id in self.tree_id_to_element
        ]

        if item_id not in siblings:
            return "*"

        index = siblings.index(item_id)
        target_index = index - 1 if direction == "prev" else index + 1

        if target_index < 0 or target_index >= len(siblings):
            return "*"

        sibling = self.tree_id_to_element.get(siblings[target_index])
        if sibling is None:
            return "*"

        try:
            sibling = to_wrapper(sibling)
            control_type = getattr(sibling.element_info, "control_type", "") or "*"
            return control_type
        except Exception:
            return "*"

    # =====================================================
    # 窗口列表
    # =====================================================
    def refresh_window_list(self):
        self.overlay_manager.clear()

        try:
            self.windows = get_open_windows(backend=self.backend)
            displays = [item["display"] for item in self.windows]
            self.window_combo["values"] = displays

            if displays:
                self.window_combo.current(0)
                self.set_status(f"已发现 {len(displays)} 个窗口，请选择要调试的窗口。")
            else:
                self.set_status("没有发现可调试窗口。")

        except Exception as e:
            self.set_status(f"刷新窗口列表失败: {type(e).__name__}: {e}")

    def on_window_selected(self, event=None):
        index = self.window_combo.current()

        if index < 0 or index >= len(self.windows):
            return

        title = self.windows[index]["title"]
        self.set_status(f"已选择窗口：{title}，点击“切换”开始调试。")

    def switch_to_selected_window(self):
        index = self.window_combo.current()

        if index < 0 or index >= len(self.windows):
            self.set_status("请先选择一个窗口。")
            return

        item = self.windows[index]
        win = item["window"]

        try:
            self.root_window = win

            try:
                self.root_window.set_focus()
            except Exception:
                pass

            self.last_xpath = None
            self.entry.delete(0, "end")
            self.selected_cell_value.set("")
            self.overlay_manager.clear()
            self.load_control_tree()

            self.set_status(f"当前调试窗口：{item['title']}")

        except Exception as e:
            self.set_status(f"切换窗口失败: {type(e).__name__}: {e}")

    # =====================================================
    # 控件树加载：控件全量加载，属性按行展开
    # =====================================================
    def clear_tree_cache(self):
        self.captured_tree_id = None
        self.element_key_to_tree_id.clear()
        self.tree_id_to_element.clear()
        self.tree_id_to_rect.clear()
        self.tree_id_to_key.clear()
        self.hit_test_items.clear()
        self.property_expanded_items.clear()
        self.property_row_items.clear()

    def load_control_tree(self):
        self.tree.delete(*self.tree.get_children())
        self.clear_tree_cache()

        if self.root_window is None:
            self.set_status("请先选择并切换一个窗口。")
            return

        try:
            root_obj = to_wrapper(self.root_window)

            root_item_id = self.insert_tree_node(
                parent_item="",
                element=root_obj,
                level=0,
                max_depth=10
            )

            runtime_count = self.sync_runtime_descendants(root_obj, root_item_id)

            self.hit_test_items.sort(key=lambda item: item[0])
            self.expand_all(update_status=False)

            if runtime_count:
                self.set_status(f"控件树加载完成：{root_obj.window_text()}，补充运行时可定位元素 {runtime_count} 个。")
            else:
                self.set_status(f"控件树加载完成：{root_obj.window_text()}")

        except Exception as e:
            self.set_status(f"控件树加载失败: {type(e).__name__}: {e}")

    def insert_tree_node(self, parent_item, element, level=0, max_depth=10, include_children=True, tags=()):
        if level > max_depth:
            return None

        try:
            values = get_tree_values(element)
            rect = safe_get_element_rect(element)

            text = (
                f"{values['control_type']} - {values['name']}"
                if values["name"]
                else values["control_type"]
            )

            item_id = self.tree.insert(
                parent_item,
                "end",
                text=text,
                values=(
                    values["control_type"],   # type
                    "▶ 属性",                # props
                    values["name"],           # name / key
                    values["auto_id"],        # value / auto_id
                    values["class_name"],     # class
                    values["rect"],           # rect
                ),
                tags=tags,
            )

            key = make_element_key(element)

            self.element_key_to_tree_id[key] = item_id
            self.tree_id_to_element[item_id] = element
            self.tree_id_to_key[item_id] = key
            self.tree_id_to_rect[item_id] = rect

            if rect is not None:
                width = rect.right - rect.left
                height = rect.bottom - rect.top
                area = width * height

                if area > 0:
                    self.hit_test_items.append((area, item_id, rect))

            if include_children:
                for child in iter_tree_children(element):
                    self.insert_tree_node(
                        parent_item=item_id,
                        element=child,
                        level=level + 1,
                        max_depth=max_depth
                    )

            return item_id

        except Exception as e:
            try:
                self.tree.insert(parent_item, "end", text=f"<节点读取失败: {type(e).__name__}: {e}>")
            except Exception:
                pass
            return None

    def sync_runtime_descendants(self, root_obj, root_item_id):
        if not root_item_id:
            return 0

        added = 0
        for element in iter_runtime_descendants(root_obj):
            if self.ensure_runtime_tree_node(element, root_item_id):
                added += 1
        return added

    def ensure_runtime_tree_node(self, element, root_item_id):
        try:
            key = make_element_key(element)
        except Exception:
            return None

        existing = self.element_key_to_tree_id.get(key)
        if existing:
            return None

        parent_item = self.find_nearest_tree_parent(element, root_item_id)
        item_id = self.insert_tree_node(
            parent_item=parent_item,
            element=element,
            include_children=False,
            tags=("runtime",),
        )
        return item_id

    def find_nearest_tree_parent(self, element, root_item_id):
        seen = set()
        current = element

        while True:
            parent = safe_parent(current)
            if parent is None:
                return root_item_id

            try:
                parent_key = make_element_key(parent)
            except Exception:
                return root_item_id

            if parent_key in seen:
                return root_item_id
            seen.add(parent_key)

            parent_item = self.element_key_to_tree_id.get(parent_key)
            if parent_item:
                return parent_item

            current = parent

    # =====================================================
    # 属性行展开 / 收起
    # =====================================================
    def toggle_properties_for_control(self, item_id):
        if item_id not in self.tree_id_to_element:
            self.set_status("当前行不是控件节点。")
            return

        if item_id in self.property_expanded_items:
            self.collapse_properties_for_control(item_id)
        else:
            self.expand_properties_for_control(item_id)

    def expand_properties_for_control(self, item_id):
        if item_id in self.property_expanded_items:
            return

        element = self.tree_id_to_element.get(item_id)

        if element is None:
            return

        props = get_all_element_properties(element)
        prop_row_ids = []

        for prop_name, prop_value in iter_ordered_properties(props):
            row_id = self.tree.insert(
                item_id,
                0,
                text="",
                values=(
                    "",          # type
                    "",          # props
                    prop_name,   # name / key
                    prop_value,  # value / auto_id
                    "",          # class
                    "",          # rect
                )
            )
            prop_row_ids.append(row_id)

        self.property_row_items[item_id] = prop_row_ids
        self.property_expanded_items.add(item_id)

        values = list(self.tree.item(item_id, "values"))
        values[1] = "▼ 属性"
        self.tree.item(item_id, values=values, open=True)

        self.set_status("已展开当前控件属性。")

    def collapse_properties_for_control(self, item_id):
        row_ids = self.property_row_items.pop(item_id, [])

        for row_id in row_ids:
            try:
                self.tree.delete(row_id)
            except Exception:
                pass

        self.property_expanded_items.discard(item_id)

        if self.tree.exists(item_id):
            values = list(self.tree.item(item_id, "values"))

            if len(values) >= 2:
                values[1] = "▶ 属性"
                self.tree.item(item_id, values=values)

        self.set_status("已收起当前控件属性。")

    # =====================================================
    # 刷新 / 展开 / 收起
    # =====================================================
    def refresh_tree(self):
        self.overlay_manager.clear()
        self.last_xpath = None

        self.load_control_tree()
        self.collapse_then_expand_tree(delay=220)

        if self.entry.get().strip():
            self.run_xpath(force=True)

    def collapse_then_expand_tree(self, delay=220):
        def _collapse_all_items():
            def _collapse(item):
                for child in self.tree.get_children(item):
                    _collapse(child)

                self.tree.item(item, open=False)

            for item in self.tree.get_children(""):
                _collapse(item)

        def _expand_after_delay():
            self.expand_all(update_status=False)
            self.set_status("控件树已刷新并展开。")

        _collapse_all_items()
        self.set_status("控件树已刷新，正在展开...")
        self.app.after(delay, _expand_after_delay)

    def on_f2_refresh_tree(self, event=None):
        self.refresh_tree()
        return "break"

    def _sync_tree_toggle_button(self):
        if self.tree_toggle_button is not None:
            text = "收起全部" if self.tree_expanded else "展开全部"
            self.tree_toggle_button.config(text=text)

    def toggle_tree_expansion(self):
        if self.tree_expanded:
            self.collapse_all()
        else:
            self.expand_all()

    def expand_all(self, update_status=True):
        def _expand(item):
            if item not in self.tree_id_to_element:
                return

            self.tree.item(item, open=True)

            for child in self.tree.get_children(item):
                _expand(child)

        for item in self.tree.get_children(""):
            _expand(item)

        self.tree_expanded = True
        self._sync_tree_toggle_button()

        if update_status:
            self.set_status("已展开全部控件节点。")

    def collapse_all(self):
        for item_id in list(self.property_expanded_items):
            self.collapse_properties_for_control(item_id)

        def _collapse(item):
            for child in self.tree.get_children(item):
                _collapse(child)
            self.tree.item(item, open=False)

        for item in self.tree.get_children(""):
            _collapse(item)

        self.tree_expanded = False
        self._sync_tree_toggle_button()

        self.set_status("已收起全部节点。")

    # =====================================================
    # 点选控件模式：透明捕获层，不使用 Hook
    # =====================================================
    def start_pick_mode(self):
        if self.root_window is None:
            self.set_status("请先选择并切换一个窗口。")
            return

        if self.pick_mode:
            self.stop_pick_mode()
            return

        self.pick_mode = True
        self.last_hover_key = None
        self.pick_current_element = None
        self.pick_current_tree_id = None
        self.pick_mouse_down = False
        self.last_pick_motion_time = 0

        try:
            root_obj = to_wrapper(self.root_window)
            root_key = make_element_key(root_obj)
            root_tree_id = self.element_key_to_tree_id.get(root_key)

            rect = self.tree_id_to_rect.get(root_tree_id)

            if rect is None:
                rect = root_obj.rectangle()

            self.pick_root_rect = rect

        except Exception as e:
            self.pick_mode = False
            self.set_status(f"点选模式启动失败: {type(e).__name__}: {e}")
            return

        width = rect.right - rect.left
        height = rect.bottom - rect.top

        if width <= 0 or height <= 0:
            self.pick_mode = False
            self.set_status("点选模式启动失败：窗口区域无效。")
            return

        self.overlay_manager.clear()

        self.pick_overlay = tk.Toplevel(self.app)
        self.pick_overlay.overrideredirect(True)
        self.pick_overlay.attributes("-topmost", True)

        self.pick_overlay.attributes("-alpha", 0.03)
        self.pick_overlay.configure(bg="black")
        self.pick_overlay.geometry(f"{width}x{height}+{rect.left}+{rect.top}")

        self.pick_overlay.bind("<Motion>", self.on_pick_overlay_motion)
        self.pick_overlay.bind("<ButtonPress-1>", self.on_pick_overlay_left_press)
        self.pick_overlay.bind("<ButtonRelease-1>", self.on_pick_overlay_left_release)
        self.pick_overlay.bind("<ButtonPress-3>", self.on_pick_overlay_right_click)
        self.pick_overlay.bind("<Escape>", self.on_pick_overlay_cancel)

        self.pick_overlay.lift()
        self.pick_overlay.focus_force()
        self.pick_overlay.grab_set()

        self.set_status(
            "点选模式已开启：移动鼠标框选控件；左键选择；右键或 Esc 取消。"
        )

    def stop_pick_mode(self):
        self.pick_mode = False
        self.last_hover_key = None
        self.pick_current_element = None
        self.pick_current_tree_id = None
        self.pick_root_rect = None
        self.pick_mouse_down = False
        self.last_pick_motion_time = 0

        if self.pick_overlay is not None:
            try:
                self.pick_overlay.grab_release()
            except Exception:
                pass

            try:
                self.pick_overlay.destroy()
            except Exception:
                pass

        self.pick_overlay = None

        self.set_status("点选模式已关闭。")

    def on_pick_overlay_cancel(self, event=None):
        self.stop_pick_mode()
        return "break"

    def on_pick_overlay_right_click(self, event=None):
        self.stop_pick_mode()
        return "break"

    def on_pick_overlay_motion(self, event):
        if not self.pick_mode:
            return "break"

        now = time.time()

        if now - self.last_pick_motion_time < 0.03:
            return "break"

        self.last_pick_motion_time = now

        x = event.x_root
        y = event.y_root

        element, tree_id = self.find_deepest_element_by_point(x, y)

        if element is None or tree_id is None:
            return "break"

        try:
            key = self.tree_id_to_key.get(tree_id)

            if key is None:
                key = make_element_key(element)

            if key == self.last_hover_key:
                return "break"

            self.last_hover_key = key
            self.pick_current_element = element
            self.pick_current_tree_id = tree_id

            rect = self.tree_id_to_rect.get(tree_id)

            if rect is None:
                rect = get_element_rect(element)
                self.tree_id_to_rect[tree_id] = rect

            self.overlay_manager.clear()
            self.overlay_manager.show_rect(rect)

            if self.pick_overlay is not None:
                self.pick_overlay.lift()
                self.pick_overlay.focus_force()

        except Exception:
            pass

        return "break"

    def on_pick_overlay_left_press(self, event):
        if not self.pick_mode:
            return "break"

        self.pick_mouse_down = True
        return "break"

    def on_pick_overlay_left_release(self, event):
        if not self.pick_mode:
            return "break"

        if not self.pick_mouse_down:
            return "break"

        x = event.x_root
        y = event.y_root

        element = self.pick_current_element
        tree_id = self.pick_current_tree_id

        if element is None:
            element, tree_id = self.find_deepest_element_by_point(x, y)

        if element is None:
            self.stop_pick_mode()
            return "break"

        try:
            rect = self.tree_id_to_rect.get(tree_id)

            if rect is None:
                rect = get_element_rect(element)
                self.tree_id_to_rect[tree_id] = rect

            self.overlay_manager.clear()
            self.overlay_manager.show_rect(rect)

            if tree_id:
                self.tree.selection_set(tree_id)
                self.tree.focus(tree_id)
                self.open_tree_parents(tree_id)
                self.tree.see(tree_id)

            xpath = self.make_unique_xpath_for_element(
                element,
                make_xpath_suggestion(element)
            )

            self.entry.delete(0, "end")
            self.entry.insert(0, xpath)
            self.entry.icursor("end")

            self.app.clipboard_clear()
            self.app.clipboard_append(xpath)

            self.last_xpath = None

            self.set_status(f"已点选控件并复制 XPath: {xpath}")

        except Exception as e:
            self.set_status(f"点选控件失败: {type(e).__name__}: {e}")

        finally:
            self.stop_pick_mode()

        return "break"

    def find_deepest_element_by_point(self, x, y):
        """
        基于缓存 rect 做命中测试。
        hit_test_items 已按面积从小到大排序。
        """
        for _area, tree_id, rect in self.hit_test_items:
            try:
                if rect.left <= x <= rect.right and rect.top <= y <= rect.bottom:
                    element = self.tree_id_to_element.get(tree_id)

                    if element is not None:
                        return element, tree_id
            except Exception:
                continue

        return None, None

    # =====================================================
    # XPath 动态匹配
    # =====================================================
    def on_xpath_change(self, event=None):
        if self.after_id:
            self.app.after_cancel(self.after_id)

        self.after_id = self.app.after(300, self.run_xpath)

    def run_xpath(self, force=False):
        if self.root_window is None:
            self.set_status("请先选择并切换一个窗口。")
            return

        xpath = self.entry.get().strip()

        if not force and self.last_xpath is not None and xpath == self.last_xpath:
            return

        self.last_xpath = xpath

        if not xpath:
            self.overlay_manager.clear()
            self.clear_tree_selection()
            self.set_status("等待输入 XPath...")
            return

        try:
            elements = find_by_xpath(
                root=self.root_window,
                xpath=xpath,
                first_only=False
            )

            if not isinstance(elements, list):
                elements = [elements]

            self.overlay_manager.clear()
            self.clear_tree_selection()

            matched_tree_items = []

            for element in elements:
                key = make_element_key(element)
                tree_id = self.element_key_to_tree_id.get(key)

                if tree_id:
                    rect = self.tree_id_to_rect.get(tree_id)
                else:
                    rect = get_element_rect(element)

                self.overlay_manager.show_rect(rect=rect)

                if tree_id:
                    matched_tree_items.append(tree_id)

            if matched_tree_items:
                self.tree.selection_set(matched_tree_items)
                self.open_tree_parents(matched_tree_items[0])
                self.tree.see(matched_tree_items[0])

            self.set_status(
                f"XPath 匹配成功: {len(elements)} 个元素，已在软件中框选。"
            )

        except Exception as e:
            self.overlay_manager.clear()
            self.clear_tree_selection()
            self.set_status(
                f"XPath 匹配失败: {type(e).__name__}: {e}"
            )

    # =====================================================
    # 其他辅助
    # =====================================================
    def open_tree_parents(self, item_id):
        parent = self.tree.parent(item_id)
        parents = []

        while parent:
            parents.append(parent)
            parent = self.tree.parent(parent)

        for p in reversed(parents):
            self.tree.item(p, open=True)

    def clear_tree_selection(self):
        selected = self.tree.selection()
        if selected:
            self.tree.selection_remove(selected)

    def clear_input(self):
        self.entry.delete(0, "end")
        self.last_xpath = None

        self.overlay_manager.clear()
        self.clear_tree_selection()

        self.set_status("已清空输入框。")

    def clear_overlay(self):
        self.overlay_manager.clear()
        self.clear_tree_selection()
        self.set_status("已清除框选。")

    def set_status(self, text):
        self.status_label.config(text=text)

    def close(self):
        if self.hotkey_poll_after_id is not None:
            try:
                self.app.after_cancel(self.hotkey_poll_after_id)
            except Exception:
                pass
            self.hotkey_poll_after_id = None
        self.stop_recorder_on_close()
        self.stop_pick_mode()
        self.close_recorder_tool(force=True)
        self.close_locator_tools()
        self.close_ocr_tool()
        self.close_pic_tool()
        self.overlay_manager.clear()
        self.app.destroy()

    def run(self):
        self.app.protocol("WM_DELETE_WINDOW", self.close)
        self.app.mainloop()


if __name__ == "__main__":
    XPathDebuggerApp().run()

