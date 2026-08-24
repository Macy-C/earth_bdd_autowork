from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from autowork_core.utils.debug_tools.recorder.models import SCHEMA_VERSION
from autowork_core.utils.debug_tools.recorder.table_usage import (
    infer_table_usage,
    table_business_outcome_candidates,
)
from autowork_core.utils.debug_tools.recorder.writer import write_json_atomic


DECISION_PACK_VERSION = "5.8"
SUPPORTED_DECISION_PACK_VERSIONS = {"5.7", DECISION_PACK_VERSION}
ANSWER_VERSION = "5.1"
LEGACY_ANSWER_VERSION = "5.0"
SUPPORTED_ANSWER_VERSIONS = {LEGACY_ANSWER_VERSION, ANSWER_VERSION}
LEGACY_TECHNICAL_PATCH_KINDS = {
    "binding",
    "operation",
    "role",
    "table_usage",
}


def build_decision_pack(
        request,
        semantic_packs,
        *,
        brief=None,
        write_path=None,
    ):
    semantic_packs = [dict(item) for item in semantic_packs or ()]
    brief = brief or {}
    ai_action_coverage = _ambiguity_ai_action_coverage(brief)
    questions = []
    forensic_blockers = []
    for pack in semantic_packs:
        step_id = str((pack.get("step") or {}).get("id") or "")
        covered_action_ids = ai_action_coverage.get(step_id, ())
        questions.extend(_pic_questions(
            pack,
            step_id,
        ))
        forensic_blockers.extend(_forensic_blockers(
            pack,
            step_id,
            excluded_action_ids=covered_action_ids,
        ))
    questions.extend(_table_usage_questions(request, brief))
    questions.extend(_ambiguity_questions(brief))
    questions = _dedupe_questions(questions)
    questions = [
        _with_question_presentation(question, request, brief)
        for question in questions
    ]
    forensic_blockers.extend(
        _brief_forensic_blockers(
            brief,
            questions,
            forensic_blockers,
            behavior_coverage=ai_action_coverage,
        )
    )
    forensic_blockers = _dedupe_blockers(forensic_blockers)
    revision_seal = (request.get("revision_snapshot") or {}).get("seal")
    value = {
        "schema_version": SCHEMA_VERSION,
        "decision_pack_version": DECISION_PACK_VERSION,
        "request_id": request.get("request_id"),
        "revision_seal": revision_seal,
        "brief_fingerprint": (brief or {}).get("brief_fingerprint"),
        "questions": questions,
        "forensic_blockers": forensic_blockers,
        "batch": {
            "batch_id": "batch-primary",
            "question_ids": [item["question_id"] for item in questions],
            "submit_policy": "all_blocking_once",
            "max_rounds": 1,
        },
        "blocking_count": sum(bool(item.get("blocking")) for item in questions),
        "forensic_blocking_count": len(forensic_blockers),
        "policy": {
            "free_text_cannot_patch_plan": True,
            "answers_must_select_declared_options": True,
            "every_option_must_cite_evidence_or_action": True,
            "technical_implementation_patches_forbidden": True,
            "pic_default": "deny",
        },
    }
    value["pack_fingerprint"] = _pack_fingerprint(value)
    value["pack_id"] = f"decision-{value['pack_fingerprint'][:16]}"
    if write_path is not None:
        write_json_atomic(write_path, value)
    return value


def persist_decision_pack(session_dir, pack):
    output = _decision_dir(session_dir, pack.get("request_id")) / (
        f"{pack['pack_id']}.json"
    )
    if output.exists():
        existing = _read_json(output)
        if existing.get("pack_fingerprint") != pack.get("pack_fingerprint"):
            raise ValueError(f"Decision Pack ID 冲突: {output}")
        return output, existing
    write_json_atomic(output, pack)
    return output, pack


def load_decision_pack(
        session_dir,
        pointer,
        request,
        *,
        brief_fingerprint=None,
    ):
    path = _resolve_decision_path(
        session_dir,
        request.get("request_id"),
        (pointer or {}).get("path"),
    )
    if path is None:
        return None
    value = _read_json(path)
    actual_fingerprint = _pack_fingerprint(value)
    if any((
        value.get("decision_pack_version")
        not in SUPPORTED_DECISION_PACK_VERSIONS,
        value.get("request_id") != request.get("request_id"),
        value.get("revision_seal")
        != (request.get("revision_snapshot") or {}).get("seal"),
        brief_fingerprint is not None
        and value.get("brief_fingerprint") != brief_fingerprint,
        value.get("pack_id") != (pointer or {}).get("pack_id"),
        value.get("pack_fingerprint")
        != (pointer or {}).get("pack_fingerprint"),
        value.get("pack_fingerprint") != actual_fingerprint,
        value.get("pack_id") != f"decision-{actual_fingerprint[:16]}",
    )):
        return None
    return value


def decision_pack_pointer(session_dir, pack, path):
    return {
        "path": Path(path).resolve().relative_to(
            Path(session_dir).resolve()
        ).as_posix(),
        "pack_id": pack.get("pack_id"),
        "pack_fingerprint": pack.get("pack_fingerprint"),
        "revision_seal": pack.get("revision_seal"),
        "question_count": len(pack.get("questions") or ()),
        "blocking_count": pack.get("blocking_count", 0),
        "forensic_blocking_count": pack.get("forensic_blocking_count", 0),
    }


