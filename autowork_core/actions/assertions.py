from autowork_core.actions.action_helper import _exists, _is_visible, _is_enabled
from autowork_core.actions.element_actions import (
    get_attr,
    get_collection_items,
    get_text,
)
from autowork_core.actions.wait_actions import wait_exists, wait_visible, wait_enabled
from autowork_core.common.log_helper import log_call
from autowork_core.common.runtime_diagnostics import (
    RuntimeDiagnostic,
    attach_runtime_diagnostic,
    clear_last_runtime_diagnostic,
    last_runtime_diagnostic,
)



def _fail(msg, diagnostic=None, *, prefer_last=False):
    error = AssertionError(msg)
    inherited = None
    if prefer_last:
        inherited = last_runtime_diagnostic()
        diagnostic = inherited or diagnostic
    if diagnostic is not None:
        attach_runtime_diagnostic(
            error,
            diagnostic,
            preserve_cause=inherited is not None,
        )
    raise error


def _assertion_diagnostic(code, summary, locator, timeout, *, last_state=None):
    return RuntimeDiagnostic(
        code=code,
        category="assertion_failure",
        stage="assertion_wait",
        summary=summary,
        locator_name=str(locator),
        wait_type=last_state,
        timeout_seconds=float(timeout),
        last_state=last_state,
    )

def assert_exists(context, locator, timeout=5, msg=None):
    entry_point = log_call(locator=locator,timeout=timeout)
    clear_last_runtime_diagnostic()

    if not wait_exists(context, locator, timeout=timeout,entry_point=entry_point):
        _fail(
            msg or f"断言失败：元素不存在 -> {locator}",
            _assertion_diagnostic(
                "ASSERT_EXISTS_TIMEOUT",
                f"{timeout} 秒内未确认目标存在",
                locator,
                timeout,
                last_state="exists",
            ),
            prefer_last=True,
        )
    return True

def assert_not_exists(context, locator, timeout=2, msg=None):
    entry_point = log_call(locator=locator,timeout=timeout)
    clear_last_runtime_diagnostic()

    if _exists(context, locator, timeout=timeout,entry_point=entry_point):
        _fail(
            msg or f"断言失败：元素不应存在 -> {locator}",
            _assertion_diagnostic(
                "ASSERT_UNEXPECTED_EXISTS",
                "目标仍然存在，但业务期望其消失",
                locator,
                timeout,
                last_state="not_exists",
            ),
        )
    return True


def assert_visible(context, locator, timeout=5, msg=None):
    entry_point = log_call(locator=locator,timeout=timeout)
    clear_last_runtime_diagnostic()

    if not wait_visible(context, locator, timeout=timeout,entry_point=entry_point):
        _fail(
            msg or f"断言失败：元素不可见 -> {locator}",
            _assertion_diagnostic(
                "ASSERT_VISIBLE_TIMEOUT",
                f"{timeout} 秒内未确认目标可见；目标可能尚未出现或仍不可见",
                locator,
                timeout,
                last_state="visible",
            ),
            prefer_last=True,
        )
    return True


def assert_not_visible(context, locator, timeout=2, msg=None):
    entry_point = log_call(locator=locator,timeout=timeout)
    clear_last_runtime_diagnostic()

    if _is_visible(context, locator, timeout=timeout,entry_point=entry_point):
        _fail(
            msg or f"断言失败：元素不应可见 -> {locator}",
            _assertion_diagnostic(
                "ASSERT_UNEXPECTED_VISIBLE",
                "目标仍然可见，但业务期望其隐藏",
                locator,
                timeout,
                last_state="not_visible",
            ),
        )
    return True


def assert_enabled(context, locator, timeout=5, msg=None):
    entry_point = log_call(locator=locator,timeout=timeout)
    clear_last_runtime_diagnostic()

    if not wait_enabled(context, locator, timeout=timeout,entry_point=entry_point):
        _fail(
            msg or f"断言失败：元素不可用 -> {locator}",
            _assertion_diagnostic(
                "ASSERT_ENABLED_TIMEOUT",
                f"{timeout} 秒内未确认目标可用；目标可能尚未出现或仍未启用",
                locator,
                timeout,
                last_state="enabled",
            ),
            prefer_last=True,
        )
    return True


