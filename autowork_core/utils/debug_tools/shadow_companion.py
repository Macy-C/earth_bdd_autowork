from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

from autowork_core.utils.debug_tools.ai_paths import (
    FRAMEWORK_AI_ROOT,
    PROJECT_KNOWLEDGE_ROOT,
)
SHADOW_COMPANION_VERSION = "1.0"
POLICY_PATH = FRAMEWORK_AI_ROOT / "context/shadow-companion-policy.json"
STORE_PATH = PROJECT_KNOWLEDGE_ROOT / "shadow-companion"
ACTIVE_PATH = STORE_PATH / "active.json"
RECENT_PATH = STORE_PATH / "recent"
CAPSULE_ID = re.compile(r"^shadow-[0-9a-f]{16}$")
RESULTS = {"passed", "failed", "unknown"}
VALIDATION_SCOPES = {"focused", "final"}
MATERIALIZATION_REASONS = {"validated_milestone", "handoff"}
MAX_TEXT = 500
PRIVATE_PATH = re.compile(
    r"(?:[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/]|"
    r"\\\\[^\\\s]+\\[^\\\s]+|/(?:home|Users)/[^\s,;]+)",
    re.IGNORECASE,
)
CREDENTIAL = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|"
    r"passwd|secret|authorization)\s*[:=]\s*[^\s,;]+"
)
SECRET_TOKEN = re.compile(
    r"(?i)(?:bearer\s+[A-Za-z0-9._~-]{16,}|"
    r"(?:^|[\s=:])(?:gh[pousr]_[A-Za-z0-9]{12,}|"
    r"sk-(?:proj-)?[A-Za-z0-9_-]{12,}))"
)
SOURCE_TEXT = re.compile(
    r"(?:^\s*(?:def|class|from|import)\s+|"
    r"^\s*[A-Za-z_]\w*\s*=|:=|=>)",
    re.IGNORECASE,
)
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def start_shadow_companion(
        project_root,
        *,
        goal,
        working_hypothesis,
        disprove_by,
        invariants,
        completed,
        next_action,
        key_files,
        evidence_facts=None,
        validation_summary=None,
        validation_result=None,
        validation_scope=None,
        materialization_reason="validated_milestone",
        now=None,
):
    root = Path(project_root).resolve()
    policy = load_shadow_companion_policy(root)
    _ensure_enabled(policy)
    with _store_lock(root):
        _ensure_store(root)
        active_path = root / ACTIVE_PATH
        if active_path.exists() or active_path.is_symlink():
            raise FileExistsError("Shadow Companion 已存在 active Capsule")
        capsule = _build_capsule(
            root,
            policy,
            capsule_id=f"shadow-{uuid.uuid4().hex[:16]}",
            created_at=_as_utc(now),
            revision=1,
            goal=goal,
            working_hypothesis=working_hypothesis,
            disprove_by=disprove_by,
            invariants=invariants,
            completed=completed,
            next_action=next_action,
            key_files=key_files,
            evidence_facts=evidence_facts,
            validation_summary=validation_summary,
            validation_result=validation_result,
            validation_scope=validation_scope,
            materialization_reason=materialization_reason,
            now=now,
        )
        _write_capsule(active_path, capsule, policy)
        _cleanup_recent_locked(root, policy, now=now)
    return _public_capsule(root, capsule)


def update_shadow_companion(
        project_root,
        *,
        capsule_id,
        goal,
        working_hypothesis,
        disprove_by,
        invariants,
        completed,
        next_action,
        key_files,
        evidence_facts=None,
        validation_summary=None,
        validation_result=None,
        validation_scope=None,
        materialization_reason="validated_milestone",
        now=None,
):
    root = Path(project_root).resolve()
    policy = load_shadow_companion_policy(root)
    _ensure_enabled(policy)
    with _store_lock(root):
        active = _read_active(root, policy)
        _require_capsule(active, capsule_id)
        capsule = _build_capsule(
            root,
            policy,
            capsule_id=active["capsule_id"],
            created_at=_parse_utc(active["created_at"]),
            revision=int(active["revision"]) + 1,
            goal=goal,
            working_hypothesis=working_hypothesis,
            disprove_by=disprove_by,
            invariants=invariants,
            completed=completed,
            next_action=next_action,
            key_files=key_files,
            evidence_facts=evidence_facts,
            validation_summary=validation_summary,
            validation_result=validation_result,
            validation_scope=validation_scope,
            materialization_reason=materialization_reason,
            now=now,
        )
        _write_capsule(root / ACTIVE_PATH, capsule, policy)
    return _public_capsule(root, capsule)


def shadow_companion_status(project_root):
    root = Path(project_root).resolve()
    policy = load_shadow_companion_policy(root)
    _ensure_enabled(policy)
    store = root / STORE_PATH
    if not (store.exists() or store.is_symlink()):
        return {
            "status": "empty",
            "active": None,
            "recent": [],
            "advisory_only": True,
            "capability_impact": "none",
        }
    with _store_lock(root):
        active_path = root / ACTIVE_PATH
        if not active_path.is_file():
            return {
                "status": "empty",
                "active": None,
                "recent": _recent_summaries(root, policy),
                "advisory_only": True,
                "capability_impact": "none",
            }
        capsule = _read_capsule(active_path, policy)
        return {
            "status": "active",
            "active": _public_capsule(root, capsule),
            "recent": _recent_summaries(root, policy),
            "advisory_only": True,
            "capability_impact": "none",
        }


