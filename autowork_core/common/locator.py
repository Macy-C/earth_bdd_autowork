from loguru import logger
from pywinauto import Desktop, findwindows
import re
from time import monotonic, sleep
from autowork_core.common.compile import (
    CompiledLocator,
    _get_locator_separator_index,
    compile_locator,
    normalize_child_criteria,
    parse_pos_coordinates,
)
from autowork_core.common.element_properties import read_accessible_name
from autowork_core.common.log_helper import log_call
from autowork_core.common.ocr_engine import find_ocr_text
from autowork_core.common.pic_engine import find_pic
from autowork_core.common.probe import ProbeResult
from autowork_core.common.root_store import RootEntry, RootResolveResult
from autowork_core.common.runtime_diagnostics import (
    RuntimeDiagnostic,
    attach_runtime_diagnostic,
    runtime_diagnostic_from_exception,
)
from autowork_core.common.wait_coordinator import poll_value
from autowork_core.common.wait_policy import WaitPolicy, normalize_wait_state
from autowork_core.common.winauto_xpath import find_by_xpath
from autowork_core.utils.visual_marker import mark_visual_target
from config.settings import settings
from pywinauto.base_wrapper import BaseWrapper
from pywinauto.findwindows import ElementAmbiguousError


def _find_by_default(root, kwargs, control_type, timeout, first_only=True, monitor=None):
    """
    Legacy 默认定位策略：
    1. 优先 auto_id
    2. 再 Accessible Name 精确匹配

    隐式 OCR fallback 已退役；视觉定位必须显式声明 OCR 和 region。
    """
    logger.warning(
        "default locator 已标记 legacy，请迁移到命名 locator 或显式 by；"
        "criteria={}",
        kwargs,
    )

    # 1. auto_id 精确匹配
    tmp_kwargs = {"auto_id": kwargs}
    ele = _find_by_child_window(root, tmp_kwargs, first_only, control_type)
    if _has_target(ele, timeout=1):
        logger.debug("legacy default locator 命中 auto_id | criteria={}", kwargs)
        return ele

    # 2. Accessible Name 精确匹配
    tmp_kwargs = {"title": kwargs}
    ele = _find_by_child_window(root, tmp_kwargs, first_only, control_type)
    if _has_target(ele, timeout=1):
        logger.debug(
            "legacy default locator 命中 Accessible Name | criteria={}",
            kwargs,
        )
        return ele

    raise LookupError(
        f"默认结构定位未找到元素: {kwargs}；隐式 OCR fallback 已退役，"
        "请使用命名 locator，或显式声明 by: ocr 和 region"
    )


def _pywinauto_name_criteria(criteria):
    native = dict(criteria)
    title = native.pop("title", None)
    title_re = native.pop("title_re", None)
    if title is None and title_re is None:
        return native

    existing_predicate = native.pop("predicate_func", None)
    title_pattern = _compile_match_pattern(title_re)

    def matches_name(element):
        if existing_predicate is not None and not existing_predicate(element):
            return False
        actual = _element_name(element)
        if title is not None and str(actual) != str(title):
            return False
        return title_pattern is None or title_pattern.match(str(actual)) is not None

    native["predicate_func"] = matches_name
    return native


def _find_by_child_window(root, kwargs, first_only, control_type, timeout=0, interval=0.2, wait_type="exists"):
    """
    child 定位：
    - first_only=True: 返回 WindowSpecification
    - first_only=False: 返回匹配到的 wrapper 列表
    """
    if isinstance(kwargs, dict):
        kwargs = normalize_child_criteria(kwargs)
    elif isinstance(kwargs, str):
        index = _get_locator_separator_index(kwargs)
        kwargs = normalize_child_criteria({kwargs[:index]:kwargs[index+1:]})
    else:
        raise RuntimeError(f'未知的参数类型: {kwargs}')

    if control_type and "control_type" not in kwargs:
        kwargs["control_type"] = control_type

    if isinstance(root, BaseWrapper):
        return _find_child_from_wrapper_root(root, kwargs, first_only, wait_type, timeout, interval)

    if first_only:
        return root.child_window(**_pywinauto_name_criteria(kwargs))

    if hasattr(root, "wrapper_object"):
        root = root.wrapper_object()
    return _by_descendants_get_elements(root, kwargs)


def _has_target(target, timeout=0):
    if target is None:
        return False
    if isinstance(target, list):
        return bool(target)
    if isinstance(target, tuple):
        return True
    if isinstance(target, BaseWrapper):
        return True
    if hasattr(target, "exists"):
        return target.exists(timeout=timeout)
    return bool(target)


def _matches_wait_type(target, wait_type):
    return _wrapper_matches_wait_type(target, wait_type)


