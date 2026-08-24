import tkinter as tk
from tkinter import filedialog, ttk

import cv2
import mss
import numpy as np

from config.settings import settings
from autowork_core.common.ocr_engine import (
    enrich_ocr_candidates,
    match_ocr_candidates,
    recognize_text_by_ocr,
)
from autowork_core.common.pic_engine import (
    match_pic_in_image,
    read_image as read_pic_image,
)
from autowork_core.utils.debug_tools.common import (
    draw_ocr_preview,
    draw_pic_preview,
    image_monitor_from_bgr,
    make_photo_image,
    read_image_bgr,
    safe_to_string,
    to_wrapper,
)


class VisualToolMixin:
    def init_visual_tool_state(self):
        self.ocr_window = None
        self.ocr_target_var = tk.StringVar(value="")
        self.ocr_match_mode_var = tk.StringVar(value="smart")
        self.ocr_det_limit_type_var = tk.StringVar(value=str(settings.ocr_det_limit_type))
        self.ocr_det_limit_side_len_var = tk.StringVar(value=str(settings.ocr_det_limit_side_len))
        self.ocr_det_box_thresh_var = tk.StringVar(value=str(settings.ocr_det_box_thresh))
        self.ocr_det_unclip_ratio_var = tk.StringVar(value=str(settings.ocr_det_unclip_ratio))
        self.ocr_cache_ttl_var = tk.StringVar(value=str(settings.ocr_cache_ttl))
        self.ocr_use_cache_var = tk.BooleanVar(value=settings.ocr_cache_ttl > 0)
        self.ocr_params_summary_var = tk.StringVar(value="")
        self.ocr_source_label_var = tk.StringVar(value="未选择图片")
        self.ocr_status_var = tk.StringVar(value="选择图片或截取当前窗口后开始识别。")
        self.ocr_image = None
        self.ocr_monitor = None
        self.ocr_candidates = []
        self.ocr_matches = []
        self.ocr_preview_photo = None
        self.ocr_preview_label = None
        self.ocr_result_tree = None

        self.pic_window = None
        self.pic_template_path_var = tk.StringVar(value="")
        self.pic_source_label_var = tk.StringVar(value="未选择搜索图")
        self.pic_threshold_var = tk.StringVar(value="0.6")
        self.pic_method_var = tk.StringVar(value="auto")
        self.pic_pos_var = tk.StringVar(value="5")
        self.pic_status_var = tk.StringVar(value="选择模板图和搜索图后开始测试匹配。")
        self.pic_template_image = None
        self.pic_source_image = None
        self.pic_source_monitor = None
        self.pic_candidate = None
        self.pic_preview_photo = None
        self.pic_preview_label = None
        self.pic_result_value = tk.StringVar(value="")
    # =====================================================
    # OCR 辅助工具
    # =====================================================
    def open_ocr_tool(self):
        if self.ocr_window is not None:
            try:
                if self.ocr_window.winfo_exists():
                    self.ocr_window.lift()
                    self.ocr_window.focus_force()
                    return
            except Exception:
                pass

        self.ocr_window = tk.Toplevel(self.app)
        self.ocr_window.title("OCR Locator Debugger")
        self.ocr_window.geometry("980x790+120+80")
        self.ocr_window.minsize(760, 560)
        self.ocr_window.attributes("-topmost", True)
        self.ocr_window.protocol("WM_DELETE_WINDOW", self.close_ocr_tool)

        top_frame = tk.Frame(self.ocr_window)
        top_frame.pack(fill="x", padx=8, pady=(8, 4))
        top_frame.columnconfigure(3, weight=1)

        tk.Button(
            top_frame,
            text="选择图片",
            width=9,
            command=self.choose_ocr_image,
        ).grid(row=0, column=0, padx=(0, 4))

        tk.Button(
            top_frame,
            text="截取当前窗口",
            width=12,
            command=self.capture_ocr_current_window,
        ).grid(row=0, column=1, padx=4)

        tk.Label(top_frame, text="目标文字：").grid(row=0, column=2, padx=(10, 0), sticky="e")

        target_entry = tk.Entry(top_frame, textvariable=self.ocr_target_var, font=("Consolas", 10))
        target_entry.grid(row=0, column=3, sticky="ew", padx=4)
        target_entry.bind("<KeyRelease>", self.refresh_ocr_matches)

        mode_combo = ttk.Combobox(
            top_frame,
            width=9,
            state="readonly",
            values=("smart", "contains", "exact"),
            textvariable=self.ocr_match_mode_var,
        )
        mode_combo.grid(row=0, column=4, sticky="w", padx=4)
        mode_combo.bind("<<ComboboxSelected>>", self.refresh_ocr_matches)

        tk.Button(
            top_frame,
            text="识别",
            width=8,
            command=self.run_ocr_debug,
        ).grid(row=0, column=5, padx=(4, 0))

        params_frame = ttk.LabelFrame(self.ocr_window, text="OCR 参数")
        params_frame.pack(fill="x", padx=8, pady=(0, 4))
        for column in (1, 3, 5, 7, 9):
            params_frame.columnconfigure(column, weight=1)

        tk.Label(params_frame, text="limit_type：").grid(row=0, column=0, padx=(8, 0), pady=(6, 2), sticky="e")
        limit_type_combo = ttk.Combobox(
            params_frame,
            width=7,
            state="readonly",
            values=("max", "min"),
            textvariable=self.ocr_det_limit_type_var,
        )
        limit_type_combo.grid(row=0, column=1, padx=4, pady=(6, 2), sticky="ew")

        tk.Label(params_frame, text="limit_side_len：").grid(row=0, column=2, padx=(8, 0), pady=(6, 2), sticky="e")
        limit_side_entry = tk.Entry(params_frame, textvariable=self.ocr_det_limit_side_len_var, width=8)
        limit_side_entry.grid(row=0, column=3, padx=4, pady=(6, 2), sticky="ew")

        tk.Label(params_frame, text="box_thresh：").grid(row=0, column=4, padx=(8, 0), pady=(6, 2), sticky="e")
        box_thresh_entry = tk.Entry(params_frame, textvariable=self.ocr_det_box_thresh_var, width=8)
        box_thresh_entry.grid(row=0, column=5, padx=4, pady=(6, 2), sticky="ew")

        tk.Label(params_frame, text="unclip_ratio：").grid(row=0, column=6, padx=(8, 0), pady=(6, 2), sticky="e")
        unclip_entry = tk.Entry(params_frame, textvariable=self.ocr_det_unclip_ratio_var, width=8)
        unclip_entry.grid(row=0, column=7, padx=4, pady=(6, 2), sticky="ew")

        tk.Label(params_frame, text="cache_ttl：").grid(row=0, column=8, padx=(8, 0), pady=(6, 2), sticky="e")
        cache_ttl_entry = tk.Entry(params_frame, textvariable=self.ocr_cache_ttl_var, width=8)
        cache_ttl_entry.grid(row=0, column=9, padx=4, pady=(6, 2), sticky="ew")

        tk.Checkbutton(
            params_frame,
            text="use_cache",
            variable=self.ocr_use_cache_var,
            command=self.update_ocr_params_summary,
        ).grid(row=0, column=10, padx=(8, 4), pady=(6, 2), sticky="w")

        tk.Button(
            params_frame,
            text="恢复默认",
            width=9,
            command=self.reset_ocr_params,
        ).grid(row=0, column=11, padx=(4, 8), pady=(6, 2), sticky="e")

        tk.Label(
            params_frame,
            textvariable=self.ocr_params_summary_var,
            anchor="w",
            font=("Consolas", 9),
        ).grid(row=1, column=0, columnspan=12, sticky="ew", padx=8, pady=(2, 6))

        for widget in (limit_side_entry, box_thresh_entry, unclip_entry, cache_ttl_entry):
            widget.bind("<KeyRelease>", self.update_ocr_params_summary)
            widget.bind("<Return>", lambda event: self.run_ocr_debug())
        limit_type_combo.bind("<<ComboboxSelected>>", self.update_ocr_params_summary)

        tk.Label(
            self.ocr_window,
            textvariable=self.ocr_source_label_var,
            anchor="w",
        ).pack(fill="x", padx=8, pady=(0, 4))

        preview_frame = tk.Frame(self.ocr_window, borderwidth=1, relief="sunken")
        preview_frame.pack(fill="both", expand=True, padx=8, pady=4)

        self.ocr_preview_label = tk.Label(
            preview_frame,
            text="选择图片或截取当前窗口后显示预览。",
            anchor="center",
            bg="#f3f3f3",
        )
        self.ocr_preview_label.pack(fill="both", expand=True)

        result_frame = tk.Frame(self.ocr_window)
        result_frame.pack(fill="both", expand=False, padx=8, pady=4)
        result_frame.rowconfigure(0, weight=1)
        result_frame.columnconfigure(0, weight=1)

        self.ocr_result_tree = ttk.Treeview(
            result_frame,
            columns=("index", "match", "text", "confidence", "bounds", "center"),
            show="headings",
            height=8,
        )
        self.ocr_result_tree.heading("index", text="#")
        self.ocr_result_tree.heading("match", text="命中")
        self.ocr_result_tree.heading("text", text="text")
        self.ocr_result_tree.heading("confidence", text="confidence")
        self.ocr_result_tree.heading("bounds", text="bounds")
        self.ocr_result_tree.heading("center", text="center")

        self.ocr_result_tree.column("index", width=45, minwidth=35, anchor="center")
        self.ocr_result_tree.column("match", width=55, minwidth=45, anchor="center")
        self.ocr_result_tree.column("text", width=280, minwidth=160)
        self.ocr_result_tree.column("confidence", width=90, minwidth=70, anchor="center")
        self.ocr_result_tree.column("bounds", width=180, minwidth=120)
        self.ocr_result_tree.column("center", width=130, minwidth=100)

        y_scroll = ttk.Scrollbar(result_frame, orient="vertical", command=self.ocr_result_tree.yview)
        self.ocr_result_tree.configure(yscrollcommand=y_scroll.set)
        self.ocr_result_tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")

        tk.Label(
            self.ocr_window,
            textvariable=self.ocr_status_var,
            anchor="w",
        ).pack(fill="x", padx=8, pady=(0, 8))

        self.update_ocr_params_summary()
        target_entry.focus_set()

        if self.ocr_image is not None:
            self.update_ocr_preview()
            self.populate_ocr_results()

    def close_ocr_tool(self):
        if self.ocr_window is not None:
            try:
                self.ocr_window.destroy()
            except Exception:
                pass
        self.ocr_window = None
        self.ocr_preview_label = None
        self.ocr_result_tree = None

    def choose_ocr_image(self):
        file_path = filedialog.askopenfilename(
            parent=self.ocr_window or self.app,
            title="选择 OCR 测试图片",
            filetypes=(
                ("图片文件", "*.png;*.jpg;*.jpeg;*.bmp;*.webp"),
                ("所有文件", "*.*"),
            ),
        )

        if not file_path:
            return

        try:
            image = read_image_bgr(file_path)
            self.ocr_image = image
            self.ocr_monitor = image_monitor_from_bgr(image)
            self.ocr_candidates = []
            self.ocr_matches = []
            self.ocr_source_label_var.set(f"图片：{file_path}")
            self.ocr_status_var.set("图片已载入，点击“识别”开始 OCR。")
            self.update_ocr_preview()
            self.populate_ocr_results()
        except Exception as e:
            self.ocr_status_var.set(f"图片载入失败: {type(e).__name__}: {e}")

    def capture_ocr_current_window(self):
        if self.root_window is None:
            self.ocr_status_var.set("请先在主窗口选择并切换一个软件窗口。")
            return

        try:
            root_obj = to_wrapper(self.root_window)
            rect = root_obj.rectangle()
            width = max(1, int(rect.right - rect.left))
            height = max(1, int(rect.bottom - rect.top))
            monitor = {
                "left": int(rect.left),
                "top": int(rect.top),
                "width": width,
                "height": height,
            }

            with mss.mss() as sct:
                screenshot = sct.grab(monitor)
                image = np.ascontiguousarray(np.asarray(screenshot)[:, :, :3])

            self.ocr_image = image
            self.ocr_monitor = monitor
            self.ocr_candidates = []
            self.ocr_matches = []
            self.ocr_source_label_var.set(
                f"当前窗口截图：{root_obj.window_text()} | {monitor}"
            )
            self.ocr_status_var.set("当前窗口截图已载入，点击“识别”开始 OCR。")
            self.update_ocr_preview()
            self.populate_ocr_results()
        except Exception as e:
            self.ocr_status_var.set(f"截取当前窗口失败: {type(e).__name__}: {e}")

    def update_ocr_params_summary(self, event=None):
        if self.ocr_params_summary_var is None:
            return

        self.ocr_params_summary_var.set(
            "参数："
            f"det_limit_type={self.ocr_det_limit_type_var.get().strip() or settings.ocr_det_limit_type} | "
            f"det_limit_side_len={self.ocr_det_limit_side_len_var.get().strip() or settings.ocr_det_limit_side_len} | "
            f"det_box_thresh={self.ocr_det_box_thresh_var.get().strip() or settings.ocr_det_box_thresh} | "
            f"det_unclip_ratio={self.ocr_det_unclip_ratio_var.get().strip() or settings.ocr_det_unclip_ratio} | "
            f"use_cache={bool(self.ocr_use_cache_var.get())} | "
            f"cache_ttl={self.ocr_cache_ttl_var.get().strip() or settings.ocr_cache_ttl}"
        )

    def reset_ocr_params(self):
        self.ocr_match_mode_var.set(settings.ocr_match_mode)
        self.ocr_det_limit_type_var.set(str(settings.ocr_det_limit_type))
        self.ocr_det_limit_side_len_var.set(str(settings.ocr_det_limit_side_len))
        self.ocr_det_box_thresh_var.set(str(settings.ocr_det_box_thresh))
        self.ocr_det_unclip_ratio_var.set(str(settings.ocr_det_unclip_ratio))
        self.ocr_cache_ttl_var.set(str(settings.ocr_cache_ttl))
        self.ocr_use_cache_var.set(settings.ocr_cache_ttl > 0)
        self.update_ocr_params_summary()
        self.refresh_ocr_matches()

    @staticmethod
    def _parse_ocr_float(value, label, *, min_value=None, max_value=None):
        text = str(value).strip()
        if text == "":
            raise ValueError(f"{label} 不能为空")
        try:
            result = float(text)
        except Exception as exc:
            raise ValueError(f"{label} 必须是数字: {text}") from exc
        if min_value is not None and result < min_value:
            raise ValueError(f"{label} 不能小于 {min_value}")
        if max_value is not None and result > max_value:
            raise ValueError(f"{label} 不能大于 {max_value}")
        return result

    @staticmethod
    def _parse_ocr_int(value, label, *, min_value=None):
        result = VisualToolMixin._parse_ocr_float(value, label, min_value=min_value)
        if int(result) != result:
            raise ValueError(f"{label} 必须是整数: {value}")
        return int(result)

    def get_ocr_debug_options(self):
        det_limit_type = (self.ocr_det_limit_type_var.get().strip() or settings.ocr_det_limit_type).lower()
        if det_limit_type not in ("max", "min"):
            raise ValueError(f"det_limit_type 只能是 max 或 min: {det_limit_type}")

        return {
            "det_limit_type": det_limit_type,
            "det_limit_side_len": self._parse_ocr_int(
                self.ocr_det_limit_side_len_var.get(),
                "det_limit_side_len",
                min_value=1,
            ),
            "det_box_thresh": self._parse_ocr_float(
                self.ocr_det_box_thresh_var.get(),
                "det_box_thresh",
                min_value=0,
                max_value=1,
            ),
            "det_unclip_ratio": self._parse_ocr_float(
                self.ocr_det_unclip_ratio_var.get(),
                "det_unclip_ratio",
                min_value=0.1,
            ),
            "use_cache": bool(self.ocr_use_cache_var.get()),
            "cache_ttl": self._parse_ocr_float(
                self.ocr_cache_ttl_var.get(),
                "cache_ttl",
                min_value=0,
            ),
        }

    def run_ocr_debug(self):
        if self.ocr_image is None:
            self.ocr_status_var.set("请先选择图片或截取当前窗口。")
            return

        target_text = self.ocr_target_var.get().strip()
        match_mode = self.ocr_match_mode_var.get().strip() or "smart"

        try:
            ocr_options = self.get_ocr_debug_options()
        except Exception as e:
            self.ocr_status_var.set(f"OCR 参数无效: {e}")
            self.update_ocr_params_summary()
            return

        try:
            if self.ocr_window is not None:
                self.ocr_window.config(cursor="watch")
                self.ocr_window.update_idletasks()

            self.ocr_status_var.set("正在识别 OCR，首次加载模型会稍慢...")
            self.update_ocr_params_summary()

            raw_candidates = recognize_text_by_ocr(
                self.ocr_image,
                det_unclip_ratio=ocr_options["det_unclip_ratio"],
                det_limit_side_len=ocr_options["det_limit_side_len"],
                det_limit_type=ocr_options["det_limit_type"],
                det_box_thresh=ocr_options["det_box_thresh"],
                cache_ttl=ocr_options["cache_ttl"],
                cache_key="debug_ocr_tool",
                use_cache=ocr_options["use_cache"],
            )
            self.ocr_candidates = enrich_ocr_candidates(raw_candidates, self.ocr_monitor)
            self.ocr_matches = match_ocr_candidates(
                self.ocr_candidates,
                target_text,
                mode=match_mode,
            ) if target_text else []

            self.populate_ocr_results()
            self.update_ocr_preview()

            if target_text:
                self.ocr_status_var.set(
                    f"OCR 完成：识别 {len(self.ocr_candidates)} 条，命中 {len(self.ocr_matches)} 条。"
                )
            else:
                self.ocr_status_var.set(
                    f"OCR 完成：识别 {len(self.ocr_candidates)} 条；输入目标文字可高亮命中。"
                )

        except Exception as e:
            self.ocr_status_var.set(f"OCR 识别失败: {type(e).__name__}: {e}")
        finally:
            if self.ocr_window is not None:
                try:
                    self.ocr_window.config(cursor="")
                except Exception:
                    pass

    def refresh_ocr_matches(self, event=None):
        if not self.ocr_candidates:
            return

        target_text = self.ocr_target_var.get().strip()
        match_mode = self.ocr_match_mode_var.get().strip() or "smart"

        self.ocr_matches = match_ocr_candidates(
            self.ocr_candidates,
            target_text,
            mode=match_mode,
        ) if target_text else []

        self.populate_ocr_results()
        self.update_ocr_preview()

        if target_text:
            self.ocr_status_var.set(
                f"已重新匹配：识别 {len(self.ocr_candidates)} 条，命中 {len(self.ocr_matches)} 条。"
            )
        else:
            self.ocr_status_var.set(
                f"已清空匹配条件：当前 OCR 结果 {len(self.ocr_candidates)} 条。"
            )

    def update_ocr_preview(self):
        if self.ocr_preview_label is None:
            return

        if self.ocr_image is None:
            self.ocr_preview_label.configure(
                image="",
                text="选择图片或截取当前窗口后显示预览。",
            )
            self.ocr_preview_photo = None
            return

        preview = draw_ocr_preview(
            self.ocr_image,
            candidates=self.ocr_candidates,
            matches=self.ocr_matches,
        )
        self.ocr_preview_photo = make_photo_image(preview)
        self.ocr_preview_label.configure(image=self.ocr_preview_photo, text="")

    def populate_ocr_results(self):
        if self.ocr_result_tree is None:
            return

        self.ocr_result_tree.delete(*self.ocr_result_tree.get_children())
        match_ids = {id(item) for item in self.ocr_matches}

        for index, candidate in enumerate(self.ocr_candidates):
            bounds = candidate.get("abs_bounds") or candidate.get("bounds") or ""
            center = candidate.get("center") or ""
            confidence = candidate.get("confidence", 0)
            is_match = id(candidate) in match_ids

            if isinstance(bounds, tuple):
                bounds = ",".join(str(int(value)) for value in bounds)

            if isinstance(center, tuple):
                center = ",".join(str(int(value)) for value in center)

            try:
                confidence = f"{float(confidence):.3f}"
            except Exception:
                confidence = safe_to_string(confidence)

            self.ocr_result_tree.insert(
                "",
                "end",
                values=(
                    index,
                    "是" if is_match else "",
                    safe_to_string(candidate.get("text", ""), max_len=500),
                    confidence,
                    bounds,
                    center,
                ),
            )

    # =====================================================
    # 图片识别辅助工具
    # =====================================================
    def open_pic_tool(self):
        if self.pic_window is not None:
            try:
                if self.pic_window.winfo_exists():
                    self.pic_window.lift()
                    self.pic_window.focus_force()
                    return
            except Exception:
                pass

        self.pic_window = tk.Toplevel(self.app)
        self.pic_window.title("Image Locator Debugger")
        self.pic_window.geometry("920x680+150+100")
        self.pic_window.minsize(760, 520)
        self.pic_window.attributes("-topmost", True)
        self.pic_window.protocol("WM_DELETE_WINDOW", self.close_pic_tool)

        top_frame = tk.Frame(self.pic_window)
        top_frame.pack(fill="x", padx=8, pady=(8, 4))
        top_frame.columnconfigure(1, weight=1)

        tk.Button(
            top_frame,
            text="选择模板",
            width=9,
            command=self.choose_pic_template,
        ).grid(row=0, column=0, padx=(0, 4))

        tk.Entry(
            top_frame,
            textvariable=self.pic_template_path_var,
            font=("Consolas", 9),
            state="readonly",
        ).grid(row=0, column=1, sticky="ew", padx=4)

        tk.Label(top_frame, text="阈值：").grid(row=0, column=2, padx=(8, 0), sticky="e")
        tk.Entry(top_frame, textvariable=self.pic_threshold_var, width=6).grid(row=0, column=3, padx=4)

        tk.Label(top_frame, text="算法：").grid(row=0, column=4, padx=(8, 0), sticky="e")
        ttk.Combobox(
            top_frame,
            width=8,
            state="readonly",
            values=("auto", "mstpl", "tpl", "sift", "brisk", "akaze", "orb", "kaze", "gmstpl"),
            textvariable=self.pic_method_var,
        ).grid(row=0, column=5, padx=4)

        tk.Label(top_frame, text="pos：").grid(row=0, column=6, padx=(8, 0), sticky="e")
        ttk.Combobox(
            top_frame,
            width=4,
            state="readonly",
            values=("1", "2", "3", "4", "5", "6", "7", "8", "9"),
            textvariable=self.pic_pos_var,
        ).grid(row=0, column=7, padx=(4, 0))

        source_frame = tk.Frame(self.pic_window)
        source_frame.pack(fill="x", padx=8, pady=(4, 4))
        source_frame.columnconfigure(3, weight=1)

        tk.Button(
            source_frame,
            text="选择搜索图",
            width=10,
            command=self.choose_pic_source_image,
        ).grid(row=0, column=0, padx=(0, 4))

        tk.Button(
            source_frame,
            text="截取当前窗口",
            width=12,
            command=self.capture_pic_current_window,
        ).grid(row=0, column=1, padx=4)

        tk.Button(
            source_frame,
            text="测试匹配",
            width=9,
            command=self.run_pic_debug,
        ).grid(row=0, column=2, padx=4)

        tk.Label(source_frame, textvariable=self.pic_source_label_var, anchor="w").grid(row=0, column=3, sticky="ew", padx=4)

        preview_frame = tk.Frame(self.pic_window, borderwidth=1, relief="sunken")
        preview_frame.pack(fill="both", expand=True, padx=8, pady=4)

        self.pic_preview_label = tk.Label(
            preview_frame,
            text="选择搜索图或截取当前窗口后显示预览。",
            anchor="center",
            bg="#f3f3f3",
        )
        self.pic_preview_label.pack(fill="both", expand=True)

        result_frame = tk.Frame(self.pic_window)
        result_frame.pack(fill="x", padx=8, pady=4)
        result_frame.columnconfigure(1, weight=1)
        tk.Label(result_frame, text="匹配结果：").grid(row=0, column=0, sticky="w")
        tk.Entry(
            result_frame,
            textvariable=self.pic_result_value,
            font=("Consolas", 9),
            state="readonly",
        ).grid(row=0, column=1, sticky="ew", padx=4)

        tk.Label(self.pic_window, textvariable=self.pic_status_var, anchor="w").pack(fill="x", padx=8, pady=(0, 8))

        if self.pic_source_image is not None:
            self.update_pic_preview()

    def close_pic_tool(self):
        if self.pic_window is not None:
            try:
                self.pic_window.destroy()
            except Exception:
                pass
        self.pic_window = None
        self.pic_preview_label = None

    def choose_pic_template(self):
        file_path = filedialog.askopenfilename(
            parent=self.pic_window or self.app,
            title="选择图片模板",
            filetypes=(
                ("图片文件", "*.png;*.jpg;*.jpeg;*.bmp;*.webp"),
                ("所有文件", "*.*"),
            ),
        )
        if not file_path:
            return

        try:
            self.pic_template_image = read_pic_image(file_path)
            self.pic_template_path_var.set(file_path)
            self.pic_candidate = None
            self.pic_result_value.set("")
            self.pic_status_var.set("模板图已载入。")
        except Exception as e:
            self.pic_status_var.set(f"模板图载入失败: {type(e).__name__}: {e}")

    def choose_pic_source_image(self):
        file_path = filedialog.askopenfilename(
            parent=self.pic_window or self.app,
            title="选择搜索图",
            filetypes=(
                ("图片文件", "*.png;*.jpg;*.jpeg;*.bmp;*.webp"),
                ("所有文件", "*.*"),
            ),
        )
        if not file_path:
            return

        try:
            self.pic_source_image = read_pic_image(file_path)
            self.pic_source_monitor = {
                "left": 0,
                "top": 0,
                "width": self.pic_source_image.shape[1],
                "height": self.pic_source_image.shape[0],
            }
            self.pic_candidate = None
            self.pic_source_label_var.set(f"搜索图：{file_path}")
            self.pic_result_value.set("")
            self.pic_status_var.set("搜索图已载入，点击“测试匹配”。")
            self.update_pic_preview()
        except Exception as e:
            self.pic_status_var.set(f"搜索图载入失败: {type(e).__name__}: {e}")

    def capture_pic_current_window(self):
        if self.root_window is None:
            self.pic_status_var.set("请先在主窗口选择并切换一个软件窗口。")
            return

        hidden_tool_window = False
        try:
            if self.pic_window is not None and self.pic_window.winfo_exists():
                self.pic_window.withdraw()
                self.app.update_idletasks()
                self.app.update()
                hidden_tool_window = True

            root_obj = to_wrapper(self.root_window)
            rect = root_obj.rectangle()
            monitor = {
                "left": int(rect.left),
                "top": int(rect.top),
                "width": max(1, int(rect.right - rect.left)),
                "height": max(1, int(rect.bottom - rect.top)),
            }
            with mss.mss() as sct:
                screenshot = sct.grab(monitor)
                self.pic_source_image = cv2.cvtColor(np.asarray(screenshot), cv2.COLOR_BGRA2BGR)

            self.pic_source_monitor = monitor
            self.pic_candidate = None
            self.pic_source_label_var.set(f"当前窗口截图：{root_obj.window_text()} | {monitor}")
            self.pic_result_value.set("")
            self.pic_status_var.set("当前窗口截图已载入，点击“测试匹配”。")
            self.update_pic_preview()
        except Exception as e:
            self.pic_status_var.set(f"截取当前窗口失败: {type(e).__name__}: {e}")
        finally:
            if hidden_tool_window and self.pic_window is not None:
                try:
                    self.pic_window.deiconify()
                    self.pic_window.lift()
                    self.pic_window.focus_force()
                except Exception:
                    pass

    def run_pic_debug(self):
        if self.pic_template_image is None:
            self.pic_status_var.set("请先选择模板图。")
            return
        if self.pic_source_image is None:
            self.pic_status_var.set("请先选择搜索图或截取当前窗口。")
            return

        try:
            threshold = float(self.pic_threshold_var.get().strip() or "0.6")
            pos = int(self.pic_pos_var.get().strip() or "5")
            method = self.pic_method_var.get().strip().lower()
            if method == "auto":
                method = ""

            criteria = {
                "file": self.pic_template_path_var.get(),
                "threshold": threshold,
                "pos": pos,
                "method": method,
            }

            self.pic_candidate = match_pic_in_image(
                criteria,
                self.pic_source_image,
                monitor=self.pic_source_monitor,
                template_image=self.pic_template_image,
            )
            self.update_pic_preview()

            if self.pic_candidate:
                center = self.pic_candidate.get("center")
                confidence = self.pic_candidate.get("confidence", 0)
                bounds = self.pic_candidate.get("abs_bounds") or self.pic_candidate.get("bounds")
                result = f"score={float(confidence):.3f}; center={center}; bounds={bounds}; method={self.pic_candidate.get('method')}"
                self.pic_result_value.set(result)
                self.pic_status_var.set("图片匹配成功，已在预览图中标注命中区域。")
            else:
                self.pic_result_value.set("未命中")
                self.pic_status_var.set("图片匹配失败，可降低阈值或切换算法。")

        except Exception as e:
            self.pic_status_var.set(f"图片匹配失败: {type(e).__name__}: {e}")

    def update_pic_preview(self):
        if self.pic_preview_label is None:
            return

        if self.pic_source_image is None:
            self.pic_preview_label.configure(
                image="",
                text="选择搜索图或截取当前窗口后显示预览。",
            )
            self.pic_preview_photo = None
            return

        preview = draw_pic_preview(self.pic_source_image, self.pic_candidate)
        self.pic_preview_photo = make_photo_image(preview)
        self.pic_preview_label.configure(image=self.pic_preview_photo, text="")


    __all__ = ["VisualToolMixin"]
