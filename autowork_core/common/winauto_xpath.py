import re

from autowork_core.common.compile import XPATH_KEY_MAP
from autowork_core.common.element_properties import (
    ElementPropertyReadError,
    read_accessible_name,
)
from pywinauto.uia_defines import NoPatternInterfaceError


def find_by_xpath(root, xpath, first_only):
    """
    //Button
    //Button[0]
    //Button[-1]
    //Button[@auto_id='id']
    //*[@auto_id='id']
    //Button[@auto_id='id'][@name='OK']
    //Button[@auto_id='id' and @name='OK']
    //Button[contains(@name,'OK')]
    //Window[@name='计算器']/Group
    //Group[@name='数字键盘']//Button[@auto_id='num5Button']
    //Text[@LegacyIAccessible.Value='完成']
    //Edit[@Value.Value='admin']
    //Window[@auto_id='qSlicerMainWindow.qSlicerDataDialog']//Button[@name='OK']
    //Text[@name='姓名']/next::Edit
    //Button[@name='查询']/prev::*[0]
    //Text[@name='姓名']/parent::Group
    """

    steps = parse_xpath(xpath)
    if _needs_node_context(steps):
        return _find_by_xpath_with_sibling(root, xpath, steps, first_only)

    return _find_by_xpath_simple(root, xpath, steps, first_only)


def _find_by_xpath_simple(root, xpath, steps, first_only):
    current_nodes = [root]

    for step_index, step in enumerate(steps):
        next_nodes = []
        is_last_step = step_index == len(steps) - 1

        for parent in current_nodes:
            candidates = _select_candidates(
                parent,
                step,
                include_self=step_index == 0 and step["axis"] == "desc",
            )

            if first_only and is_last_step and step["index"] is None:
                for element in candidates:
                    if _match_element(element, step):
                        return element
                continue

            matched = [
                element for element in candidates
                if _match_element(element, step)
            ]

            if step["index"] is not None:
                try:
                    matched = [matched[step["index"]]]
                except IndexError:
                    matched = []

            next_nodes.extend(matched)

        current_nodes = next_nodes

        if not current_nodes:
            raise LookupError(
                f"XPath 未找到元素: {xpath}, 失败 step={step}"
            )

    if first_only:
        return current_nodes[0]

    return current_nodes


def _find_by_xpath_with_sibling(root, xpath, steps, first_only):
    current_nodes = [_make_node(root)]

    for step_index, step in enumerate(steps):
        next_nodes = []
        is_last_step = step_index == len(steps) - 1

        for parent_node in current_nodes:
            candidates = _select_candidate_nodes(parent_node, step)
            if step_index == 0 and step["axis"] == "desc":
                candidates = [parent_node] + candidates

            if first_only and is_last_step and step["index"] is None:
                for candidate in candidates:
                    if _match_element(candidate["element"], step):
                        return candidate["element"]
                continue

            matched = [
                candidate for candidate in candidates
                if _match_element(candidate["element"], step)
            ]

            if step["index"] is not None:
                try:
                    matched = [matched[step["index"]]]
                except IndexError:
                    matched = []

            next_nodes.extend(matched)

        current_nodes = next_nodes

        if not current_nodes:
            raise LookupError(
                f"XPath 未找到元素: {xpath}, 失败 step={step}"
            )

    if first_only:
        return current_nodes[0]["element"]

    return [node["element"] for node in current_nodes]


def _needs_node_context(steps):
    return any(
        step["axis"] in ("next", "prev", "parent", "ancestor")
        for step in steps
    )


def _make_node(element, parent=None, siblings=None, sibling_index=None, parent_node=None):
    return {
        "element": element,
        "parent": parent,
        "siblings": siblings,
        "sibling_index": sibling_index,
        "parent_node": parent_node,
    }


#======================================================= xpath =========================================================


