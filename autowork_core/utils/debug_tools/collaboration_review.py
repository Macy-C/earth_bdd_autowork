from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path, PurePosixPath

from autowork_core.utils.debug_tools.ai_paths import (
    PROJECT_KNOWLEDGE_ROOT,
)

REVIEW_VERSION = "1.0"
PROMOTION_VERSION = "1.0"
REVIEW_PREFIX = "collaboration-review-"
PROMOTION_PREFIX = "collaboration-promotion-"
REVIEW_DIRECTORY = PROJECT_KNOWLEDGE_ROOT / "collaboration-reviews"
PROMOTION_DIRECTORY = PROJECT_KNOWLEDGE_ROOT / "collaboration-promotions"
SCOPES = {"user", "repository", "architecture"}
TARGETS = {
    "user_memory",
    "copilot_instructions",
    "file_instructions",
    "prompt",
    "ai_context",
    "none",
}
CONFIDENCES = {"low", "medium", "high"}
EVIDENCE_KINDS = {
    "user_correction",
    "successful_pattern",
    "failed_attempt",
    "validation_result",
    "explicit_preference",
}
TARGET_SCOPE = {
    "user_memory": "user",
    "copilot_instructions": "repository",
    "file_instructions": "repository",
    "prompt": "repository",
    "ai_context": "architecture",
}
PRIVATE_PATH = re.compile(
    r"(?:[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/]|"
    r"\\\\[^\\\s]+\\[^\\\s]+)",
    re.IGNORECASE,
)
CREDENTIAL = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|password|passwd|secret|"
    r"authorization)\s*[:=]\s*[^\s,;]+"
)
SENSITIVE_KEY = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|password|passwd|secret|"
    r"authorization)"
)
REVIEW_REQUIRED = {
    "review_version",
    "review_id",
    "generated_at",
    "repository",
    "window",
    "source_sessions",
    "summary",
    "candidates",
}
PROMOTION_REQUIRED = {
    "promotion_version",
    "promotion_id",
    "promoted_at",
    "source_review",
    "approved_candidate_ids",
    "changes",
    "withheld",
    "validation",
    "rollback",
}
CANDIDATE_REQUIRED = {
    "candidate_id",
    "status",
    "title",
    "rule",
    "scope",
    "target",
    "target_path",
    "confidence",
    "independent_session_count",
    "occurrence_count",
    "evidence",
    "counterevidence",
    "expected_benefit",
    "risk",
    "validation",
}
REPOSITORY_FIELDS = {"name", "root", "remote", "branch"}
WINDOW_FIELDS = {"requested", "from", "to"}
SOURCE_SESSION_FIELDS = {
    "session_id",
    "agent_name",
    "summary",
    "updated_at",
}
SUMMARY_FIELDS = {
    "sessions_analyzed",
    "user_turns_analyzed",
    "candidate_count",
    "insufficient_evidence",
    "limitations",
}
CANDIDATE_FIELDS = CANDIDATE_REQUIRED | {"supersedes"}
EVIDENCE_FIELDS = {"session_id", "turn_index", "kind", "summary"}
CHANGE_FIELDS = {"candidate_id", "target", "path", "summary"}
WITHHELD_FIELDS = {"candidate_id", "reason"}
VALIDATION_FIELDS = {"check", "status", "detail"}
ROLLBACK_FIELDS = {"candidate_id", "path", "action"}