class _ChildWaitExpired(TimeoutError):
    pass


class _XPathWaitExpired(TimeoutError):
    pass


def _find_child_from_wrapper_root(root, kwargs, first_only, wait_type, timeout, interval):
    last_candidate_count = 0

    def probe():
        nonlocal last_candidate_count
        candidates = _by_descendants_get_elements(root, dict(kwargs))
        last_candidate_count = len(candidates)
        if first_only and len(candidates) > 1:
            error = ElementAmbiguousError(
                f"Child locator 匹配到 {len(candidates)} 个元素: {kwargs}"
            )
            error.elements = candidates
            raise error
        ready = [candidate for candidate in candidates if _matches_wait_type(candidate, wait_type)]

        if ready:
            return ready[0] if first_only else ready
        return None if first_only else []

    try:
        return poll_value(
            probe,
            (
                (lambda value: value is not None)
                if first_only
                else bool
            ),
            timeout=timeout,
            interval=interval,
            timeout_message="Child locator 未找到匹配状态的元素",
            timeout_error_type=_ChildWaitExpired,
            fatal_errors=(Exception,),
            monotonic=monotonic,
            sleep=sleep,
        )
    except _ChildWaitExpired as wait_error:
        state = _normalize_wait_type(wait_type)
        if last_candidate_count and state not in {"none", "exists"}:
            error = TimeoutError(
                f"已找到 {last_candidate_count} 个候选，但未在 "
                f"{timeout} 秒内达到 {state} 状态"
            )
            attach_runtime_diagnostic(error, RuntimeDiagnostic(
                code="CHILD_STATE_TIMEOUT",
                category="state_timeout",
                stage="wait_state",
                summary=str(error),
                backend="child",
                wait_type=state,
                timeout_seconds=float(timeout),
                interval_seconds=float(interval),
                probe_count=getattr(wait_error, "probe_count", None),
                candidate_count=last_candidate_count,
                last_state=state,
            ))
            raise error from wait_error
        return None if first_only else []


def _element_name(element):
    return read_accessible_name(element)


def _compile_match_pattern(value):
    if value is None:
        return None
    return value if isinstance(value, re.Pattern) else re.compile(str(value))


def _regex_matches(pattern, value):
    compiled = _compile_match_pattern(pattern)
    return compiled is not None and compiled.match(str(value)) is not None


def _to_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off", "none")
    return bool(value)


def _by_descendants_get_elements(root, kwargs):
    """
    在 BaseWrapper root 下执行 child_window 等价查找，返回 wrapper 列表。

    wrapper root 已经是解析后的容器，这里只通过 root.descendants()
    获取 wrapper，再做 child locator 需要的条件过滤，保证返回值仍可被
    wait/click/text 直接使用。
    """
    kwargs = dict(kwargs or {})
    if not hasattr(root, "descendants"):
        raise AttributeError(f"root 不支持 descendants(): {root!r}")

    descendant_kwargs = {}
    for key in ("control_type", "class_name", "process", "depth"):
        if key in kwargs and kwargs[key] is not None:
            descendant_kwargs[key] = kwargs[key]

    candidates = root.descendants(**descendant_kwargs)

    result = [element for element in candidates if _element_matches_criteria(element, kwargs)]

    return result


def _find_by_pos(kwargs):
    run_width, run_height = settings.desktop_size
    write_x, write_y, write_width, write_height = parse_pos_coordinates(kwargs)
    run_x = write_x / write_width * run_width
    run_y = write_y / write_height * run_height
    return (int(run_x), int(run_y)), 'pos'


def _find_by_pic(kwargs, timeout, pos=5, monitor=None):
    """
    Click on different positions of the target image, and the default is the center point 5
    1 2 3
    4 5 6
    7 8 9
    """
    candidate = find_pic(kwargs, timeout=timeout, monitor=monitor)
    if candidate:
        x, y = candidate["center"]
        logger.debug("____find_by_pic | criteria={} | candidate={}", kwargs, candidate)
        return (int(x), int(y)), 'pic', candidate

    logger.error("[_find_by_pic] 图片未找到 | criteria={}", kwargs)
    return None


def _find_by_ocr(kwargs, timeout, monitor=None, debug_on_fail=None):
    if monitor is not None:
        monitor = {
            "left": max(0, monitor["left"] - 20),
            "top": max(0, monitor["top"] - 10),
            "width": monitor["width"] + 40,
            "height": monitor["height"] + 20,
        }

    candidate = find_ocr_text(
        kwargs,
        timeout=timeout,
        monitor=monitor,
        default_index=-1,
        default_match_mode="contains",
        debug_on_fail=debug_on_fail,
    )
    if candidate is not None:
        x, y = candidate["center"]
        logger.debug("____find_by_ocr | criteria={} | candidate={}", kwargs, candidate)
        return (int(x), int(y)), 'ocr', candidate

    logger.error("[_find_by_ocr] OCR 未找到 | criteria={}", kwargs)