def validate_answers(pack, answers, *, request=None):
    answers = dict(answers or {})
    errors = []
    expected_answer_version = (
        LEGACY_ANSWER_VERSION
        if pack.get("decision_pack_version") == "5.7"
        else ANSWER_VERSION
    )
    if answers.get("answer_version") != expected_answer_version:
        errors.append("answer_version 无效")
    if answers.get("pack_id") != pack.get("pack_id"):
        errors.append("答案不属于当前 Decision Pack")
    if answers.get("pack_fingerprint") != pack.get("pack_fingerprint"):
        errors.append("Decision Pack 指纹不一致")
    expected_seal = pack.get("revision_seal")
    actual_seal = answers.get("revision_seal")
    if expected_seal != actual_seal:
        errors.append("答案 revision 已过期")
    if request is not None and actual_seal != (
            request.get("revision_snapshot") or {}
    ).get("seal"):
        errors.append("答案与 Request revision 不一致")
    questions = {
        item["question_id"]: item
        for item in pack.get("questions") or ()
    }
    answer_map = {}
    answer_counts = Counter(
        str(item.get("question_id"))
        for item in answers.get("answers") or ()
        if item.get("question_id")
    )
    duplicates = sorted(
        question_id
        for question_id, count in answer_counts.items()
        if count > 1
    )
    if duplicates:
        errors.append(f"同一问题不能重复回答: {duplicates}")
    for answer in answers.get("answers") or ():
        question_id = answer.get("question_id")
        question = questions.get(question_id)
        if question is None:
            errors.append(f"回答引用未知问题: {question_id}")
            continue
        option_id = answer.get("option_id")
        option = next(
            (
                item
                for item in question.get("options") or ()
                if item.get("option_id") == option_id
            ),
            None,
        )
        if option is None:
            errors.append(
                f"问题 {question_id} 引用未知选项: {option_id}"
            )
            continue
        answer_map[question_id] = {
            "question": question,
            "option": option,
            "note": str(answer.get("note") or ""),
        }
    for question_id, question in questions.items():
        if question.get("blocking") and question_id not in answer_map:
            errors.append(f"阻塞问题未回答: {question_id}")
    return errors, answer_map


def compile_answers_to_plan_patch(pack, answers, *, request=None):
    errors, answer_map = validate_answers(pack, answers, request=request)
    if errors:
        raise ValueError(f"Decision answers 无效: {errors}")
    steps = {}
    pic_authorizations = []
    ambiguity_resolutions = []
    decisions = []
    for question_id, resolved in answer_map.items():
        question = resolved["question"]
        option = resolved["option"]
        patch = dict(option.get("plan_patch") or {})
        step_id = str(question.get("step_id") or "")
        empty_step_patch = {
            "operations": [],
            "role_overrides": {},
            "binding_decisions": {},
            "ignored_action_ids": [],
            "table_usage": None,
        }
        if pack.get("decision_pack_version") != "5.7":
            empty_step_patch["table_business_outcome"] = None
        step_patch = steps.setdefault(step_id, empty_step_patch)
        kind = patch.get("kind")
        if (
            pack.get("decision_pack_version") != "5.7"
            and kind in LEGACY_TECHNICAL_PATCH_KINDS
        ):
            raise ValueError(
                "Decision Pack 5.8不能修改技术实现字段: "
                f"kind={kind}"
            )
        if kind == "operation":
            operation = dict(patch.get("operation") or {})
            operation["decision_ids"] = [question_id]
            operation["confidence"] = option.get("confidence")
            step_patch["operations"].append(operation)
        elif kind == "role":
            step_patch["role_overrides"][str(patch["action_id"])] = patch["role"]
        elif kind == "binding":
            step_patch["binding_decisions"][str(patch["action_id"])] = {
                "source": patch["source"],
                "value": patch.get("value"),
                "decision_id": question_id,
                "confidence": option.get("confidence"),
            }
        elif kind == "table_usage":
            step_patch["table_usage"] = dict(
                patch.get("table_usage") or {}
            )
        elif kind == "table_business_outcome":
            step_patch["table_business_outcome"] = str(
                patch.get("outcome") or ""
            )
        elif kind == "pic_authorization":
            pic_authorizations.append(_pic_authorization(
                pack,
                question,
                option,
                question_id,
            ))
        elif kind == "ambiguity_resolution":
            resolution = dict(patch.get("resolution") or {})
            resolution["decision_ids"] = [question_id]
            ambiguity_resolutions.append(resolution)
            if patch.get("effect") == "ignored_action":
                step_patch["ignored_action_ids"] = _unique([
                    *step_patch["ignored_action_ids"],
                    *(resolution.get("action_ids") or []),
                ])
        decisions.append({
            "question_id": question_id,
            "step_id": step_id,
            "option_id": option.get("option_id"),
            "confidence": option.get("confidence"),
            "evidence_ids": question.get("evidence_ids") or [],
            "action_ids": question.get("action_ids") or [],
            "note": resolved["note"],
        })
    return {
        "steps": steps,
        "ambiguity_resolutions": ambiguity_resolutions,
        "pic_authorizations": pic_authorizations,
        "decision_trace": decisions,
        "uncertainties": [
            question["question_id"]
            for question in pack.get("questions") or ()
            if question.get("question_id") not in answer_map
        ],
    }


