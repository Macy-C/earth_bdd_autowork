from typing import TypeVar, cast

from autowork_core.actions.base import (
    click, double_click, right_click,
    input_text, send_text_keys, remove_text, clear_text, focus)
from autowork_core.actions.assertions import (
    assert_exists,
    assert_not_exists,
    assert_visible,
    assert_not_visible,
    assert_enabled,
    assert_disabled,
    assert_text_equal,
    assert_text_contains,
    assert_text_not_contains,
    assert_text_empty,
    assert_attr_equal,
    assert_attr_contains,
    assert_collection_equal,)
from autowork_core.actions.element_actions import (
    get_attr,
    get_collection_items,
    get_text,
)
from autowork_core.actions.control_actions import (
    select_list_item,
    select_radio,
    select_tab,
    select_tree_item,
    set_checked,
    set_slider_value,
    set_tree_expanded,
)
from autowork_core.actions.dropdown_actions import expand_dropdown, select_dropdown_option
from autowork_core.actions.mouse_keys_actions import drag_by_offset, scroll_to
from autowork_core.actions.variable_actions import (
    get_variable as get_scenario_variable,
    save_attr as save_scenario_attr,
    save_text as save_scenario_text,
    set_variable as set_scenario_variable,
)
from autowork_core.actions.visual_actions import (
    assert_ocr_contains as visual_assert_ocr_contains,
    assert_ocr_not_contains as visual_assert_ocr_not_contains,
    assert_pic_exists as visual_assert_pic_exists,
    assert_pic_not_exists as visual_assert_pic_not_exists,
    click_ocr_relative as visual_click_ocr_relative,
    click_pic_relative as visual_click_pic_relative,
    extract_ocr_regex as visual_extract_ocr_regex,
    get_ocr_text as visual_get_ocr_text,
    get_ocr_relative_position as visual_get_ocr_relative_position,
    get_pic_relative_position as visual_get_pic_relative_position,
    get_pic_region as visual_get_pic_region,
    save_ocr_debug_image as visual_save_ocr_debug_image,
    save_pic_debug_image as visual_save_pic_debug_image,
    wait_ocr_text_absent as visual_wait_ocr_text_absent,
    wait_ocr_text_present as visual_wait_ocr_text_present,
    wait_pic_absent as visual_wait_pic_absent,
    wait_pic_present as visual_wait_pic_present,
)
from autowork_core.actions.wait_actions import (
    wait_enabled,
    wait_exposed,
    wait_exists,
    wait_not_exists,
    wait_ready,
    wait_visible,
)
from autowork_core.actions.window_actions import (
    bring_to_front,
    minimize_window,
    send_to_back,
    set_root,
    set_window_topmost,
    unset_window_topmost,
)


TPage = TypeVar("TPage")


def get_page(
    context,
    page_cls: type[TPage],
    *,
    refresh=False,
    cache=True,
) -> TPage:
    scenario = getattr(context, "autowork_scenario", None)
    page_dic = getattr(scenario, "pages", None)
    if page_dic is None:
        page_dic = context.autowork_feature.pages

    if not cache:
        return page_cls(context)

    key = f"{page_cls.__module__}.{page_cls.__qualname__}"
    if refresh or key not in page_dic:
        page_dic[key] = page_cls(context)
    return cast(TPage, page_dic[key])

class Singleton(object):
    def __init__(self, cls):
        self._cls = cls
        self.uniqueInstance = None

    def __call__(self,*args,**kwargs):
        if self.uniqueInstance is None:
            # with self._instance_lock:
            #     if self.uniqueInstance is None:
            self.uniqueInstance = self._cls(*args,**kwargs)
        return self.uniqueInstance

