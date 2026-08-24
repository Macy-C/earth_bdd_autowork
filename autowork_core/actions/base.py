import pyperclip
from pywinauto.keyboard import SendKeys
from autowork_core.actions.action_helper import _do_click, _is_coords_target
from autowork_core.common.element_finder import get_element
from autowork_core.common.log_helper import log_call


#========================================== click ===============================================
def click(context, locator, offset_x=None, offset_y=None, wait_type="visible", wait_timeout=5, visual_timeout=10, entry_point=None):
    entry_point = log_call(entry_point,locator=locator,offset_x=offset_x,offset_y=offset_y,wait_type=wait_type,wait_timeout=wait_timeout,visual_timeout=visual_timeout)
    el = get_element(context, locator, wait_type=wait_type, wait_timeout=wait_timeout, visual_timeout=visual_timeout, entry_point=entry_point)
    if (offset_x is None) != (offset_y is None):
        raise ValueError("offset_x和offset_y必须同时提供")
    offset = None if offset_x is None else (offset_x, offset_y)
    _do_click(el, offset=offset)

def double_click(context, locator, wait_type="visible", wait_timeout=5, visual_timeout=10, entry_point=None):
    entry_point = log_call(entry_point,locator=locator,wait_type=wait_type,wait_timeout=wait_timeout,visual_timeout=visual_timeout)
    el = get_element(context, locator, wait_type=wait_type, wait_timeout=wait_timeout, visual_timeout=visual_timeout, entry_point=entry_point)
    _do_click(el, double=True)

def right_click(context, locator, wait_type="visible", wait_timeout=5, visual_timeout=10, entry_point=None):
    entry_point = log_call(entry_point,locator=locator,wait_type=wait_type,wait_timeout=wait_timeout,visual_timeout=visual_timeout)
    el = get_element(context, locator, wait_type=wait_type, wait_timeout=wait_timeout, visual_timeout=visual_timeout, entry_point=entry_point)
    _do_click(el, button="right")

def focus(context, locator, wait_type="visible", wait_timeout=5, visual_timeout=10, entry_point=None):
    entry_point = log_call(entry_point,locator=locator,wait_type=wait_type,wait_timeout=wait_timeout,visual_timeout=visual_timeout)
    el = get_element(context, locator, wait_type=wait_type, wait_timeout=wait_timeout, visual_timeout=visual_timeout, entry_point=entry_point)
    if _is_coords_target(el):
        _do_click(el)
    else:
        try:
            el.set_focus()
        except Exception:
            _do_click(el)
#========================================== click ===============================================
#========================================== edit_box ===============================================

def clear_text(context, locator, wait_type="enabled", wait_timeout=5, visual_timeout=10, entry_point=None):
    entry_point = log_call(entry_point,locator=locator,wait_type=wait_type,wait_timeout=wait_timeout,visual_timeout=visual_timeout)
    el = get_element(context, locator, wait_type=wait_type, wait_timeout=wait_timeout, visual_timeout=visual_timeout, entry_point=entry_point)

    if _is_coords_target(el):
        _do_click(el)
        SendKeys("^a{BACKSPACE}")
        return

    try:
        el.set_edit_text("")
        return
    except Exception:
        pass

    try:
        _do_click(el)
        el.type_keys("^a{BACKSPACE}")
    except Exception:
        _do_click(el)
        SendKeys("^a{BACKSPACE}")

def input_text(context, locator, text, clear=True, use_paste=False, wait_type="enabled", wait_timeout=5, visual_timeout=10, entry_point=None):
    entry_point = log_call(entry_point,locator=locator,text=text,clear=clear,use_paste=use_paste,wait_type=wait_type,wait_timeout=wait_timeout,visual_timeout=visual_timeout)
    el = get_element(context, locator, wait_type=wait_type, wait_timeout=wait_timeout, visual_timeout=visual_timeout, entry_point=entry_point)

    if _is_coords_target(el):
        _do_click(el)
        if clear:
            SendKeys("^a{BACKSPACE}")
        if use_paste:
            pyperclip.copy(str(text))
            SendKeys("^v")
        else:
            SendKeys(str(text), with_spaces=True)
        return

    # 1. 优先标准编辑方式
    if clear:
        try:
            el.set_edit_text(str(text))
            return
        except Exception:
            pass

    # 2. 点击后 type_keys
    try:
        _do_click(el)
        if clear:
            try:
                el.type_keys("^a{BACKSPACE}")
            except Exception:
                SendKeys("^a{BACKSPACE}")
        el.type_keys(str(text), with_spaces=True)
        return
    except Exception:
        pass

    # 3. 兜底：全局 send_keys / 粘贴
    _do_click(el)
    if clear:
        SendKeys("^a{BACKSPACE}")

    if use_paste:
        pyperclip.copy(str(text))
        SendKeys("^v")
    else:
        SendKeys(str(text), with_spaces=True)


def send_text_keys(context, locator, keys, wait_type="enabled", wait_timeout=5, visual_timeout=10, entry_point=None):
    """
    只做 聚焦 + 发键盘键
    """
    entry_point = log_call(entry_point,locator=locator,keys=keys,wait_type=wait_type,wait_timeout=wait_timeout,visual_timeout=visual_timeout)

    el = get_element(context, locator, wait_type=wait_type, wait_timeout=wait_timeout, visual_timeout=visual_timeout, entry_point=entry_point)
    if _is_coords_target(el):
        _do_click(el)
    else:
        try:
            _do_click(el)
        except Exception:
            pass
    SendKeys(keys, with_spaces=True)
#========================================== edit_box ===============================================