def _get_attr(element, key):
    """
    根据 XPath 属性名读取 pywinauto 元素属性。
    """
    info = element.element_info

    key = key.strip().lower()

    if key in XPATH_KEY_MAP:
        real_key = XPATH_KEY_MAP[key]

        if real_key == "name":
            return _get_name(element, info)

        if real_key == "enabled":
            return _call_wrapper_bool(element, "is_enabled")

        if real_key == "visible":
            return _call_wrapper_bool(element, "is_visible")

        if real_key == "value.value":
            return _get_value(element, info)

        if real_key == "value.isreadonly":
            return _get_value_is_readonly(element, info)

        if real_key.startswith("legacyiaccessible."):
            return _get_legacy_attr(element, info, real_key)

        return getattr(info, real_key, None)

    if key.startswith("legacyiaccessible."):
        return _get_legacy_attr(element, info, key)

    if key == "value.value":
        return _get_value(element, info)

    if key == "value.isreadonly":
        return _get_value_is_readonly(element, info)

    return getattr(info, key, None)


def _get_name(element, info):
    return read_accessible_name(element, element_info=info)


def _get_value(element, info):
    try:
        return element.iface_value.CurrentValue
    except NoPatternInterfaceError:
        return None
    except Exception as error:
        raise ElementPropertyReadError("Value.Value") from error


def _get_value_is_readonly(element, info):
    try:
        return element.iface_value.CurrentIsReadOnly
    except NoPatternInterfaceError:
        return None
    except Exception as error:
        raise ElementPropertyReadError("Value.IsReadOnly") from error


def _get_legacy_attr(element, info, key):
    legacy_key = key.split(".", 1)[1].lower()
    try:
        legacy_props = element.legacy_properties()
    except NoPatternInterfaceError:
        return None
    except Exception as error:
        raise ElementPropertyReadError(key) from error

    for k, v in legacy_props.items():
        if str(k).lower() == legacy_key:
            return v

    return None


def _call_wrapper_bool(element, method_name):
    method = getattr(element, method_name, None)
    if method is None:
        return None
    try:
        return bool(method()) if callable(method) else bool(method)
    except Exception as error:
        raise ElementPropertyReadError(method_name) from error


def _split_xpath_steps(xpath):
    """
    稳定拆分 XPath。

    支持：
      //Button
      /Pane/Button
      //Group[@name='A']//Button[@name='B']

    不会因为属性值里的 / 被错误拆分。
    """

    xpath = xpath.strip()

    if not xpath:
        raise ValueError("XPath 不能为空")

    if _has_unquoted_pipe(xpath):
        raise ValueError("当前简化 XPath 不支持 | 联合表达式")

    steps = []
    i = 0
    axis = None
    buf = []
    bracket_depth = 0
    quote = None

    while i < len(xpath):
        ch = xpath[i]

        # 处理引号
        if ch in ("'", '"'):
            if quote is None:
                quote = ch
            elif quote == ch:
                quote = None

            buf.append(ch)
            i += 1
            continue

        # 引号内部不解析结构
        if quote:
            buf.append(ch)
            i += 1
            continue

        # 处理 predicate 深度
        if ch == "[":
            bracket_depth += 1
            buf.append(ch)
            i += 1
            continue

        if ch == "]":
            bracket_depth -= 1
            if bracket_depth < 0:
                raise ValueError(f"XPath 方括号不匹配: {xpath}")

            buf.append(ch)
            i += 1
            continue

        # 只有不在 [] 里面，才解析 / 和 //
        if bracket_depth == 0 and xpath.startswith("//", i):
            if buf:
                steps.append((axis or "desc", "".join(buf).strip()))
                buf = []

            axis = "desc"
            i += 2
            continue

        if bracket_depth == 0 and ch == "/":
            if buf:
                steps.append((axis or "desc", "".join(buf).strip()))
                buf = []

            axis = "child"
            i += 1
            continue

        buf.append(ch)
        i += 1

    if quote:
        raise ValueError(f"XPath 引号不闭合: {xpath}")

    if bracket_depth != 0:
        raise ValueError(f"XPath 方括号不匹配: {xpath}")

    if buf:
        steps.append((axis or "desc", "".join(buf).strip()))

    if not steps:
        raise ValueError(f"XPath 未解析到任何 step: {xpath}")

    return steps


