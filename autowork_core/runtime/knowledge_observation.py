from functools import wraps
from inspect import signature
from pathlib import Path
import sys

from autowork_core.common.compile import CompiledLocator
from autowork_core.utils.bus import normalize
from autowork_core.runtime.evidence.context import ACTIVE_ACTION as ACTIVE_KNOWLEDGE_ACTION
from autowork_core.runtime.evidence.models import captured_value, utc_now


MUTATING_ACTIONS = frozenset({
    "click", "double_click", "right_click", "focus", "bring_to_front",
    "minimize_window", "set_window_topmost", "unset_window_topmost", "send_to_back",
    "input_text", "send_text_keys", "remove_text", "clear_text", "expand_dropdown",
    "select_dropdown_option", "set_checked", "select_radio", "select_tab",
    "select_list_item", "select_tree_item", "set_tree_expanded", "set_slider_value",
    "click_ocr_relative", "click_pic_relative", "set_root", "scroll_to", "drag_by_offset",
})
READ_ACTIONS = frozenset({
    "get_text", "get_attr", "get_collection_items", "save_text", "save_attr",
    "get_ocr_text", "extract_ocr_regex", "get_ocr_relative_position", "get_pic_region",
    "get_pic_relative_position", "save_ocr_debug_image", "save_pic_debug_image",
})
WAIT_ACTIONS = frozenset({
    "wait_exists", "wait_not_exists", "wait_visible", "wait_enabled", "wait_ready", "wait_exposed",
    "wait_ocr_text_present", "wait_ocr_text_absent", "wait_pic_present", "wait_pic_absent",
})
ASSERT_ACTIONS = frozenset({
    "assert_exists", "assert_not_exists", "assert_visible", "assert_not_visible", "assert_enabled",
    "assert_disabled", "assert_text_equal", "assert_text_contains", "assert_text_not_contains",
    "assert_text_empty", "assert_attr_equal", "assert_attr_contains", "assert_collection_equal",
    "assert_ocr_contains", "assert_ocr_not_contains", "assert_pic_exists", "assert_pic_not_exists",
})
VARIABLE_ACTIONS = frozenset({"set_variable", "get_variable"})
OBSERVED_ACTIONS = MUTATING_ACTIONS | READ_ACTIONS | WAIT_ACTIONS | ASSERT_ACTIONS | VARIABLE_ACTIONS
FRAMEWORK_PAGE_METHODS = frozenset({
    "close", "load_resources", "load_window_views", "get_view",
    "get_locator", "get_data", "get_visual_value",
    "wait_until_open", "wait_until_active",
})


def page_owner(page):
    from config.paths import Paths

    window_page = page.__dict__.get("page", page)
    owner = {
        "page": f"{type(window_page).__module__}.{type(window_page).__qualname__}",
        "view": f"{type(page).__module__}.{type(page).__qualname__}",
        "root_locator": getattr(window_page, "root_locator", None),
        "root_locator_file": getattr(window_page, "root_locator_file", None),
        "locator_files": list(page._iter_resource_files(page.locator_files, page.locator_file)),
        "page_source": _class_source(type(window_page), Paths.BASE_DIR),
        "source": _class_source(type(page), Paths.BASE_DIR),
    }
    root_name = getattr(window_page, "window_root_name", None)
    if root_name:
        locator = window_page.__dict__.get("_window_locators", {}).get(root_name)
        owner["window_cache_name"] = getattr(locator, "root_cache_name", None) or root_name
        owner["window_locator"] = _window_capture_locator(locator)
    return owner


def _window_capture_locator(locator):
    if not isinstance(locator, CompiledLocator):
        return None
    root = locator.root_locator if locator.root_name else locator
    if not isinstance(root, CompiledLocator) or root.root_name or not isinstance(root.criteria, dict):
        return None
    return {
        "criteria": captured_value(root.criteria),
        "screen_selector": captured_value(root.screen_selector),
        "root_name": root.name,
    }


def _class_source(cls, project_root):
    source = getattr(sys.modules.get(cls.__module__), "__file__", None)
    if source:
        try:
            return Path(source).resolve().relative_to(project_root.resolve()).as_posix()
        except ValueError:
            pass
    return None


def observe_page_actions(page_class):
    for name in OBSERVED_ACTIONS:
        method = page_class.__dict__.get(name)
        if method is not None:
            setattr(page_class, name, _observed_method(name, method))
    for name in ("get_data", "get_locator", "get_visual_value"):
        method = page_class.__dict__.get(name)
        if method is not None:
            setattr(page_class, name, _observed_resolution(name, method))
    return page_class


def observe_subclass_actions(page_class):
    module_name = getattr(page_class, "__module__", "") or ""
    if not (module_name.startswith("Bdd.") or getattr(page_class, "observe_custom_actions", False)):
        return page_class
    for name, attr in list(page_class.__dict__.items()):
        if (
            name.startswith("_")
            or name in OBSERVED_ACTIONS
            or name in FRAMEWORK_PAGE_METHODS
            or isinstance(attr, (property, classmethod, staticmethod, type))
            or not callable(attr)
            or getattr(attr, "_autowork_observed", False)
        ):
            continue
        setattr(page_class, name, _observed_method(name, attr, custom_phases=()))
    return page_class