def persist_answers(session_dir, request, pack, answers):
    errors, _resolved = validate_answers(pack, answers, request=request)
    if errors:
        raise ValueError(f"Decision answers 无效: {errors}")
    compiled_patch = compile_answers_to_plan_patch(
        pack,
        answers,
        request=request,
    )
    answer_fingerprint = _hash({
        "pack_fingerprint": pack.get("pack_fingerprint"),
        "revision_seal": pack.get("revision_seal"),
        "answers": answers.get("answers") or [],
    })
    output = _decision_dir(session_dir, request.get("request_id")) / (
        f"answers-{answer_fingerprint[:16]}.json"
    )
    value = {
        **dict(answers),
        "request_id": request.get("request_id"),
        "answered_at": datetime.now().isoformat(timespec="seconds"),
        "compiled_patch": compiled_patch,
        "answer_fingerprint": answer_fingerprint,
    }
    if output.exists():
        existing = _read_json(output)
        if existing.get("answer_fingerprint") != answer_fingerprint:
            raise ValueError(f"Decision Answers ID 冲突: {output}")
        return output, existing
    write_json_atomic(output, value)
    return output, value


def load_answer_record(session_dir, pointer, request, pack):
    path = _resolve_decision_path(
        session_dir,
        request.get("request_id"),
        (pointer or {}).get("path"),
    )
    if path is None:
        return None
    value = _read_json(path)
    actual_answer_fingerprint = _answer_fingerprint(pack, value)
    if any((
        value.get("answer_version") not in SUPPORTED_ANSWER_VERSIONS,
        value.get("request_id") != request.get("request_id"),
        value.get("pack_id") != pack.get("pack_id"),
        value.get("pack_fingerprint") != pack.get("pack_fingerprint"),
        value.get("revision_seal") != pack.get("revision_seal"),
        value.get("answer_fingerprint")
        != (pointer or {}).get("answer_fingerprint"),
        value.get("answer_fingerprint") != actual_answer_fingerprint,
    )):
        return None
    errors, _resolved = validate_answers(pack, value, request=request)
    if errors:
        return None
    compiled = compile_answers_to_plan_patch(pack, value, request=request)
    if compiled != value.get("compiled_patch"):
        return None
    return value


def answer_pointer(session_dir, answers, path):
    return {
        "path": Path(path).resolve().relative_to(
            Path(session_dir).resolve()
        ).as_posix(),
        "answer_fingerprint": answers.get("answer_fingerprint"),
        "pack_id": answers.get("pack_id"),
        "pack_fingerprint": answers.get("pack_fingerprint"),
        "revision_seal": answers.get("revision_seal"),
    }


def _ambiguity_ai_action_coverage(brief):
    coverage = {}
    for ambiguity in brief.get("ambiguities") or ():
        if not any(
            outcome.get("authority") == "ai"
            and outcome.get("effect") in {
                "behavior_coverage",
                "plan_coverage",
            }
            for outcome in ambiguity.get("allowed_outcomes") or ()
        ):
            continue
        step_id = str(ambiguity.get("step_id") or "")
        covered = coverage.setdefault(step_id, set())
        covered.update({
            str(action_id)
            for action_id in ambiguity.get("action_ids") or ()
            if action_id
        })
    return coverage


def _ambiguity_questions(brief):
    questions = []
    for ambiguity in brief.get("ambiguities") or ():
        user_outcomes = [
            item
            for item in ambiguity.get("allowed_outcomes") or ()
            if item.get("authority") == "user"
        ]
        if not user_outcomes:
            continue
        ambiguity_id = str(ambiguity.get("ambiguity_id") or "")
        step_id = str(ambiguity.get("step_id") or "")
        code = str(ambiguity.get("code") or "")
        question_id = _question_id(
            "ambiguity",
            step_id,
            ambiguity_id,
            ambiguity.get("code"),
        )
        options = []
        for outcome in user_outcomes:
            outcome_name = str(outcome.get("outcome") or "")
            options.append({
                "option_id": f"ambiguity-{ambiguity_id}-{outcome_name}",
                "label": _ambiguity_outcome_label(
                    ambiguity.get("code"),
                    outcome_name,
                ),
                "confidence": 1.0,
                "plan_patch": {
                    "kind": "ambiguity_resolution",
                    "effect": outcome.get("effect"),
                    "resolution": {
                        "ambiguity_id": ambiguity_id,
                        "outcome": outcome_name,
                        "action_ids": list(
                            ambiguity.get("action_ids") or []
                        ),
                        "evidence_ids": list(
                            ambiguity.get("evidence_ids") or []
                        ),
                        "candidate_id": None,
                        "reason": "Selected by the user Decision.",
                    },
                },
            })
        questions.append({
            "question_id": question_id,
            "step_id": step_id,
            "type": (
                "specification_business_conflict"
                if code == "specification_business_conflict"
                else "step_context_business_conflict"
                if code == "step_context_business_conflict"
                else "ambiguity_resolution"
            ),
            "title": _ambiguity_title(ambiguity.get("code")),
            "prompt": str(
                (ambiguity.get("facts") or {}).get("message")
                or "请选择该录制歧义在当前 Step 中的业务归属。"
            ),
            "blocking": True,
            "ambiguity_id": ambiguity_id,
            "facts": dict(ambiguity.get("facts") or {}),
            "action_ids": list(ambiguity.get("action_ids") or []),
            "evidence_ids": list(ambiguity.get("evidence_ids") or []),
            "answer_format": "single_choice",
            "options": options,
            "verification_rule": "frozen_ambiguity_user_outcome",
        })
    return questions