def _find_by_xpath(root, xpath, first_only):
    return find_by_xpath(root,xpath,first_only)


def is_transient_lookup_error(error):
    return type(error) is LookupError


def _probe_xpath(root_result, criteria, first_only):
    try:
        value = _find_by_xpath(
            root_result.root,
            criteria,
            first_only,
        )
    except LookupError as error:
        if not is_transient_lookup_error(error):
            raise
        return ProbeResult.from_legacy(
            "xpath",
            None,
            root_result=root_result,
            error=error,
        )
    return ProbeResult.from_legacy(
        "xpath",
        value,
        root_result=root_result,
    )


def _normalize_wait_type(wait_type):
    return normalize_wait_state(wait_type)


def _wrapper_matches_wait_type(wrapper, wait_type):
    wait_type = _normalize_wait_type(wait_type)
    if wait_type in {"none", "exists"}:
        return True
    if wait_type == "visible":
        return bool(wrapper.is_visible())
    if wait_type == "enabled":
        return bool(wrapper.is_enabled())
    if wait_type == "ready":
        return bool(wrapper.is_visible()) and bool(wrapper.is_enabled())
    return True


def _wait_wrapper(wrapper, wait_type):
    wait_type = _normalize_wait_type(wait_type)
    if _wrapper_matches_wait_type(wrapper, wait_type):
        return True
    message = {
        "visible": "元素不可见",
        "enabled": "元素不可用",
        "ready": "元素未 ready",
    }.get(wait_type)
    if message is not None:
        raise TimeoutError(message)
    return True


def _wait_spec(spec, timeout, wait_type):
    if wait_type == "exists":
        if not spec.exists(timeout=timeout):
            raise TimeoutError("元素不存在")
        return True
    spec.wait(wait_type, timeout)
    return True


def _wait_target(target, timeout=5, wait_type="ready", first_only=True):
    wait_type = _normalize_wait_type(wait_type)
    if wait_type == "none" or target is None:
        return True
    if isinstance(target, tuple):
        return True
    if isinstance(target, list):
        if wait_type == "exists" and not target:
            raise TimeoutError("元素列表为空")
        for item in target:
            _wait_target(item, timeout=timeout, wait_type=wait_type, first_only=True)
        return True
    if isinstance(target, BaseWrapper):
        return _wait_wrapper(target, wait_type)
    if hasattr(target, "exists"):
        return _wait_spec(target, timeout, wait_type)
    return True


def _wait_for_xpath_target(
        context,
        compiled,
        first_only,
        wait_type,
        wait_timeout,
        required,
        interval=0.2,
):
    policy = WaitPolicy.from_legacy(
        wait_type,
        wait_timeout,
        interval=interval,
        required=required,
    )
    def probe():
        root_result = _switch_root(context, compiled)
        result = _probe_xpath(
            root_result,
            compiled.criteria,
            first_only,
        )
        if result.error is not None:
            return result
        try:
            _wait_target(
                result.legacy_value,
                timeout=0,
                wait_type=policy.state,
                first_only=first_only,
            )
        except TimeoutError as error:
            return ProbeResult.from_legacy(
                "xpath",
                result.legacy_value,
                root_result=result.root_result,
                error=error,
            )
        return result

    try:
        result = poll_value(
            probe,
            lambda current: current.error is None,
            timeout=policy.timeout,
            interval=policy.interval,
            timeout_message=(
                f"未在 {wait_timeout} 秒内找到元素: "
                f"{compiled.name or compiled.criteria}"
            ),
            timeout_error_type=_XPathWaitExpired,
            fatal_errors=(Exception,),
            monotonic=monotonic,
            sleep=sleep,
        )
    except _XPathWaitExpired as wait_error:
        last_probe = wait_error.last_value
        root_result = last_probe.root_result
        if not policy.required:
            return (None if first_only else []), root_result
        if isinstance(last_probe.error, TimeoutError):
            root_result.mark_stale_if_hot(_root_spec_from_criteria)
        if last_probe.error is not None:
            code = (
                "XPATH_STATE_TIMEOUT"
                if isinstance(last_probe.error, TimeoutError)
                else "XPATH_NOT_FOUND"
                if type(last_probe.error) is LookupError
                else "XPATH_BACKEND_ERROR"
            )
            category = (
                "state_timeout"
                if code == "XPATH_STATE_TIMEOUT"
                else "not_found"
                if code == "XPATH_NOT_FOUND"
                else "backend_error"
            )
            attach_runtime_diagnostic(
                last_probe.error,
                RuntimeDiagnostic(
                    code=code,
                    category=category,
                    stage=(
                        "wait_state"
                        if code == "XPATH_STATE_TIMEOUT"
                        else "locate_xpath"
                    ),
                    summary=(
                        f"未在 {wait_timeout} 秒内通过 XPath 定位并达到 "
                        f"{policy.state} 状态"
                    ),
                    backend="xpath",
                    locator_name=compiled.name,
                    locator_kind="xpath",
                    root_name=compiled.root_name,
                    root_state=_root_result_state(root_result),
                    wait_type=policy.state,
                    timeout_seconds=policy.timeout,
                    interval_seconds=policy.interval,
                    probe_count=getattr(wait_error, "probe_count", None),
                    candidate_count=_target_count(last_probe.legacy_value),
                ),
            )
            raise last_probe.error
        error = LookupError(str(wait_error))
        attach_runtime_diagnostic(error, RuntimeDiagnostic(
            code="XPATH_NOT_FOUND",
            category="not_found",
            stage="locate_xpath",
            summary=str(wait_error),
            backend="xpath",
            locator_name=compiled.name,
            locator_kind="xpath",
            root_name=compiled.root_name,
            root_state=_root_result_state(root_result),
            wait_type=policy.state,
            timeout_seconds=policy.timeout,
            interval_seconds=policy.interval,
            probe_count=getattr(wait_error, "probe_count", None),
            candidate_count=0,
        ))
        raise error

    return result.legacy_value, result.root_result