def _has_unquoted_pipe(text):
    quote = None

    for ch in text:
        if ch in ("'", '"'):
            if quote is None:
                quote = ch
            elif quote == ch:
                quote = None
            continue

        if quote:
            continue

        if ch == "|":
            return True

    if quote:
        raise ValueError(f"XPath 引号不闭合: {text}")

    return False


def _parse_predicates(predicate_text):
    """
    解析 [] 内的条件。

    支持：
      [0]
      [-1]
      [@name='OK']
      [@auto_id="btnOk"]
      [contains(@name,'OK')]
      [@name='OK' and @auto_id='xxx']
    """

    predicate_text = predicate_text.strip()

    if not predicate_text:
        return {
            "index": None,
            "equals": [],
            "contains": [],
        }

    # index: [0] / [-1]
    if re.fullmatch(r"-?\d+", predicate_text):
        return {
            "index": int(predicate_text),
            "equals": [],
            "contains": [],
        }

    equals = []
    contains = []

    parts = _split_predicate_by_and(predicate_text)

    for part in parts:
        part = part.strip()

        # contains(@name,'xxx')
        m = re.fullmatch(
            r"contains\(\s*@?([\w\.]+)\s*,\s*(['\"])(.*?)\2\s*\)",
            part
        )
        if m:
            contains.append((m.group(1), m.group(3)))
            continue

        # @name='xxx'
        m = re.fullmatch(
            r"@?([\w\.]+)\s*=\s*(['\"])(.*?)\2",
            part
        )
        if m:
            equals.append((m.group(1), m.group(3)))
            continue

        raise ValueError(f"不支持的 XPath 条件: [{part}]")

    return {
        "index": None,
        "equals": equals,
        "contains": contains,
    }


def _split_predicate_by_and(predicate_text):
    parts = []
    buf = []
    quote = None
    i = 0

    while i < len(predicate_text):
        ch = predicate_text[i]

        if ch in ("'", '"'):
            if quote is None:
                quote = ch
            elif quote == ch:
                quote = None
            buf.append(ch)
            i += 1
            continue

        if quote:
            buf.append(ch)
            i += 1
            continue

        if _is_and_operator(predicate_text, i):
            part = "".join(buf).strip()
            if not part:
                raise ValueError(f"XPath 条件 and 前缺少表达式: [{predicate_text}]")
            parts.append(part)
            buf = []
            i += 3
            continue

        buf.append(ch)
        i += 1

    if quote:
        raise ValueError(f"XPath 条件引号不闭合: [{predicate_text}]")

    part = "".join(buf).strip()
    if not part:
        raise ValueError(f"XPath 条件 and 后缺少表达式: [{predicate_text}]")
    parts.append(part)

    return parts


def _is_and_operator(text, index):
    if text[index:index + 3].lower() != "and":
        return False

    before = text[index - 1] if index > 0 else ""
    after_index = index + 3
    after = text[after_index] if after_index < len(text) else ""

    return bool(before and before.isspace() and after and after.isspace())


def _parse_step(step_text):
    """
    解析单个 step。

    Button[@name='OK'][0]
    *[@auto_id='xxx']
    Button[contains(@name,'OK')]
    """

    step_text = step_text.strip()

    tag_match = re.match(r"^(\*|[A-Za-z_][\w]*)", step_text)

    if not tag_match:
        raise ValueError(f"XPath step 缺少控件类型: {step_text}")

    tag = tag_match.group(1)
    rest = step_text[tag_match.end():].strip()

    index = None
    equals = []
    contains = []

    while rest:
        if not rest.startswith("["):
            raise ValueError(f"XPath step 格式错误: {step_text}")

        end = _find_matching_bracket(rest)

        predicate = rest[1:end]
        parsed = _parse_predicates(predicate)

        if parsed["index"] is not None:
            if index is not None:
                raise ValueError(f"同一个 step 不允许多个 index: {step_text}")
            index = parsed["index"]

        equals.extend(parsed["equals"])
        contains.extend(parsed["contains"])

        rest = rest[end + 1:].strip()

    return {
        "tag": tag,
        "equals": equals,
        "contains": contains,
        "index": index,
    }