def archive_shadow_companion(project_root, *, capsule_id, now=None):
    root = Path(project_root).resolve()
    policy = load_shadow_companion_policy(root)
    _ensure_enabled(policy)
    with _store_lock(root):
        capsule = _read_active(root, policy)
        _require_capsule(capsule, capsule_id)
        target = root / RECENT_PATH / f"{capsule_id}.json"
        if target.exists() or target.is_symlink():
            raise FileExistsError("Dormant Shadow Capsule 已存在")
        os.replace(root / ACTIVE_PATH, target)
        _cleanup_recent_locked(root, policy, now=now)
    return {
        "capsule_id": capsule_id,
        "status": "dormant",
        "advisory_only": True,
    }


def resume_shadow_companion(project_root, *, capsule_id, now=None):
    root = Path(project_root).resolve()
    policy = load_shadow_companion_policy(root)
    _ensure_enabled(policy)
    capsule_id = _capsule_id(capsule_id)
    with _store_lock(root):
        active_path = root / ACTIVE_PATH
        if active_path.exists() or active_path.is_symlink():
            raise FileExistsError("已有 active Shadow Capsule")
        source = root / RECENT_PATH / f"{capsule_id}.json"
        capsule = _read_capsule(source, policy)
        _require_capsule(capsule, capsule_id)
        freshness = _freshness(root, capsule)
        if not freshness["resume_eligible"]:
            raise ValueError("Shadow Capsule 项目或分支不匹配，不能自动恢复")
        os.replace(source, active_path)
    return _public_capsule(root, capsule, state="active")


def complete_shadow_companion(project_root, *, capsule_id):
    root = Path(project_root).resolve()
    policy = load_shadow_companion_policy(root)
    _ensure_enabled(policy)
    with _store_lock(root):
        capsule = _read_active(root, policy)
        _require_capsule(capsule, capsule_id)
        validation = capsule.get("last_validation") or {}
        freshness = _freshness(root, capsule)
        if any((
            validation.get("result") != "passed",
            validation.get("scope") != "final",
            freshness["validation_current"] is not True,
        )):
            raise ValueError(
                "完成 Shadow Capsule 需要 fresh passed final validation"
            )
        (root / ACTIVE_PATH).unlink()
        _prune_empty_store(root)
    return {
        "capsule_id": capsule_id,
        "status": "completed",
        "deleted": True,
        "advisory_only": True,
    }


def discard_shadow_companion(project_root, *, capsule_id):
    root = Path(project_root).resolve()
    policy = load_shadow_companion_policy(root)
    _ensure_enabled(policy)
    with _store_lock(root):
        capsule = _read_active(root, policy)
        _require_capsule(capsule, capsule_id)
        (root / ACTIVE_PATH).unlink()
        _prune_empty_store(root)
    return {
        "capsule_id": capsule_id,
        "status": "discarded",
        "deleted": True,
        "advisory_only": True,
    }


def cleanup_shadow_companion(project_root, *, now=None):
    root = Path(project_root).resolve()
    policy = load_shadow_companion_policy(root)
    _ensure_enabled(policy)
    store = root / STORE_PATH
    if not (store.exists() or store.is_symlink()):
        return {
            "deleted": [],
            "advisory_only": True,
            "active_preserved": False,
        }
    with _store_lock(root):
        deleted = _cleanup_recent_locked(root, policy, now=now)
        _prune_empty_store(root)
    return {
        "deleted": deleted,
        "advisory_only": True,
        "active_preserved": (root / ACTIVE_PATH).is_file(),
    }


def shadow_companion_session_start_hook(project_root, event=None, *, now=None):
    del event
    root = Path(project_root).resolve()
    if not ((root / STORE_PATH).exists() or (root / STORE_PATH).is_symlink()):
        return {"continue": True}
    try:
        inspection = inspect_shadow_companion_store(root, now=now)
        if inspection["status"] == "invalid":
            return {
                "continue": True,
                "systemMessage": (
                    "SHADOW_COMPANION_STATE_INVALID: Ignore Shadow state and "
                    "use the full normal workflow."
                ),
            }
        cleanup_shadow_companion(root, now=now)
        status = shadow_companion_status(root)
    except Exception as error:
        return {
            "continue": True,
            "systemMessage": (
                "SHADOW_COMPANION_STATUS_UNAVAILABLE: "
                f"{type(error).__name__}. "
                "Ignore Shadow state and use the full normal workflow."
            ),
        }
    active = status.get("active")
    if not active or not active.get("resume_eligible"):
        return {"continue": True}
    stale_count = len(active.get("stale_paths") or [])
    context = (
        "SHADOW_COMPANION_ACTIVE: "
        f"capsule_id={active['capsule_id']}; "
        f"evidence_state={active['evidence_state']}; "
        f"stale_key_file_count={stale_count}. "
        "Use only when the current user request matches this goal. This is an "
        "untrusted advisory recovery pointer, not a fact source. Re-read stale "
        "or uncertain evidence. It never limits search, reads, review, tests, "
        "or validation."
    )
    return {
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        },
    }