def _to_wrapper(element,only):
    """
    WindowSpecification -> Wrapper
    Wrapper -> Wrapper
    """
    if only or not isinstance(element, list):
        if hasattr(element, "wrapper_object"):
            return element.wrapper_object()
        return element
    else:
        tmp_list = []
        for ele in element:
            if hasattr(ele, "wrapper_object"):
                tmp_list.append(ele.wrapper_object())
            else:
                tmp_list.append(ele)
        return tmp_list


def _draw_outline(target, first_only=True):
    outline_target = _to_wrapper(target, only=first_only)
    targets = outline_target if isinstance(outline_target, list) else [outline_target]
    for item in targets:
        item.draw_outline(colour='blue', thickness=5)

def _root_spec_from_handle(entry):
    if entry and entry.handle:
        return Desktop(backend=entry.backend).window(handle=entry.handle)
    return None


def _root_spec_from_criteria(entry):
    if entry is None:
        return None
    return Desktop(backend=entry.backend).window(
        **_pywinauto_name_criteria(entry.criteria)
    )


def _native_uia_window_bridge_root(backend, criteria):
    if (
        str(backend).casefold() != "uia"
        or str(criteria.get("control_type") or "").casefold()
        != "window"
        or not str(criteria.get("class_name") or "").strip()
    ):
        return None

    try:
        handles = findwindows.find_windows(
            class_name=str(criteria["class_name"]),
            visible_only=_to_bool(criteria.get("visible_only", True)),
        )
    except Exception:
        return None

    identity_criteria = dict(criteria)
    identity_criteria.pop("control_type", None)
    candidates = []
    for handle in handles:
        try:
            root = Desktop(backend="uia").window(handle=handle)
            wrapper = root.wrapper_object()
        except Exception:
            continue
        if str(_element_value(wrapper, "control_type") or "").casefold() == (
                "window"
        ):
            continue
        if _element_matches_criteria(wrapper, identity_criteria):
            candidates.append(root)

    if len(candidates) > 1:
        raise ElementAmbiguousError(
            "UIA native window bridge匹配到多个窗口: "
            f"class_name={criteria['class_name']} count={len(candidates)}"
        )
    return candidates[0] if candidates else None


def _element_value(element, key):
    source = getattr(element, "element_info", element)
    if key == "auto_id":
        return getattr(source, "automation_id", None)
    if key == "title":
        return _element_name(element)
    if key == "class_name":
        return getattr(source, "class_name", None)
    if key == "control_type":
        return getattr(source, "control_type", None)
    if key == "process":
        return getattr(source, "process_id", None)
    if key == "handle":
        return getattr(source, "handle", None)
    if key == "framework_id":
        return getattr(source, "framework_id", None)
    if key in ("visible", "visible_only"):
        visible = getattr(element, "is_visible", None)
        return visible() if callable(visible) else getattr(source, "visible", None)
    if key in ("enabled", "enabled_only"):
        enabled = getattr(element, "is_enabled", None)
        return enabled() if callable(enabled) else getattr(source, "enabled", None)
    return None