def _find_matching_bracket(text):
    """
    找到 text 中第一个 [ 对应的 ]。
    text 必须以 [ 开头。
    """

    if not text.startswith("["):
        raise ValueError(f"不是 predicate: {text}")

    quote = None
    depth = 0

    for i, ch in enumerate(text):
        if ch in ("'", '"'):
            if quote is None:
                quote = ch
            elif quote == ch:
                quote = None

            continue

        if quote:
            continue

        if ch == "[":
            depth += 1

        elif ch == "]":
            depth -= 1

            if depth == 0:
                return i

    raise ValueError(f"方括号不闭合: {text}")


def parse_xpath(xpath):
    """
    完整解析 XPath。
    返回 steps。
    """

    raw_steps = _split_xpath_steps(xpath)

    steps = []
    for axis, step_text in raw_steps:
        axis, step_text = _parse_explicit_axis(axis, step_text)
        step = _parse_step(step_text)
        step["axis"] = axis
        steps.append(step)
    return steps


def _parse_explicit_axis(axis, step_text):
    match = re.match(
        r"^\s*(next|prev|parent|ancestor|child|descendant|"
        r"following-sibling|preceding-sibling)::(.+)$",
        step_text,
        flags=re.I,
    )
    if not match:
        return axis, step_text

    target = match.group(2).strip()
    if not target:
        raise ValueError(f"XPath axis step 缺少目标控件类型: {step_text}")

    normalized = {
        "following-sibling": "next",
        "preceding-sibling": "prev",
        "descendant": "desc",
    }.get(match.group(1).lower(), match.group(1).lower())
    return normalized, target


def _equals(actual, expected):
    if isinstance(actual, (list, tuple, set)):
        return any(_equals(value, expected) for value in actual)

    if isinstance(actual, bool):
        expected_text = str(expected).strip().lower()
        if expected_text in ("true", "1", "yes", "y"):
            return actual is True
        if expected_text in ("false", "0", "no", "n"):
            return actual is False

    actual_text = str(actual).strip()
    expected_text = str(expected).strip()

    if actual_text == expected_text:
        return True

    if _is_number_equivalent_allowed(actual, expected):
        actual_number = _number_value(actual)
        expected_number = _number_value(expected)
        if actual_number is not None and expected_number is not None:
            return actual_number == expected_number

    expected_label = _display_label(expected_text)
    if expected_label and actual_text == expected_label:
        return True

    actual_label = _display_label(actual_text)
    if actual_label and actual_label == expected_text:
        return True

    return False


def _number_value(value):
    if isinstance(value, bool):
        return None

    if isinstance(value, int):
        return value

    text = str(value).strip()
    match = re.search(r"\(\s*0x([0-9a-fA-F]+)\s*\)", text)
    if match:
        return int(match.group(1), 16)

    if re.fullmatch(r"0x[0-9a-fA-F]+", text):
        return int(text, 16)

    if re.fullmatch(r"-?\d+", text):
        return int(text)

    return None


def _is_number_equivalent_allowed(actual, expected):
    return (
        isinstance(actual, int)
        and not isinstance(actual, bool)
    ) or _has_hex_number(actual) or _has_hex_number(expected)


def _has_hex_number(value):
    text = str(value).strip()
    return bool(
        re.search(r"\(\s*0x[0-9a-fA-F]+\s*\)", text)
        or re.fullmatch(r"0x[0-9a-fA-F]+", text)
    )


def _display_label(text):
    match = re.match(r"^(.+?)\s*\(\s*0x[0-9a-fA-F]+\s*\)$", text)
    return match.group(1).strip() if match else None