def _ambiguity_title(code):
    return {
        "pause_state_changed": "确认暂停期间变化的业务归属",
        "unsupported_scroll": "确认滚动动作是否属于当前 Step",
        "specification_business_conflict": "确认冲突业务规格的权威来源",
        "step_context_business_conflict": "确认Step说明与Feature的权威来源",
        "assertion_business_expectation_required": "确认当前结果是否就是业务期望",
        "provisional_window": "确认新窗口是否属于业务流程",
        "window_closed_during_take": "确认窗口关闭的业务含义",
    }.get(str(code or ""), "确认录制歧义的业务归属")


def _ambiguity_outcome_label(code, outcome):
    labels = {
        ("pause_state_changed", "unrelated_to_step"): "与当前 Step 无关",
        ("pause_state_changed", "step_precondition"): "属于当前 Step 的前置状态",
        ("pause_state_changed", "belongs_to_step"): "属于当前 Step，需要补录动作",
        ("unsupported_scroll", "belongs_to_step"): "滚动属于当前 Step",
        ("unsupported_scroll", "ignore_as_noise"): "滚动不属于当前 Step，按噪声忽略",
        (
            "specification_business_conflict",
            "follow_feature_requirement",
        ): "以 Feature 需求说明为准",
        (
            "specification_business_conflict",
            "follow_rule_requirement",
        ): "以当前 Rule 文案为准",
        (
            "step_context_business_conflict",
            "follow_feature_requirement",
        ): "以 Feature 需求说明为准",
        (
            "step_context_business_conflict",
            "follow_step_user_context",
        ): "以当前 Step 业务说明为准",
        (
            "assertion_business_expectation_required",
            "confirm_observed_result_as_expected",
        ): "当前看到的结果就是期望",
        (
            "assertion_business_expectation_required",
            "reject_observed_result_as_expected",
        ): "当前结果不是期望，返回修改检查或补录",
        (
            "provisional_window",
            "belongs_to_business_flow",
        ): "这个窗口属于当前业务流程",
        (
            "provisional_window",
            "unrelated_window",
        ): "这个窗口与当前业务无关",
        (
            "window_closed_during_take",
            "expected_close",
        ): "关闭窗口就是本步骤的预期结果",
        (
            "window_closed_during_take",
            "workflow_transition",
        ): "关闭窗口是进入下一业务阶段",
        (
            "window_closed_during_take",
            "unexpected_close",
        ): "窗口意外关闭，需要修复或重新录制",
    }
    return labels.get((str(code or ""), str(outcome or "")), str(outcome))


def _table_usage_questions(request, brief):
    actions = brief.get("actions") or []
    questions = []
    for step in (request.get("target") or {}).get("steps") or []:
        if not step.get("table"):
            continue
        resolution = infer_table_usage(
            step,
            code_candidates=(brief.get("semantics") or {}).get(
                "reuse_candidates"
            ) or [],
        )
        if resolution["status"] != "ambiguous":
            continue
        step_id = str(step.get("id") or "")
        step_actions = [
            str(action.get("id"))
            for action in actions
            if str(action.get("step_id") or "") == step_id
            and action.get("id")
        ]
        evidence_ids = _unique(
            evidence
            for action in actions
            if str(action.get("step_id") or "") == step_id
            for evidence in action.get("evidence") or ()
        )
        options = [
            {
                "option_id": "table-business-" + _hash({
                    "step_id": step_id,
                    "outcome": candidate["outcome"],
                })[:16],
                "label": _table_business_outcome_label(
                    candidate["outcome"]
                ),
                "confidence": candidate["confidence"],
                "plan_patch": {
                    "kind": "table_business_outcome",
                    "outcome": candidate["outcome"],
                },
            }
            for candidate in table_business_outcome_candidates(
                resolution["candidates"]
            )
        ]
        questions.append({
            "question_id": _question_id("table_usage", step_id, "table"),
            "step_id": step_id,
            "type": "table_usage",
            "title": "确认表格业务用法",
            "prompt": (
                "请选择当前 Step 如何消费 Data Table；"
                "表格形状本身不会自动决定是否循环。"
            ),
            "blocking": True,
            "action_ids": step_actions,
            "evidence_ids": evidence_ids,
            "answer_format": "single_choice",
            "options": options,
            "verification_rule": "table_usage_matches_business_intent",
        })
    return questions


