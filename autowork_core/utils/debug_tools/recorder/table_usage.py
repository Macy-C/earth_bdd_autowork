from __future__ import annotations

import hashlib
import json
import re


TABLE_CONSUMPTIONS = {"each_row", "scenario_state", "whole_table"}
TABLE_SHAPES = {"action_sequence", "list", "mapping", "object", "records"}
TABLE_CONSUMERS = {"page_object", "scenario_context", "step_definition"}
TABLE_COLUMN_ROLES = {
    "action",
    "expected",
    "field",
    "input",
    "key",
    "metadata",
    "option",
    "target",
    "value",
}
TABLE_BUSINESS_OUTCOMES = {
    "continuous_rows",
    "independent_rows",
    "scenario_state",
    "whole_table",
}
_CONTEXT_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_EACH_ROW_MARKERS = (
    "for each",
    "for every",
    "each entered",
    "each input",
    "each row",
    "every row",
    "每个",
    "每组",
    "逐行",
)
_RESET_MARKERS = (
    "each independently",
    "independently reset",
    "independent row",
    "reset between",
    "reset for each",
    "每行独立",
    "每组独立",
    "逐行重置",
)
_CARRY_MARKERS = (
    "carry state",
    "cumulative",
    "continue from previous",
    "累计",
    "连续处理",
    "沿用上一行",
)
_OPTION_LIST_MARKERS = (
    "available options",
    "following options",
    "provides the following",
    "以下选项",
    "可用选项",
)
_ACTION_SEQUENCE_MARKERS = (
    "actions in order",
    "following actions",
    "in the following order",
    "按顺序",
    "以下操作",
)
_OBJECT_MARKERS = (
    "configured with the following",
    "following fields",
    "以下字段",
    "以下配置",
)
_STORE_MARKERS = (
    "available for subsequent",
    "defined for later",
    "for later steps",
    "store for later",
    "subsequent steps",
    "供后续",
    "保存供后续",
)


def normalize_table_usage(value):
    if not isinstance(value, dict) or not value:
        return None
    return {
        "consumption": str(value.get("consumption") or "").strip(),
        "shape": str(value.get("shape") or "").strip(),
        "consumer": str(value.get("consumer") or "").strip(),
        "ordered": value.get("ordered"),
        "reset_between_rows": value.get("reset_between_rows"),
        "columns": {
            str(column): str(role).strip()
            for column, role in dict(value.get("columns") or {}).items()
        },
        "context_key": str(value.get("context_key") or "").strip() or None,
        "reason": str(value.get("reason") or "").strip(),
    }


def validate_table_usage(step_id, value, table, *, required=False):
    table = table if isinstance(table, dict) and table else None
    usage = normalize_table_usage(value)
    if usage is None:
        return [f"Step {step_id} table_usage 缺失"] if required else []
    if table is None:
        return [f"Step {step_id} 没有 Data Table，不能声明 table_usage"]

    errors = []
    consumption = usage["consumption"]
    shape = usage["shape"]
    consumer = usage["consumer"]
    if consumption not in TABLE_CONSUMPTIONS:
        errors.append(
            f"Step {step_id} table_usage.consumption 无效: {consumption}"
        )
    if shape not in TABLE_SHAPES:
        errors.append(f"Step {step_id} table_usage.shape 无效: {shape}")
    if consumer not in TABLE_CONSUMERS:
        errors.append(
            f"Step {step_id} table_usage.consumer 无效: {consumer}"
        )
    if not isinstance(usage["ordered"], bool):
        errors.append(f"Step {step_id} table_usage.ordered 必须是 boolean")

    reset = usage["reset_between_rows"]
    if consumption == "each_row" and not isinstance(reset, bool):
        errors.append(
            f"Step {step_id} each_row 必须声明 reset_between_rows"
        )
    elif consumption != "each_row" and reset is not None:
        errors.append(
            f"Step {step_id} 非 each_row 不能声明 reset_between_rows"
        )

    headings = [str(item) for item in table.get("headings") or ()]
    columns = usage["columns"]
    if set(columns) != set(headings):
        errors.append(
            f"Step {step_id} table_usage 列范围不匹配: "
            f"expected={headings}, actual={sorted(columns)}"
        )
    invalid_roles = sorted(
        role for role in set(columns.values())
        if role not in TABLE_COLUMN_ROLES
    )
    if invalid_roles:
        errors.append(
            f"Step {step_id} table_usage 列角色无效: {invalid_roles}"
        )

    context_key = usage["context_key"]
    if consumption == "scenario_state":
        if consumer != "scenario_context":
            errors.append(
                f"Step {step_id} scenario_state 必须由 scenario_context 消费"
            )
        if context_key is None:
            errors.append(f"Step {step_id} scenario_state 缺少 context_key")
    else:
        if consumer == "scenario_context":
            errors.append(
                f"Step {step_id} scenario_context 只适用于 scenario_state"
            )
        if context_key is not None:
            errors.append(
                f"Step {step_id} 只有 scenario_state 可以声明 context_key"
            )
    if context_key is not None and _CONTEXT_KEY.fullmatch(context_key) is None:
        errors.append(f"Step {step_id} table_usage.context_key 无效")

    if shape == "list" and list(columns.values()).count("option") != 1:
        errors.append(f"Step {step_id} list 必须声明一个 option 列")
    if shape == "mapping" and any((
        list(columns.values()).count("key") != 1,
        list(columns.values()).count("value") != 1,
    )):
        errors.append(f"Step {step_id} mapping 必须声明一个 key 和一个 value 列")
    if shape == "action_sequence" and any((
        consumption != "each_row",
        usage["ordered"] is not True,
        list(columns.values()).count("action") != 1,
    )):
        errors.append(
            f"Step {step_id} action_sequence 必须按行有序并声明一个 action 列"
        )
    if not usage["reason"]:
        errors.append(f"Step {step_id} table_usage 缺少 reason")
    return errors


