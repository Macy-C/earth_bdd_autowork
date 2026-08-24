from autowork_core.common.locator import _find


def get_element_by_xpath(context, kwargs, wait_type="ready", wait_timeout=5, visual_timeout=10, entry_point=None):
    return _find(context, kwargs, wait_type=wait_type, wait_timeout=wait_timeout, visual_timeout=visual_timeout, entry_point=entry_point)

def get_element_by_Button(context, kwargs, wait_type="ready", wait_timeout=5, visual_timeout=10, entry_point=None):
    return _find(context, kwargs, control_type='Button', wait_type=wait_type, wait_timeout=wait_timeout, visual_timeout=visual_timeout, entry_point=entry_point)

def get_element_by_Edit(context, kwargs, wait_type="ready", wait_timeout=5, visual_timeout=10, entry_point=None):
    return _find(context, kwargs, control_type='Edit', wait_type=wait_type, wait_timeout=wait_timeout, visual_timeout=visual_timeout, entry_point=entry_point)

def get_element(context, kwargs, visual_timeout=10, wait_type="ready", wait_timeout=5, required=True, entry_point=None):
    return _find(
        context,
        kwargs,
        visual_timeout=visual_timeout,
        wait_type=wait_type,
        wait_timeout=wait_timeout,
        required=required,
        entry_point=entry_point,
    )

def get_elements(context, kwargs, wait_type="ready", wait_timeout=5, visual_timeout=10, entry_point=None):
    return _find(context, kwargs, first_only=False, wait_type=wait_type, wait_timeout=wait_timeout, visual_timeout=visual_timeout, entry_point=entry_point)