def _table_business_outcome_label(outcome):
    return {
        "independent_rows": "每行是独立业务案例",
        "continuous_rows": "各行按顺序连续推进业务状态",
        "whole_table": "整张表共同描述一个业务集合或对象",
        "scenario_state": "保存整张表供后续 Step 使用",
    }.get(str(outcome or ""), str(outcome or ""))
def _pic_questions(pack, step_id):
    questions = []
    for entry in pack.get("locator_fallback_candidates") or ():
        candidate = entry.get("pic_candidate")
        if not candidate:
            continue
        action_id = str(entry.get("action_id"))
        authorized = bool(
            candidate.get("audit_status") == "passed"
            and candidate.get("template_sha256")
            and candidate.get("template_artifact")
            and candidate.get("template_request_path")
            and candidate.get("region")
            and candidate.get("region_locator_name")
            and candidate.get("region_locator")
            and (
                candidate.get("cross_frame_validation") or {}
            ).get("cross_frame_unique_match") is True
        )
        options = [{
            "option_id": "deny-pic",
            "label": "不授权 PIC，使用或修复其他定位方式",
            "confidence": 1.0,
            "plan_patch": {
                "kind": "pic_authorization",
                "authorized": False,
                "action_id": action_id,
                "candidate": candidate,
            },
        }]
        if authorized:
            options.append({
                "option_id": "authorize-pic",
                "label": "授权当前 Action 使用受控 PIC",
                "confidence": 0.9,
                "plan_patch": {
                    "kind": "pic_authorization",
                    "authorized": True,
                    "action_id": action_id,
                    "candidate": candidate,
                },
            })
        questions.append({
            "question_id": _question_id("pic", step_id, action_id),
            "step_id": step_id,
            "type": "pic_authorization",
            "title": "是否授权图片定位",
            "prompt": (
                "结构化定位未能形成唯一目标。请选择继续使用 POS，"
                "或授权当前 Action 使用经验证的 PIC 模板。"
            ),
            "blocking": True,
            "action_ids": [action_id],
            "evidence_ids": [],
            "answer_format": "single_choice",
            "options": options,
            "verification_rule": "pic_candidate_and_template_audit",
        })
    return questions


def _forensic_blockers(pack, step_id, *, excluded_action_ids=()):
    known = {
        "assertion_candidate_missing",
        "pic_authorization_required",
    }
    blockers = []
    excluded_action_ids = set(excluded_action_ids)
    for item in pack.get("unresolved_decisions") or ():
        if item.get("code") in known:
            continue
        action_id = str(item.get("action_id") or "unknown")
        if action_id in excluded_action_ids:
            continue
        blockers.append({
            "blocker_id": "forensic-" + _hash({
                "step_id": step_id,
                "action_id": action_id,
                "code": item.get("code"),
            })[:16],
            "step_id": step_id,
            "code": item.get("code"),
            "action_ids": [action_id] if action_id != "unknown" else [],
            "evidence_ids": item.get("evidence_ids") or [],
            "resolution": "inspect_evidence_or_rerecord",
        })
    return blockers


