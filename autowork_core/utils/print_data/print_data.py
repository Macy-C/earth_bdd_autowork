import ast
import unicodedata
from pprint import pformat


KEY_WIDTH = 48
TYPE_WIDTH = 14
VALUE_WIDTH = 90


def str_width(text):
    """
    计算字符串显示宽度。
    中文、全角字符按 2，英文按 1。
    """
    text = str(text)
    width = 0

    for char in text:
        if unicodedata.east_asian_width(char) in ("F", "W"):
            width += 2
        else:
            width += 1

    return width


def truncate_width(text, max_width):
    """
    按显示宽度截断字符串，避免表格被长内容撑乱。
    """
    text = str(text)

    if str_width(text) <= max_width:
        return text

    result = []
    width = 0

    for char in text:
        char_width = 2 if unicodedata.east_asian_width(char) in ("F", "W") else 1

        if width + char_width > max_width - 3:
            break

        result.append(char)
        width += char_width

    return "".join(result) + "..."


def ljust_cn(text, width):
    """
    中文英文混合左对齐。
    """
    text = str(text)
    return text + " " * max(0, width - str_width(text))


def clean_input_text(text):
    """
    兼容这种复制内容：
        请输入字典或列表：{'a': 1}

    自动截取第一个 { 或 [ 开始的内容。
    """
    text = str(text).strip()

    if not text:
        return text

    dict_index = text.find("{")
    list_index = text.find("[")

    indexes = [i for i in (dict_index, list_index) if i != -1]

    if indexes:
        start = min(indexes)
        return text[start:].strip()

    return text


def sanitize_object_repr(text):
    """
    把非引号内的 <xxx object at 0x...> 这种对象 repr 转成字符串。

    例如：
        {'app': <Application object at 0x123>}

    转成：
        {'app': '<Application object at 0x123>'}
    """
    result = []
    i = 0
    quote = None

    while i < len(text):
        ch = text[i]

        # 处理字符串引号状态
        if ch in ("'", '"'):
            result.append(ch)

            backslash_count = 0
            j = i - 1
            while j >= 0 and text[j] == "\\":
                backslash_count += 1
                j -= 1

            escaped = backslash_count % 2 == 1

            if not escaped:
                if quote is None:
                    quote = ch
                elif quote == ch:
                    quote = None

            i += 1
            continue

        # 引号内部不处理
        if quote:
            result.append(ch)
            i += 1
            continue

        # 处理 <...>
        if ch == "<":
            start = i
            i += 1

            while i < len(text):
                if text[i] == ">":
                    i += 1
                    break
                i += 1

            obj_repr = text[start:i]
            result.append(repr(obj_repr))
            continue

        result.append(ch)
        i += 1

    return "".join(result)


def sanitize_callable_repr(text):
    """
    把非引号内的 Name(...) 这种调用形式整体转成字符串。

    例如：
        {'a': CompiledLocator(prefix='child', root=None)}

    转成：
        {'a': "CompiledLocator(prefix='child', root=None)"}

    说明：
    - 不关心 Name 是类、函数还是其他可调用对象
    - 统一按字符串处理
    - 支持括号嵌套
    - 支持参数内部有字符串
    """
    result = []
    i = 0
    quote = None
    length = len(text)

    while i < length:
        ch = text[i]

        # 处理字符串引号状态
        if ch in ("'", '"'):
            result.append(ch)

            backslash_count = 0
            j = i - 1
            while j >= 0 and text[j] == "\\":
                backslash_count += 1
                j -= 1

            escaped = backslash_count % 2 == 1

            if not escaped:
                if quote is None:
                    quote = ch
                elif quote == ch:
                    quote = None

            i += 1
            continue

        # 引号内部不处理
        if quote:
            result.append(ch)
            i += 1
            continue

        # 尝试识别 Name(...)
        if ch.isalpha() or ch == "_":
            start = i

            # 读取名字
            while i < length and (text[i].isalnum() or text[i] == "_"):
                i += 1

            # 暂存名字文本
            name_text = text[start:i]

            # 跳过空白
            k = i
            while k < length and text[k].isspace():
                k += 1

            # 如果后面不是 (，就不是调用
            if k >= length or text[k] != "(":
                result.append(name_text)
                continue

            # 识别完整的调用片段 Name(...)
            call_start = start
            i = k
            depth = 0
            inner_quote = None

            while i < length:
                c = text[i]

                if inner_quote:
                    if c in ("'", '"'):
                        backslash_count = 0
                        j = i - 1
                        while j >= 0 and text[j] == "\\":
                            backslash_count += 1
                            j -= 1
                        escaped = backslash_count % 2 == 1
                        if not escaped and c == inner_quote:
                            inner_quote = None
                    i += 1
                    continue

                if c in ("'", '"'):
                    inner_quote = c
                    i += 1
                    continue

                if c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break

                i += 1

            call_text = text[call_start:i]
            result.append(repr(call_text))
            continue

        result.append(ch)
        i += 1

    return "".join(result)


