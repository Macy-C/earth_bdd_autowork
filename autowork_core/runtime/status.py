"""统一 Behave 执行状态分类，并判断失败产物是否需要保留。

Normalizes Behave execution status categories and decides whether failure
artifacts should be retained.
"""


FAILED_STATUS_KEYWORDS = {"error", "failed", "undefined"}
PASSED_STATUS_KEYWORDS = {"passed"}
SKIPPED_STATUS_KEYWORDS = {"skipped", "untested"}


def status_text(status):
    name = getattr(status, "name", None)
    return str(name if name is not None else status).strip().lower()


def is_failed_status(status):
    return status_category(status) == "failed"


def status_category(status):
    text = status_text(status)
    if any(keyword in text for keyword in FAILED_STATUS_KEYWORDS):
        return "failed"
    if any(keyword in text for keyword in SKIPPED_STATUS_KEYWORDS):
        return "skipped"
    if any(keyword in text for keyword in PASSED_STATUS_KEYWORDS):
        return "passed"
    return "unknown"


def should_keep_artifacts(status):
    return is_failed_status(status)