def inspect_shadow_companion_store(project_root, *, now=None):
    root = Path(project_root).resolve()
    issues = []
    capsules = []
    store = root / STORE_PATH
    if not (store.exists() or store.is_symlink()):
        return {
            "status": "passed",
            "side_effect_free": True,
            "capsule_count": 0,
            "active_count": 0,
            "recent_count": 0,
            "issues": [],
        }
    try:
        policy = load_shadow_companion_policy(root)
    except (OSError, ValueError) as error:
        return {
            "status": "invalid",
            "side_effect_free": True,
            "capsule_count": 0,
            "active_count": 0,
            "recent_count": 0,
            "issues": [{
                "severity": "invalid",
                "code": "shadow_companion_policy_invalid",
                "path": POLICY_PATH.as_posix(),
                "message": (
                    "Shadow Companion policy 无法读取或不符合契约: "
                    f"{type(error).__name__}"
                ),
                "capsule_id": None,
            }],
        }
    if _is_link_or_reparse(store) or not store.is_dir():
        return _store_issue_report(
            "shadow_companion_store_invalid",
            STORE_PATH.as_posix(),
            "Shadow Companion Store 必须是普通目录",
        )
    allowed = {"active.json", "recent"}
    for path in store.iterdir():
        if path.name not in allowed:
            issues.append(_store_issue(
                "invalid",
                "shadow_companion_unknown_entry",
                path.relative_to(root).as_posix(),
                "Shadow Companion Store 包含未知文件或目录",
            ))
    active_path = root / ACTIVE_PATH
    if active_path.exists() or active_path.is_symlink():
        capsule = _inspect_capsule_file(
            root,
            active_path,
            policy,
            expected_status="active",
            issues=issues,
        )
        if capsule is not None:
            capsules.append(("active", capsule))
            issues.append(_store_issue(
                "warning",
                "shadow_companion_active",
                ACTIVE_PATH.as_posix(),
                "活动 Capsule 是本地恢复状态，不进入普通项目迁移",
                capsule["capsule_id"],
            ))
            freshness = _freshness(root, capsule)
            if not freshness["resume_eligible"] or not freshness["evidence_current"]:
                issues.append(_store_issue(
                    "warning",
                    "shadow_companion_stale",
                    ACTIVE_PATH.as_posix(),
                    "活动 Capsule 与当前项目、分支、环境或关键文件不完全一致",
                    capsule["capsule_id"],
                ))
    recent = root / RECENT_PATH
    if recent.exists() or recent.is_symlink():
        if _is_link_or_reparse(recent) or not recent.is_dir():
            issues.append(_store_issue(
                "invalid",
                "shadow_companion_recent_invalid",
                RECENT_PATH.as_posix(),
                "Shadow Companion recent 必须是普通目录",
            ))
        else:
            known = []
            for path in recent.iterdir():
                if not path.is_file() or not path.name.endswith(".json"):
                    issues.append(_store_issue(
                        "invalid",
                        "shadow_companion_recent_unknown_entry",
                        path.relative_to(root).as_posix(),
                        "Shadow Companion recent 包含未知文件或目录",
                    ))
                    continue
                capsule = _inspect_capsule_file(
                    root,
                    path,
                    policy,
                    expected_status="dormant",
                    issues=issues,
                )
                if capsule is not None:
                    known.append(capsule)
                    capsules.append(("dormant", capsule))
                    if _parse_utc(capsule["updated_at"]) < (
                        _as_utc(now) - timedelta(days=policy["retention_days"])
                    ):
                        issues.append(_store_issue(
                            "warning",
                            "shadow_companion_recent_expired",
                            path.relative_to(root).as_posix(),
                            "Dormant Capsule 已超过保留期，等待有界清理",
                            capsule["capsule_id"],
                        ))
            if len(known) > policy["max_recent"]:
                issues.append(_store_issue(
                    "invalid",
                    "shadow_companion_recent_limit_exceeded",
                    RECENT_PATH.as_posix(),
                    "Dormant Capsule 数量超过 policy 上限",
                ))
    ids = [capsule["capsule_id"] for _state, capsule in capsules]
    if len(ids) != len(set(ids)):
        issues.append(_store_issue(
            "invalid",
            "shadow_companion_capsule_duplicate",
            STORE_PATH.as_posix(),
            "Shadow Companion Store 包含重复 capsule_id",
        ))
    invalid = any(item["severity"] == "invalid" for item in issues)
    warning = any(item["severity"] == "warning" for item in issues)
    return {
        "status": "invalid" if invalid else "warning" if warning else "passed",
        "side_effect_free": True,
        "capsule_count": len(capsules),
        "active_count": sum(state == "active" for state, _item in capsules),
        "recent_count": sum(state == "dormant" for state, _item in capsules),
        "issues": issues,
    }


