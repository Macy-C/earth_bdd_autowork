from pywinauto import Desktop
from loguru import logger
from autowork_core.actions.action_helper import (
    _coords_from_target,
    _do_click,
    _target_to_wrapper,
)
from autowork_core.common.element_finder import get_element
from autowork_core.common.locator import _restore_cached_root
from autowork_core.common.log_helper import log_call
from autowork_core.common.root_store import RootEntry

try:
    import win32con
    import win32gui
except Exception:  # pragma: no cover - pywin32 is Windows-only
    win32con = None
    win32gui = None


def bring_to_front(context, locator, wait_type="exists", wait_timeout=5, visual_timeout=10, activate=False, entry_point=None):
    entry_point = log_call(
        entry_point,
        locator=locator,
        wait_type=wait_type,
        wait_timeout=wait_timeout,
        visual_timeout=visual_timeout,
        activate=activate,
    )
    cached_handle = _cached_top_root_handle(context, locator)
    if cached_handle:
        if activate:
            _activate_handle(cached_handle)
        else:
            _send_handle_to_front(cached_handle)
        return Desktop(backend="uia").window(handle=cached_handle)

    target = get_element(
        context,
        locator,
        wait_type=wait_type,
        wait_timeout=wait_timeout,
        visual_timeout=visual_timeout,
        entry_point=entry_point,
    )

    coords = _coords_from_target(target)
    if coords is not None:
        if not activate:
            raise RuntimeError("bring_to_front: 坐标目标没有所属窗口，无法前置")
        _do_click(target)
        return target

    target = _target_to_wrapper(target, timeout=wait_timeout)
    if target is None:
        raise RuntimeError("bring_to_front: 未找到目标窗口或控件")

    top_window = _top_level_wrapper(target)

    if not activate:
        _send_window_to_front(top_window)
        return target

    _restore_window(top_window)

    try:
        _force_foreground(top_window)
    except Exception:
        pass

    try:
        target.set_focus()
    except Exception:
        try:
            top_window.set_focus()
        except Exception:
            _do_click(target)

    return target


def minimize_window(context, locator, wait_type="exists", wait_timeout=5, visual_timeout=10, entry_point=None):
    entry_point = log_call(
        entry_point,
        locator=locator,
        wait_type=wait_type,
        wait_timeout=wait_timeout,
        visual_timeout=visual_timeout,
    )
    cached_handle = _cached_top_root_handle(context, locator)
    if cached_handle:
        _minimize_handle(cached_handle)
        return Desktop(backend="uia").window(handle=cached_handle)

    target = get_element(
        context,
        locator,
        wait_type=wait_type,
        wait_timeout=wait_timeout,
        visual_timeout=visual_timeout,
        entry_point=entry_point,
    )

    if _coords_from_target(target) is not None:
        raise RuntimeError("minimize_window: 坐标目标没有所属窗口，无法最小化")

    target = _target_to_wrapper(target, timeout=wait_timeout)
    if target is None:
        raise RuntimeError("minimize_window: 未找到目标窗口或控件")

    top_window = _top_level_wrapper(target)
    _minimize_window(top_window)
    return top_window


def set_window_topmost(context, locator, topmost=True, wait_type="exists", wait_timeout=5, visual_timeout=10, entry_point=None):
    entry_point = log_call(
        entry_point,
        locator=locator,
        topmost=topmost,
        wait_type=wait_type,
        wait_timeout=wait_timeout,
        visual_timeout=visual_timeout,
    )
    cached_handle = _cached_top_root_handle(context, locator)
    if cached_handle:
        _set_handle_topmost(cached_handle, topmost=topmost)
        return Desktop(backend="uia").window(handle=cached_handle)

    target = get_element(
        context,
        locator,
        wait_type=wait_type,
        wait_timeout=wait_timeout,
        visual_timeout=visual_timeout,
        entry_point=entry_point,
    )

    if _coords_from_target(target) is not None:
        raise RuntimeError("set_window_topmost: 坐标目标没有所属窗口，无法设置置顶状态")

    target = _target_to_wrapper(target, timeout=wait_timeout)
    if target is None:
        raise RuntimeError("set_window_topmost: 未找到目标窗口或控件")

    top_window = _top_level_wrapper(target)
    _set_window_topmost(top_window, topmost=topmost)
    return top_window


def unset_window_topmost(context, locator, wait_type="exists", wait_timeout=5, visual_timeout=10, entry_point=None):
    return set_window_topmost(
        context,
        locator,
        topmost=False,
        wait_type=wait_type,
        wait_timeout=wait_timeout,
        visual_timeout=visual_timeout,
        entry_point=entry_point,
    )