def validate_table_business_outcome(step_id, outcome, table_usage):
    outcome = str(outcome or "").strip()
    usage = normalize_table_usage(table_usage)
    if outcome not in TABLE_BUSINESS_OUTCOMES:
        return [f"Step {step_id} table business outcome 无效: {outcome}"]
    if usage is None:
        return [
            f"Step {step_id} 缺少实现 table business outcome {outcome} "
            "所需的 table_usage"
        ]
    matches = {
        "independent_rows": (
            usage["consumption"] == "each_row"
            and usage["reset_between_rows"] is True
        ),
        "continuous_rows": (
            usage["consumption"] == "each_row"
            and usage["reset_between_rows"] is False
        ),
        "whole_table": usage["consumption"] == "whole_table",
        "scenario_state": usage["consumption"] == "scenario_state",
    }
    if matches[outcome]:
        return []
    return [
        f"Step {step_id} table_usage 与用户确认的业务关系不一致: "
        f"outcome={outcome}, consumption={usage['consumption']}, "
        f"reset_between_rows={usage['reset_between_rows']}"
    ]


def table_business_outcome_candidates(table_usage_candidates):
    grouped = {}
    for candidate in table_usage_candidates or ():
        usage = normalize_table_usage(candidate.get("table_usage"))
        if usage is None:
            continue
        if usage["consumption"] == "scenario_state":
            outcome = "scenario_state"
        elif usage["consumption"] == "whole_table":
            outcome = "whole_table"
        elif (
            usage["consumption"] == "each_row"
            and usage["reset_between_rows"] is True
        ):
            outcome = "independent_rows"
        elif (
            usage["consumption"] == "each_row"
            and usage["reset_between_rows"] is False
        ):
            outcome = "continuous_rows"
        else:
            continue
        confidence = float(candidate.get("confidence") or 0.0)
        if confidence > float((grouped.get(outcome) or {}).get("confidence") or 0.0):
            grouped[outcome] = {
                "outcome": outcome,
                "confidence": confidence,
            }
    return [
        grouped[outcome]
        for outcome in (
            "independent_rows",
            "continuous_rows",
            "whole_table",
            "scenario_state",
        )
        if outcome in grouped
    ]


def infer_table_usage(step, *, code_candidates=()):
    candidates = table_usage_candidates(step)
    _apply_code_hints(
        candidates,
        table_code_hints(step, code_candidates),
    )
    if not candidates:
        return {
            "status": "not_applicable",
            "selected": None,
            "candidates": [],
        }
    ranked = sorted(
        candidates,
        key=lambda item: (-item["confidence"], item["candidate_id"]),
    )
    best = ranked[0]
    runner_up = ranked[1]["confidence"] if len(ranked) > 1 else 0.0
    selected = (
        best["table_usage"]
        if best["confidence"] >= 0.9
        and best["confidence"] - runner_up >= 0.2
        else None
    )
    return {
        "status": "resolved" if selected is not None else "ambiguous",
        "selected": selected,
        "candidates": ranked,
    }