def _match_element(element, step):
    """
    判断元素是否匹配 step。
    """

    info = element.element_info

    tag = step["tag"]

    if tag != "*":
        actual_type = info.control_type

        if str(actual_type).lower() != str(tag).lower():
            return False

    for key, expected in step["equals"]:
        actual = _get_attr(element, key)

        if not _equals(actual, expected):
            return False

    for key, expected in step["contains"]:
        actual = _get_attr(element, key)

        if actual is None:
            return False

        if str(expected).lower() not in str(actual).lower():
            return False

    return True


def _select_candidates(parent, step, include_self=False):
    """
    根据 axis 获取候选元素。
    """

    tag = step["tag"]
    control_type = None if tag == "*" else tag

    if step["axis"] == "child":
        if control_type:
            return parent.children(control_type=control_type)
        return parent.children()

    if step["axis"] == "desc":
        if control_type:
            candidates = parent.descendants(control_type=control_type)
        else:
            candidates = parent.descendants()
        return [parent] + candidates if include_self else candidates

    raise ValueError(f"不支持的 axis: {step['axis']}")


def _select_candidate_nodes(parent_node, step):
    axis = step["axis"]

    if axis == "child":
        return _select_child_nodes(parent_node)

    if axis == "desc":
        return list(_iter_descendant_nodes(parent_node))

    if axis in ("next", "prev"):
        return _select_sibling_nodes(parent_node, direction=axis)

    if axis == "parent":
        parent_node = parent_node.get("parent_node")
        return [parent_node] if parent_node is not None else []

    if axis == "ancestor":
        return _select_ancestor_nodes(parent_node)

    raise ValueError(f"不支持的 axis: {axis}")


def _select_ancestor_nodes(node):
    values = []
    current = node.get("parent_node")
    while current is not None:
        values.append(current)
        current = current.get("parent_node")
    return values


def _select_child_nodes(parent_node):
    parent = parent_node["element"]
    children = parent.children()
    return [
        _make_node(
            child,
            parent=parent,
            siblings=children,
            sibling_index=index,
            parent_node=parent_node,
        )
        for index, child in enumerate(children)
    ]


def _iter_descendant_nodes(parent_node):
    parent = parent_node["element"]
    children = parent.children()
    for index, child in enumerate(children):
        child_node = _make_node(
            child,
            parent=parent,
            siblings=children,
            sibling_index=index,
            parent_node=parent_node,
        )
        yield child_node
        yield from _iter_descendant_nodes(child_node)


def _select_sibling_nodes(node, direction):
    siblings = node.get("siblings")
    sibling_index = node.get("sibling_index")

    if siblings is None or sibling_index is None:
        parent = node.get("parent")
        if parent is None:
            return []
        siblings = parent.children()
        sibling_index = _find_sibling_index(siblings, node["element"])
        if sibling_index is None:
            return []

    if direction == "next":
        indexes = range(sibling_index + 1, len(siblings))
    elif direction == "prev":
        indexes = range(sibling_index - 1, -1, -1)
    else:
        raise ValueError(f"不支持的兄弟方向: {direction}")

    parent = node.get("parent")
    return [
        _make_node(
            siblings[index],
            parent=parent,
            siblings=siblings,
            sibling_index=index,
            parent_node=node.get("parent_node"),
        )
        for index in indexes
    ]


def _find_sibling_index(siblings, target):
    for index, element in enumerate(siblings):
        if element is target or _same_element(element, target):
            return index
    return None


def _same_element(left, right):
    if left is right:
        return True

    left_info = getattr(left, "element_info", None)
    right_info = getattr(right, "element_info", None)
    if left_info is None or right_info is None:
        return False

    for attr in ("handle", "runtime_id"):
        left_value = getattr(left_info, attr, None)
        right_value = getattr(right_info, attr, None)
        if left_value and right_value and left_value == right_value:
            return True

    try:
        return left.rectangle() == right.rectangle()
    except Exception:
        return False


#======================================================= xpath =========================================================