def assert_disabled(context, locator, timeout=5, msg=None):
    entry_point = log_call(locator=locator,timeout=timeout)
    clear_last_runtime_diagnostic()

    if _is_enabled(context, locator, timeout=timeout,entry_point=entry_point):
        _fail(
            msg or f"断言失败：元素应为不可用状态 -> {locator}",
            _assertion_diagnostic(
                "ASSERT_UNEXPECTED_ENABLED",
                "目标仍然可用，但业务期望其禁用",
                locator,
                timeout,
                last_state="disabled",
            ),
        )
    return True


def assert_text_equal(context, locator, expected, timeout=5, msg=None):
    entry_point = log_call(locator=locator,expected=expected,timeout=timeout)
    clear_last_runtime_diagnostic()

    if not wait_exists(context, locator, timeout=timeout,entry_point=entry_point):
        _fail(
            msg or f"断言失败：元素不存在，无法校验文本 -> {locator}",
            _assertion_diagnostic(
                "ASSERT_TEXT_TARGET_TIMEOUT",
                f"{timeout} 秒内未找到可读取文本的目标",
                locator,
                timeout,
                last_state="exists",
            ),
            prefer_last=True,
        )

    actual = get_text(context, locator, timeout=timeout,entry_point=entry_point)
    if str(actual) != str(expected):
        _fail(
            msg or
            f"断言失败：文本不一致 -> locator={locator}, expected={expected}, actual={actual}",
            _assertion_diagnostic(
                "ASSERT_TEXT_MISMATCH",
                f"文本与业务期望不一致：expected={expected}, actual={actual}",
                locator,
                timeout,
                last_state="text_equal",
            ),
        )
    return True


def assert_text_contains(context, locator, expected, timeout=5, msg=None):
    entry_point = log_call(locator=locator,expected=expected,timeout=timeout)
    clear_last_runtime_diagnostic()

    if not wait_exists(context, locator, timeout=timeout,entry_point=entry_point):
        _fail(
            msg or f"断言失败：元素不存在，无法校验文本 -> {locator}",
            _assertion_diagnostic(
                "ASSERT_TEXT_TARGET_TIMEOUT",
                f"{timeout} 秒内未找到可读取文本的目标",
                locator,
                timeout,
                last_state="exists",
            ),
            prefer_last=True,
        )

    actual = get_text(context, locator, timeout=timeout,entry_point=entry_point)
    if str(expected) not in str(actual):
        _fail(
            msg or
            f"断言失败：文本不包含 -> locator={locator}, expected contains={expected}, actual={actual}",
            _assertion_diagnostic(
                "ASSERT_TEXT_CONTAINS_MISMATCH",
                f"文本未包含业务期望内容：expected={expected}, actual={actual}",
                locator,
                timeout,
                last_state="text_contains",
            ),
        )
    return True


def assert_text_not_contains(context, locator, expected, timeout=5, msg=None):
    entry_point = log_call(locator=locator,expected=expected,timeout=timeout)
    clear_last_runtime_diagnostic()

    if not wait_exists(context, locator, timeout=timeout,entry_point=entry_point):
        _fail(
            msg or f"断言失败：元素不存在，无法校验文本 -> {locator}",
            _assertion_diagnostic(
                "ASSERT_TEXT_TARGET_TIMEOUT",
                f"{timeout} 秒内未找到可读取文本的目标",
                locator,
                timeout,
                last_state="exists",
            ),
            prefer_last=True,
        )

    actual = get_text(context, locator, timeout=timeout,entry_point=entry_point)
    if str(expected) in str(actual):
        _fail(
            msg or
            f"断言失败：文本不应包含 -> locator={locator}, unexpected={expected}, actual={actual}",
            _assertion_diagnostic(
                "ASSERT_TEXT_UNEXPECTED_CONTENT",
                f"文本包含了业务上不应出现的内容：unexpected={expected}, actual={actual}",
                locator,
                timeout,
                last_state="text_not_contains",
            ),
        )
    return True