def _brief_forensic_blockers(
        brief,
        questions,
        existing,
        *,
        behavior_coverage=None,
    ):
    mode = (brief.get("risk") or {}).get("mode")
    if mode not in {"clarify", "forensic"}:
        return []
    if "ambiguities" in brief:
        return [
            _brief_blocker(
                code=str(item.get("code") or "evidence_required"),
                step_id=item.get("step_id"),
                action_id=(item.get("action_ids") or [None])[0],
                reason=str(item.get("ambiguity_id") or "evidence_required"),
                mode="forensic",
            )
            for item in brief.get("ambiguities") or ()
            if item.get("routing") == "evidence_required"
        ]
    covered_action_refs = {
        (str(question.get("step_id") or ""), str(action_id))
        for question in questions
        if question.get("blocking")
        for action_id in question.get("action_ids") or ()
    }
    covered_unscoped_action_ids = {
        action_id
        for step_id, action_id in covered_action_refs
        if not step_id
    }
    covered_codes = {
        str(item.get("code") or "")
        for item in existing
    }
    blocked_action_refs = {
        (str(item.get("step_id") or ""), str(action_id))
        for item in existing
        for action_id in item.get("action_ids") or ()
    }
    covered_operation_actions = {
        str(step_id): set(action_ids)
        for step_id, action_ids in (behavior_coverage or {}).items()
    }
    for question in questions:
        options = question.get("options") or []
        if (
            not question.get("blocking")
            or not options
            or not all(
                (option.get("plan_patch") or {}).get("kind") == "operation"
                for option in options
            )
        ):
            continue
        action_sets = [
            set(
                ((option.get("plan_patch") or {}).get("operation") or {}).get(
                    "action_ids"
                ) or []
            )
            for option in options
        ]
        guaranteed_actions = set.intersection(*action_sets)
        covered_operation_actions.setdefault(
            str(question.get("step_id") or ""),
            set(),
        ).update(str(item) for item in guaranteed_actions if item)
    for step_id, implementation in (
        ((brief.get("draft_plan") or {}).get("implementation") or {}).items()
    ):
        covered = covered_operation_actions.setdefault(str(step_id), set())
        for operation in implementation.get("operations") or ():
            covered.update(
                str(item)
                for item in operation.get("action_ids") or ()
                if item
            )
        covered.update(
            str(item)
            for item in implementation.get("ignored_action_ids") or ()
            if item
        )
    for step_id, action_id in blocked_action_refs:
        covered_operation_actions.setdefault(step_id, set()).add(action_id)
    effective_actions = {}
    for action in brief.get("actions") or ():
        if action.get("role") == "noise" or not action.get("id"):
            continue
        effective_actions.setdefault(
            str(action.get("step_id") or ""),
            set(),
        ).add(str(action["id"]))
    operation_complete_steps = {
        step_id
        for step_id, action_ids in effective_actions.items()
        if action_ids <= covered_operation_actions.get(step_id, set())
    }
    blockers = []
    suggestions = [
        item
        for item in (brief.get("adjustment") or {}).get("suggestions") or ()
        if item.get("blocking")
    ]
    for suggestion in suggestions:
        reason = str(suggestion.get("reason") or suggestion.get("kind") or "")
        action_id = str(
            suggestion.get("action_id")
            or _action_id_from_reason(reason)
            or ""
        )
        step_id = str(suggestion.get("step_id") or "")
        if action_id and (step_id, action_id) in blocked_action_refs:
            continue
        if action_id and (
            (step_id and (step_id, action_id) in covered_action_refs)
            or (
                not step_id
                and action_id in covered_unscoped_action_ids
            )
        ):
            continue
        if (
            reason.startswith("step_operations_missing:")
            and step_id in operation_complete_steps
        ):
            continue
        blockers.append(_brief_blocker(
            code=str(suggestion.get("kind") or reason or "brief_ambiguity"),
            step_id=step_id or None,
            action_id=action_id,
            reason=reason,
            mode=mode,
        ))
    if blockers or suggestions:
        return blockers
    for reason_value in (brief.get("risk") or {}).get("reasons") or ():
        reason = str(reason_value)
        if reason in {"decision_pack_required"} or reason in covered_codes:
            continue
        action_id = _action_id_from_reason(reason)
        if action_id and action_id in covered_unscoped_action_ids:
            continue
        blockers.append(_brief_blocker(
            code=reason.split(":", 1)[0] or "brief_ambiguity",
            step_id=None,
            action_id=action_id,
            reason=reason,
            mode=mode,
        ))
    return blockers


def _brief_blocker(*, code, step_id, action_id, reason, mode):
    return {
        "blocker_id": "forensic-" + _hash({
            "source": "generation_brief",
            "code": code,
            "step_id": step_id,
            "action_id": action_id,
            "reason": reason,
        })[:16],
        "step_id": step_id,
        "code": code,
        "action_ids": [action_id] if action_id else [],
        "evidence_ids": [],
        "reason": reason,
        "source": "generation_brief",
        "resolution": (
            "inspect_named_evidence_or_rerecord"
            if mode == "forensic"
            else "propose_structured_options_or_rerecord"
        ),
    }


def _action_id_from_reason(reason):
    parts = str(reason or "").split(":")
    return parts[1] if len(parts) >= 2 and parts[1] else None


def _pic_authorization(pack, question, option, decision_id):
    patch = option.get("plan_patch") or {}
    candidate = patch.get("candidate") or {}
    return {
        "authorization_id": "pic-auth-" + _hash({
            "pack": pack.get("pack_id"),
            "decision": decision_id,
            "candidate": candidate.get("candidate_id"),
        })[:16],
        "authorized": bool(patch.get("authorized")),
        "request_id": pack.get("request_id"),
        "revision_seal": pack.get("revision_seal"),
        "step_id": question.get("step_id"),
        "action_id": patch.get("action_id"),
        "candidate_id": candidate.get("candidate_id"),
        "source_frame": candidate.get("source_frame"),
        "crop_rectangle": candidate.get("crop_rectangle"),
        "template_artifact": candidate.get("template_artifact"),
        "template_request_path": candidate.get("template_request_path"),
        "template_request_sha256": candidate.get("template_request_sha256"),
        "template_sha256": candidate.get("template_sha256"),
        "region": candidate.get("region"),
        "region_locator_name": candidate.get("region_locator_name"),
        "region_locator": candidate.get("region_locator"),
        "target_data_path": _pic_target_data_path(
            pack.get("request_id"),
            candidate.get("candidate_id"),
        ),
        "audit_id": candidate.get("audit_id"),
        "audit_request_path": candidate.get("audit_request_path"),
        "audit_request_sha256": candidate.get("audit_request_sha256"),
        "audit_status": candidate.get("audit_status"),
        "cross_frame_validation": candidate.get("cross_frame_validation"),
        "constraints": {
            "region_required": True,
            "cross_frame_unique_match_required": True,
            "template_hash_required_before_prepare": True,
            "expires_after_transaction": True,
        },
    }