def validate_review(path, *, project_root):
    project_root = Path(project_root).resolve()
    errors = []
    resolved = _contained_artifact(
        path,
        project_root,
        REVIEW_DIRECTORY,
        REVIEW_PREFIX,
        errors,
    )
    value, text = _read_object(resolved, errors)
    if value is None:
        return _result("review", resolved, errors)

    _validate_review_structure(value, errors)
    _required(value, REVIEW_REQUIRED, errors, "review")
    _expect(value.get("review_version") == REVIEW_VERSION, errors,
            "review_version 必须为 1.0")
    review_id = _text(value.get("review_id"))
    _expect(
        re.fullmatch(
            r"collaboration-review-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}",
            review_id,
        ) is not None,
        errors,
        "review_id 格式无效",
    )
    _expect(
        resolved is not None and resolved.name == f"{review_id}.json",
        errors,
        "review_id 必须与文件名一致",
    )
    repository = _object(value.get("repository"))
    _expect(repository.get("root") == ".", errors,
            "repository.root 必须为 .")
    remote = repository.get("remote")
    if remote is not None:
        _expect(
            re.fullmatch(
                r"[A-Za-z0-9.-]+/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+",
                str(remote),
            ) is not None,
            errors,
            "repository.remote 必须为空或 credential-free host/owner/repo",
        )

    sessions = _array(value.get("source_sessions"))
    session_ids = [_text(item.get("session_id")) for item in sessions
                   if isinstance(item, dict)]
    _unique(session_ids, errors, "source_sessions.session_id")
    known_sessions = set(session_ids)
    candidates = _array(value.get("candidates"))
    _expect(len(candidates) <= 12, errors, "candidates 不能超过 12 条")
    candidate_ids = [
        _text(item.get("candidate_id"))
        for item in candidates
        if isinstance(item, dict)
    ]
    _unique(candidate_ids, errors, "candidate_id")
    for candidate in candidates:
        _validate_candidate(candidate, known_sessions, errors)

    summary = _object(value.get("summary"))
    _expect(
        summary.get("candidate_count") == len(candidates),
        errors,
        "summary.candidate_count 与 candidates 数量不一致",
    )
    _expect(
        summary.get("sessions_analyzed") == len(sessions),
        errors,
        "summary.sessions_analyzed 与 source_sessions 数量不一致",
    )
    expected_suffix = _review_suffix(session_ids, candidates)
    _expect(
        bool(review_id) and review_id.endswith(expected_suffix),
        errors,
        "review_id 摘要与 source sessions/candidate rules 不一致",
    )
    _privacy(value, errors)
    _expect(_is_git_ignored(resolved, project_root), errors,
            "review 文件必须被 Git ignore")
    return _result("review", resolved, errors, value)