def remember_page_sources(context, page):
    run = getattr(context, "autowork_run", None)
    capture = getattr(run, "collector", None) or getattr(run, "knowledge_capture", None)
    if capture is None or getattr(capture, "current_step", None) is None or not getattr(capture, "detailed", True):
        return
    try:
        from config.paths import Paths

        owner = page_owner(page)
        sources = [source for source in (owner.get("page_source"), owner.get("source")) if source]
        resources = [owner.get("root_locator_file")] + owner["locator_files"]
        for resource in resources:
            if resource:
                resource_name = str(resource)
                resource_name = resource_name if resource_name.endswith((".yaml", ".yml")) else resource_name + ".yaml"
                sources.append("Bdd/locators/" + resource_name.replace("\\", "/"))
        for source in dict.fromkeys(sources):
            capture.remember_source(Paths.BASE_DIR / source)
    except Exception as error:
        capture.record_error(f"page_source:{type(page).__name__}", error)


def _observed_resolution(name, method):
    @wraps(method)
    def observed(*args, **kwargs):
        result = method(*args, **kwargs)
        active = ACTIVE_KNOWLEDGE_ACTION.get()
        if active is not None:
            capture, action = active
            try:
                if getattr(capture, "detailed", True):
                    action.setdefault("resolved_arguments", {})[name] = captured_value(result)
            except Exception as error:
                capture.record_error(f"resolve:{name}", error)
        return result

    return observed


def _observed_method(name, method, *, custom_phases=None):
    method_signature = signature(method)

    @wraps(method)
    def observed(page, *args, **kwargs):
        run = getattr(page.ctx, "autowork_run", None)
        capture = getattr(run, "collector", None) or getattr(run, "knowledge_capture", None)
        if capture is None or capture.current_step is None:
            return method(page, *args, **kwargs)
        try:
            bound = method_signature.bind(page, *args, **kwargs)
            bound.apply_defaults()
            arguments = {key: value for key, value in bound.arguments.items() if key != "self"}
            owner = page_owner(page)
            raw_target = arguments.get("locator_or_name", arguments.get("target", arguments.get("region")))
            if isinstance(raw_target, CompiledLocator):
                owner["window_cache_name"] = raw_target.root_cache_name or raw_target.root_name
                owner["window_locator"] = _window_capture_locator(raw_target)
            elif isinstance(raw_target, dict) and raw_target.get("root"):
                root_name = normalize(str(raw_target["root"]))
                if root_name != normalize(str(owner.get("root_locator") or "")):
                    owner["window_cache_name"] = root_name
                    owner.pop("window_locator", None)
        except Exception as error:
            capture.record_error(f"observe:{name}", error)
            return method(page, *args, **kwargs)
        from config.paths import Paths

        sources = [source for source in (owner.get("page_source"), owner.get("source")) if source]
        resources = [owner.get("root_locator_file")] + owner["locator_files"]
        for resource in resources:
            if resource:
                resource_name = str(resource)
                resource_name = resource_name if resource_name.endswith((".yaml", ".yml")) else resource_name + ".yaml"
                sources.append("Bdd/locators/" + resource_name.replace("\\", "/"))
        for source in dict.fromkeys(sources) if getattr(capture, "detailed", True) else ():
            try:
                capture.remember_source(Paths.BASE_DIR / source)
            except Exception as error:
                capture.record_error(f"source:{source}", error)
        owner["sources"] = sources
        if custom_phases is not None:
            phases = custom_phases
        else:
            phases = () if name in VARIABLE_ACTIONS else ("after",)
        return capture.invoke(name, lambda: method(page, *args, **kwargs), arguments=arguments, owner=owner, phases=phases)

    observed._autowork_observed = True
    return observed


def observe_target_resolution(method):
    @wraps(method)
    def observed(*args, **kwargs):
        result = method(*args, **kwargs)
        active = ACTIVE_KNOWLEDGE_ACTION.get()
        if active is not None:
            capture, action = active
            try:
                if not getattr(capture, "detailed", True):
                    action["target_resolution_count"] = action.get("target_resolution_count", 0) + 1
                    return result
                locator = args[1] if len(args) > 1 else kwargs.get("kwargs", kwargs.get("locator"))
                action["target_resolution_count"] = action.get("target_resolution_count", 0) + 1
                action["resolved_locator"] = captured_value(locator)
                action["resolved_target"] = captured_value(result)
                action["target_identity"] = _target_identity(result)
                if isinstance(locator, CompiledLocator) and locator.root_cache_name:
                    action["owner"]["window_cache_name"] = locator.root_cache_name
                collector = getattr(capture, "collector", capture)
                if hasattr(collector, "on_target_resolved"):
                    collector.on_target_resolved(action, result)
            except Exception as error:
                capture.record_error(f"target:{action['id']}", error)
        return result

    return observed


