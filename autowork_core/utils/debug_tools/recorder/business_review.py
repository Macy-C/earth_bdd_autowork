from __future__ import annotations

import hashlib
import json
from copy import deepcopy


BUSINESS_REVIEW_WORKSET_VERSION = "1.0"
BUSINESS_REVIEW_REQUIREMENT_VERSION = "1.1"
BUSINESS_REVIEW_PATCH_VERSION = "1.0"


def build_business_review_workset(brief, decision_pack=None):
    brief = deepcopy(dict(brief or {}))
    decision_pack = deepcopy(dict(decision_pack or {}))
    units = []
    units.extend(_system_decision_question_units(decision_pack))
    workset = {
        "business_review_workset_version": BUSINESS_REVIEW_WORKSET_VERSION,
        "status": "required" if units else "not_required",
        "request_id": brief.get("request_id"),
        "brief_fingerprint": brief.get("brief_fingerprint"),
        "decision_pack_fingerprint": decision_pack.get(
            "pack_fingerprint"
        ),
        "unit_count": len(units),
        "units": units,
    }
    workset["workset_fingerprint"] = _hash({
        key: value
        for key, value in workset.items()
        if key != "workset_fingerprint"
    })
    return workset


def build_business_review_requirement(workset):
    workset = deepcopy(dict(workset or {}))
    requirements = []
    for unit in workset.get("units") or ():
        if not isinstance(unit, dict):
            continue
        requirements.append({
            "unit_id": unit.get("unit_id"),
            "unit_type": unit.get("unit_type"),
            "step_id": unit.get("step_id"),
            "action_id": unit.get("action_id"),
            "allowed_decisions": list(unit.get("allowed_decisions") or []),
            "submit_arguments": [
                "--review-decision",
                f"{unit.get('unit_id')}=<decision>",
                "--review-reason",
                f"{unit.get('unit_id')}=<reason>",
            ],
            "ask_user_question_template": _question_template_for_unit(unit),
        })
    value = {
        "business_review_requirement_version": BUSINESS_REVIEW_REQUIREMENT_VERSION,
        "status": "required" if requirements else "not_required",
        "patch_type": "business_review",
        "workset_fingerprint": workset.get("workset_fingerprint"),
        "unit_count": len(requirements),
        "requirements": requirements,
        "retired_submit": "submit-business-review is not a normal product-path command",
        "forbidden_fields": [
            "locator",
            "xpath",
            "path",
            "class",
            "method",
            "operation",
            "proof",
            "pic",
        ],
    }
    value["requirement_fingerprint"] = _hash({
        key: item for key, item in value.items()
        if key != "requirement_fingerprint"
    })
    return value


def build_business_review_questions(workset):
    workset = deepcopy(dict(workset or {}))
    questions = []
    for unit in workset.get("units") or ():
        if not isinstance(unit, dict):
            continue
        question = _question_template_for_unit(unit)
        if not isinstance(question, dict) or not question.get("question_id"):
            continue
        question = deepcopy(question)
        unit_type = str(unit.get("unit_type") or "")
        question.update({
            "unit_id": unit.get("unit_id"),
            "unit_type": unit_type,
            "step_id": unit.get("step_id") or question.get("step_id"),
            "blocking": True,
        })
        if unit_type == "system_decision_question":
            question["source_decision_question_id"] = question.get("question_id")
        questions.append(question)
    return questions


def build_business_review_patch_from_decisions(workset, decisions, reasons):
    workset = deepcopy(dict(workset or {}))
    decisions = {
        str(unit_id): str(decision)
        for unit_id, decision in dict(decisions or {}).items()
    }
    reasons = {
        str(unit_id): str(reason)
        for unit_id, reason in dict(reasons or {}).items()
    }
    unit_decisions = []
    for unit in workset.get("units") or ():
        if not isinstance(unit, dict) or not unit.get("unit_id"):
            continue
        unit_id = str(unit["unit_id"])
        decision = decisions.get(unit_id, "")
        item = {
            "unit_id": unit_id,
            "decision": decision,
            "reason": reasons.get(unit_id, ""),
        }
        if decision in {"ask_user", "rephrase_business_question"}:
            item["question"] = _question_template_for_unit(unit)
        unit_decisions.append(item)
    return {
        "business_review_patch_version": BUSINESS_REVIEW_PATCH_VERSION,
        "workset_fingerprint": workset.get("workset_fingerprint"),
        "unit_decisions": unit_decisions,
    }