def validate_promotion(
    path,
    *,
    project_root,
    approved_candidate_ids,
):
    project_root = Path(project_root).resolve()
    errors = []
    resolved = _contained_artifact(
        path,
        project_root,
        PROMOTION_DIRECTORY,
        PROMOTION_PREFIX,
        errors,
    )
    value, text = _read_object(resolved, errors)
    if value is None:
        return _result("promotion", resolved, errors)

    _validate_promotion_structure(value, errors)
    _required(value, PROMOTION_REQUIRED, errors, "promotion")
    _expect(value.get("promotion_version") == PROMOTION_VERSION, errors,
            "promotion_version 必须为 1.0")
    promotion_id = _text(value.get("promotion_id"))
    _expect(
        re.fullmatch(
            r"collaboration-promotion-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}",
            promotion_id,
        ) is not None,
        errors,
        "promotion_id 格式无效",
    )
    _expect(
        resolved is not None and resolved.name == f"{promotion_id}.json",
        errors,
        "promotion_id 必须与文件名一致",
    )
    source_value = _text(value.get("source_review"))
    source_path = _resolve_relative(source_value, project_root, errors,
                                    "source_review")
    review = validate_review(source_path, project_root=project_root)
    if review["status"] != "passed":
        errors.extend(
            f"source review: {message}"
            for message in review.get("errors") or []
        )
    review_candidates = {
        item.get("candidate_id"): item
        for item in (review.get("value") or {}).get("candidates") or []
        if isinstance(item, dict) and item.get("candidate_id")
    }
    approved = [_text(item) for item in
                _array(value.get("approved_candidate_ids"))]
    _unique(approved, errors, "approved_candidate_ids")
    _expect(bool(approved), errors, "approved_candidate_ids 不能为空")
    invocation_approved = [_text(item) for item in approved_candidate_ids]
    _unique(invocation_approved, errors, "本次调用 approved_candidate_ids")
    _expect(bool(invocation_approved), errors,
            "validate-promotion 必须显式传入本次批准 candidate IDs")
    _expect(
        set(invocation_approved) == set(approved),
        errors,
        "本次调用批准 candidate IDs 与 promotion receipt 不一致",
    )
    unknown = sorted(set(approved) - set(review_candidates))
    _expect(not unknown, errors, f"批准了 review 外的 candidate: {unknown}")
    for candidate_id in approved:
        candidate = review_candidates.get(candidate_id) or {}
        _expect(candidate.get("status") == "proposed", errors,
                f"{candidate_id} 不是 proposed")
        _expect(candidate.get("confidence") != "low", errors,
                f"{candidate_id} 置信度为 low，不能晋升")
        _expect(candidate.get("target") != "none", errors,
                f"{candidate_id} target=none，不能晋升")

    changes = _array(value.get("changes"))
    withheld = _array(value.get("withheld"))
    _expect(bool(changes), errors, "changes 不能为空；没有落地变更则不应创建回执")
    changed_ids = [_text(item.get("candidate_id")) for item in changes
                   if isinstance(item, dict)]
    withheld_ids = [_text(item.get("candidate_id")) for item in withheld
                    if isinstance(item, dict)]
    _unique(changed_ids, errors, "changes.candidate_id")
    _unique(withheld_ids, errors, "withheld.candidate_id")
    _expect(not (set(changed_ids) & set(withheld_ids)), errors,
            "changes 与 withheld 不能包含同一 candidate")
    _expect(set(changed_ids) | set(withheld_ids) == set(approved), errors,
            "changes 与 withheld 必须完整覆盖批准 candidate")
    for change in changes:
        _validate_change(change, review_candidates, set(approved), errors)
    for item in withheld:
        _expect(
            isinstance(item, dict)
            and item.get("candidate_id") in approved
            and bool(_text(item.get("reason"))),
            errors,
            "withheld 必须引用已批准 candidate 并给出原因",
        )

    validations = _array(value.get("validation"))
    _expect(bool(validations), errors, "validation 不能为空")
    for validation in validations:
        _expect(
            isinstance(validation, dict)
            and validation.get("status") == "passed",
            errors,
            "所有 validation status 必须为 passed",
        )
    rollback = _array(value.get("rollback"))
    rollback_ids = [
        _text(item.get("candidate_id"))
        for item in rollback
        if isinstance(item, dict)
    ]
    _unique(rollback_ids, errors, "rollback.candidate_id")
    change_paths = {
        item.get("candidate_id"): item.get("path")
        for item in changes
        if isinstance(item, dict)
    }
    rollback_paths = {
        item.get("candidate_id"): item.get("path")
        for item in rollback
        if isinstance(item, dict) and bool(_text(item.get("action")))
    }
    _expect(change_paths == rollback_paths, errors,
            "rollback 必须以相同 candidate/path 逐项覆盖 changes")
    for item in rollback:
        if isinstance(item, dict):
            _persistent_path(item.get("path"), errors, "rollback.path")

    expected_suffix = _promotion_suffix(
        _text((review.get("value") or {}).get("review_id")),
        approved,
        [item.get("path") for item in changes if isinstance(item, dict)],
    )
    _expect(
        bool(promotion_id) and promotion_id.endswith(expected_suffix),
        errors,
        "promotion_id 摘要与 review/approvals/paths 不一致",
    )
    _privacy(value, errors)
    _expect(_is_git_ignored(resolved, project_root), errors,
            "promotion receipt 必须被 Git ignore")
    return _result("promotion", resolved, errors, value)