def assert_text_empty(context, locator, timeout=5, msg=None):
    entry_point = log_call(locator=locator,timeout=timeout)
    clear_last_runtime_diagnostic()

    if not wait_exists(context, locator, timeout=timeout,entry_point=entry_point):
        _fail(
            msg or f"断言失败：元素不存在，无法校验文本 -> {locator}",
            _assertion_diagnostic(
                "ASSERT_TEXT_TARGET_TIMEOUT",
                f"{timeout} 秒内未找到可读取文本的目标",
                locator,
                timeout,
                last_state="exists",
            ),
            prefer_last=True,
        )

    actual = get_text(context, locator, timeout=timeout,entry_point=entry_point)
    if str(actual) != "":
        _fail(
            msg or
            f"断言失败：文本应为空 -> locator={locator}, actual={actual}",
            _assertion_diagnostic(
                "ASSERT_TEXT_NOT_EMPTY",
                f"文本不为空：actual={actual}",
                locator,
                timeout,
                last_state="text_empty",
            ),
        )
    return True

def assert_attr_equal(context, locator, attr_name, expected, timeout=5, msg=None):
    entry_point = log_call(locator=locator,attr_name=attr_name,expected=expected,timeout=timeout)
    clear_last_runtime_diagnostic()

    actual = get_attr(context, locator, attr_name, timeout=timeout, default=None,entry_point=entry_point)
    if str(actual) != str(expected):
        _fail(
            msg or f"断言失败：属性值不一致 -> locator={locator}, attr={attr_name}, expected={expected}, actual={actual}",
            _assertion_diagnostic(
                "ASSERT_ATTR_MISMATCH",
                f"属性 {attr_name} 与业务期望不一致：expected={expected}, actual={actual}",
                locator,
                timeout,
                last_state="attribute_equal",
            ),
            prefer_last=True,
        )
    return True


def assert_attr_contains(context, locator, attr_name, expected, timeout=5, msg=None):
    entry_point = log_call(locator=locator,attr_name=attr_name,expected=expected,timeout=timeout)
    clear_last_runtime_diagnostic()

    actual = get_attr(context, locator, attr_name, timeout=timeout, default="",entry_point=entry_point)
    if str(expected) not in str(actual):
        _fail(
            msg or f"断言失败：属性值不包含 -> locator={locator}, attr={attr_name}, expected contains={expected}, actual={actual}",
            _assertion_diagnostic(
                "ASSERT_ATTR_CONTAINS_MISMATCH",
                f"属性 {attr_name} 未包含业务期望内容：expected={expected}, actual={actual}",
                locator,
                timeout,
                last_state="attribute_contains",
            ),
            prefer_last=True,
        )
    return True


def assert_collection_equal(
        context,
        locator,
        expected,
        timeout=5,
        max_items=200,
        msg=None,
    ):
    entry_point = log_call(
        locator=locator,
        expected=expected,
        timeout=timeout,
        max_items=max_items,
    )
    if not isinstance(expected, (list, tuple)):
        raise TypeError("集合断言 expected 必须是 list 或 tuple")
    clear_last_runtime_diagnostic()
    actual = get_collection_items(
        context,
        locator,
        timeout=timeout,
        max_items=max_items,
        entry_point=entry_point,
    )
    expected_items = [str(item) for item in expected]
    if actual != expected_items:
        _fail(
            msg
            or "断言失败：集合项不一致 -> "
            f"locator={locator}, expected={expected_items}, actual={actual}",
            _assertion_diagnostic(
                "ASSERT_COLLECTION_MISMATCH",
                f"集合项与业务期望不一致：expected={expected_items}, actual={actual}",
                locator,
                timeout,
                last_state="collection_equal",
            ),
            prefer_last=True,
        )
    return True