def parse_text_to_data(text):
    """
    把用户输入解析成 Python 数据。

    支持：
    - 普通 dict/list/tuple/set
    - <Application object at 0x123> 这类对象 repr
    - 任意 Name(...) 调用形式，统一按字符串处理
    """
    text = clean_input_text(text)

    # 先按正常 Python 字面量解析
    try:
        return ast.literal_eval(text)
    except Exception:
        pass

    # 处理 <xxx object at 0x...>
    fixed_text = sanitize_object_repr(text)

    # 处理任意 Name(...)
    fixed_text = sanitize_callable_repr(fixed_text)

    try:
        return ast.literal_eval(fixed_text)
    except Exception as e:
        raise ValueError(e)


def value_type_name(value):
    """
    获取显示用类型名。
    """
    if isinstance(value, dict):
        return "dict"
    if isinstance(value, list):
        return "list"
    if isinstance(value, tuple):
        return "tuple"
    if isinstance(value, set):
        return "set"
    if value is None:
        return "NoneType"
    return type(value).__name__


def value_display(value):
    """
    获取显示用 value。
    容器类型不在 value 列展示内容，只显示类型。
    """
    if isinstance(value, (dict, list, tuple, set)):
        return ""

    try:
        if isinstance(value, str):
            text = value
        else:
            text = repr(value)
    except Exception:
        text = f"<unreprable {type(value).__name__}>"

    return truncate_width(text, VALUE_WIDTH)


def flatten_data(data, name="root", level=0, rows=None, prefix="", is_last=True, visited=None, max_depth=8):
    """
    把嵌套数据展开成表格行。
    使用树形符号显示层级。
    """
    if rows is None:
        rows = []

    if visited is None:
        visited = set()

    connector = "└─ " if is_last else "├─ "
    tree_name = prefix + connector + str(name)

    type_name = value_type_name(data)
    value_text = value_display(data)

    rows.append([
        str(level),
        truncate_width(tree_name, KEY_WIDTH),
        type_name,
        value_text
    ])

    # 基础类型不继续展开
    if not isinstance(data, (dict, list, tuple, set)):
        return rows

    obj_id = id(data)

    # 防循环引用
    if obj_id in visited:
        rows.append([
            str(level + 1),
            truncate_width(prefix + "   └─ <cycle reference>", KEY_WIDTH),
            "cycle",
            ""
        ])
        return rows

    if level >= max_depth:
        rows.append([
            str(level + 1),
            truncate_width(prefix + "   └─ <max depth>", KEY_WIDTH),
            "limit",
            ""
        ])
        return rows

    visited.add(obj_id)

    # 子节点前缀
    child_prefix = prefix + ("   " if is_last else "│  ")

    if isinstance(data, dict):
        items = list(data.items())
        for index, (key, value) in enumerate(items):
            flatten_data(
                value,
                name=key,
                level=level + 1,
                rows=rows,
                prefix=child_prefix,
                is_last=index == len(items) - 1,
                visited=visited,
                max_depth=max_depth
            )

    elif isinstance(data, (list, tuple)):
        items = list(enumerate(data))
        for index, value in items:
            flatten_data(
                value,
                name=f"[{index}]",
                level=level + 1,
                rows=rows,
                prefix=child_prefix,
                is_last=index == len(items) - 1,
                visited=visited,
                max_depth=max_depth
            )

    elif isinstance(data, set):
        items = list(data)
        for index, value in enumerate(items):
            flatten_data(
                value,
                name=f"set[{index}]",
                level=level + 1,
                rows=rows,
                prefix=child_prefix,
                is_last=index == len(items) - 1,
                visited=visited,
                max_depth=max_depth
            )

    visited.remove(obj_id)
    return rows