def send_to_back(context, locator, wait_type="exists", wait_timeout=5, visual_timeout=10, entry_point=None):
    entry_point = log_call(
        entry_point,
        locator=locator,
        wait_type=wait_type,
        wait_timeout=wait_timeout,
        visual_timeout=visual_timeout,
    )
    cached_handle = _cached_top_root_handle(context, locator)
    if cached_handle:
        _send_handle_to_back(cached_handle)
        return Desktop(backend="uia").window(handle=cached_handle)

    target = get_element(
        context,
        locator,
        wait_type=wait_type,
        wait_timeout=wait_timeout,
        visual_timeout=visual_timeout,
        entry_point=entry_point,
    )

    if _coords_from_target(target) is not None:
        raise RuntimeError("send_to_back: 坐标目标没有所属窗口，无法后置")

    target = _target_to_wrapper(target, timeout=wait_timeout)
    if target is None:
        raise RuntimeError("send_to_back: 未找到目标窗口或控件")

    top_window = _top_level_wrapper(target)
    _send_window_to_back(top_window)
    return top_window


def _top_level_wrapper(target):
    handle = _top_level_handle(target)
    if handle:
        return Desktop(backend="uia").window(handle=handle)

    try:
        top_level = target.top_level_parent()
        if top_level is not None:
            return top_level
    except Exception:
        pass
    return target


def _restore_window(window):
    try:
        if window.is_minimized():
            window.restore()
    except Exception:
        pass

    handle = _window_handle(window)
    if handle and win32gui and win32con:
        try:
            if win32gui.IsIconic(handle):
                win32gui.ShowWindow(handle, win32con.SW_RESTORE)
            else:
                win32gui.ShowWindow(handle, win32con.SW_SHOW)
        except Exception:
            pass


def _force_foreground(window):
    handle = _window_handle(window)
    if handle and win32gui and win32con:
        win32gui.ShowWindow(handle, win32con.SW_RESTORE)
        win32gui.BringWindowToTop(handle)
        win32gui.SetForegroundWindow(handle)
        return
    window.set_focus()


def _activate_handle(handle):
    if not handle or not win32gui or not win32con:
        raise RuntimeError("bring_to_front: 未获取到可激活的窗口 handle")
    win32gui.ShowWindow(handle, win32con.SW_RESTORE)
    win32gui.BringWindowToTop(handle)
    win32gui.SetForegroundWindow(handle)
    logger.debug(f"^^^^^^ 窗口已激活前置 -> handle={handle}")


def _minimize_window(window):
    handle = _window_handle(window)
    if handle and win32gui and win32con:
        _minimize_handle(handle)
        return

    try:
        window.minimize()
        return
    except Exception:
        pass

    raise RuntimeError("minimize_window: 目标窗口不支持最小化")


def _minimize_handle(handle):
    if not handle or not win32gui or not win32con:
        raise RuntimeError("minimize_window: 未获取到可最小化的窗口 handle")
    win32gui.ShowWindow(handle, win32con.SW_MINIMIZE)
    logger.debug(f"^^^^^^ 窗口已最小化 -> handle={handle}")


def _set_window_topmost(window, topmost=True):
    handle = _top_level_handle(window) or _window_handle(window)
    if not handle or not win32gui or not win32con:
        raise RuntimeError("set_window_topmost: 未获取到可设置置顶状态的窗口 handle")

    _set_handle_topmost(handle, topmost=topmost)


def _set_handle_topmost(handle, topmost=True):
    if not handle or not win32gui or not win32con:
        raise RuntimeError("set_window_topmost: 未获取到可设置置顶状态的窗口 handle")

    insert_after = getattr(win32con, "HWND_TOPMOST", -1) if topmost else getattr(win32con, "HWND_NOTOPMOST", -2)
    flags = (
        getattr(win32con, "SWP_NOMOVE", 0x0002)
        | getattr(win32con, "SWP_NOSIZE", 0x0001)
        | getattr(win32con, "SWP_NOACTIVATE", 0x0010)
    )
    win32gui.SetWindowPos(handle, insert_after, 0, 0, 0, 0, flags)
    logger.debug(f"^^^^^^ 窗口置顶状态已设置 -> handle={handle}, topmost={topmost}")


def _send_window_to_back(window):
    handle = _top_level_handle(window) or _window_handle(window)
    if not handle or not win32gui or not win32con:
        raise RuntimeError("send_to_back: 未获取到可后置的窗口 handle")

    _send_handle_to_back(handle)


def _send_handle_to_back(handle):
    if not handle or not win32gui or not win32con:
        raise RuntimeError("send_to_back: 未获取到可后置的窗口 handle")

    flags = (
        getattr(win32con, "SWP_NOMOVE", 0x0002)
        | getattr(win32con, "SWP_NOSIZE", 0x0001)
        | getattr(win32con, "SWP_NOACTIVATE", 0x0010)
    )
    win32gui.SetWindowPos(handle, getattr(win32con, "HWND_NOTOPMOST", -2), 0, 0, 0, 0, flags)
    win32gui.SetWindowPos(handle, getattr(win32con, "HWND_BOTTOM", 1), 0, 0, 0, 0, flags)
    logger.debug(f"^^^^^^ 窗口已后置 -> handle={handle}")


