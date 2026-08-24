from autowork_core.actions.action_helper import (
    _exists,
    _is_enabled,
    _is_exposed,
    _is_visible,
)
from autowork_core.common.element_finder import get_element
from autowork_core.common.log_helper import log_call
from autowork_core.common.wait_coordinator import poll_boolean
from autowork_core.common.runtime_diagnostics import (
    clear_last_runtime_diagnostic,
    remember_runtime_diagnostic,
)
import time


def wait_exists(context, locator, timeout=10, interval=0.5,entry_point=None):
    clear_last_runtime_diagnostic()
    entry_point = log_call(entry_point,locator=locator,timeout=timeout,interval=interval)
    return _poll_probe(
        lambda: _exists(
            context,
            locator,
            timeout=0,
            entry_point=entry_point,
        ),
        timeout,
        interval,
    )


def wait_not_exists(context, locator, timeout=10, interval=0.5,entry_point=None):
    clear_last_runtime_diagnostic()
    entry_point = log_call(entry_point,locator=locator,timeout=timeout,interval=interval)
    return _poll_probe(
        lambda: _exists(
            context,
            locator,
            timeout=0,
            entry_point=entry_point,
        ),
        timeout,
        interval,
        expected=False,
    )

def wait_visible(context, locator, timeout=10, interval=0.5,entry_point=None):
    clear_last_runtime_diagnostic()
    entry_point = log_call(entry_point,locator=locator,timeout=timeout,interval=interval)
    return _poll_probe(
        lambda: _is_visible(
            context,
            locator,
            timeout=0,
            entry_point=entry_point,
        ),
        timeout,
        interval,
    )

def wait_enabled(context, locator, timeout=10, interval=0.5,entry_point=None):
    clear_last_runtime_diagnostic()
    entry_point = log_call(entry_point,locator=locator,timeout=timeout,interval=interval)
    return _poll_probe(
        lambda: _is_enabled(
            context,
            locator,
            timeout=0,
            entry_point=entry_point,
        ),
        timeout,
        interval,
    )

def wait_ready(context, locator, timeout=10, interval=0.5,entry_point=None):
    clear_last_runtime_diagnostic()
    entry_point = log_call(entry_point,locator=locator,timeout=timeout,interval=interval)

    def probe(remaining):
        try:
            get_element(
                context,
                locator,
                visual_timeout=0,
                wait_type="ready",
                wait_timeout=min(interval, remaining) if remaining else 0,
                required=True,
                entry_point=entry_point,
            )
            return True
        except Exception as error:
            remember_runtime_diagnostic(error)
            return False

    return poll_boolean(
        probe,
        timeout=timeout,
        interval=interval,
        pass_remaining=True,
        monotonic=time.monotonic,
        sleep=time.sleep,
    )

def wait_exposed(context, locator, timeout=10, interval=0.5, entry_point=None):
    clear_last_runtime_diagnostic()
    entry_point = log_call(
        entry_point,
        locator=locator,
        timeout=timeout,
        interval=interval,
    )

    return _poll_probe(
        lambda: _is_exposed(
                context,
                locator,
                timeout=0,
                entry_point=entry_point,
        ),
        timeout,
        interval,
    )


def _poll_probe(probe, timeout, interval, expected=True):
    return poll_boolean(
        probe,
        timeout=timeout,
        interval=interval,
        expected=expected,
        monotonic=time.monotonic,
        sleep=time.sleep,
    )