def load_shadow_companion_policy(project_root):
    path = Path(project_root).resolve() / POLICY_PATH
    value = _read_json(path, maximum=64 * 1024)
    fields = {
        "policy_version",
        "enabled",
        "mode",
        "scope",
        "task_confirmation_required",
        "write_mode",
        "session_start_detection",
        "default_context_injection",
        "local_only",
        "advisory_only",
        "record_source_content",
        "record_terminal_output",
        "mutate_source",
        "mutate_instructions",
        "automatic_git_operations",
        "restrict_search",
        "restrict_file_reads",
        "skip_tests",
        "select_final_validation",
        "full_fallback",
        "max_key_files",
        "max_facts",
        "max_invariants",
        "max_completed",
        "max_recent",
        "max_bytes",
        "retention_days",
    }
    if set(value) != fields:
        raise ValueError("Shadow Companion policy 字段无效")
    expected = {
        "policy_version": "1.0",
        "mode": "shadow_companion",
        "scope": "project_engineering_work",
        "task_confirmation_required": False,
        "write_mode": "semantic_milestone_only",
        "session_start_detection": True,
        "default_context_injection": "validated_pointer_only",
        "local_only": True,
        "advisory_only": True,
        "record_source_content": False,
        "record_terminal_output": False,
        "mutate_source": False,
        "mutate_instructions": False,
        "automatic_git_operations": False,
        "restrict_search": False,
        "restrict_file_reads": False,
        "skip_tests": False,
        "select_final_validation": False,
        "full_fallback": True,
    }
    for key, expected_value in expected.items():
        if value.get(key) != expected_value:
            raise ValueError(f"Shadow Companion policy.{key} 无效")
    if not isinstance(value.get("enabled"), bool):
        raise ValueError("Shadow Companion policy.enabled 必须是 boolean")
    for key in (
        "max_key_files",
        "max_facts",
        "max_invariants",
        "max_completed",
        "max_recent",
        "max_bytes",
        "retention_days",
    ):
        item = value.get(key)
        if not isinstance(item, int) or isinstance(item, bool) or item < 1:
            raise ValueError(f"Shadow Companion policy.{key} 必须是正整数")
    return value


def validate_shadow_capsule(value, policy):
    errors = []
    fields = {
        "shadow_companion_version",
        "capsule_id",
        "git",
        "environment",
        "created_at",
        "updated_at",
        "materialization_reason",
        "goal",
        "working_hypothesis",
        "invariants",
        "completed",
        "evidence_facts",
        "key_files",
        "last_validation",
        "next_action",
        "capability_guards",
        "revision",
    }
    if not isinstance(value, dict):
        return ["capsule_not_object"]
    if set(value) != fields:
        errors.append("capsule_fields_invalid")
    if value.get("shadow_companion_version") != SHADOW_COMPANION_VERSION:
        errors.append("capsule_version_invalid")
    if CAPSULE_ID.fullmatch(str(value.get("capsule_id") or "")) is None:
        errors.append("capsule_id_invalid")
    if value.get("materialization_reason") not in MATERIALIZATION_REASONS:
        errors.append("capsule_materialization_reason_invalid")
    if not isinstance(value.get("revision"), int) or value.get("revision", 0) < 1:
        errors.append("capsule_revision_invalid")
    for key in ("created_at", "updated_at"):
        try:
            _parse_utc(value.get(key))
        except ValueError:
            errors.append(f"capsule_{key}_invalid")
    for key in ("goal", "next_action"):
        if not _valid_text(value.get(key)):
            errors.append(f"capsule_{key}_invalid")
    hypothesis = value.get("working_hypothesis")
    if not isinstance(hypothesis, dict) or set(hypothesis) != {
        "statement", "disprove_by",
    }:
        errors.append("capsule_hypothesis_invalid")
    elif not all(_valid_text(hypothesis.get(key)) for key in hypothesis):
        errors.append("capsule_hypothesis_text_invalid")
    limits = {
        "invariants": policy["max_invariants"],
        "completed": policy["max_completed"],
    }
    for key, maximum in limits.items():
        items = value.get(key)
        if (
            not isinstance(items, list)
            or not items
            or len(items) > maximum
            or len(items) != len(set(items))
            or not all(_valid_text(item) for item in items)
        ):
            errors.append(f"capsule_{key}_invalid")
    facts = value.get("evidence_facts")
    if (
        not isinstance(facts, list)
        or len(facts) > policy["max_facts"]
        or len(facts) != len(set(facts))
        or not all(_valid_text(item) for item in facts)
    ):
        errors.append("capsule_evidence_facts_invalid")
    key_files = value.get("key_files")
    if (
        not isinstance(key_files, list)
        or not key_files
        or len(key_files) > policy["max_key_files"]
    ):
        errors.append("capsule_key_files_invalid")
    else:
        paths = []
        for item in key_files:
            if not isinstance(item, dict) or set(item) != {
                "path", "exists", "sha256",
            }:
                errors.append("capsule_key_file_invalid")
                continue
            path = item.get("path")
            paths.append(path)
            if not _safe_relative(path):
                errors.append("capsule_key_file_path_invalid")
            digest = item.get("sha256")
            if digest is not None and re.fullmatch(r"[0-9a-f]{64}", str(digest)) is None:
                errors.append("capsule_key_file_hash_invalid")
            if not isinstance(item.get("exists"), bool):
                errors.append("capsule_key_file_exists_invalid")
        if len(paths) != len(set(paths)):
            errors.append("capsule_key_file_duplicate")
    validation = value.get("last_validation")
    if validation is not None:
        if not isinstance(validation, dict) or set(validation) != {
            "summary", "result", "scope", "recorded_at",
        }:
            errors.append("capsule_validation_invalid")
        else:
            if not _valid_text(validation.get("summary")):
                errors.append("capsule_validation_summary_invalid")
            if validation.get("result") not in RESULTS:
                errors.append("capsule_validation_result_invalid")
            if validation.get("scope") not in VALIDATION_SCOPES:
                errors.append("capsule_validation_scope_invalid")
            try:
                _parse_utc(validation.get("recorded_at"))
            except ValueError:
                errors.append("capsule_validation_time_invalid")
    git = value.get("git")
    if not isinstance(git, dict) or set(git) != {"branch", "head"}:
        errors.append("capsule_git_invalid")
    elif not all(_valid_text(git.get(key)) for key in git):
        errors.append("capsule_git_text_invalid")
    environment = value.get("environment")
    if not isinstance(environment, dict) or set(environment) != {
        "python", "platform",
    }:
        errors.append("capsule_environment_invalid")
    elif not all(_valid_text(environment.get(key)) for key in environment):
        errors.append("capsule_environment_text_invalid")
    guards = value.get("capability_guards")
    expected_guards = _capability_guards()
    if guards != expected_guards:
        errors.append("capsule_capability_guards_invalid")
    if _privacy_errors(value):
        errors.append("capsule_privacy_invalid")
    return list(dict.fromkeys(errors))


