from __future__ import annotations

from pathlib import Path
from typing import TypeVar, cast

import yaml

from autowork_core.common.compile import compile_window_locator_package
from autowork_core.page.singleton import BasePage
from autowork_core.utils.bus import normalize
from config.paths import Paths


TView = TypeVar("TView", bound="WindowView")


class WindowPage(BasePage):
    root_locator_file = None
    root_locator = None
    view_locator_files = None

    def __init__(self, context):
        self.ctx = context
        self._view_locator_files = []
        self._owned_view_locator_files = {}
        self._window_locators = {}
        self._views: dict[str, WindowView] = {}
        if not self.root_locator_file or not self.root_locator:
            raise TypeError(
                f"{type(self).__name__} 必须声明 root_locator_file 和 "
                "root_locator"
            )
        self.load_window_views(self.view_locator_files)
        for data_file in self._iter_resource_files(
            self.data_files,
            self.data_file,
        ):
            self.ctx.autowork_feature.data.load_data(data_file)

    @property
    def window_root_name(self):
        return normalize(str(self.root_locator))

    def wait_until_open(self, timeout=10):
        return self.wait_ready(f"${self.root_locator}", timeout=timeout)

    def load_window_views(self, locator_files=None):
        for file_name in self._iter_resource_files(locator_files, None):
            if file_name not in self._view_locator_files:
                self._view_locator_files.append(file_name)

        self._rebuild_window_locators()
        return self

    def load_owned_window_view(self, locator_file, root_locator):
        locator_file = str(locator_file)
        root_locator = normalize(str(root_locator))
        if not root_locator:
            raise TypeError("独立Root的WindowView必须声明root_locator")
        previous = self._owned_view_locator_files.get(locator_file)
        if previous is not None and previous != root_locator:
            raise ValueError(
                "同一WindowView locator文件不能绑定不同root_locator: "
                f"{locator_file}"
            )
        self._owned_view_locator_files[locator_file] = root_locator
        self._rebuild_window_locators()
        return self

    def _rebuild_window_locators(self):

        root_data = self._load_locator_file(self.root_locator_file)
        view_data = [
            self._load_locator_file(file_name)
            for file_name in self._view_locator_files
        ]
        package = compile_window_locator_package(
            root_data,
            view_data,
            package_name=self.root_locator_file,
        )
        if package.root_name != self.window_root_name:
            raise ValueError(
                f"{type(self).__name__} root_locator 不匹配: "
                f"declared={self.window_root_name}, "
                f"actual={package.root_name}"
            )
        locators = dict(package.locators)
        for file_name, expected_root in (
                self._owned_view_locator_files.items()
            ):
            owned = compile_window_locator_package(
                self._load_locator_file(file_name),
                package_name=file_name,
            )
            if owned.root_name != expected_root:
                raise ValueError(
                    "WindowView root_locator 不匹配: "
                    f"declared={expected_root}, actual={owned.root_name}"
                )
            duplicates = sorted(set(locators) & set(owned.locators))
            if duplicates:
                raise ValueError(
                    f"WindowPage/View locator名称冲突: {duplicates}"
                )
            locators.update(owned.locators)
        self._window_locators = locators

    def get_view(self, view_cls: type[TView]) -> TView:
        key = f"{view_cls.__module__}.{view_cls.__qualname__}"
        if key not in self._views:
            self._views[key] = view_cls(self)
        view = cast(TView, self._views[key])
        if view.active_locator:
            view.wait_until_active()
        return view

    def _require_locator(self, key, source=None):
        locator = self._window_locators.get(normalize(str(key)))
        if locator is None:
            raise KeyError(
                "窗口包 locator key 不存在: "
                f"{key} (引用: {source or key})"
            )
        return locator

    def get_visual_value(self, value):
        ref = self._parse_strict_ref(value)
        if not ref or ref["kind"] == "literal":
            return super().get_visual_value(value)
        if ref["kind"] == "loc":
            return self._require_locator(ref["key"], ref.get("source"))
        if ref["kind"] == "data":
            return self._require_data(ref["key"], ref.get("source"))

        locator = self._window_locators.get(normalize(ref["key"]))
        data = self.ctx.autowork_feature.data[ref["key"]]
        if locator is not None and data is not None:
            raise KeyError(
                f"严格引用同时命中窗口 locator 和 data: "
                f"{ref.get('source')}，请改用 $loc:{ref['key']} "
                f"或 $data:{ref['key']}"
            )
        if locator is not None:
            return locator
        if data is not None:
            return data
        raise KeyError(
            f"严格引用不存在: {ref['key']} "
            f"(引用: {ref.get('source')})"
        )

    @staticmethod
    def _load_locator_file(file_name):
        file_name = str(file_name)
        if not file_name.endswith((".yaml", ".yml")):
            file_name += ".yaml"
        path = (Paths.LOCATORS_DIR / file_name).resolve()
        try:
            path.relative_to(Paths.LOCATORS_DIR.resolve())
        except ValueError as error:
            raise ValueError(f"窗口 locator 文件越界: {file_name}") from error
        if not path.is_file():
            raise FileNotFoundError(f"窗口 locator 文件不存在: {path}")
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(value, dict):
            raise ValueError(f"窗口 locator YAML 必须是 mapping: {path}")
        return value

class WindowView(BasePage):
    locator_file = None
    active_locator = None
    root_locator = None

    def __init__(self, page):
        if not isinstance(page, WindowPage):
            raise TypeError("WindowView 必须由 WindowPage 创建")
        self.page = page
        self.ctx = page.ctx
        if self.locator_file:
            if self.root_locator:
                page.load_owned_window_view(
                    self.locator_file,
                    self.root_locator,
                )
            else:
                page.load_window_views(self.locator_file)

    @property
    def window_root_name(self):
        if self.root_locator:
            return normalize(str(self.root_locator))
        return self.page.window_root_name

    def wait_until_active(self, timeout=10):
        if not self.active_locator:
            raise TypeError(
                f"{type(self).__name__} 必须声明 active_locator"
            )
        return self.page.wait_visible(self.active_locator, timeout=timeout)

    def _require_locator(self, key, source=None):
        return self.page._require_locator(key, source)

    def _require_data(self, key, source=None):
        return self.page._require_data(key, source)

    def get_visual_value(self, value):
        return self.page.get_visual_value(value)