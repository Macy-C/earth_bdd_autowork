from __future__ import annotations

import hashlib
import json
from copy import deepcopy


BUSINESS_REVIEW_WORKSET_VERSION = "1.0"
BUSINESS_REVIEW_REQUIREMENT_VERSION = "1.1"


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