def validate_business_review_patch(workset, patch):
    errors = []
    workset = dict(workset or {})
    patch = dict(patch or {})
    if workset.get("business_review_workset_version") != (
            BUSINESS_REVIEW_WORKSET_VERSION
    ):
        errors.append("BusinessReviewWorkset版本无效")
    if patch.get("business_review_patch_version") != (
            BUSINESS_REVIEW_PATCH_VERSION
    ):
        errors.append("BusinessReviewPatch版本无效")
    if patch.get("workset_fingerprint") != workset.get("workset_fingerprint"):
        errors.append("BusinessReviewPatch不属于当前Workset")
    decisions = patch.get("unit_decisions") or []
    if not isinstance(decisions, list):
        errors.append("BusinessReviewPatch unit_decisions必须是列表")
        decisions = []
    units = {
        str(unit.get("unit_id") or ""): unit
        for unit in workset.get("units") or ()
        if isinstance(unit, dict) and unit.get("unit_id")
    }
    decision_counts = {}
    for decision in decisions:
        if isinstance(decision, dict):
            unit_id = str(decision.get("unit_id") or "")
        else:
            unit_id = ""
        decision_counts[unit_id] = decision_counts.get(unit_id, 0) + 1
    missing = sorted(set(units) - set(decision_counts))
    if missing:
        errors.append(f"BusinessReviewPatch缺少Unit decision: {missing}")
    duplicates = sorted(
        unit_id for unit_id, count in decision_counts.items() if count > 1
    )
    if duplicates:
        errors.append(f"BusinessReviewPatch重复Unit decision: {duplicates}")
    unknown = sorted(set(decision_counts) - set(units) - {""})
    if unknown:
        errors.append(f"BusinessReviewPatch引用未知Unit: {unknown}")
    for index, decision in enumerate(decisions, start=1):
        if not isinstance(decision, dict):
            errors.append(f"BusinessReviewPatch decision {index} 必须是object")
            continue
        errors.extend(_validate_unit_decision(units, decision, index))
    return errors


def _system_decision_question_units(decision_pack):
    units = []
    for question in decision_pack.get("questions") or ():
        if not isinstance(question, dict) or not question.get("blocking"):
            continue
        question_id = str(question.get("question_id") or "")
        if not question_id:
            continue
        allow_freeform = bool(question.get("allow_freeform"))
        units.append({
            "unit_id": "unit-" + _hash({
                "type": "system_decision_question",
                "question_id": question_id,
                "pack_fingerprint": decision_pack.get("pack_fingerprint"),
            })[:20],
            "unit_type": "system_decision_question",
            "step_id": str(question.get("step_id") or ""),
            "action_id": str(
                (question.get("action_ids") or [""])[0]
                or question.get("action_id")
                or ""
            ),
            "question_id": question_id,
            "facts": {
                "question": _public_question(question),
            },
            "allowed_decisions": [
                "include_as_is",
                "rephrase_business_question",
                "merge_with_ai_question",
            ],
            "allowed_answer_modes": [
                "option_or_freeform" if allow_freeform else "option",
            ],
            "allow_freeform": allow_freeform,
        })
    return units


def _question_template_for_unit(unit):
    unit_type = str(unit.get("unit_type") or "")
    if unit_type == "system_decision_question":
        return deepcopy((unit.get("facts") or {}).get("question") or {})
    return {}