def table_usage_candidates(step):
    table = step.get("table") if isinstance(step, dict) else None
    if not isinstance(table, dict) or not table:
        return []
    headings = [str(item) for item in table.get("headings") or ()]
    if not headings:
        return []
    text = " ".join(str(step.get("text") or "").casefold().split())
    candidates = []

    each_row_explicit = _contains_any(text, _EACH_ROW_MARKERS)
    reset_explicit = _contains_any(text, _RESET_MARKERS)
    carry_explicit = _contains_any(text, _CARRY_MARKERS)
    reset_confidence = (
        0.99 if reset_explicit
        else 0.42 if carry_explicit
        else 0.92 if each_row_explicit
        else 0.48
    )
    candidates.append(_candidate(
        step,
        "each_row_records_reset",
        reset_confidence,
        {
            "consumption": "each_row",
            "shape": "records",
            "consumer": "step_definition",
            "ordered": _ordered(text),
            "reset_between_rows": True,
            "columns": _record_roles(headings, default="input"),
            "context_key": None,
            "reason": (
                "Step wording requires independent rows with state reset."
                if reset_explicit
                else "Step wording requires processing each row; reset semantics need confirmation."
                if each_row_explicit
                else "Each row may represent a repeated business case."
            ),
        },
    ))
    carry_confidence = (
        0.99 if carry_explicit
        else 0.42 if reset_explicit
        else 0.91 if each_row_explicit
        else 0.46
    )
    candidates.append(_candidate(
        step,
        "each_row_records_carry",
        carry_confidence,
        {
            "consumption": "each_row",
            "shape": "records",
            "consumer": "step_definition",
            "ordered": True,
            "reset_between_rows": False,
            "columns": _record_roles(headings, default="input"),
            "context_key": None,
            "reason": (
                "Step wording requires cumulative row processing."
                if carry_explicit
                else "Rows may execute sequentially while preserving prior state."
            ),
        },
    ))

    if len(headings) == 1:
        option_confidence = (
            0.98 if _contains_any(text, _OPTION_LIST_MARKERS) else 0.56
        )
        candidates.append(_candidate(
            step,
            "whole_table_list",
            option_confidence,
            {
                "consumption": "whole_table",
                "shape": "list",
                "consumer": "step_definition",
                "ordered": _ordered(text),
                "reset_between_rows": None,
                "columns": {headings[0]: "option"},
                "context_key": None,
                "reason": (
                    "Step wording describes one expected option list."
                    if option_confidence >= 0.9
                    else "A single-column table may be consumed as one list."
                ),
            },
        ))

    if len(headings) == 2:
        candidates.append(_candidate(
            step,
            "whole_table_mapping",
            0.52,
            {
                "consumption": "whole_table",
                "shape": "mapping",
                "consumer": "step_definition",
                "ordered": _ordered(text),
                "reset_between_rows": None,
                "columns": {headings[0]: "key", headings[1]: "value"},
                "context_key": None,
                "reason": "Two columns may describe one key/value mapping.",
            },
        ))

    object_confidence = 0.94 if _contains_any(text, _OBJECT_MARKERS) else 0.44
    candidates.append(_candidate(
        step,
        "whole_table_object",
        object_confidence,
        {
            "consumption": "whole_table",
            "shape": "object",
            "consumer": "step_definition",
            "ordered": False,
            "reset_between_rows": None,
            "columns": {heading: "field" for heading in headings},
            "context_key": None,
            "reason": (
                "Step wording describes one object or configuration."
                if object_confidence >= 0.9
                else "The table may describe fields of one business object."
            ),
        },
    ))

    action_column = next(
        (
            heading for heading in headings
            if _heading_has(heading, ("action", "operation", "动作", "操作"))
        ),
        None,
    )
    if action_column is not None:
        action_confidence = (
            0.97 if _contains_any(text, _ACTION_SEQUENCE_MARKERS) else 0.68
        )
        roles = {heading: "metadata" for heading in headings}
        roles[action_column] = "action"
        target_column = next(
            (
                heading for heading in headings
                if _heading_has(heading, ("target", "目标"))
            ),
            None,
        )
        if target_column is not None:
            roles[target_column] = "target"
        candidates.append(_candidate(
            step,
            "action_sequence",
            action_confidence,
            {
                "consumption": "each_row",
                "shape": "action_sequence",
                "consumer": "step_definition",
                "ordered": True,
                "reset_between_rows": False,
                "columns": roles,
                "context_key": None,
                "reason": "Rows describe an ordered business action sequence.",
            },
        ))

    store_confidence = 0.95 if _contains_any(text, _STORE_MARKERS) else 0.4
    context_key = _context_key(step)
    candidates.append(_candidate(
        step,
        "scenario_state",
        store_confidence,
        {
            "consumption": "scenario_state",
            "shape": "records",
            "consumer": "scenario_context",
            "ordered": _ordered(text),
            "reset_between_rows": None,
            "columns": {heading: "field" for heading in headings},
            "context_key": context_key,
            "reason": (
                "Step wording stores table data for subsequent Steps."
                if store_confidence >= 0.9
                else "The table may define data consumed by subsequent Steps."
            ),
        },
    ))
    return candidates