def _build_capsule(
        root,
        policy,
        *,
        capsule_id,
        created_at,
        revision,
        goal,
        working_hypothesis,
        disprove_by,
        invariants,
        completed,
        next_action,
        key_files,
        evidence_facts,
        validation_summary,
        validation_result,
        validation_scope,
        materialization_reason,
        now,
):
    if materialization_reason not in MATERIALIZATION_REASONS:
        raise ValueError("materialization_reason 无效")
    current = _as_utc(now)
    git = _git_context(root)
    captured = _capture_files(root, key_files, policy["max_key_files"])
    facts = _bounded_texts(
        evidence_facts,
        "evidence_facts",
        policy["max_facts"],
        allow_empty=True,
    )
    validation = None
    if any(item is not None for item in (
        validation_summary,
        validation_result,
        validation_scope,
    )):
        if validation_result not in RESULTS:
            raise ValueError("validation_result 无效")
        if validation_scope not in VALIDATION_SCOPES:
            raise ValueError("validation_scope 无效")
        validation = {
            "summary": _text(validation_summary, "validation_summary"),
            "result": validation_result,
            "scope": validation_scope,
            "recorded_at": _utc_text(current),
        }
    if materialization_reason == "validated_milestone" and (
        validation is None or validation["result"] != "passed"
    ):
        raise ValueError("validated_milestone 必须绑定 passed validation")
    capsule = {
        "shadow_companion_version": SHADOW_COMPANION_VERSION,
        "capsule_id": _capsule_id(capsule_id),
        "git": git,
        "environment": _environment(),
        "created_at": _utc_text(created_at),
        "updated_at": _utc_text(current),
        "materialization_reason": materialization_reason,
        "goal": _text(goal, "goal"),
        "working_hypothesis": {
            "statement": _text(working_hypothesis, "working_hypothesis"),
            "disprove_by": _text(disprove_by, "disprove_by"),
        },
        "invariants": _bounded_texts(
            invariants,
            "invariants",
            policy["max_invariants"],
        ),
        "completed": _bounded_texts(
            completed,
            "completed",
            policy["max_completed"],
        ),
        "evidence_facts": facts,
        "key_files": captured,
        "last_validation": validation,
        "next_action": _text(next_action, "next_action"),
        "capability_guards": _capability_guards(),
        "revision": revision,
    }
    errors = validate_shadow_capsule(capsule, policy)
    if errors:
        raise ValueError(f"Shadow Capsule 无效: {errors}")
    return capsule


def _public_capsule(root, capsule, *, state="active"):
    freshness = _freshness(root, capsule)
    return {
        "capsule_id": capsule["capsule_id"],
        "status": state,
        "revision": capsule["revision"],
        "goal": capsule["goal"],
        "completed": capsule["completed"],
        "next_action": capsule["next_action"],
        "evidence_state": (
            "current" if freshness["evidence_current"] else "stale"
        ),
        "resume_eligible": freshness["resume_eligible"],
        "stale_paths": freshness["stale_paths"],
        "validation_current": freshness["validation_current"],
        "advisory_only": True,
        "capability_impact": "none",
    }