def _validate_review_structure(value, errors):
    _strict_object(value, REVIEW_REQUIRED, REVIEW_REQUIRED, errors, "review")
    _string(value.get("review_version"), errors, "review_version")
    _string(value.get("review_id"), errors, "review_id")
    _date_time(value.get("generated_at"), errors, "generated_at")

    repository = _strict_object(
        value.get("repository"),
        REPOSITORY_FIELDS,
        {"name", "root"},
        errors,
        "repository",
    )
    _string(repository.get("name"), errors, "repository.name", max_length=240)
    _string(repository.get("root"), errors, "repository.root")
    _nullable_string(repository.get("remote"), errors, "repository.remote")
    _nullable_string(repository.get("branch"), errors, "repository.branch")

    window = _strict_object(
        value.get("window"),
        WINDOW_FIELDS,
        WINDOW_FIELDS,
        errors,
        "window",
    )
    _string(window.get("requested"), errors, "window.requested", max_length=240)
    _date_time(window.get("from"), errors, "window.from")
    _date_time(window.get("to"), errors, "window.to")

    sessions = _strict_array(
        value.get("source_sessions"),
        errors,
        "source_sessions",
        max_items=50,
    )
    for index, item in enumerate(sessions):
        label = f"source_sessions[{index}]"
        session = _strict_object(
            item,
            SOURCE_SESSION_FIELDS,
            {"session_id", "agent_name", "updated_at"},
            errors,
            label,
        )
        _string(session.get("session_id"), errors, f"{label}.session_id")
        _nullable_string(
            session.get("agent_name"),
            errors,
            f"{label}.agent_name",
        )
        _nullable_string(
            session.get("summary"),
            errors,
            f"{label}.summary",
            max_length=240,
        )
        _date_time(session.get("updated_at"), errors, f"{label}.updated_at")

    summary = _strict_object(
        value.get("summary"),
        SUMMARY_FIELDS,
        SUMMARY_FIELDS - {"limitations"},
        errors,
        "summary",
    )
    _integer(summary.get("sessions_analyzed"), errors,
             "summary.sessions_analyzed", minimum=0)
    _integer(summary.get("user_turns_analyzed"), errors,
             "summary.user_turns_analyzed", minimum=0)
    _integer(summary.get("candidate_count"), errors,
             "summary.candidate_count", minimum=0, maximum=12)
    _boolean(summary.get("insufficient_evidence"), errors,
             "summary.insufficient_evidence")
    limitations = _strict_array(
        summary.get("limitations", []),
        errors,
        "summary.limitations",
        max_items=12,
    )
    for index, item in enumerate(limitations):
        _string(item, errors, f"summary.limitations[{index}]", max_length=240)

    candidates = _strict_array(
        value.get("candidates"),
        errors,
        "candidates",
        max_items=12,
    )
    for index, candidate in enumerate(candidates):
        _validate_candidate_structure(candidate, errors, f"candidates[{index}]")


def _validate_candidate_structure(value, errors, label):
    candidate = _strict_object(
        value,
        CANDIDATE_FIELDS,
        CANDIDATE_REQUIRED,
        errors,
        label,
    )
    for key, maximum in (
        ("candidate_id", 120),
        ("status", 20),
        ("title", 100),
        ("rule", 500),
        ("scope", 20),
        ("target", 40),
        ("confidence", 20),
        ("expected_benefit", 300),
        ("risk", 300),
    ):
        _string(candidate.get(key), errors, f"{label}.{key}", max_length=maximum)
    _nullable_string(
        candidate.get("target_path"),
        errors,
        f"{label}.target_path",
        max_length=240,
    )
    _integer(candidate.get("independent_session_count"), errors,
             f"{label}.independent_session_count", minimum=1)
    _integer(candidate.get("occurrence_count"), errors,
             f"{label}.occurrence_count", minimum=1)
    for key, minimum in (("evidence", 1), ("counterevidence", 0)):
        items = _strict_array(
            candidate.get(key),
            errors,
            f"{label}.{key}",
            min_items=minimum,
            max_items=8,
        )
        for index, item in enumerate(items):
            _validate_evidence_structure(
                item,
                errors,
                f"{label}.{key}[{index}]",
            )
    validations = _strict_array(
        candidate.get("validation"),
        errors,
        f"{label}.validation",
        min_items=1,
        max_items=8,
    )
    for index, item in enumerate(validations):
        _string(
            item,
            errors,
            f"{label}.validation[{index}]",
            max_length=240,
        )
    supersedes = _strict_array(
        candidate.get("supersedes", []),
        errors,
        f"{label}.supersedes",
        max_items=8,
    )
    for index, item in enumerate(supersedes):
        _string(
            item,
            errors,
            f"{label}.supersedes[{index}]",
            max_length=120,
        )


def _validate_evidence_structure(value, errors, label):
    evidence = _strict_object(
        value,
        EVIDENCE_FIELDS,
        EVIDENCE_FIELDS,
        errors,
        label,
    )
    _string(evidence.get("session_id"), errors, f"{label}.session_id")
    _integer(evidence.get("turn_index"), errors,
             f"{label}.turn_index", minimum=0)
    _string(evidence.get("kind"), errors, f"{label}.kind")
    _string(evidence.get("summary"), errors,
            f"{label}.summary", max_length=240)