def _with_question_presentation(question, request, brief):
    question = dict(question)
    step = _decision_step_summary(
        question.get("step_id"),
        request,
        brief,
    )
    observed = _decision_observed_summary(
        question,
        step,
        request,
        brief,
    )
    uncertainty = _decision_uncertainty(question, brief)
    recommendation = _decision_recommendation(question)
    option_effects = [
        {
            "option_id": option.get("option_id"),
            "label": option.get("label"),
            "effect": _decision_option_effect(question, option, brief),
        }
        for option in question.get("options") or ()
    ]
    step_label = step.get("text") or step.get("id") or "当前 Step"
    question["prompt"] = (
        f"在 Step「{step_label}」中，{observed}"
        f"{uncertainty}请选择最符合真实业务的选项。"
    )
    question["presentation"] = {
        "presentation_version": "1.0",
        "step": step,
        "observed": observed,
        "uncertainty": uncertainty,
        "recommendation": recommendation,
        "option_effects": option_effects,
        "fallback": "如果都不符合，请返回时间线校正或最小补录；自由文本不会直接修改 Plan。",
    }
    return question


def _decision_step_summary(step_id, request, brief):
    step_id = str(step_id or "")
    candidates = [
        *((request.get("target") or {}).get("steps") or ()),
        *((brief.get("target") or {}).get("steps") or ()),
    ]
    step = next((
        item
        for item in candidates
        if isinstance(item, dict)
        and str(item.get("id") or "") == step_id
    ), {})
    return {
        "id": step_id,
        "keyword": str(
            step.get("keyword") or step.get("semantic_type") or ""
        ),
        "text": str(step.get("text") or ""),
    }


def _decision_observed_summary(question, step, request, brief):
    question_type = str(question.get("type") or "")
    pause_evidence = _pause_decision_evidence(question, brief)
    if pause_evidence is not None:
        summary = pause_evidence.get("state_diff_summary") or {}
        start_note = str(pause_evidence.get("start_note") or "").strip()
        end_note = str(pause_evidence.get("end_note") or "").strip()
        note_summary = (
            (
                f"我记录到暂停开始备注「{start_note or '未填写'}」，"
                f"结束备注「{end_note or '未填写'}」；"
            )
            if start_note or end_note
            else ""
        )
        if not summary:
            return (
                note_summary + "该旧录制没有可展示的状态差异计数。"
                if note_summary
                else "该录制没有可展示的状态差异计数。"
            )
        return note_summary + (
            f"状态差异为新增 {summary.get('added_count', 0)}、删除 "
            f"{summary.get('removed_count', 0)}、变化 "
            f"{summary.get('changed_count', 0)}，涉及 "
            f"{summary.get('changed_window_count', 0)} 个窗口。"
        )
    if question_type == "specification_business_conflict":
        facts = question.get("facts") or {}
        return (
            "我看到 Feature 声明「"
            + str(facts.get("feature_claim") or "")
            + "」，但当前 Rule 声明「"
            + str(facts.get("rule_claim") or "")
            + "」。"
        )
    if question_type == "step_context_business_conflict":
        facts = question.get("facts") or {}
        return (
            "我看到 Feature 声明「"
            + str(facts.get("feature_claim") or "")
            + "」，但当前 Step 业务说明声明「"
            + str(facts.get("step_context_claim") or "")
            + "」。"
        )
    if question_type == "table_usage":
        source_step = next((
            item
            for item in [
                *((request.get("target") or {}).get("steps") or ()),
                *((brief.get("target") or {}).get("steps") or ()),
            ]
            if isinstance(item, dict)
            and str(item.get("id") or "") == str(step.get("id") or "")
        ), None)
        table = (source_step or {}).get("table") or {}
        row_count = int(
            table.get("row_count")
            or len(table.get("rows") or ())
        )
        headings = [
            str(item) for item in table.get("headings") or ()
        ]
        columns = "、".join(headings) if headings else "未命名列"
        return f"我看到一个包含 {row_count} 行、列为 {columns} 的 Data Table。"
    actions = [
        action
        for action in brief.get("actions") or ()
        if isinstance(action, dict)
        and str(action.get("id") or "")
        in {str(item) for item in question.get("action_ids") or ()}
    ]
    if actions:
        descriptions = [
            _decision_action_description(action)
            for action in actions[:3]
        ]
        suffix = "等动作" if len(actions) > len(descriptions) else ""
        return "我录到" + "、".join(descriptions) + suffix + "。"
    if question_type == "pic_authorization":
        return "结构化定位没有形成可用的唯一目标，但存在一个已审计的图片模板。"
    return "我保留了与该问题关联的录制证据，但现有事实不能唯一确定业务含义。"


def _decision_action_description(action):
    target = action.get("target") or {}
    action_type = str(action.get("type") or "操作")
    target_name = str(
        target.get("name")
        or target.get("locator_name")
        or target.get("control_type")
        or "录制目标"
    )
    return f"{action_type}「{target_name}」"