def _freshness(root, capsule):
    try:
        git = _git_context(root)
        branch_match = git["branch"] == capsule["git"]["branch"]
    except ValueError:
        git = {"head": None}
        branch_match = False
    environment_match = _environment() == capsule["environment"]
    stale_paths = []
    for item in capsule["key_files"]:
        path = root / item["path"]
        current_exists = path.is_file() and not _is_link_or_reparse(path)
        current_hash = _file_hash(path) if current_exists else None
        if (
            current_exists != item["exists"]
            or current_hash != item["sha256"]
        ):
            stale_paths.append(item["path"])
    resume_eligible = branch_match
    head_changed = git.get("head") != capsule["git"]["head"]
    evidence_current = (
        resume_eligible
        and not head_changed
        and environment_match
        and not stale_paths
    )
    return {
        "branch_match": branch_match,
        "head_changed": head_changed,
        "environment_match": environment_match,
        "stale_paths": stale_paths,
        "resume_eligible": resume_eligible,
        "evidence_current": evidence_current,
        "validation_current": (
            capsule.get("last_validation") is not None and evidence_current
        ),
    }


def _cleanup_recent_locked(root, policy, *, now=None):
    recent = root / RECENT_PATH
    if not recent.is_dir():
        return []
    current = _as_utc(now)
    valid = []
    for path in sorted(recent.glob("shadow-*.json")):
        if _is_link_or_reparse(path):
            continue
        try:
            capsule = _read_capsule(path, policy)
            updated = _parse_utc(capsule["updated_at"])
        except ValueError:
            continue
        valid.append((updated, path))
    cutoff = current - timedelta(days=policy["retention_days"])
    keep = [item for item in valid if item[0] >= cutoff]
    delete = [item for item in valid if item[0] < cutoff]
    keep.sort(key=lambda item: item[0], reverse=True)
    delete.extend(keep[policy["max_recent"]:])
    deleted = []
    for _updated, path in delete:
        if path.is_file() and not _is_link_or_reparse(path):
            path.unlink()
            deleted.append(path.name)
    return deleted


def _recent_summaries(root, policy):
    recent = root / RECENT_PATH
    result = []
    if not recent.is_dir():
        return result
    for path in sorted(recent.glob("shadow-*.json")):
        try:
            capsule = _read_capsule(path, policy)
        except ValueError:
            continue
        result.append({
            "capsule_id": capsule["capsule_id"],
            "goal": capsule["goal"],
            "updated_at": capsule["updated_at"],
        })
    return result


def _read_active(root, policy):
    path = root / ACTIVE_PATH
    if not path.is_file():
        raise FileNotFoundError("没有 active Shadow Capsule")
    return _read_capsule(path, policy)


def _inspect_capsule_file(
        root,
        path,
        policy,
        *,
        expected_status,
        issues,
):
    relative = path.relative_to(root).as_posix()
    try:
        capsule = _read_capsule(path, policy)
    except ValueError as error:
        issues.append(_store_issue(
            "invalid",
            "shadow_companion_capsule_invalid",
            relative,
            (
                "Shadow Capsule 无法读取或不符合契约: "
                f"{type(error).__name__}"
            ),
        ))
        return None
    if path.name != (
        "active.json"
        if expected_status == "active"
        else f"{capsule['capsule_id']}.json"
    ):
        issues.append(_store_issue(
            "invalid",
            "shadow_companion_capsule_name_invalid",
            relative,
            "Capsule 文件名与身份不一致",
            capsule["capsule_id"],
        ))
    return capsule


def _store_issue(severity, code, path, message, capsule_id=None):
    return {
        "severity": severity,
        "code": code,
        "path": path,
        "message": message,
        "capsule_id": capsule_id,
    }


def _store_issue_report(code, path, message):
    return {
        "status": "invalid",
        "side_effect_free": True,
        "capsule_count": 0,
        "active_count": 0,
        "recent_count": 0,
        "issues": [_store_issue("invalid", code, path, message)],
    }


def _read_capsule(path, policy):
    path = Path(path)
    if _is_link_or_reparse(path):
        raise ValueError("Shadow Capsule 不能是链接或 reparse point")
    value = _read_json(path, maximum=policy["max_bytes"])
    if isinstance(value, dict):
        value.pop("project_id", None)
    errors = validate_shadow_capsule(value, policy)
    if errors:
        raise ValueError(f"Shadow Capsule 无效: {errors}")
    return value