def print_table(rows):
    """
    打印固定宽度表格，避免长内容撑乱。
    """
    headers = ["层级", "Key / Index", "Type", "Value"]

    widths = [
        4,
        KEY_WIDTH,
        TYPE_WIDTH,
        VALUE_WIDTH
    ]

    def line():
        return "+" + "+".join("-" * (w + 2) for w in widths) + "+"

    print(line())
    print("| " + " | ".join(ljust_cn(headers[i], widths[i]) for i in range(len(headers))) + " |")
    print(line())

    for row in rows:
        row = [
            truncate_width(row[0], widths[0]),
            truncate_width(row[1], widths[1]),
            truncate_width(row[2], widths[2]),
            truncate_width(row[3], widths[3]),
        ]

        print("| " + " | ".join(ljust_cn(row[i], widths[i]) for i in range(len(headers))) + " |")

    print(line())


def print_dual_view(data, max_depth=8):
    """
    同时打印结构化摘要和全量内容。

    - 结构化摘要：沿用表格视图，便于快速看层级和类型
    - 全量内容：使用 pformat，保留完整内容，不做截断
    """
    if not isinstance(data, (dict, list, tuple, set)):
        print("\n请输入字典、列表、元组或集合类型的数据。")
        return

    print("\n结构化摘要：\n")
    rows = flatten_data(
        data,
        name="root",
        level=0,
        max_depth=max_depth
    )
    print_table(rows)

    print("\n全量内容：\n")
    print(pformat(data, width=120, sort_dicts=False))


def parse_and_print(text, max_depth=8):
    """
    解析并打印。
    """
    try:
        data = parse_text_to_data(text)
    except Exception as e:
        print("\n输入格式不正确，无法解析。")
        print("错误信息：", e)
        return

    print_dual_view(data, max_depth=max_depth)


def main():
    print("=" * 80)
    print("字典 / 列表 / 元组 / 集合 结构打印工具")
    print("=" * 80)
    print("输入 Python 格式的数据，然后按回车解析。")
    print("退出方式：输入 q / quit / exit 后回车。")
    print()
    print("示例1：{1: 2, 'a': {3: 4, 'b': {5: 6}}}")
    print("示例2：[1, 2, {'a': 3, 'b': [4, 5]}]")
    print("示例3：请输入字典或列表：{'app': <Application object at 0x123>}")
    print("示例4：{'a': CompiledLocator(prefix='child', criteria={'auto_id': '1'}, root=None)}")
    print("示例5：{'x': SomeClass(a=1, b=[1, 2, 3]), 'y': Demo(k={'n': 1})}")
    print("=" * 80)

    while True:
        print()
        text = input("请输入字典或列表：").strip()

        if text.lower() in ("q", "quit", "exit"):
            print("\n程序已退出。")
            break

        if not text:
            print("输入为空，请重新输入。")
            continue

        parse_and_print(text)


if __name__ == "__main__":
    main()