def _element_matches_criteria(
        element,
        criteria,
        *,
        ignore_dynamic_state=False,
):
    for key, expected in criteria.items():
        if key in {"backend", "parent", "top_level_only", "depth", "found_index"}:
            continue
        if ignore_dynamic_state and key in ("visible_only", "enabled_only"):
            continue
        if key in ("visible_only", "enabled_only"):
            if _to_bool(_element_value(element, key)) != _to_bool(expected):
                return False
            continue
        if key in ("title_re", "class_name_re"):
            target_key = "title" if key == "title_re" else "class_name"
            actual = _element_value(element, target_key)
            if actual is None or not _regex_matches(expected, actual):
                return False
            continue
        if key == "auto_id_re":
            actual = _element_value(element, "auto_id")
            if actual is None or not _regex_matches(expected, actual):
                return False
            continue
        actual = _element_value(element, key)
        if actual is None or str(actual) != str(expected):
            return False
    return True


def _restore_top_root_from_entry(entry):
    if not entry or entry.kind != "top" or not entry.handle:
        return None
    try:
        root = _root_spec_from_handle(entry)
        element = root.wrapper_object()
        process_id = _element_value(element, "process")
        if entry.process_id and process_id != entry.process_id:
            return None
        if not _element_matches_criteria(
            element,
            entry.criteria,
            ignore_dynamic_state=True,
        ):
            return None
        entry.mark_hot(root, entry.handle, process_id or entry.process_id)
        return entry.root
    except Exception as e:
        logger.debug(f"^^^^^^ 顶层 root handle 缓存失效 -> {entry.name}, handle={entry.handle}, err={e}")
        return None


def _upgrade_top_root_entry(entry):
    if entry is None or entry.kind != "top" or entry.handle or entry.root is None:
        return False

    try:
        wrapper = entry.root.wrapper_object()
    except Exception as e:
        logger.debug(f"^^^^^^ 顶层 root handle 补全失败 -> {entry.name}, err={e}")
        return False

    info = getattr(wrapper, "element_info", None)
    handle = getattr(wrapper, "handle", None) or getattr(info, "handle", None)
    if not handle:
        return False

    process_id = getattr(info, "process_id", None)
    if process_id is None:
        process_getter = getattr(wrapper, "process_id", None)
        try:
            process_id = process_getter() if callable(process_getter) else process_getter
        except Exception:
            process_id = None

    entry.mark_hot(Desktop(backend=entry.backend).window(handle=handle), handle, process_id)
    logger.debug(
        f"^^^^^^ 顶层 root handle 已补全 -> {entry.name}, "
        f"handle={entry.handle}, process_id={entry.process_id}"
    )
    return True


def _upgrade_named_root_entry(windows, locator):
    if not locator.root_name:
        return False
    name = locator.root_cache_name or locator.root_name
    return _upgrade_top_root_entry(windows.get_entry(name))


def _upgrade_self_top_root_entry(windows, locator):
    if not getattr(locator, "name", None):
        return False
    name = locator.root_cache_name or locator.name
    return _upgrade_top_root_entry(windows.get_entry(name))


def _finalize_successful_find(windows, locator, self_top_root=False, confirmed=False):
    if not confirmed:
        return
    if self_top_root:
        _upgrade_self_top_root_entry(windows, locator)
    elif locator.root_name:
        _upgrade_named_root_entry(windows, locator)


def _restore_cached_root(windows, name):
    entry = windows.get_entry(name)
    if entry is None:
        return RootResolveResult(root=windows.get(name))

    if entry.kind == "top":
        if entry.handle:
            root = _restore_top_root_from_entry(entry)
            if root is not None:
                return RootResolveResult(root=root, stale_entry=entry)
            entry.mark_cold(_root_spec_from_criteria(entry))
            return RootResolveResult(root=entry.root, stale_entry=entry)

        entry.root = _root_spec_from_criteria(entry)
        entry.state = "cold"
        return RootResolveResult(root=entry.root, stale_entry=entry)

    return RootResolveResult(root=entry.root)