def _send_window_to_front(window):
    handle = _top_level_handle(window) or _window_handle(window)
    if not handle or not win32gui or not win32con:
        raise RuntimeError("bring_to_front: 未获取到可前置的窗口 handle")

    _send_handle_to_front(handle)


def _send_handle_to_front(handle):
    if not handle or not win32gui or not win32con:
        raise RuntimeError("bring_to_front: 未获取到可前置的窗口 handle")

    flags = (
        getattr(win32con, "SWP_NOMOVE", 0x0002)
        | getattr(win32con, "SWP_NOSIZE", 0x0001)
        | getattr(win32con, "SWP_NOACTIVATE", 0x0010)
    )
    show_cmd = (
        getattr(win32con, "SW_SHOWNOACTIVATE", 4)
        if win32gui.IsIconic(handle)
        else getattr(win32con, "SW_SHOWNA", 8)
    )
    win32gui.ShowWindow(handle, show_cmd)
    win32gui.SetWindowPos(handle, getattr(win32con, "HWND_TOPMOST", -1), 0, 0, 0, 0, flags)
    win32gui.SetWindowPos(handle, getattr(win32con, "HWND_NOTOPMOST", -2), 0, 0, 0, 0, flags)
    logger.debug(f"^^^^^^ 窗口已前置但未激活 -> handle={handle}")


def _top_level_handle(target):
    handle = _window_handle(target)
    if not handle or not win32gui:
        return handle
    try:
        return win32gui.GetAncestor(handle, getattr(win32con, "GA_ROOT", 2)) or handle
    except Exception:
        return handle


def _window_handle(window):
    for getter in (
        lambda: getattr(window, "handle", None),
        lambda: getattr(getattr(window, "element_info", None), "handle", None),
    ):
        try:
            handle = getter()
            if handle:
                return int(handle)
        except Exception:
            pass
    return None


def _cached_top_root_handle(context, locator):
    name = (
        getattr(locator, "root_cache_name", None)
        or getattr(locator, "name", None)
    )
    if not name:
        return None

    windows = context.autowork_scenario.windows

    entry = windows.get_entry(name)
    if entry is None or entry.kind != "top" or not entry.handle:
        return None

    _restore_cached_root(windows, name)
    if not entry.is_hot_handle():
        logger.debug(
            f"^^^^^^ 缓存窗口 handle 身份失效，回退 locator -> "
            f"name={name}"
        )
        return None
    return int(entry.handle)


def _window_process_id(window):
    for getter in (
        lambda: getattr(getattr(window, "element_info", None), "process_id", None),
        lambda: getattr(window, "process_id", None),
    ):
        try:
            process_id = getter()
            if callable(process_id):
                process_id = process_id()
            if process_id:
                return int(process_id)
        except Exception:
            pass
    return None


def _top_root_entry(name, backend, criteria, root_obj):
    entry = RootEntry(
        name=name,
        kind="top",
        backend=backend,
        criteria=dict(criteria),
        root=root_obj,
    )
    try:
        wrapper = root_obj.wrapper_object() if hasattr(root_obj, "wrapper_object") else root_obj
    except Exception as e:
        logger.debug(f"^^^^^^ set_root 顶层 root handle 获取失败 -> {name}, err={e}")
        return entry

    handle = _window_handle(wrapper)
    if handle:
        process_id = _window_process_id(wrapper)
        entry.mark_hot(Desktop(backend=backend).window(handle=handle), handle, process_id)
        logger.debug(
            f"^^^^^^ set_root 顶层 root 已缓存 -> {name}, "
            f"backend={backend}, criteria={criteria}, handle={handle}, process_id={process_id}"
        )
    return entry


def set_root(context, root, name=None,entry_point=None):
    """
    显式设置 root，并缓存到 context.autowork_scenario.windows。
    context: behave context
    root:  dict: 顶层窗口定位条件，如 {"title": "...", "control_type": "Window"}
    name: 可选，同时缓存到 context.autowork_scenario.windows.set(name, root)
    返回:  root 对象
    """
    log_call(entry_point,root=root,name=name)

    if root is None:
        raise ValueError("set_root: root 不能为空")

    # dict -> 顶层 window spec
    if isinstance(root, dict):
        criteria = dict(root)
        backend = criteria.pop("backend", "uia")
        root_obj = Desktop(backend=backend).window(**criteria)

    else:
        raise RuntimeError('显式切换root需要dict参数')
        # # 直接认为传进来的是 spec / wrapper
        # root_obj = root

    windows = context.autowork_scenario.windows
    windows.set_last(root_obj)

    if name:
        entry = _top_root_entry(name, backend, criteria, root_obj)
        root_obj = windows.set_entry(entry)
        windows.set_last(root_obj)

    return root_obj