def _validate_promotion_structure(value, errors):
    _strict_object(
        value,
        PROMOTION_REQUIRED,
        PROMOTION_REQUIRED,
        errors,
        "promotion",
    )
    _string(value.get("promotion_version"), errors, "promotion_version")
    _string(value.get("promotion_id"), errors, "promotion_id")
    _date_time(value.get("promoted_at"), errors, "promoted_at")
    _string(value.get("source_review"), errors, "source_review", max_length=300)
    approved = _strict_array(
        value.get("approved_candidate_ids"),
        errors,
        "approved_candidate_ids",
        min_items=1,
        max_items=12,
    )
    for index, item in enumerate(approved):
        _string(item, errors, f"approved_candidate_ids[{index}]", max_length=120)
    changes = _strict_array(
        value.get("changes"),
        errors,
        "changes",
        min_items=1,
        max_items=20,
    )
    for index, item in enumerate(changes):
        label = f"changes[{index}]"
        change = _strict_object(
            item,
            CHANGE_FIELDS,
            CHANGE_FIELDS,
            errors,
            label,
        )
        _string(change.get("candidate_id"), errors, f"{label}.candidate_id")
        _string(change.get("target"), errors, f"{label}.target")
        _string(change.get("path"), errors, f"{label}.path", max_length=300)
        _string(change.get("summary"), errors, f"{label}.summary", max_length=300)
    withheld = _strict_array(
        value.get("withheld"),
        errors,
        "withheld",
        max_items=12,
    )
    for index, item in enumerate(withheld):
        label = f"withheld[{index}]"
        withheld_item = _strict_object(
            item,
            WITHHELD_FIELDS,
            WITHHELD_FIELDS,
            errors,
            label,
        )
        _string(withheld_item.get("candidate_id"), errors,
                f"{label}.candidate_id")
        _string(withheld_item.get("reason"), errors,
                f"{label}.reason", max_length=300)
    validations = _strict_array(
        value.get("validation"),
        errors,
        "validation",
        min_items=1,
        max_items=20,
    )
    for index, item in enumerate(validations):
        label = f"validation[{index}]"
        validation = _strict_object(
            item,
            VALIDATION_FIELDS,
            {"check", "status"},
            errors,
            label,
        )
        _string(validation.get("check"), errors, f"{label}.check", max_length=300)
        _string(validation.get("status"), errors, f"{label}.status")
        _nullable_string(validation.get("detail"), errors,
                         f"{label}.detail", max_length=500)
    rollback = _strict_array(
        value.get("rollback"),
        errors,
        "rollback",
        min_items=1,
        max_items=20,
    )
    for index, item in enumerate(rollback):
        label = f"rollback[{index}]"
        rollback_item = _strict_object(
            item,
            ROLLBACK_FIELDS,
            ROLLBACK_FIELDS,
            errors,
            label,
        )
        _string(rollback_item.get("candidate_id"), errors,
                f"{label}.candidate_id")
        _string(rollback_item.get("path"), errors,
                f"{label}.path", max_length=300)
        _string(rollback_item.get("action"), errors,
                f"{label}.action", max_length=300)