def _switch_root(context, locator: CompiledLocator):
    """恢复或创建 locator 直接引用的顶层 Root。"""
    windows = context.autowork_scenario.windows
    cache_name = locator.root_cache_name or locator.root_name
    if locator.root_name and windows.has(cache_name):
        root_result = _restore_cached_root(windows, cache_name)
        if root_result.root is not None:
            windows.set_last(root_result.root)
            logger.debug(f"^^^^^^ 命中 root 缓存 -> {cache_name}")
            return root_result

    if not locator.root_name:
        last_root = windows.last()
        if last_root is not None:
            logger.debug("^^^^^^ 未指定 root，回退使用 last_root")
            return RootResolveResult(root=last_root)
        raise RuntimeError(f"^^^^^^  last_root 还未被定义")

    root_locator = locator.root_locator
    if root_locator is None:
        root_locator = context.autowork_feature.locators.get(
            locator.root_name
        )
    if root_locator is None:
        raise KeyError(f"locator 引用了不存在的 root: {locator.root_name}")
    if not _is_self_top_root_locator(root_locator):
        raise ValueError(f"locator root 必须是顶层窗口: {locator.root_name}")

    root_result = _resolve_self_top_root(windows, root_locator)
    if root_result.root is not None:
        windows.set_last(root_result.root)
        logger.debug(f"^^^^^^ 已切换顶层 root -> {locator.root_name}")
    return root_result


def _is_self_top_root_locator(locator):
    if locator.top_level and locator.root_name:
        raise ValueError(
            f"locator[{locator.name}] 声明了 top_level=true，但同时又引用 root。"
            f"top_level root 必须从桌面顶层查找，不能再声明父 root。"
        )
    if locator.root_name:
        return False
    if locator.top_level:
        return True
    if not isinstance(locator.criteria, dict):
        return False

    control_type = str(locator.criteria.get("control_type", "")).strip().lower()
    return control_type == "window"


def _resolve_self_top_root(windows, locator):
    if not isinstance(locator.criteria, dict):
        raise RuntimeError(
            f"顶层 root 必须是 dict 定位: root={locator.name}, criteria={locator.criteria}"
        )

    cache_name = locator.root_cache_name or locator.name
    if cache_name and windows.has(cache_name):
        root_result = _restore_cached_root(windows, cache_name)
        if root_result.root is not None:
            windows.set_last(root_result.root)
            logger.debug(f"^^^^^^ 命中 root 缓存 -> {cache_name}")
            return root_result

    criteria = dict(locator.criteria)
    backend = criteria.pop("backend", "uia")
    native_bridge = _native_uia_window_bridge_root(backend, criteria)
    if native_bridge is not None:
        windows.set_last(native_bridge)
        logger.debug(
            f"^^^^^^ 已通过native handle切换UIA桥接root -> {locator.name}"
        )
        return RootResolveResult(root=native_bridge)
    root = Desktop(backend=backend).window(
        **_pywinauto_name_criteria(criteria)
    )
    entry = None

    if cache_name:
        entry = RootEntry(
            name=cache_name,
            kind="top",
            backend=backend,
            criteria=dict(criteria),
            root=root,
        )
        windows.set_entry(entry)
        logger.debug(
            f"^^^^^^ 顶层 root lazy 已缓存 -> {locator.name}, "
            f"backend={backend}, criteria={criteria}"
        )

    windows.set_last(root)
    return RootResolveResult(root=root, stale_entry=entry)


def _bind_runtime_root_if_needed(context, locator: CompiledLocator):
    """校验临时 locator 的直接顶层 root 和普通 region 引用。"""
    if locator is None:
        raise RuntimeError(f"^^^^^^ locator is None")

    feature = context.autowork_feature
    scenario = context.autowork_scenario
    if locator.root_name and not scenario.windows.has(locator.root_name):
        root = locator.root_locator or feature.locators.get(locator.root_name)

        if root is None:
            raise KeyError(f"locator 引用了不存在的 root: {locator.root_name}")
        if not _is_self_top_root_locator(root):
            raise ValueError(f"locator root 必须是顶层窗口: {locator.root_name}")

    if locator.region_name:
        region = locator.region_locator or feature.locators.get(locator.region_name)

        if region is None:
            raise KeyError(f"locator 引用了不存在的 region: {locator.region_name}")
        if region.prefix not in ("child", "xpath"):
            raise ValueError(
                f"locator region 必须是 Child/XPath: {locator.region_name}"
            )

    return locator

def _get_monitor_from_root(root_result):
    if root_result is None or root_result.root is None:
        raise RuntimeError("获取 root monitor 失败: root 尚未解析")

    try:
        root_obj = root_result.root

        if hasattr(root_obj, "wrapper_object"):
            root_obj = root_obj.wrapper_object()

        rect = root_obj.rectangle()
        width = rect.right - rect.left
        height = rect.bottom - rect.top

        if width <= 0 or height <= 0:
            raise ValueError(f"root monitor 区域无效: rect={rect}")

        return {
            "left": rect.left,
            "top": rect.top,
            "width": width,
            "height": height
        }
    except Exception as error:
        root_result.mark_stale_if_hot(_root_spec_from_criteria)
        root_name = getattr(root_result.stale_entry, "name", None)
        logger.warning(
            "获取 root monitor 失败: root={}, err={}",
            root_name,
            error,
        )
        raise