def _rect_dict(rect_or_bounds):
    if rect_or_bounds is None:
        return None
    try:
        if isinstance(rect_or_bounds, dict):
            left, top, right, bottom = (
                int(rect_or_bounds["left"]),
                int(rect_or_bounds["top"]),
                int(rect_or_bounds["right"]),
                int(rect_or_bounds["bottom"]),
            )
        elif isinstance(rect_or_bounds, (tuple, list)) and len(rect_or_bounds) >= 4:
            left, top, right, bottom = (int(v) for v in rect_or_bounds[:4])
        else:
            left_attr = getattr(rect_or_bounds, "left", None)
            top_attr = getattr(rect_or_bounds, "top", None)
            right_attr = getattr(rect_or_bounds, "right", None)
            bottom_attr = getattr(rect_or_bounds, "bottom", None)
            left = int(left_attr() if callable(left_attr) else left_attr)
            top = int(top_attr() if callable(top_attr) else top_attr)
            right = int(right_attr() if callable(right_attr) else right_attr)
            bottom = int(bottom_attr() if callable(bottom_attr) else bottom_attr)
        if right <= left or bottom <= top:
            return None
        return {"left": left, "top": top, "right": right, "bottom": bottom}
    except Exception:
        return None


def _target_identity(target):
    from autowork_core.common.target import ResolvedTarget
    from pywinauto.base_wrapper import BaseWrapper

    target = ResolvedTarget.from_legacy(target)
    identity = {"kind": target.kind, "coords": captured_value(target.coords)}
    if target.source is not None:
        identity["source"] = str(target.source)
    candidates = target.value if target.kind == "wrappers" else [target.value]
    identity["controls"] = []
    first_rect = None
    if isinstance(target.candidate, dict):
        first_rect = _rect_dict(target.candidate.get("abs_bounds"))
    for candidate in candidates:
        if not isinstance(candidate, BaseWrapper) and hasattr(candidate, "wrapper_object"):
            try:
                candidate = candidate.wrapper_object()
            except Exception:
                candidate = None
        if isinstance(candidate, BaseWrapper):
            info = getattr(candidate, "element_info", None)
            rect = None
            try:
                rect = _rect_dict(candidate.rectangle())
            except Exception:
                rect = _rect_dict(getattr(info, "rectangle", None))
            if first_rect is None and rect is not None:
                first_rect = rect
            ctrl_entry = {
                key: captured_value(getattr(info, key, None))
                for key in ("handle", "process_id", "automation_id", "control_type", "name", "class_name")
            }
            if rect is not None:
                ctrl_entry["rectangle"] = rect
            identity["controls"].append(ctrl_entry)
    if first_rect is not None:
        identity["rectangle"] = first_rect
    return identity


def capture_window_image(context, action, phase):
    import win32gui
    import win32process
    from io import BytesIO
    from PIL import Image
    from autowork_core.utils.screenshot import capture_screen_region

    cache_name = action["owner"].get("window_cache_name")
    windows = getattr(getattr(context, "autowork_scenario", None), "windows", None)
    entry = windows.get_entry(cache_name) if windows is not None and cache_name else None
    if entry is not None and entry.is_hot_handle():
        handle, process_id = int(entry.handle), entry.process_id
    else:
        locator = action["owner"].get("window_locator")
        if locator is None and entry is not None and entry.kind == "top" and entry.criteria:
            locator = {
                "criteria": entry.criteria, "screen_selector": entry.screen_selector, "root_name": entry.name,
            }
        if not locator:
            raise RuntimeError("Owning window is not confirmed in the existing window cache")
        from autowork_core.common.locator import find_window_for_observation

        window = find_window_for_observation(**locator)
        info = getattr(window, "element_info", None)
        handle = getattr(window, "handle", None) or getattr(info, "handle", None)
        process_id = getattr(info, "process_id", None)
        if not handle or process_id is None:
            raise RuntimeError("Observed window handle or process identity is missing")
        handle = int(handle)
    if not win32gui.IsWindow(handle) or win32gui.IsIconic(handle) or not win32gui.IsWindowVisible(handle):
        raise RuntimeError("Confirmed window is unavailable, hidden or minimized")
    actual_pid = win32process.GetWindowThreadProcessId(handle)[1]
    if process_id is None or actual_pid != process_id:
        raise RuntimeError("Confirmed window process identity is missing or changed")
    left, top, right, bottom = win32gui.GetWindowRect(handle)
    coords = (action.get("target_identity") or {}).get("coords")
    if coords and not (left <= coords[0] < right and top <= coords[1] < bottom):
        raise RuntimeError("Resolved coordinates are outside the confirmed owning window")
    requested = {"left": left, "top": top, "width": right - left, "height": bottom - top}
    captured_at = utc_now()
    pixels, region = capture_screen_region(requested)
    buffer = BytesIO()
    Image.fromarray(pixels[:, :, ::-1]).save(buffer, format="PNG")
    region = dict(region, requested_region=requested, window_handle=handle, process_id=actual_pid, captured_at=captured_at)
    return buffer.getvalue(), region