def _validate_candidate(candidate, known_sessions, errors):
    if not isinstance(candidate, dict):
        errors.append("candidate 必须是 object")
        return
    _required(candidate, CANDIDATE_REQUIRED, errors, "candidate")
    candidate_id = _text(candidate.get("candidate_id"))
    _expect(
        re.fullmatch(r"candidate-[0-9]{2}-[a-z0-9-]+", candidate_id)
        is not None,
        errors,
        f"candidate_id 格式无效: {candidate_id}",
    )
    _expect(candidate.get("status") == "proposed", errors,
            f"{candidate_id} status 必须为 proposed")
    scope = candidate.get("scope")
    target = candidate.get("target")
    confidence = candidate.get("confidence")
    _expect(scope in SCOPES, errors, f"{candidate_id} scope 无效")
    _expect(target in TARGETS, errors, f"{candidate_id} target 无效")
    _expect(confidence in CONFIDENCES, errors,
            f"{candidate_id} confidence 无效")
    if confidence == "low":
        _expect(target == "none", errors,
                f"{candidate_id} low confidence 必须 target=none")
    if target == "none":
        _expect(candidate.get("target_path") is None, errors,
                f"{candidate_id} target=none 必须 target_path=null")
    else:
        _expect(TARGET_SCOPE.get(target) == scope, errors,
                f"{candidate_id} scope 与 target 不匹配")
        _target_path(target, candidate.get("target_path"), errors,
                     f"{candidate_id}.target_path")
    evidence = _array(candidate.get("evidence"))
    _expect(1 <= len(evidence) <= 8, errors,
            f"{candidate_id} evidence 数量必须为 1..8")
    counterevidence = _array(candidate.get("counterevidence"))
    evidence_references = [
        (
            item.get("session_id"),
            item.get("turn_index"),
            item.get("kind"),
        )
        for item in evidence + counterevidence
        if isinstance(item, dict)
    ]
    _unique(evidence_references, errors, f"{candidate_id} evidence reference")
    for item in evidence + counterevidence:
        if not isinstance(item, dict):
            errors.append(f"{candidate_id} evidence 必须是 object")
            continue
        _expect(item.get("session_id") in known_sessions, errors,
                f"{candidate_id} 引用未知 session")
        _expect(item.get("kind") in EVIDENCE_KINDS, errors,
                f"{candidate_id} evidence kind 无效")
        _expect(isinstance(item.get("turn_index"), int)
                and item["turn_index"] >= 0, errors,
                f"{candidate_id} turn_index 无效")
    independent = {
        item.get("session_id")
        for item in evidence
        if isinstance(item, dict) and item.get("session_id")
    }
    independent_count = candidate.get("independent_session_count")
    _expect(
        isinstance(independent_count, int) and independent_count >= 1,
        errors,
        f"{candidate_id} independent_session_count 必须是正整数",
    )
    if isinstance(independent_count, int):
        _expect(independent_count == len(independent), errors,
                f"{candidate_id} independent_session_count 不准确")
    occurrence_count = candidate.get("occurrence_count")
    _expect(
        isinstance(occurrence_count, int) and occurrence_count >= 1,
        errors,
        f"{candidate_id} occurrence_count 必须是正整数",
    )
    if isinstance(occurrence_count, int):
        _expect(occurrence_count >= len(evidence), errors,
                f"{candidate_id} occurrence_count 小于 evidence 数量")
    evidence_kinds = {
        item.get("kind")
        for item in evidence
        if isinstance(item, dict)
    }
    if confidence == "high":
        _expect(len(independent) >= 3, errors,
                f"{candidate_id} high confidence 至少需要 3 个独立 session")
        _expect(not _array(candidate.get("counterevidence")), errors,
                f"{candidate_id} high confidence 不能包含反例")
    elif confidence == "medium":
        preference_count = sum(
            item.get("kind") == "explicit_preference"
            for item in evidence
            if isinstance(item, dict)
        )
        corroborated = (
            preference_count >= 2
            and bool(
                {"successful_pattern", "validation_result"}
                & evidence_kinds
            )
        )
        _expect(len(independent) >= 2 or corroborated, errors,
                f"{candidate_id} medium confidence 证据不足")


def _validate_change(change, candidates, approved, errors):
    if not isinstance(change, dict):
        errors.append("change 必须是 object")
        return
    candidate_id = change.get("candidate_id")
    _expect(candidate_id in approved, errors,
            f"change 引用未批准 candidate: {candidate_id}")
    candidate = candidates.get(candidate_id) or {}
    _expect(change.get("target") == candidate.get("target"), errors,
            f"{candidate_id} change target 与 review 不一致")
    _expect(change.get("path") == candidate.get("target_path"), errors,
            f"{candidate_id} change path 与 review 不一致")
    _target_path(change.get("target"), change.get("path"), errors,
                 f"{candidate_id}.change.path")


def _target_path(target, path, errors, label):
    if target == "user_memory":
        _expect(_memory_path(path), errors, f"{label} 必须位于 /memories")
        return
    if target == "copilot_instructions":
        _expect(path == ".github/copilot-instructions.md", errors,
                f"{label} 必须为 .github/copilot-instructions.md")
        return
    patterns = {
        "file_instructions": r"\.github/instructions/[A-Za-z0-9._-]+\.instructions\.md",
        "prompt": r"ai/prompts/[A-Za-z0-9._-]+\.md",
        "ai_context": r"ai/context/[A-Za-z0-9._/-]+\.md",
    }
    _expect(
        isinstance(path, str)
        and re.fullmatch(patterns.get(target, r"(?!)"), path) is not None
        and _safe_relative(path),
        errors,
        f"{label} 与 target 不匹配或路径不安全",
    )