def _switch_root_with_monitor(context, locator):
    root_result = _switch_root(context, locator)
    stale_entry = root_result.stale_entry
    can_rebind = (
        stale_entry is not None
        and stale_entry.is_hot_handle()
    )

    try:
        monitor = _get_monitor_from_root(root_result)
    except Exception:
        if not can_rebind:
            raise
        root_result = _switch_root(context, locator)
        monitor = _get_monitor_from_root(root_result)

    return root_result, monitor


def _get_monitor_from_region(
        context,
        region_name,
        timeout=5,
        region_locator=None,
):
    if region_locator is None:
        region_locator = context.autowork_feature.locators.get(region_name)
    if region_locator is None:
        raise KeyError(f"region 引用了不存在的 locator: {region_name}")
    if region_locator.prefix in ("ocr", "pic", "pos"):
        raise ValueError(
            f"region locator 必须是 Child/XPath 元素: "
            f"name={region_name}, prefix={region_locator.prefix}"
        )

    target = _find(
        context,
        region_locator,
        visual_timeout=timeout,
        first_only=False,
        wait_type="exists",
        wait_timeout=timeout,
        required=True,
    )
    target = _first_region_target(target, region_name)
    if isinstance(target, tuple):
        raise ValueError(f"region locator 必须返回控件元素: {region_name}")
    if hasattr(target, "wrapper_object"):
        target = target.wrapper_object()

    rect = target.rectangle()
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        raise ValueError(f"region locator 区域无效: {region_name}, rect={rect}")
    return {
        "left": int(rect.left),
        "top": int(rect.top),
        "width": int(width),
        "height": int(height),
    }


def _first_region_target(target, region):
    if not isinstance(target, list):
        return target
    if len(target) > 1:
        error = ElementAmbiguousError(
            f"region locator 匹配到 {len(target)} 个元素: {region}"
        )
        error.elements = target
        raise error
    return target[0] if target else None

_SUPPORTED_PREFIXES = frozenset({
    "default",
    "child",
    "xpath",
    "pos",
    "pic",
    "ocr",
})

def _find(
        context,
        kwargs,
        visual_timeout=10,
        control_type=None,
        first_only=True,
        wait_type="ready",
        wait_timeout=5,
        required=True,
        entry_point=None,
):
    wait_type = _normalize_wait_type(wait_type)
    windows = context.autowork_scenario.windows

    if isinstance(kwargs, CompiledLocator):
        compiled = kwargs
    else:
        compiled = compile_locator(kwargs)
        compiled = _bind_runtime_root_if_needed(context, compiled)

    log_call(
        entry_point,
        compiled=compiled,
        visual_timeout=visual_timeout,
        wait_type=wait_type,
        wait_timeout=wait_timeout,
        control_type=control_type,
        first_only=first_only,
        required=required,
    )

    if compiled.prefix not in _SUPPORTED_PREFIXES:
        raise ValueError(f"未知 locator 类型: {compiled.prefix}, raw={compiled.raw}")
    self_top_root = _is_self_top_root_locator(compiled)
    root_result = None
    dlg = None
    confirmed = False
    xpath_wait_completed = False

    if self_top_root:
        root_result = _resolve_self_top_root(windows, compiled)
        dlg = root_result.root
    elif compiled.prefix in ['ocr', 'pic']:
        if getattr(compiled, "region_name", None):
            monitor = _get_monitor_from_region(
                context,
                compiled.region_name,
                timeout=visual_timeout,
                region_locator=compiled.region_locator,
            )
        elif getattr(compiled, "root_name", None):
            root_result, monitor = _switch_root_with_monitor(
                context,
                compiled,
            )
        else:
            monitor = None
        find_visual = _find_by_ocr if compiled.prefix == "ocr" else _find_by_pic
        dlg = find_visual(
            compiled.criteria,
            visual_timeout,
            monitor=monitor,
        )
    elif compiled.prefix in ['pos']:
        dlg = _find_by_pos(compiled.criteria)
    elif compiled.prefix in ['xpath']:
        dlg, root_result = _wait_for_xpath_target(
            context,
            compiled,
            first_only,
            wait_type,
            wait_timeout,
            required,
        )
        confirmed = isinstance(dlg, (list, BaseWrapper))
        xpath_wait_completed = True
    elif compiled.prefix in ['default']:
        root_result, monitor = _switch_root_with_monitor(
            context,
            compiled,
        )
        dlg = _find_by_default(
            root_result.root,
            compiled.criteria,
            control_type,
            visual_timeout,
            first_only=first_only,
            monitor=monitor,
        )
    else:
        root_result = _switch_root(context, compiled)
        try:
            dlg = _find_by_child_window(
                root_result.root,
                compiled.criteria,
                first_only=first_only,
                control_type=control_type,
                timeout=wait_timeout,
                wait_type=wait_type,
            )
        except Exception as error:
            _attach_find_diagnostic(
                error,
                compiled,
                root_result,
                wait_type=wait_type,
                wait_timeout=wait_timeout,
                visual_timeout=visual_timeout,
                entry_point=entry_point,
            )
            raise
    # ((x,y),'ocr') | ((x,y),'pic') | ((x,y),'pos') | None
    # Wrapper (ButtonWrapper / EditWrapper) | WindowSpecification |

    if required and not dlg:
        logger.error(f"^^^^^^ locator '{kwargs}' not found.")
        error = LookupError(f"^^^^^^ locator '{kwargs}' not found.")
        _attach_find_diagnostic(
            error,
            compiled,
            root_result,
            wait_type=wait_type,
            wait_timeout=wait_timeout,
            visual_timeout=visual_timeout,
            entry_point=entry_point,
        )
        raise error

    if dlg:
        if isinstance(dlg, tuple):
            confirmed = True
        else:
            if not xpath_wait_completed:
                confirmed = isinstance(dlg, (list, BaseWrapper))
            if wait_type != "none" and not xpath_wait_completed:
                try:
                    _wait_target(dlg, timeout=wait_timeout, wait_type=wait_type, first_only=first_only)
                except Exception as error:
                    if root_result is not None:
                        root_result.mark_stale_if_hot(_root_spec_from_criteria)
                    _attach_find_diagnostic(
                        error,
                        compiled,
                        root_result,
                        wait_type=wait_type,
                        wait_timeout=wait_timeout,
                        visual_timeout=visual_timeout,
                        entry_point=entry_point,
                    )
                    raise
                confirmed = True

    if dlg:
        _finalize_successful_find(windows, compiled, self_top_root=self_top_root, confirmed=confirmed)

        if settings.draw_outline:
            if isinstance(dlg, tuple):
                mark_visual_target(dlg)
            else:
                try:
                    _draw_outline(dlg, first_only=first_only)
                except Exception as e:
                    logger.debug(f"^^^^^^ draw_outline 失败，已忽略: {e}")

    return dlg