def _write_capsule(path, capsule, policy):
    errors = validate_shadow_capsule(capsule, policy)
    if errors:
        raise ValueError(f"Shadow Capsule 无效: {errors}")
    path = Path(path)
    _safe_store_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _safe_store_path(path.parent)
    if (path.exists() or path.is_symlink()) and _is_link_or_reparse(path):
        raise ValueError("Shadow Capsule 目标不能是链接或 reparse point")
    raw = (json.dumps(
        capsule,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n").encode("utf-8")
    if len(raw) > policy["max_bytes"]:
        raise ValueError("Shadow Capsule 超过大小限制")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(raw)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _ensure_store(root):
    store = root / STORE_PATH
    _safe_store_path(store)
    store.mkdir(parents=True, exist_ok=True)
    (root / RECENT_PATH).mkdir(parents=True, exist_ok=True)
    _safe_store_path(store)
    _safe_store_path(root / RECENT_PATH)


def _prune_empty_store(root):
    recent = root / RECENT_PATH
    store = root / STORE_PATH
    if recent.is_dir() and not any(recent.iterdir()):
        recent.rmdir()
    if store.is_dir() and not any(store.iterdir()):
        store.rmdir()


def _safe_store_path(path):
    path = Path(path)
    current = path
    while True:
        if (current.exists() or current.is_symlink()) and _is_link_or_reparse(current):
            raise ValueError("Shadow Companion Store 不能包含链接或 reparse point")
        if current.parent == current:
            break
        current = current.parent


def _git_context(root):
    available, git_root = _git_value(root, "rev-parse", "--show-toplevel")
    if not available or Path(git_root).resolve() != root:
        raise ValueError("Git root 不可用或与 project root 不一致")
    head_ok, head = _git_value(root, "rev-parse", "HEAD")
    if not head_ok or re.fullmatch(r"[0-9a-f]{40}", head) is None:
        raise ValueError("Git HEAD 不可用")
    branch_ok, branch = _git_value(root, "symbolic-ref", "--short", "-q", "HEAD")
    return {
        "branch": branch if branch_ok and branch else f"detached:{head}",
        "head": head,
    }


def _git_value(root, *args):
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False, ""
    return result.returncode == 0, result.stdout.strip()


def _capture_files(root, paths, maximum):
    values = list(dict.fromkeys(str(path) for path in paths or []))
    if not values or len(values) > maximum:
        raise ValueError("key_files 数量无效")
    result = []
    for value in values:
        relative, absolute = _workspace_path(root, value)
        exists = absolute.is_file() and not _is_link_or_reparse(absolute)
        if absolute.exists() and not exists:
            raise ValueError(f"key_file 必须是普通文件: {relative}")
        result.append({
            "path": relative,
            "exists": exists,
            "sha256": _file_hash(absolute) if exists else None,
        })
    return result


def _workspace_path(root, value):
    text = str(value or "").replace("\\", "/")
    posix = PurePosixPath(text)
    if (
        not text
        or posix.is_absolute()
        or PureWindowsPath(text).is_absolute()
        or ".." in posix.parts
    ):
        raise ValueError(f"文件路径必须是安全相对路径: {value}")
    relative = posix.as_posix()
    if relative == STORE_PATH.as_posix() or relative.startswith(
        STORE_PATH.as_posix() + "/"
    ):
        raise ValueError("Shadow Companion 不能引用自己的 Store")
    absolute = (root / Path(*posix.parts)).resolve()
    try:
        absolute.relative_to(root)
    except ValueError as error:
        raise ValueError(f"文件路径越界: {value}") from error
    current = absolute
    while current != root:
        if (current.exists() or current.is_symlink()) and _is_link_or_reparse(current):
            raise ValueError(f"key_file 路径包含链接或 reparse point: {relative}")
        current = current.parent
    return relative, absolute


def _require_capsule(capsule, capsule_id):
    capsule_id = _capsule_id(capsule_id)
    if capsule.get("capsule_id") != capsule_id:
        raise ValueError("Shadow Capsule ID 不匹配")


def _capsule_id(value):
    text = str(value or "").strip()
    if CAPSULE_ID.fullmatch(text) is None:
        raise ValueError(f"无效 capsule_id: {text}")
    return text


def _environment():
    return {
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "platform": sys.platform,
    }


def _capability_guards():
    return {
        "advisory_only": True,
        "task_confirmation_required": False,
        "restrict_search": False,
        "restrict_file_reads": False,
        "skip_tests": False,
        "select_final_validation": False,
        "mutate_source": False,
        "mutate_instructions": False,
        "automatic_git_operations": False,
        "record_source_content": False,
        "record_terminal_output": False,
        "full_fallback": True,
    }


def _bounded_texts(values, label, maximum, *, allow_empty=False):
    result = list(dict.fromkeys(
        _text(value, label) for value in values or []
    ))
    if len(result) > maximum:
        raise ValueError(f"{label} 超过数量限制")
    if not result and not allow_empty:
        raise ValueError(f"{label} 不能为空")
    return result


def _text(value, label):
    text = str(value or "").strip()
    if not _valid_text(text):
        raise ValueError(f"{label} 无效或包含不允许的内容")
    return text


def _valid_text(value):
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= MAX_TEXT
        and "\n" not in value
        and "\r" not in value
        and PRIVATE_PATH.search(value) is None
        and CREDENTIAL.search(value) is None
        and SECRET_TOKEN.search(value) is None
        and SOURCE_TEXT.search(value) is None
    )


def _privacy_errors(value):
    text = "\n".join(_string_values(value))
    return any((
        PRIVATE_PATH.search(text),
        CREDENTIAL.search(text),
        SECRET_TOKEN.search(text),
        any(SOURCE_TEXT.search(item) for item in _string_values(value)),
    ))


def _string_values(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _string_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _string_values(item)
    elif isinstance(value, str):
        yield value


def _safe_relative(value):
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts


def _read_json(path, *, maximum):
    path = Path(path)
    if _is_link_or_reparse(path):
        raise ValueError(f"JSON 不能是链接或 reparse point: {path}")
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ValueError(f"JSON 无法读取: {path}: {error}") from error
    if len(raw) > maximum:
        raise ValueError(f"JSON 超过大小限制: {path}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"JSON 不是 UTF-8: {path}") from error

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"JSON 包含重复键: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(text, object_pairs_hook=pairs)
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"JSON 无效: {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"JSON 顶层必须是 object: {path}")
    return value


def _file_hash(path):
    path = Path(path)
    if not path.is_file() or _is_link_or_reparse(path):
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_link_or_reparse(path):
    path = Path(path)
    if path.is_symlink():
        return True
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _as_utc(value):
    if value is None:
        return datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("now 必须是带 timezone 的 datetime")
    return value.astimezone(timezone.utc)


def _parse_utc(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("UTC timestamp 无效")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("UTC timestamp 无效") from error
    if parsed.tzinfo is None:
        raise ValueError("UTC timestamp 无效")
    return parsed.astimezone(timezone.utc)


def _utc_text(value):
    return value.astimezone(timezone.utc).isoformat(
        timespec="seconds",
    ).replace("+00:00", "Z")


def _ensure_enabled(policy):
    if policy.get("enabled") is not True:
        raise RuntimeError("Shadow Companion 已禁用")


@contextmanager
def _store_lock(project_root):
    root = Path(project_root).resolve()
    key = str(root).casefold()
    with _LOCKS_GUARD:
        thread_lock = _LOCKS.setdefault(key, threading.RLock())
    with thread_lock:
        lock_path = root / PROJECT_KNOWLEDGE_ROOT / ".shadow-companion.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        stream = lock_path.open("a+b")
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            stream.close()


def _add_snapshot_arguments(parser):
    parser.add_argument("--goal", required=True)
    parser.add_argument("--working-hypothesis", required=True)
    parser.add_argument("--disprove-by", required=True)
    parser.add_argument("--invariant", action="append", required=True)
    parser.add_argument("--completed", action="append", required=True)
    parser.add_argument("--next-action", required=True)
    parser.add_argument("--key-file", action="append", required=True)
    parser.add_argument("--fact", action="append", default=[])
    parser.add_argument("--validation-summary")
    parser.add_argument("--validation-result", choices=sorted(RESULTS))
    parser.add_argument(
        "--validation-scope",
        choices=sorted(VALIDATION_SCOPES),
    )
    parser.add_argument(
        "--materialization-reason",
        choices=sorted(MATERIALIZATION_REASONS),
        default="validated_milestone",
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Manage the local advisory Shadow Companion Capsule",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    _add_snapshot_arguments(start)
    milestone = commands.add_parser("milestone")
    milestone.add_argument("capsule_id")
    _add_snapshot_arguments(milestone)
    for name in ("status", "cleanup", "session-start"):
        commands.add_parser(name)
    for name in ("archive", "resume", "complete", "discard"):
        command = commands.add_parser(name)
        command.add_argument("capsule_id")
    for command in commands.choices.values():
        command.add_argument("--project-root", default=".")
    args = parser.parse_args(argv)
    snapshot = {
        "goal": getattr(args, "goal", None),
        "working_hypothesis": getattr(args, "working_hypothesis", None),
        "disprove_by": getattr(args, "disprove_by", None),
        "invariants": getattr(args, "invariant", None),
        "completed": getattr(args, "completed", None),
        "next_action": getattr(args, "next_action", None),
        "key_files": getattr(args, "key_file", None),
        "evidence_facts": getattr(args, "fact", None),
        "validation_summary": getattr(args, "validation_summary", None),
        "validation_result": getattr(args, "validation_result", None),
        "validation_scope": getattr(args, "validation_scope", None),
        "materialization_reason": getattr(
            args,
            "materialization_reason",
            None,
        ),
    }
    if args.command == "start":
        result = start_shadow_companion(args.project_root, **snapshot)
    elif args.command == "milestone":
        result = update_shadow_companion(
            args.project_root,
            capsule_id=args.capsule_id,
            **snapshot,
        )
    elif args.command == "status":
        result = shadow_companion_status(args.project_root)
    elif args.command == "archive":
        result = archive_shadow_companion(
            args.project_root,
            capsule_id=args.capsule_id,
        )
    elif args.command == "resume":
        result = resume_shadow_companion(
            args.project_root,
            capsule_id=args.capsule_id,
        )
    elif args.command == "complete":
        result = complete_shadow_companion(
            args.project_root,
            capsule_id=args.capsule_id,
        )
    elif args.command == "discard":
        result = discard_shadow_companion(
            args.project_root,
            capsule_id=args.capsule_id,
        )
    elif args.command == "cleanup":
        result = cleanup_shadow_companion(args.project_root)
    else:
        result = shadow_companion_session_start_hook(
            args.project_root,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())