class BasePage:
    locator_file = None
    locator_files = None
    data_file = None
    data_files = None

    def __init__(self, context):
        self.ctx = context
        self.load_resources(
            locator_files=self.locator_files,
            locator_file=self.locator_file,
            data_files=self.data_files,
            data_file=self.data_file,
        )

    def load_resources(self, *, locator_files=None, locator_file=None, data_files=None, data_file=None):
        for locator_file in self._iter_resource_files(locator_files, locator_file):
            self.ctx.autowork_feature.locators.load_locator(locator_file)

        for data_file in self._iter_resource_files(data_files, data_file):
            self.ctx.autowork_feature.data.load_data(data_file)

        return self

    @staticmethod
    def _iter_resource_files(resource_files, resource_file):
        seen = set()
        for source in (resource_files, resource_file):
            if not source:
                continue
            if isinstance(source, (list, tuple)):
                candidates = source
            else:
                candidates = (source,)
            for file_name in candidates:
                if not file_name:
                    continue
                file_name = str(file_name)
                if file_name in seen:
                    continue
                seen.add(file_name)
                yield file_name

    # ========================================== click ===============================================
    def click(self, locator_or_name, offset_x=None, offset_y=None, wait_type="visible", wait_timeout=5, visual_timeout=10):
        locator = self.get_locator(locator_or_name)
        return click(self.ctx, locator, offset_x=offset_x, offset_y=offset_y, wait_type=wait_type, wait_timeout=wait_timeout, visual_timeout=visual_timeout)

    def double_click(self, locator_or_name, wait_type="visible", wait_timeout=5, visual_timeout=10):
        locator = self.get_locator(locator_or_name)
        return double_click(self.ctx, locator, wait_type=wait_type, wait_timeout=wait_timeout, visual_timeout=visual_timeout)

    def right_click(self, locator_or_name, wait_type="visible", wait_timeout=5, visual_timeout=10):
        locator = self.get_locator(locator_or_name)
        return right_click(self.ctx, locator, wait_type=wait_type, wait_timeout=wait_timeout, visual_timeout=visual_timeout)

    def focus(self,locator_or_name, wait_type="visible", wait_timeout=5, visual_timeout=10):
        locator = self.get_locator(locator_or_name)
        return focus(self.ctx,locator, wait_type=wait_type, wait_timeout=wait_timeout, visual_timeout=visual_timeout)

    def bring_to_front(self, locator_or_name, wait_type="exists", wait_timeout=5, visual_timeout=10,activate=False):
        locator = self.get_locator(locator_or_name)
        return bring_to_front(self.ctx, locator, wait_type=wait_type, wait_timeout=wait_timeout, visual_timeout=visual_timeout,activate=activate)

    def minimize_window(self, locator_or_name, wait_type="exists", wait_timeout=5, visual_timeout=10):
        locator = self.get_locator(locator_or_name)
        return minimize_window(self.ctx, locator, wait_type=wait_type, wait_timeout=wait_timeout, visual_timeout=visual_timeout)

    def set_window_topmost(self, locator_or_name, topmost=True, wait_type="exists", wait_timeout=5, visual_timeout=10):
        locator = self.get_locator(locator_or_name)
        return set_window_topmost(self.ctx, locator, topmost=topmost, wait_type=wait_type, wait_timeout=wait_timeout, visual_timeout=visual_timeout)

    def unset_window_topmost(self, locator_or_name, wait_type="exists", wait_timeout=5, visual_timeout=10):
        locator = self.get_locator(locator_or_name)
        return unset_window_topmost(self.ctx, locator, wait_type=wait_type, wait_timeout=wait_timeout, visual_timeout=visual_timeout)

    def send_to_back(self, locator_or_name, wait_type="exists", wait_timeout=5, visual_timeout=10):
        locator = self.get_locator(locator_or_name)
        return send_to_back(self.ctx, locator, wait_type=wait_type, wait_timeout=wait_timeout, visual_timeout=visual_timeout)
    # ========================================== click ===============================================

    # ========================================== edit_box ===============================================
    def input_text(self,locator_or_name,data_or_name,use_paste=False, wait_type="enabled", wait_timeout=5, visual_timeout=10):
        locator = self.get_locator(locator_or_name)
        data = self.get_data(data_or_name)
        return input_text(self.ctx,locator,data,use_paste=use_paste, wait_type=wait_type, wait_timeout=wait_timeout, visual_timeout=visual_timeout)

    def send_text_keys(self,locator_or_name,data_or_name, wait_type="enabled", wait_timeout=5, visual_timeout=10):
        locator = self.get_locator(locator_or_name)
        keys = self.get_data(data_or_name)
        return send_text_keys(self.ctx,locator,keys, wait_type=wait_type, wait_timeout=wait_timeout, visual_timeout=visual_timeout)

    def remove_text(self, locator_or_name, data_or_name, wait_type="enabled", wait_timeout=5, visual_timeout=10):
        locator = self.get_locator(locator_or_name)
        text = self.get_data(data_or_name)
        return remove_text(self.ctx, locator, text, wait_type=wait_type, wait_timeout=wait_timeout, visual_timeout=visual_timeout)

    def clear_text(self,locator_or_name, wait_type="enabled", wait_timeout=5, visual_timeout=10):
        locator = self.get_locator(locator_or_name)
        return clear_text(self.ctx,locator, wait_type=wait_type, wait_timeout=wait_timeout, visual_timeout=visual_timeout)

    # ========================================== edit_box ===============================================

    # ========================================== dropdown ===============================================
    def expand_dropdown(self, locator_or_name, timeout=5, interval=0.2, visual_timeout=10):
        locator = self.get_locator(locator_or_name)
        return expand_dropdown(
            self.ctx,
            locator,
            timeout=timeout,
            interval=interval,
            visual_timeout=visual_timeout,
        )

    def select_dropdown_option(self, locator_or_name, option_or_name, timeout=5, interval=0.2, visual_timeout=10):
        locator = self.get_locator(locator_or_name)
        option = self.get_data(option_or_name)
        return select_dropdown_option(
            self.ctx,
            locator,
            option,
            timeout=timeout,
            interval=interval,
            visual_timeout=visual_timeout,
        )
    # ========================================== dropdown ===============================================

    # ========================================== semantic controls =====================================
    def set_checked(self, locator_or_name, checked=True, timeout=5, interval=0.2, visual_timeout=10):
        locator = self.get_locator(locator_or_name)
        desired = self.get_data(checked)
        return set_checked(
            self.ctx,
            locator,
            desired,
            timeout=timeout,
            interval=interval,
            visual_timeout=visual_timeout,
        )

    def select_radio(self, locator_or_name, timeout=5, interval=0.2, visual_timeout=10):
        locator = self.get_locator(locator_or_name)
        return select_radio(
            self.ctx,
            locator,
            timeout=timeout,
            interval=interval,
            visual_timeout=visual_timeout,
        )

    def select_tab(self, locator_or_name, timeout=5, interval=0.2, visual_timeout=10):
        locator = self.get_locator(locator_or_name)
        return select_tab(
            self.ctx,
            locator,
            timeout=timeout,
            interval=interval,
            visual_timeout=visual_timeout,
        )

    def select_list_item(self, locator_or_name, timeout=5, interval=0.2, visual_timeout=10):
        locator = self.get_locator(locator_or_name)
        return select_list_item(
            self.ctx,
            locator,
            timeout=timeout,
            interval=interval,
            visual_timeout=visual_timeout,
        )

    def select_tree_item(self, locator_or_name, timeout=5, interval=0.2, visual_timeout=10):
        locator = self.get_locator(locator_or_name)
        return select_tree_item(
            self.ctx,
            locator,
            timeout=timeout,
            interval=interval,
            visual_timeout=visual_timeout,
        )

    def set_tree_expanded(self, locator_or_name, expanded=True, timeout=5, interval=0.2, visual_timeout=10):
        locator = self.get_locator(locator_or_name)
        desired = self.get_data(expanded)
        return set_tree_expanded(
            self.ctx,
            locator,
            desired,
            timeout=timeout,
            interval=interval,
            visual_timeout=visual_timeout,
        )

    def set_slider_value(self, locator_or_name, value_or_name, expected_minimum=None, expected_maximum=None, timeout=5, interval=0.2, visual_timeout=10):
        locator = self.get_locator(locator_or_name)
        value = self.get_data(value_or_name)
        return set_slider_value(
            self.ctx,
            locator,
            value,
            expected_minimum=expected_minimum,
            expected_maximum=expected_maximum,
            timeout=timeout,
            interval=interval,
            visual_timeout=visual_timeout,
        )
    # ========================================== semantic controls =====================================

    # ========================================== wait ===============================================
    def wait_exists(self, locator_or_name, timeout=10, interval=0.5):
        locator = self.get_locator(locator_or_name)
        return wait_exists(self.ctx, locator, timeout=timeout, interval=interval)

    def wait_not_exists(self, locator_or_name, timeout=10, interval=0.5):
        locator = self.get_locator(locator_or_name)
        return wait_not_exists(self.ctx, locator, timeout=timeout, interval=interval)

    def wait_visible(self, locator_or_name, timeout=10, interval=0.5):
        locator = self.get_locator(locator_or_name)
        return wait_visible(self.ctx, locator, timeout=timeout, interval=interval)

    def wait_enabled(self, locator_or_name, timeout=10, interval=0.5):
        locator = self.get_locator(locator_or_name)
        return wait_enabled(self.ctx, locator, timeout=timeout, interval=interval)

    def wait_ready(self, locator_or_name, timeout=10, interval=0.5):
        locator = self.get_locator(locator_or_name)
        return wait_ready(self.ctx, locator, timeout=timeout, interval=interval)

    def wait_exposed(self, locator_or_name, timeout=10, interval=0.5):
        locator = self.get_locator(locator_or_name)
        return wait_exposed(self.ctx, locator, timeout=timeout, interval=interval)
    # ========================================== wait ===============================================

    # ========================================== get_ele_info ===============================================
    def get_text(self,locator_or_name, first_only=True):
        locator = self.get_locator(locator_or_name)
        return get_text(self.ctx,locator, first_only=first_only)

    def get_attr(self, locator_or_name, attr_name, timeout=3, default=None, first_only=True):
        locator = self.get_locator(locator_or_name)
        return get_attr(self.ctx, locator, attr_name, timeout=timeout, default=default, first_only=first_only)

    def get_collection_items(self, locator_or_name, timeout=5, max_items=200):
        locator = self.get_locator(locator_or_name)
        return get_collection_items(
            self.ctx,
            locator,
            timeout=timeout,
            max_items=max_items,
        )

    def save_text(
            self,
            locator_or_name,
            variable_name,
            *,
            timeout=3,
            first_only=True,
            overwrite=False,
            allow_empty=False,
        ):
        locator = self.get_locator(locator_or_name)
        return save_scenario_text(
            self.ctx,
            locator,
            variable_name,
            timeout=timeout,
            first_only=first_only,
            overwrite=overwrite,
            allow_empty=allow_empty,
        )

    def save_attr(
            self,
            locator_or_name,
            attr_name,
            variable_name,
            *,
            timeout=3,
            default=None,
            first_only=True,
            overwrite=False,
            allow_empty=False,
        ):
        locator = self.get_locator(locator_or_name)
        return save_scenario_attr(
            self.ctx,
            locator,
            attr_name,
            variable_name,
            timeout=timeout,
            default=default,
            first_only=first_only,
            overwrite=overwrite,
            allow_empty=allow_empty,
        )
    # ========================================== get_ele_info ===============================================

    # ========================================== scenario_variable =========================================
    def set_variable(
            self,
            variable_name,
            value,
            *,
            overwrite=False,
            allow_empty=False,
        ):
        return set_scenario_variable(
            self.ctx,
            variable_name,
            value,
            overwrite=overwrite,
            allow_empty=allow_empty,
        )

    def get_variable(self, variable_name):
        return get_scenario_variable(self.ctx, variable_name)
    # ========================================== scenario_variable =========================================

    # ========================================== assert ===============================================
    def assert_exists(self, locator_or_name, timeout=5, msg=None):
        locator = self.get_locator(locator_or_name)
        return assert_exists(self.ctx, locator, timeout=timeout, msg=msg)

    def assert_not_exists(self, locator_or_name, timeout=2, msg=None):
        locator = self.get_locator(locator_or_name)
        return assert_not_exists(self.ctx, locator, timeout=timeout, msg=msg)

    def assert_visible(self, locator_or_name, timeout=5, msg=None):
        locator = self.get_locator(locator_or_name)
        return assert_visible(self.ctx, locator, timeout=timeout, msg=msg)

    def assert_not_visible(self, locator_or_name, timeout=2, msg=None):
        locator = self.get_locator(locator_or_name)
        return assert_not_visible(self.ctx, locator, timeout=timeout, msg=msg)

    def assert_enabled(self, locator_or_name, timeout=5, msg=None):
        locator = self.get_locator(locator_or_name)
        return assert_enabled(self.ctx, locator, timeout=timeout, msg=msg)

    def assert_disabled(self, locator_or_name, timeout=5, msg=None):
        locator = self.get_locator(locator_or_name)
        return assert_disabled(self.ctx, locator, timeout=timeout, msg=msg)

    def assert_text_equal(self, locator_or_name, expected, timeout=5, msg=None):
        locator = self.get_locator(locator_or_name)
        expected = self.get_data(expected)
        return assert_text_equal(self.ctx, locator, expected, timeout=timeout, msg=msg)

    def assert_text_contains(self, locator_or_name, expected, timeout=5, msg=None):
        locator = self.get_locator(locator_or_name)
        expected = self.get_data(expected)
        return assert_text_contains(self.ctx, locator, expected, timeout=timeout, msg=msg)

    def assert_text_not_contains(self, locator_or_name, expected, timeout=5, msg=None):
        locator = self.get_locator(locator_or_name)
        expected = self.get_data(expected)
        return assert_text_not_contains(self.ctx, locator, expected, timeout=timeout, msg=msg)

    def assert_text_empty(self, locator_or_name, timeout=5, msg=None):
        locator = self.get_locator(locator_or_name)
        return assert_text_empty(self.ctx, locator, timeout=timeout, msg=msg)

    def assert_attr_equal(self, locator_or_name, attr_name, expected, timeout=5, msg=None):
        locator = self.get_locator(locator_or_name)
        expected = self.get_data(expected)
        return assert_attr_equal(self.ctx, locator, attr_name, expected, timeout=timeout, msg=msg)

    def assert_attr_contains(self, locator_or_name, attr_name, expected, timeout=5, msg=None):
        locator = self.get_locator(locator_or_name)
        expected = self.get_data(expected)
        return assert_attr_contains(self.ctx, locator, attr_name, expected, timeout=timeout, msg=msg)

    def assert_collection_equal(
            self,
            locator_or_name,
            expected,
            timeout=5,
            max_items=200,
            msg=None,
        ):
        locator = self.get_locator(locator_or_name)
        expected = self.get_data(expected)
        return assert_collection_equal(
            self.ctx,
            locator,
            expected,
            timeout=timeout,
            max_items=max_items,
            msg=msg,
        )
    # ========================================== assert ===============================================

    # ========================================== visual ===============================================
    def get_ocr_text(self, region=None, timeout=None, joiner=" ", use_cache=False):
        region = self.get_locator(region) if region else None
        return visual_get_ocr_text(
            self.ctx,
            region=region,
            timeout=timeout,
            joiner=joiner,
            use_cache=use_cache,
        )

    def extract_ocr_regex(
            self,
            pattern,
            region=None,
            timeout=None,
            flags=None,
            required=True,
            joiner=" ",
            use_cache=False,
    ):
        pattern = self.get_visual_value(pattern)
        region = self.get_locator(region) if region else None
        kwargs = {
            "region": region,
            "timeout": timeout,
            "required": required,
            "joiner": joiner,
            "use_cache": use_cache,
        }
        if flags is not None:
            kwargs["flags"] = flags
        return visual_extract_ocr_regex(self.ctx, pattern, **kwargs)

    def wait_ocr_text_present(self, text, timeout=None, interval=None, region=None):
        text = self.get_visual_value(text)
        region = self.get_locator(region) if region else None
        return visual_wait_ocr_text_present(self.ctx, text, timeout=timeout, interval=interval, region=region)

    def wait_ocr_text_absent(self, text, timeout=None, interval=None, region=None):
        text = self.get_visual_value(text)
        region = self.get_locator(region) if region else None
        return visual_wait_ocr_text_absent(self.ctx, text, timeout=timeout, interval=interval, region=region)

    def assert_ocr_contains(self, text, timeout=None, region=None, msg=None):
        text = self.get_visual_value(text)
        region = self.get_locator(region) if region else None
        return visual_assert_ocr_contains(self.ctx, text, timeout=timeout, region=region, msg=msg)

    def assert_ocr_not_contains(self, text, timeout=None, region=None, msg=None):
        text = self.get_visual_value(text)
        region = self.get_locator(region) if region else None
        return visual_assert_ocr_not_contains(self.ctx, text, timeout=timeout, region=region, msg=msg)

    def get_ocr_relative_position(self, text, direction="right", offset=20, timeout=None, region=None):
        text = self.get_visual_value(text)
        region = self.get_locator(region) if region else None
        return visual_get_ocr_relative_position(
            self.ctx,
            text,
            direction=direction,
            offset=offset,
            timeout=timeout,
            region=region,
        )

    def click_ocr_relative(self, text, direction="right", offset=20, timeout=None, region=None):
        text = self.get_visual_value(text)
        region = self.get_locator(region) if region else None
        return visual_click_ocr_relative(
            self.ctx,
            text,
            direction=direction,
            offset=offset,
            timeout=timeout,
            region=region,
        )

    def save_ocr_debug_image(self, text=None):
        text = self.get_data(text) if text else None
        return visual_save_ocr_debug_image(self.ctx, target_text=text)

    def wait_pic_present(self, pic_or_name, timeout=None, interval=None, region=None):
        criteria = self.get_locator(pic_or_name)
        region = self.get_locator(region) if region else None
        return visual_wait_pic_present(self.ctx, criteria, timeout=timeout, interval=interval, region=region)

    def wait_pic_absent(self, pic_or_name, timeout=None, interval=None, region=None):
        criteria = self.get_locator(pic_or_name)
        region = self.get_locator(region) if region else None
        return visual_wait_pic_absent(self.ctx, criteria, timeout=timeout, interval=interval, region=region)

    def assert_pic_exists(self, pic_or_name, timeout=None, region=None, msg=None):
        criteria = self.get_locator(pic_or_name)
        region = self.get_locator(region) if region else None
        return visual_assert_pic_exists(self.ctx, criteria, timeout=timeout, region=region, msg=msg)

    def assert_pic_not_exists(self, pic_or_name, timeout=None, region=None, msg=None):
        criteria = self.get_locator(pic_or_name)
        region = self.get_locator(region) if region else None
        return visual_assert_pic_not_exists(self.ctx, criteria, timeout=timeout, region=region, msg=msg)

    def get_pic_region(self, pic_or_name, timeout=None, region=None, padding=0):
        criteria = self.get_locator(pic_or_name)
        region = self.get_locator(region) if region else None
        return visual_get_pic_region(self.ctx, criteria, timeout=timeout, region=region, padding=padding)

    def get_pic_relative_position(self, pic_or_name, direction="right", offset=20, timeout=None, region=None):
        criteria = self.get_locator(pic_or_name)
        region = self.get_locator(region) if region else None
        return visual_get_pic_relative_position(
            self.ctx,
            criteria,
            direction=direction,
            offset=offset,
            timeout=timeout,
            region=region,
        )

    def click_pic_relative(self, pic_or_name, direction="right", offset=20, timeout=None, region=None):
        criteria = self.get_locator(pic_or_name)
        region = self.get_locator(region) if region else None
        return visual_click_pic_relative(
            self.ctx,
            criteria,
            direction=direction,
            offset=offset,
            timeout=timeout,
            region=region,
        )

    def save_pic_debug_image(self, pic_or_name):
        criteria = self.get_locator(pic_or_name)
        return visual_save_pic_debug_image(self.ctx, criteria)
    # ========================================== visual ===============================================

    # ========================================== other ===============================================
    def set_root(self,root,name=None):
        set_root(self.ctx,root,name=name)

    def scroll_to(self,target=None,direction='down',steps=1, wait_type="visible", wait_timeout=5, visual_timeout=10):
        target = self.get_locator(target)
        scroll_to(
            self.ctx,
            target=target,
            direction=direction,
            steps=steps,
            wait_type=wait_type,
            wait_timeout=wait_timeout,
            visual_timeout=visual_timeout,
        )

    def drag_by_offset(self, target, delta_x, delta_y, wait_type="enabled", wait_timeout=5, visual_timeout=10):
        target = self.get_locator(target)
        return drag_by_offset(
            self.ctx,
            target,
            delta_x,
            delta_y,
            wait_type=wait_type,
            wait_timeout=wait_timeout,
            visual_timeout=visual_timeout,
        )

    @staticmethod
    def _parse_strict_ref(value):
        if not isinstance(value, str) or not value.startswith("$"):
            return None
        if value.startswith("$$"):
            return {"kind": "literal", "value": value[1:]}

        raw = value[1:]
        if not raw:
            return {"kind": "literal", "value": value}

        prefix, separator, key = raw.partition(":")
        prefix = prefix.strip().lower()
        if separator and prefix in ("loc", "locator"):
            key = key.strip()
            if not key:
                raise ValueError(f"严格 locator 引用缺少 key: {value}")
            return {"kind": "loc", "key": key, "source": value}
        if separator and prefix == "data":
            key = key.strip()
            if not key:
                raise ValueError(f"严格 data 引用缺少 key: {value}")
            return {"kind": "data", "key": key, "source": value}
        return {"kind": "auto", "key": raw.strip(), "source": value}

    def _require_locator(self, key, source=None):
        locator = self.ctx.autowork_feature.locators[key]
        if locator is None:
            raise KeyError(f"locator key 不存在: {key} (引用: {source or key})")
        return locator

    def _require_data(self, key, source=None):
        data = self.ctx.autowork_feature.data[key]
        if data is None:
            raise KeyError(f"data key 不存在: {key} (引用: {source or key})")
        return data

    def get_locator(self, locator_or_name):
        ref = self._parse_strict_ref(locator_or_name)
        if ref:
            if ref["kind"] == "literal":
                return ref["value"]
            if ref["kind"] in ("auto", "loc"):
                return self._require_locator(ref["key"], ref.get("source"))
            raise TypeError(f"locator 参数不支持 data 引用: {ref.get('source')}")
        return locator_or_name

    def get_visual_value(self, value):
        ref = self._parse_strict_ref(value)
        if ref:
            if ref["kind"] == "literal":
                return ref["value"]
            if ref["kind"] == "loc":
                return self._require_locator(ref["key"], ref.get("source"))
            if ref["kind"] == "data":
                return self._require_data(ref["key"], ref.get("source"))

            locator = self.ctx.autowork_feature.locators[ref["key"]]
            data = self.ctx.autowork_feature.data[ref["key"]]
            if locator is not None and data is not None:
                raise KeyError(
                    f"严格引用同时命中 locator 和 data: {ref.get('source')}，"
                    f"请改用 $loc:{ref['key']} 或 $data:{ref['key']}"
                )
            if locator is not None:
                return locator
            if data is not None:
                return data
            raise KeyError(f"严格引用不存在: {ref['key']} (引用: {ref.get('source')})")

        return value

    def get_data(self, data_or_name):
        ref = self._parse_strict_ref(data_or_name)
        if ref:
            if ref["kind"] == "literal":
                return ref["value"]
            if ref["kind"] in ("auto", "data"):
                return self._require_data(ref["key"], ref.get("source"))
            raise TypeError(f"data 参数不支持 locator 引用: {ref.get('source')}")

        return data_or_name
    # ========================================== other ===============================================


class ScriptPage(BasePage):
    def __init__(self, context):
        self._loaded_locator_files = set()
        self._loaded_data_files = set()
        super().__init__(context)

    def load_resources(self, *, locator_files=None, locator_file=None, data_files=None, data_file=None):
        for file_name in self._iter_resource_files(locator_files, locator_file):
            if file_name in self._loaded_locator_files:
                continue
            self.ctx.autowork_feature.locators.load_locator(file_name)
            self._loaded_locator_files.add(file_name)

        for file_name in self._iter_resource_files(data_files, data_file):
            if file_name in self._loaded_data_files:
                continue
            self.ctx.autowork_feature.data.load_data(file_name)
            self._loaded_data_files.add(file_name)

        return self

    def clear_loaded_resources(self):
        self._loaded_locator_files.clear()
        self._loaded_data_files.clear()
        return self


def get_script_page(context, *, locator_files=None, locator_file=None, data_files=None, data_file=None, refresh=False):
    page = get_page(context, ScriptPage, refresh=refresh)
    if refresh:
        page.clear_loaded_resources()
    return page.load_resources(
        locator_files=locator_files,
        locator_file=locator_file,
        data_files=data_files,
        data_file=data_file,
    )