def _validate_unit_decision(units, decision, index):
    errors = []
    allowed_keys = {"unit_id", "decision", "reason", "question"}
    unknown = sorted(set(decision) - allowed_keys)
    if unknown:
        return [
            f"BusinessReviewPatch decision {index} 包含技术字段或未知字段: {unknown}"
        ]
    unit = units.get(str(decision.get("unit_id") or ""))
    if unit is None:
        return errors
    decision_value = str(decision.get("decision") or "")
    if decision_value not in set(unit.get("allowed_decisions") or ()): 
        errors.append(
            f"BusinessReviewPatch decision {index} 决策不被Unit允许: {decision_value}"
        )
    reason = str(decision.get("reason") or "").strip()
    if not reason or len(reason) > 1000:
        errors.append(f"BusinessReviewPatch decision {index} reason为空或过长")
    question = decision.get("question")
    if decision_value in {"ask_user", "rephrase_business_question"}:
        if not isinstance(question, dict):
            errors.append(f"BusinessReviewPatch decision {index} 缺少question")
        else:
            errors.extend(_validate_review_question(unit, question, index))
    elif question is not None:
        errors.append(f"BusinessReviewPatch decision {index} 不应携带question")
    return errors


def _validate_review_question(unit, question, index):
    errors = []
    allowed_keys = {
        "question_id",
        "prompt",
        "answer_mode",
        "options",
        "allow_freeform",
    }
    unknown = sorted(set(question) - allowed_keys)
    if unknown:
        return [
            f"BusinessReviewPatch question {index} 包含技术字段或未知字段: {unknown}"
        ]
    question_id = str(question.get("question_id") or "")
    if not question_id or len(question_id) > 120:
        errors.append(f"BusinessReviewPatch question {index} question_id无效")
    prompt = str(question.get("prompt") or "").strip()
    if not prompt or len(prompt) > 1000:
        errors.append(f"BusinessReviewPatch question {index} prompt为空或过长")
    answer_mode = str(question.get("answer_mode") or "")
    if answer_mode not in set(unit.get("allowed_answer_modes") or ()): 
        errors.append(f"BusinessReviewPatch question {index} answer_mode无效")
    if bool(question.get("allow_freeform")) and not unit.get("allow_freeform"):
        errors.append(f"BusinessReviewPatch question {index} 不允许自由回答")
    options = question.get("options") or []
    if not isinstance(options, list) or not options:
        errors.append(f"BusinessReviewPatch question {index} options为空或格式无效")
        return errors
    if len(options) > 6:
        errors.append(f"BusinessReviewPatch question {index} options过多")
    option_ids = set()
    for option_index, option in enumerate(options, start=1):
        if not isinstance(option, dict):
            errors.append(f"BusinessReviewPatch question {index} option {option_index} 必须是object")
            continue
        errors.extend(_validate_review_option(unit, option, index, option_index))
        option_id = str(option.get("option_id") or "")
        if option_id in option_ids:
            errors.append(f"BusinessReviewPatch question {index} option_id重复: {option_id}")
        option_ids.add(option_id)
    return errors


def _validate_review_option(unit, option, question_index, option_index):
    errors = []
    allowed_keys = {"option_id", "label", "business_fact"}
    unknown = sorted(set(option) - allowed_keys)
    if unknown:
        return [
            "BusinessReviewPatch option "
            f"{question_index}.{option_index} 包含技术字段或未知字段: {unknown}"
        ]
    option_id = str(option.get("option_id") or "")
    if not option_id or len(option_id) > 100:
        errors.append(
            f"BusinessReviewPatch option {question_index}.{option_index} option_id无效"
        )
    label = str(option.get("label") or "").strip()
    if not label or len(label) > 300:
        errors.append(
            f"BusinessReviewPatch option {question_index}.{option_index} label为空或过长"
        )
    fact = option.get("business_fact")
    if not isinstance(fact, dict):
        errors.append(
            f"BusinessReviewPatch option {question_index}.{option_index} 缺少business_fact"
        )
        return errors
    errors.extend(_validate_option_business_fact(unit, fact, question_index, option_index))
    return errors


