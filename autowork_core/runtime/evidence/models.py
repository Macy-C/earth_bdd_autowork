from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
import re


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def captured_value(value, seen=None):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, Enum):
        return captured_value(value.value, seen)
    if isinstance(value, float):
        return value if value not in (float("inf"), float("-inf")) and value == value else str(value)
    seen = set() if seen is None else seen
    if len(seen) >= 24:
        return {"unavailable": "maximum depth"}
    identity = id(value)
    if identity in seen:
        return {"unavailable": "cyclic value"}
    if isinstance(value, (dict, list, tuple)) or (is_dataclass(value) and not isinstance(value, type)):
        seen.add(identity)
        try:
            if is_dataclass(value):
                return {item.name: captured_value(getattr(value, item.name), seen) for item in fields(value)}
            if isinstance(value, dict):
                return {str(key): captured_value(item, seen) for key, item in value.items()
                        if isinstance(key, (str, int, float, bool)) or key is None}
            return [captured_value(item, seen) for item in value]
        finally:
            seen.remove(identity)
    return {"unavailable": "not serialized", "type": f"{type(value).__module__}.{type(value).__qualname__}"}


def redacted(text):
    return re.sub(
        r'''(?i)(["']?(?:password|passwd|token|api[_-]?key|secret)["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;]+)''',
        r"\1<redacted>", str(text),
    )


def plain_record(value):
    if isinstance(value, dict):
        return {key: plain_record(item) for key, item in value.items() if not str(key).startswith("_")}
    if isinstance(value, (list, tuple)):
        return [plain_record(item) for item in value]
    return captured_value(value)


def step_fact(step):
    table = getattr(step, "table", None)
    return {
        "name": str(getattr(step, "name", "")),
        "keyword": str(getattr(step, "keyword", "")),
        "step_type": str(getattr(step, "step_type", "")),
        "line": getattr(step, "line", None),
        "text": None if getattr(step, "text", None) is None else str(step.text),
        "table": None if table is None else {
            "headings": list(table.headings), "rows": [list(row.cells) for row in table.rows],
        },
    }


def row_fact(row):
    return None if row is None else {
        "id": getattr(row, "id", None), "line": getattr(row, "line", None),
        "headings": list(getattr(row, "headings", ())), "cells": list(getattr(row, "cells", ())),
    }