def _persistent_path(path, errors, label):
    _expect(_memory_path(path) or _safe_relative(path), errors,
            f"{label} 不是允许的持久化路径")


def _memory_path(path):
    return (
        isinstance(path, str)
        and re.fullmatch(r"/memories/[A-Za-z0-9._/-]+", path) is not None
        and ".." not in PurePosixPath(path).parts
    )


def _safe_relative(path):
    if not isinstance(path, str) or "\\" in path:
        return False
    pure = PurePosixPath(path)
    return (
        not pure.is_absolute()
        and ".." not in pure.parts
        and pure.parts
        and pure.parts[0] in {".github", "ai", "Bdd"}
    )


def _contained_artifact(path, project_root, relative_root, prefix, errors):
    candidate = Path(path)
    resolved = (
        candidate.resolve()
        if candidate.is_absolute()
        else (project_root / candidate).resolve()
    )
    root = (project_root / relative_root).resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        errors.append(f"artifact 路径越界: {resolved}")
        return None
    _expect(len(relative.parts) == 1, errors,
            "artifact 必须直接位于指定目录")
    _expect(resolved.name.startswith(prefix) and resolved.suffix == ".json",
            errors, "artifact 文件名无效")
    return resolved


def _resolve_relative(value, project_root, errors, label):
    if not _safe_relative(value):
        errors.append(f"{label} 必须是安全仓库相对路径")
        return project_root / "__invalid__"
    resolved = (project_root / value).resolve()
    try:
        resolved.relative_to(project_root)
    except ValueError:
        errors.append(f"{label} 路径越界")
    return resolved


def _read_object(path, errors):
    if path is None:
        return None, ""
    try:
        text = path.read_text(encoding="utf-8")
        value = json.loads(text)
    except (OSError, json.JSONDecodeError) as error:
        errors.append(f"JSON 无法读取: {type(error).__name__}: {error}")
        return None, ""
    if not isinstance(value, dict):
        errors.append("JSON 顶层必须是 object")
        return None, text
    return value, text


def _privacy(value, errors):
    text = "\n".join(_string_values(value))
    _expect(PRIVATE_PATH.search(text) is None, errors,
            "artifact 包含绝对私有路径或 UNC 路径")
    _expect(CREDENTIAL.search(text) is None, errors,
            "artifact 包含凭据形态文本")
    sensitive_keys = [
        key
        for key in _mapping_keys(value)
        if SENSITIVE_KEY.search(key)
    ]
    _expect(not sensitive_keys, errors,
            f"artifact 包含敏感字段名: {sorted(set(sensitive_keys))}")