def table_code_hints(step, candidates):
    text = _normalized_text(step.get("text"))
    headings = {
        str(item)
        for item in ((step.get("table") or {}).get("headings") or ())
    }
    result = []
    for candidate in candidates or ():
        hint = candidate.get("table_usage_hint")
        patterns = {
            _normalized_text(item)
            for item in candidate.get("step_patterns") or ()
        }
        hint_columns = {
            str(item) for item in (hint or {}).get("columns") or ()
        }
        if not isinstance(hint, dict) or float(candidate.get("score") or 0) < 12:
            continue
        if text not in patterns or not hint_columns or hint_columns != headings:
            continue
        result.append({
            **hint,
            "score": float(candidate.get("score") or 0),
            "path": candidate.get("path"),
            "symbol": candidate.get("symbol"),
        })
    return result


def _apply_code_hints(candidates, hints):
    for hint in hints:
        consumption = hint.get("consumption")
        shape = hint.get("shape")
        if not consumption:
            continue
        matching = [
            candidate
            for candidate in candidates
            if candidate["table_usage"]["consumption"] == consumption
            and (
                not shape
                or candidate["table_usage"]["shape"] == shape
            )
            and (
                hint.get("reset_between_rows") is None
                or candidate["table_usage"]["reset_between_rows"]
                == hint.get("reset_between_rows")
            )
        ]
        for candidate in matching:
            candidate["confidence"] = max(
                candidate["confidence"],
                min(0.98, 0.88 + hint["score"] / 500),
            )
            candidate["table_usage"]["reason"] = (
                f"{candidate['table_usage']['reason']} "
                f"Existing code {hint.get('path')}:{hint.get('symbol')} "
                f"supports this structure."
            )
            if (
                consumption == "scenario_state"
                and hint.get("context_key")
            ):
                candidate["table_usage"]["context_key"] = hint["context_key"]


def _record_roles(headings, *, default):
    roles = {}
    for heading in headings:
        if _heading_has(
            heading,
            ("expected", "displayed", "result", "output", "期望", "显示", "结果"),
        ):
            roles[heading] = "expected"
        elif _heading_has(
            heading,
            ("input", "entered", "source", "request", "输入"),
        ):
            roles[heading] = "input"
        else:
            roles[heading] = default
    return roles


def _candidate(step, kind, confidence, table_usage):
    fingerprint = hashlib.sha256(json.dumps(
        {
            "step_id": step.get("id"),
            "kind": kind,
            "table_usage": table_usage,
        },
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")).hexdigest()
    return {
        "candidate_id": f"table-usage-{fingerprint[:16]}",
        "kind": kind,
        "confidence": confidence,
        "table_usage": table_usage,
    }


def _contains_any(text, values):
    return any(value.casefold() in text for value in values)


def _normalized_text(value):
    return " ".join(str(value or "").casefold().split())


def _heading_has(heading, values):
    text = str(heading).casefold()
    return any(value.casefold() in text for value in values)


def _ordered(text):
    return _contains_any(text, ("in order", "ordered", "依次", "按顺序"))


def _context_key(step):
    suffix = hashlib.sha256(
        str(step.get("id") or step.get("text") or "table").encode("utf-8")
    ).hexdigest()[:8]
    return f"table_{suffix}"


__all__ = [
    "TABLE_COLUMN_ROLES",
    "TABLE_CONSUMERS",
    "TABLE_CONSUMPTIONS",
    "TABLE_SHAPES",
    "normalize_table_usage",
    "infer_table_usage",
    "table_code_hints",
    "table_usage_candidates",
    "validate_table_usage",
]