def _attach_find_diagnostic(
        error,
        compiled,
        root_result,
        *,
        wait_type,
        wait_timeout,
        visual_timeout,
        entry_point,
    ):
    if runtime_diagnostic_from_exception(error) is not None:
        return error
    prefix = str(compiled.prefix or "")
    if prefix in {"ocr", "pic"}:
        code = f"VISUAL_{prefix.upper()}_MISS"
        category = "visual_miss"
        stage = "locate_visual"
        timeout = float(visual_timeout)
        summary = (
            f"{prefix.upper()} 在 {visual_timeout} 秒内未匹配到目标"
        )
    elif isinstance(error, TimeoutError) and wait_type not in {None, "none"}:
        code = "CONTROL_STATE_TIMEOUT"
        category = "state_timeout"
        stage = "wait_state"
        timeout = float(wait_timeout)
        summary = (
            f"已定位目标，但未在 {wait_timeout} 秒内达到 "
            f"{_normalize_wait_type(wait_type)} 状态"
        )
    elif type(error) is LookupError:
        code = f"{prefix.upper()}_NOT_FOUND"
        category = "not_found"
        stage = f"locate_{prefix}"
        timeout = float(wait_timeout)
        summary = f"未在 {wait_timeout} 秒内找到目标元素"
    else:
        code = f"{prefix.upper()}_BACKEND_ERROR"
        category = "backend_error"
        stage = f"locate_{prefix}"
        timeout = float(wait_timeout)
        summary = f"{prefix} 自动化后端读取失败"
    return attach_runtime_diagnostic(error, RuntimeDiagnostic(
        code=code,
        category=category,
        stage=stage,
        summary=summary,
        backend=prefix,
        entry_point=entry_point,
        locator_name=compiled.name,
        locator_kind=prefix,
        root_name=compiled.root_name,
        root_state=_root_result_state(root_result),
        wait_type=_normalize_wait_type(wait_type),
        timeout_seconds=timeout,
        candidate_count=0 if category in {"not_found", "visual_miss"} else None,
    ))


def _root_result_state(root_result):
    entry = getattr(root_result, "stale_entry", None)
    return str(getattr(entry, "state", "") or "") or None


def _target_count(value):
    if value is None:
        return 0
    return len(value) if isinstance(value, list) else 1