def _string_values(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _string_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _string_values(item)
    elif isinstance(value, str):
        yield value


def _mapping_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _mapping_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _mapping_keys(item)


def _is_git_ignored(path, project_root):
    if path is None:
        return False
    try:
        result = subprocess.run(
            ["git", "check-ignore", "--quiet", str(path)],
            cwd=project_root,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    return result.returncode == 0


def _review_suffix(session_ids, candidates):
    value = {
        "session_ids": sorted(session_ids),
        "rules": sorted(
            str(item.get("rule") or "")
            for item in candidates
            if isinstance(item, dict)
        ),
    }
    return _stable_suffix(value)


def _promotion_suffix(source_review, approved, paths):
    return _stable_suffix({
        "source_review": source_review,
        "approved_candidate_ids": sorted(approved),
        "target_paths": sorted(str(path or "") for path in paths),
    })


def _stable_suffix(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:8]


def _expect(condition, errors, message):
    if not condition:
        errors.append(message)


def _required(value, keys, errors, label):
    missing = sorted(keys - set(value))
    _expect(not missing, errors, f"{label} 缺少必需字段: {missing}")


def _strict_object(value, allowed, required, errors, label):
    if not isinstance(value, dict):
        errors.append(f"{label} 必须是 object")
        return {}
    _required(value, set(required), errors, label)
    unknown = sorted(set(value) - set(allowed))
    _expect(not unknown, errors, f"{label} 包含未知字段: {unknown}")
    return value


def _strict_array(
        value,
        errors,
        label,
        *,
        min_items=0,
        max_items=None,
):
    if not isinstance(value, list):
        errors.append(f"{label} 必须是 array")
        return []
    _expect(len(value) >= min_items, errors,
            f"{label} 至少需要 {min_items} 项")
    if max_items is not None:
        _expect(len(value) <= max_items, errors,
                f"{label} 不能超过 {max_items} 项")
    return value


def _string(value, errors, label, *, max_length=None):
    valid = isinstance(value, str) and bool(value.strip())
    _expect(valid, errors, f"{label} 必须是非空 string")
    if valid and max_length is not None:
        _expect(len(value) <= max_length, errors,
                f"{label} 不能超过 {max_length} 字符")


def _nullable_string(value, errors, label, *, max_length=None):
    if value is None:
        return
    _string(value, errors, label, max_length=max_length)


def _integer(value, errors, label, *, minimum=None, maximum=None):
    valid = isinstance(value, int) and not isinstance(value, bool)
    _expect(valid, errors, f"{label} 必须是 integer")
    if not valid:
        return
    if minimum is not None:
        _expect(value >= minimum, errors, f"{label} 不能小于 {minimum}")
    if maximum is not None:
        _expect(value <= maximum, errors, f"{label} 不能大于 {maximum}")


def _boolean(value, errors, label):
    _expect(isinstance(value, bool), errors, f"{label} 必须是 boolean")


def _date_time(value, errors, label):
    if not isinstance(value, str):
        errors.append(f"{label} 必须是 date-time string")
        return
    if re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T"
        r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?"
        r"(?:Z|[+-][0-9]{2}:[0-9]{2})",
        value,
    ) is None:
        errors.append(f"{label} 必须是带时区的 RFC3339 date-time")
        return
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError("timezone required")
    except ValueError:
        errors.append(f"{label} 不是有效 date-time")


def _unique(values, errors, label):
    compact = [value for value in values if value]
    _expect(len(compact) == len(values), errors, f"{label} 不能为空")
    _expect(len(compact) == len(set(compact)), errors, f"{label} 必须唯一")


def _array(value):
    return value if isinstance(value, list) else []


def _object(value):
    return value if isinstance(value, dict) else {}


def _text(value):
    return str(value or "").strip()


def _result(kind, path, errors, value=None):
    return {
        "validator_version": "1.0",
        "kind": kind,
        "status": "passed" if not errors else "invalid",
        "path": str(path) if path is not None else None,
        "errors": list(dict.fromkeys(errors)),
        "value": value or {},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Validate collaboration review and promotion artifacts",
    )
    parser.add_argument(
        "command",
        choices=(
            "validate-review",
            "validate-promotion",
        ),
    )
    parser.add_argument("path", nargs="?")
    parser.add_argument("--project-root", default=".")
    parser.add_argument(
        "--approved-candidate",
        action="append",
        default=[],
        help=(
            "Candidate ID explicitly approved in the current invocation; "
            "repeat for each approved candidate"
        ),
    )
    args = parser.parse_args(argv)
    if not args.path:
        parser.error(f"{args.command} requires an artifact path")
    if args.command == "validate-review":
        if args.approved_candidate:
            parser.error(
                "--approved-candidate is only valid for validate-promotion"
            )
        report = validate_review(args.path, project_root=args.project_root)
    else:
        if not args.approved_candidate:
            parser.error(
                "validate-promotion requires at least one "
                "--approved-candidate from the current user request"
            )
        report = validate_promotion(
            args.path,
            project_root=args.project_root,
            approved_candidate_ids=args.approved_candidate,
        )
    public = {
        key: value
        for key, value in report.items()
        if key != "value"
    }
    value = report.get("value") or {}
    if report["kind"] == "review":
        public["artifact_id"] = value.get("review_id")
        public["item_count"] = len(value.get("candidates") or [])
    else:
        public["artifact_id"] = value.get("promotion_id")
        public["item_count"] = len(value.get("changes") or [])
    print(json.dumps(public, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())