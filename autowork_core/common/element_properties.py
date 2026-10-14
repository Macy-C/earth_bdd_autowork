import re


class ElementPropertyReadError(RuntimeError):
    def __init__(self, property_name):
        self.property_name = property_name
        super().__init__(f"无法读取元素属性: {property_name}")


def read_accessible_name(element, *, element_info=None):
    info = element_info
    if info is None:
        try:
            info = element.element_info
        except AttributeError:
            info = element
        except Exception as error:
            raise ElementPropertyReadError("name") from error

    try:
        value = info.name
    except Exception as error:
        raise ElementPropertyReadError("name") from error
    return "" if value is None else value


def matches_property_value(actual, expected):
    if isinstance(actual, (list, tuple, set)):
        return any(matches_property_value(value, expected) for value in actual)

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