def _validate_option_business_fact(unit, fact, question_index, option_index):
    errors = []
    allowed_keys = {"fact_type", "fact_value", "applies_to", "source"}
    unknown = sorted(set(fact) - allowed_keys)
    if unknown:
        return [
            "BusinessReviewPatch business_fact "
            f"{question_index}.{option_index} 包含技术字段或未知字段: {unknown}"
        ]
    if str(fact.get("fact_type") or "") != "value_authority":
        errors.append(
            f"BusinessReviewPatch business_fact {question_index}.{option_index} fact_type无效"
        )
    applies_to = fact.get("applies_to") or {}
    if not isinstance(applies_to, dict):
        errors.append(
            f"BusinessReviewPatch business_fact {question_index}.{option_index} applies_to无效"
        )
    else:
        unknown_applies = sorted(set(applies_to) - {"scope", "action_id"})
        if unknown_applies:
            errors.append(
                "BusinessReviewPatch business_fact "
                f"{question_index}.{option_index} applies_to包含技术字段: {unknown_applies}"
            )
        if applies_to.get("scope") != "action_value":
            errors.append(
                f"BusinessReviewPatch business_fact {question_index}.{option_index} scope无效"
            )
        if str(applies_to.get("action_id") or "") != str(unit.get("action_id") or ""):
            errors.append(
                "BusinessReviewPatch business_fact "
                f"{question_index}.{option_index} action_id不属于Unit"
            )
    source = fact.get("source") or {}
    if not isinstance(source, dict):
        errors.append(
            f"BusinessReviewPatch business_fact {question_index}.{option_index} source无效"
        )
        return errors
    fact_value = fact.get("fact_value")
    if not _source_value_in_unit(unit, source, fact_value):
        errors.append(
            "BusinessReviewPatch business_fact "
            f"{question_index}.{option_index} 不来自Unit冻结事实"
        )
    return errors


def _source_value_in_unit(unit, source, value):
    candidates = (unit.get("facts") or {}).get("available_value_sources") or []
    normalized_source = {
        key: str(source[key])
        for key in ("kind", "action_id", "reference")
        if source.get(key) is not None
    }
    for candidate in candidates:
        candidate_source = {
            key: str((candidate.get("source") or {})[key])
            for key in ("kind", "action_id", "reference")
            if (candidate.get("source") or {}).get(key) is not None
        }
        if candidate_source == normalized_source and candidate.get("value") == value:
            return True
    question = (unit.get("facts") or {}).get("question") or {}
    for option in question.get("options") or ():
        fact = (option.get("business_fact") or {}) if isinstance(option, dict) else {}
        fact_source = {
            key: str((fact.get("source") or {})[key])
            for key in ("kind", "action_id", "reference")
            if (fact.get("source") or {}).get(key) is not None
        }
        if fact_source == normalized_source and fact.get("fact_value") == value:
            return True
    return False


def _public_question(question):
    value = {
        "question_id": question.get("question_id"),
        "type": question.get("type"),
        "title": question.get("title"),
        "prompt": question.get("prompt"),
        "step_id": question.get("step_id"),
        "blocking": question.get("blocking"),
        "allow_freeform": bool(question.get("allow_freeform")),
        **(
            {"screenshots": deepcopy(question["screenshots"])}
            if question.get("screenshots")
            else {}
        ),
        "options": [
            {
                "option_id": option.get("option_id"),
                "label": option.get("label"),
                **(
                    {"business_fact": deepcopy(option["business_fact"])}
                    if option.get("business_fact")
                    else {}
                ),
            }
            for option in question.get("options") or ()
            if isinstance(option, dict)
        ],
    }
    return value


def _hash(value):
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _unique_strings(values):
    result = []
    seen = set()
    for value in values or ():
        text = str(value)
        if text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result
