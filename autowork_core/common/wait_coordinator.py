from math import isfinite


def _timeout_seconds(value):
    seconds = float(value or 0)
    if not isfinite(seconds):
        raise ValueError("timeout 必须为有限数")
    return max(seconds, 0)


def _sleep_delay(interval, remaining, *, clamp=False):
    delay = max(float(interval or 0), 0) if clamp else float(interval)
    if not isfinite(delay):
        raise ValueError("interval 必须为有限数")
    return min(delay, max(0, remaining))


def poll_boolean(
        probe,
        *,
        timeout,
        interval,
        expected=True,
        pass_remaining=False,
        monotonic,
        sleep,
):
    deadline = monotonic() + _timeout_seconds(timeout)

    while True:
        if pass_remaining:
            remaining = max(0, deadline - monotonic())
            value = probe(remaining)
        else:
            value = probe()
        if bool(value) is bool(expected):
            return True
        if monotonic() >= deadline:
            return False
        sleep(_sleep_delay(interval, deadline - monotonic()))


def poll_value(
        probe,
        predicate,
        *,
        timeout,
        interval,
        timeout_message,
        timeout_error_type=TimeoutError,
        fatal_errors=(),
        clamp_interval=False,
        monotonic,
        sleep,
):
    deadline = monotonic() + _timeout_seconds(timeout)
    last_error = None
    last_value = None
    probe_count = 0

    while True:
        probe_count += 1
        try:
            current = probe()
            last_value = current
            if predicate(current):
                return current
        except fatal_errors:
            raise
        except Exception as error:
            last_error = error

        now = monotonic()
        if now >= deadline:
            timeout_error = timeout_error_type(timeout_message)
            timeout_error.last_value = last_value
            timeout_error.last_error = last_error
            timeout_error.probe_count = probe_count
            timeout_error.timeout_seconds = _timeout_seconds(timeout)
            if last_error is not None:
                raise timeout_error from last_error
            raise timeout_error

        sleep(_sleep_delay(
            interval,
            deadline - now,
            clamp=clamp_interval,
        ))