def _decision_uncertainty(question, brief):
    if _pause_decision_evidence(question, brief) is not None:
        return "我不确定暂停期间的状态变化是当前 Step 的前置准备、业务动作，还是与它无关。"
    return {
        "table_usage": "我不确定这些行是独立执行、连续执行，还是作为整体数据使用。",
        "pic_authorization": "我不确定你是否接受图片定位的维护成本和误匹配风险。",
        "ambiguity_resolution": "现有证据支持多个业务解释，我不确定哪一个符合你的意图。",
        "specification_business_conflict": (
            "两条冻结规格对同一阈值给出相反结果，我不能替业务方决定哪一条正确。"
        ),
        "step_context_business_conflict": (
            "Feature与当前Step业务说明给出相反结果，我不能替业务方决定采用哪一个。"
        ),
    }.get(
        str(question.get("type") or ""),
        "我不确定当前证据对应哪一种业务解释。",
    )


def _decision_recommendation(question):
    question_type = str(question.get("type") or "")
    if question_type == "pic_authorization":
        text = "这是高风险授权，AI 不会替你预选；请根据目标稳定性和维护成本决定。"
    else:
        text = "这是业务意图问题，AI 不会替你预选。"
    return {
        "option_id": None,
        "text": text,
        "basis": "user_authority",
    }


def _decision_option_effect(question, option, brief):
    patch = option.get("plan_patch") or {}
    kind = str(patch.get("kind") or "")
    if kind == "table_business_outcome":
        return (
            "该选择只确认表格行之间的业务关系；"
            "具体代码结构、消费位置和列绑定由 AI 设计。"
        )
    if kind == "pic_authorization":
        return (
            "允许当前 Action 使用审计通过且事务内有效的图片模板。"
            if patch.get("authorized")
            else "不使用图片定位，转而修复结构定位或补录证据。"
        )
    if kind == "ambiguity_resolution":
        if _pause_decision_evidence(question, brief) is not None:
            outcome = str(
                (patch.get("resolution") or {}).get("outcome") or ""
            )
            return {
                "unrelated_to_step": "确认暂停变化与当前 Step 无关，不把它写入实现。",
                "step_precondition": "确认暂停变化属于执行当前 Step 前必须满足的状态。",
                "belongs_to_step": "确认变化属于当前 Step，并要求回到时间线补录对应动作。",
            }.get(outcome, "该选择只约束暂停变化的业务归属。")
        if question.get("type") in {
            "specification_business_conflict",
            "step_context_business_conflict",
        }:
            return (
                "该选择冻结本次生成采用的业务规格来源；AI仍负责根据选择形成完整Plan。"
            )
        return "该选择只约束对应业务歧义；AI 仍负责形成完整实现 Plan。"
    return "该选择将作为用户确认写入 Decision，并约束后续 Plan。"


def _pause_decision_evidence(question, brief):
    ambiguity_id = str(question.get("ambiguity_id") or "")
    ambiguity = next((
        item
        for item in brief.get("ambiguities") or ()
        if str(item.get("ambiguity_id") or "") == ambiguity_id
    ), None)
    if not ambiguity or ambiguity.get("code") != "pause_state_changed":
        return None
    evidence = (ambiguity.get("facts") or {}).get("evidence")
    return evidence if isinstance(evidence, dict) else {}


def _dedupe_questions(questions):
    result = {}
    for question in questions:
        result[question["question_id"]] = question
    return list(result.values())


def _dedupe_blockers(blockers):
    result = {}
    for blocker in blockers:
        result[blocker["blocker_id"]] = blocker
    return list(result.values())


def _question_id(kind, step_id, action_id, code=None):
    identity = _hash({
        "kind": kind,
        "step_id": step_id,
        "action_id": action_id,
        "code": code,
    })[:16]
    return f"q-{kind}-{identity}"


def _pic_target_data_path(request_id, candidate_id):
    safe_request = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(request_id or "request"))
    safe_candidate = re.sub(
        r"[^0-9A-Za-z_.-]+",
        "_",
        str(candidate_id or "candidate"),
    )
    return f"recorder_pic/{safe_request}/{safe_candidate}.png"


def _unique(values):
    return list(dict.fromkeys(value for value in values if value))


def _decision_dir(session_dir, request_id):
    return (
        Path(session_dir).resolve()
        / "ai"
        / "decisions"
        / str(request_id)
    )


def _resolve_decision_path(session_dir, request_id, value):
    if not value:
        return None
    session_dir = Path(session_dir).resolve()
    root = _decision_dir(session_dir, request_id).resolve()
    candidate = Path(value)
    path = (
        candidate.resolve()
        if candidate.is_absolute()
        else (session_dir / candidate).resolve()
    )
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path if path.is_file() else None


def _read_json(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 必须是 object: {path}")
    return value


def _hash(value):
    return hashlib.sha256(json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")).hexdigest()


def _pack_fingerprint(value):
    return _hash({
        key: item
        for key, item in value.items()
        if key not in {"pack_fingerprint", "pack_id"}
    })


def _answer_fingerprint(pack, answers):
    return _hash({
        "pack_fingerprint": pack.get("pack_fingerprint"),
        "revision_seal": pack.get("revision_seal"),
        "answers": answers.get("answers") or [],
    })