from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath

from autowork_core.utils.debug_tools.ai_paths import PROJECT_KNOWLEDGE_ROOT
from autowork_core.utils.debug_tools.collaboration_review import (
    validate_promotion,
    validate_review,
)
from autowork_core.utils.debug_tools.recorder.knowledge_store import (
    KNOWLEDGE_STORE_VERSION,
    knowledge_root_for_recording_root,
)
from autowork_core.utils.debug_tools.recorder.models import (
    SUPPORTED_SCHEMA_VERSIONS,
)
from autowork_core.utils.debug_tools.recorder.project_memory import (
    AUTHORITIES,
)
from autowork_core.utils.debug_tools.shadow_companion import (
    inspect_shadow_companion_store,
)


KNOWLEDGE_AUDIT_VERSION = "1.2"
CAPABILITY_STATUSES = {"confirmed", "stale", "drifted"}
CAPABILITY_VERSION = "2.0"
CAPABILITY_REQUIRED = {
    "schema_version",
    "capability_version",
    "capability_id",
    "published_at",
    "status",
    "feature",
    "scenario",
    "step",
    "plan",
    "source",
    "reuse_policy",
}
CAPABILITY_OBJECT_FIELDS = {
    "feature",
    "scenario",
    "step",
    "plan",
    "source",
    "reuse_policy",
}
RETIREMENT_STATUSES = {"prepared", "completed", "cleanup_pending", "failed"}
PRIVATE_PATH = re.compile(
    r"(?:[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/]|"
    r"\\\\[^\\\s]+\\[^\\\s]+|"
    r"/(?:home|Users)/[^/\s]+(?:/|\b))",
    re.IGNORECASE,
)
ABSOLUTE_PATH = re.compile(
    r"(?:[A-Za-z]:[\\/][^\s,;]+|\\\\[^\\\s]+\\[^\s,;]+|"
    r"/(?:etc|home|Users|var|tmp|opt|usr|private|mnt|Volumes)/[^\s,;]+)",
    re.IGNORECASE,
)
CREDENTIAL = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|"
    r"password|passwd|secret|"
    r"authorization)\s*[:=]\s*[^\s,;]+"
)
SECRET_TOKEN = re.compile(
    r"(?i)(?:bearer\s+[A-Za-z0-9._~-]{16,}|"
    r"(?:^|[\s=:])(?:gh[pousr]_[A-Za-z0-9]{12,}|"
    r"sk-(?:proj-)?[A-Za-z0-9_-]{12,}))"
)
SENSITIVE_KEY = re.compile(
    r"(?i)^(?:api[_-]?key|access[_-]?token|auth(?:orization)?|password|"
    r"passwd|secret|client[_-]?secret|refresh[_-]?token|bearer[_-]?token)"
    r"(?:[_-]?(?:value|text|credential))?$"
)
MAX_KNOWLEDGE_JSON_BYTES = 1024 * 1024


def inspect_knowledge_store(recording_root):
    started = time.perf_counter()
    root = knowledge_root_for_recording_root(recording_root).resolve()
    findings = []
    stats = {
        "files_scanned": 0,
        "capability_files": 0,
        "catalog_entries": 0,
        "memory_events": 0,
        "retirement_receipts": 0,
        "collaboration_reviews": 0,
        "collaboration_promotions": 0,
        "shadow_companion_capsules": 0,
        "work_packages": 0,
        "knowledge_quarantines": 0,
    }

    manifest = _read_object(
        root / "manifest.json",
        findings,
        stats,
        missing_code="knowledge_manifest_missing",
        corrupt_code="knowledge_manifest_invalid",
    )
    if manifest is not None:
        if manifest.get("knowledge_store_version") != KNOWLEDGE_STORE_VERSION:
            _finding(
                findings,
                "invalid",
                "knowledge_version_unsupported",
                root / "manifest.json",
                (
                    "knowledge_store_version 必须为 "
                    f"{KNOWLEDGE_STORE_VERSION}"
                ),
            )
        policy = manifest.get("policy") or {}
        if policy.get("runtime_evidence") is not False:
            _finding(
                findings,
                "invalid",
                "knowledge_runtime_evidence_policy_invalid",
                root / "manifest.json",
                "knowledge store 不能声明为 runtime evidence",
            )
        if policy.get("raw_media_allowed") is not False:
            _finding(
                findings,
                "invalid",
                "knowledge_raw_media_policy_invalid",
                root / "manifest.json",
                "knowledge store 不能允许 raw media",
            )

    _inspect_capabilities(root, findings, stats)
    _inspect_memory(root, findings, stats)
    _inspect_retirements(root, findings, stats)
    _inspect_collaboration(root, findings, stats)
    _inspect_shadow_companion(root, findings, stats)
    _inspect_work_packages(root, findings, stats)
    _inspect_quarantines(root, findings, stats)
    _inspect_temporary_files(root, findings, stats)

    invalid_count = sum(
        finding["severity"] == "invalid"
        for finding in findings
    )
    warning_count = sum(
        finding["severity"] == "warning"
        for finding in findings
    )
    public_findings = [
        _public_finding(item, root)
        for item in findings
    ]
    return {
        "knowledge_audit_version": KNOWLEDGE_AUDIT_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "knowledge_root": PROJECT_KNOWLEDGE_ROOT.as_posix(),
        "side_effect_free": True,
        "status": (
            "invalid"
            if invalid_count
            else "warning"
            if warning_count
            else "passed"
        ),
        "summary": {
            "invalid_count": invalid_count,
            "warning_count": warning_count,
        },
        "stats": {
            **stats,
            "scan_time_ms": round(
                (time.perf_counter() - started) * 1000,
                3,
            ),
        },
        "findings": public_findings,
    }


def _inspect_capabilities(root, findings, stats):
    directory = root / "capabilities"
    files = sorted(directory.glob("capability-*.json"))
    stats["capability_files"] = len(files)
    catalog_path = directory / "catalog.json"
    catalog = _read_object(
        catalog_path,
        findings,
        stats,
        required=False,
        corrupt_code="capability_catalog_invalid",
    )
    if catalog is None:
        if files and not catalog_path.exists():
            _finding(
                findings,
                "invalid",
                "capability_catalog_missing",
                catalog_path,
                "存在 Capability 文件但缺少 catalog.json",
            )
        for path in files:
            _inspect_capability_file(path, None, root, findings, stats)
        return

    if catalog.get("schema_version") not in SUPPORTED_SCHEMA_VERSIONS:
        _finding(
            findings,
            "invalid",
            "capability_catalog_version_invalid",
            catalog_path,
            "Capability catalog schema_version 不受支持",
        )

    entries = catalog.get("capabilities")
    if not isinstance(entries, list):
        _finding(
            findings,
            "invalid",
            "capability_catalog_entries_invalid",
            catalog_path,
            "capabilities 必须是 array",
        )
        entries = []
    stats["catalog_entries"] = len(entries)
    paths = {}
    ids = {}
    for entry in entries:
        if not isinstance(entry, dict):
            _finding(
                findings,
                "invalid",
                "capability_catalog_entry_invalid",
                catalog_path,
                "Capability catalog entry 必须是 object",
            )
            continue
        capability_id = str(entry.get("capability_id") or "")
        relative = str(entry.get("path") or "")
        try:
            path = _contained(root, root / relative)
        except ValueError:
            _finding(
                findings,
                "invalid",
                "capability_path_outside_store",
                catalog_path,
                f"Capability 路径越界: {relative}",
                capability_id,
            )
            continue
        if not capability_id or not relative:
            _finding(
                findings,
                "invalid",
                "capability_catalog_identity_missing",
                catalog_path,
                "Capability entry 缺少 id 或 path",
                capability_id or None,
            )
            continue
        if capability_id in ids and ids[capability_id] != path:
            _finding(
                findings,
                "invalid",
                "capability_id_conflict",
                catalog_path,
                f"Capability id 映射到多个文件: {capability_id}",
                capability_id,
            )
        if path in paths and paths[path] != capability_id:
            _finding(
                findings,
                "invalid",
                "capability_path_conflict",
                catalog_path,
                f"Capability 文件映射到多个 id: {relative}",
                capability_id,
            )
        ids[capability_id] = path
        paths[path] = capability_id
        if not path.is_file():
            _finding(
                findings,
                "invalid",
                "capability_file_missing",
                path,
                "Catalog 指向的 Capability 文件不存在",
                capability_id,
            )
            continue
        capability = _inspect_capability_file(
            path,
            capability_id,
            root,
            findings,
            stats,
        )
        if capability is not None and entry.get("status") != capability.get(
            "status"
        ):
            _finding(
                findings,
                "invalid",
                "capability_catalog_detail_status_mismatch",
                path,
                "Capability catalog 与 detail status 不一致",
                capability_id,
            )

    for path in files:
        if path.resolve() not in paths:
            _finding(
                findings,
                "warning",
                "capability_orphan_file",
                path,
                "Capability 文件未被 catalog 索引",
            )
            _inspect_capability_file(path, None, root, findings, stats)


def _inspect_capability_file(
        path,
        expected_id,
        root,
        findings,
        stats,
):
    finding_start = len(findings)
    capability = _read_object(
        path,
        findings,
        stats,
        corrupt_code="capability_file_invalid",
    )
    if capability is None:
        inferred_id = path.stem
        if re.fullmatch(
            r"capability-[a-z0-9][a-z0-9-]{2,79}",
            inferred_id,
        ):
            for finding in findings[finding_start:]:
                if Path(finding["path"]).resolve() == Path(path).resolve():
                    finding["record_id"] = inferred_id
        return None
    capability_id = str(capability.get("capability_id") or "")
    missing = sorted(CAPABILITY_REQUIRED - set(capability))
    if missing:
        _finding(
            findings,
            "invalid",
            "capability_required_fields_missing",
            path,
            f"Capability 缺少必需字段: {missing}",
            capability_id or None,
        )
    for field in sorted(CAPABILITY_OBJECT_FIELDS):
        if not isinstance(capability.get(field), dict):
            _finding(
                findings,
                "invalid",
                "capability_field_invalid",
                path,
                f"Capability {field} 必须是 object",
                capability_id or None,
            )
    if (
        capability.get("schema_version") not in SUPPORTED_SCHEMA_VERSIONS
        or capability.get("capability_version") != CAPABILITY_VERSION
    ):
        _finding(
            findings,
            "invalid",
            "capability_version_invalid",
            path,
            "Capability schema_version 或 capability_version 不受支持",
            capability_id or None,
        )
    expected_name = f"{capability_id}.json"
    if not capability_id or path.name != expected_name:
        _finding(
            findings,
            "invalid",
            "capability_file_id_mismatch",
            path,
            "Capability id 必须与文件名一致",
            capability_id or None,
        )
    if expected_id and capability_id != expected_id:
        _finding(
            findings,
            "invalid",
            "capability_catalog_id_mismatch",
            path,
            f"Catalog id 与文件内容不一致: {expected_id}",
            capability_id or expected_id,
        )
    status = capability.get("status")
    if not isinstance(status, str) or status not in CAPABILITY_STATUSES:
        _finding(
            findings,
            "invalid",
            "capability_status_unknown",
            path,
            f"未知 Capability status: {capability.get('status')}",
            capability_id or None,
        )
    _inspect_value_privacy(
        capability,
        path,
        findings,
        capability_id or None,
    )
    source_value = capability.get("source")
    source = source_value if isinstance(source_value, dict) else {}
    for key in ("session_path", "request_path"):
        value = source.get(key)
        if value and Path(str(value)).is_absolute():
            _finding(
                findings,
                "warning",
                "capability_source_not_portable",
                path,
                f"Capability source.{key} 应使用相对路径",
                capability_id or None,
            )
    provenance = source.get("provenance")
    if not isinstance(provenance, dict):
        _finding(
            findings,
            "warning",
            "capability_provenance_unknown",
            path,
            "Capability 缺少显式 production/test provenance",
            capability_id or None,
        )
    elif (
        provenance.get("context") not in {"production", "test"}
        or provenance.get("producer") not in {
            "recorder_generation",
            "recorder_test_fixture",
        }
        or provenance.get("confirmation_source") != "user_adjustment"
    ):
        _finding(
            findings,
            "invalid",
            "capability_provenance_invalid",
            path,
            "Capability provenance context 或 producer 无效",
            capability_id or None,
        )
    for field in ("plan_fingerprint", "revision_seal"):
        if re.fullmatch(r"[0-9a-f]{64}", str(source.get(field) or "")) is None:
            _finding(
                findings,
                "invalid",
                "capability_confirmation_trace_invalid",
                path,
                f"Capability source.{field} 缺失或无效",
                capability_id or None,
            )
    if _contains_media(path, root):
        _finding(
            findings,
            "invalid",
            "raw_media_in_knowledge_store",
            path,
            "knowledge store 不允许 raw media",
            capability_id or None,
        )
    return capability


def _inspect_memory(root, findings, stats):
    path = root / "project-memory" / "events.jsonl"
    if not path.is_file():
        return
    stats["files_scanned"] += 1
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        _finding(
            findings,
            "invalid",
            "memory_journal_unreadable",
            path,
            f"{type(error).__name__}: {error}",
        )
        return
    ids = set()
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            _finding(
                findings,
                "warning",
                "memory_journal_corrupt_line",
                path,
                f"第 {line_number} 行: {error}",
            )
            continue
        stats["memory_events"] += 1
        if not isinstance(event, dict) or not event.get("memory_id"):
            _finding(
                findings,
                "warning",
                "memory_event_invalid",
                path,
                f"第 {line_number} 行不是有效事件",
            )
            continue
        memory_id = str(event["memory_id"])
        if memory_id in ids:
            _finding(
                findings,
                "warning",
                "memory_event_duplicate_id",
                path,
                f"重复 memory_id: {memory_id}",
                memory_id,
            )
        ids.add(memory_id)
        if event.get("authority") not in AUTHORITIES:
            _finding(
                findings,
                "warning",
                "memory_authority_unknown",
                path,
                f"未知 authority: {event.get('authority')}",
                memory_id,
            )
        _inspect_value_privacy(event, path, findings, memory_id)


def _inspect_retirements(root, findings, stats):
    for path in sorted((root / "retirements").glob("*.json")):
        stats["retirement_receipts"] += 1
        receipt = _read_object(
            path,
            findings,
            stats,
            corrupt_code="retirement_receipt_invalid",
        )
        if receipt is None:
            continue
        status = receipt.get("status")
        if status not in RETIREMENT_STATUSES:
            _finding(
                findings,
                "warning",
                "retirement_status_unknown",
                path,
                f"未知 retirement status: {status}",
            )
        if status != "cleanup_pending":
            continue
        cleanup = Path(str(receipt.get("cleanup_path") or ""))
        if cleanup.is_dir():
            _finding(
                findings,
                "warning",
                "retirement_cleanup_pending",
                path,
                "退役暂存目录仍待清理",
            )
        else:
            _finding(
                findings,
                "invalid",
                "retirement_cleanup_state_inconsistent",
                path,
                "cleanup_pending 但 cleanup_path 不存在",
            )


def _inspect_collaboration(root, findings, stats):
    project_root = root.parents[2].resolve()
    git_available = _git_worktree_available(project_root)
    review_directory = root / "collaboration-reviews"
    for path in sorted(review_directory.glob("*.json")):
        stats["collaboration_reviews"] += 1
        result = validate_review(path, project_root=project_root)
        if result.get("status") != "passed":
            _report_collaboration_validation(
                findings,
                path,
                result,
                git_available=git_available,
                invalid_code="collaboration_review_invalid",
                ignored_error="review 文件必须被 Git ignore",
            )

    promotion_directory = root / "collaboration-promotions"
    for path in sorted(promotion_directory.glob("*.json")):
        stats["collaboration_promotions"] += 1
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            approved = (
                value.get("approved_candidate_ids")
                if isinstance(value, dict)
                else []
            )
        except (OSError, json.JSONDecodeError):
            approved = []
        result = validate_promotion(
            path,
            project_root=project_root,
            approved_candidate_ids=approved or [],
        )
        if result.get("status") != "passed":
            _report_collaboration_validation(
                findings,
                path,
                result,
                git_available=git_available,
                invalid_code="collaboration_promotion_invalid",
                ignored_error="promotion receipt 必须被 Git ignore",
            )

def _report_collaboration_validation(
        findings,
        path,
        result,
        *,
        git_available,
        invalid_code,
        ignored_error,
):
    errors = list(result.get("errors") or [])
    if not git_available:
        errors = [error for error in errors if error != ignored_error]
        if not errors:
            _finding(
                findings,
                "warning",
                "collaboration_git_hygiene_unavailable",
                path,
                "当前恢复目录不是 Git worktree，无法验证 ignore hygiene",
                result.get("artifact_id"),
            )
            return
    _finding(
        findings,
        "invalid",
        invalid_code,
        path,
        "; ".join(errors or ["Collaboration validation failed"]),
        result.get("artifact_id"),
    )


def _git_worktree_available(project_root):
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(project_root),
                "rev-parse",
                "--is-inside-work-tree",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def _inspect_work_packages(root, findings, stats):
    directory = root / "work-packages"
    if not directory.is_dir():
        return
    for package_dir in sorted(
        path
        for path in directory.iterdir()
        if path.is_dir() and path.name != ".locks"
    ):
        stats["work_packages"] += 1
        documents = {}
        for name in (
            "package.json",
            "read-ledger.json",
            "decision-ledger.json",
            "checkpoint.json",
        ):
            path = package_dir / name
            value = _read_object(
                path,
                findings,
                stats,
                missing_code="work_package_file_missing",
                corrupt_code="work_package_file_invalid",
            )
            if value is not None:
                documents[name] = value
                _inspect_value_privacy(
                    value,
                    path,
                    findings,
                    package_dir.name,
                )
        package = documents.get("package.json") or {}
        if any((
            package.get("work_package_version") != "1.0",
            package.get("package_id") != package_dir.name,
            package.get("mode") != "shadow",
            package.get("status") not in {"active", "closed"},
        )):
            _finding(
                findings,
                "invalid",
                "work_package_identity_invalid",
                package_dir / "package.json",
                "Work Package version、id、mode 或 status 无效",
                package_dir.name,
            )
        guards = package.get("capability_guards") or {}
        required_false = (
            "automatic_activation",
            "default_context_injection",
            "restrict_search",
            "restrict_file_reads",
            "skip_tests",
            "select_final_validation",
            "mutate_instructions",
            "mutate_source",
        )
        if any((
            guards.get("advisory_only") is not True,
            guards.get("full_fallback") is not True,
            any(guards.get(key) is not False for key in required_false),
        )):
            _finding(
                findings,
                "invalid",
                "work_package_capability_guard_invalid",
                package_dir / "package.json",
                "Work Package 试图限制 AI 能力或关闭完整 fallback",
                package_dir.name,
            )
        if package.get("status") == "active":
            _finding(
                findings,
                "warning",
                "work_package_active",
                package_dir / "package.json",
                "迁移前应完成、关闭或显式导出活动 Work Package",
                package_dir.name,
            )
        for name, value in documents.items():
            if value.get("package_id") != package_dir.name:
                _finding(
                    findings,
                    "invalid",
                    "work_package_document_mismatch",
                    package_dir / name,
                    "Work Package 文档 package_id 与目录不一致",
                    package_dir.name,
                )
        _inspect_work_package_reads(
            package_dir,
            documents.get("read-ledger.json") or {},
            findings,
        )


def _inspect_shadow_companion(root, findings, stats):
    project_root = root.parents[2]
    report = inspect_shadow_companion_store(project_root)
    stats["shadow_companion_capsules"] = report["capsule_count"]
    for issue in report["issues"]:
        path = project_root / issue["path"]
        _finding(
            findings,
            issue["severity"],
            issue["code"],
            path,
            issue["message"],
            issue.get("capsule_id"),
        )


def _inspect_work_package_reads(package_dir, ledger, findings):
    entries = ledger.get("entries")
    if not isinstance(entries, list):
        _finding(
            findings,
            "invalid",
            "work_package_read_ledger_invalid",
            package_dir / "read-ledger.json",
            "read ledger entries 必须是 array",
            package_dir.name,
        )
        return
    for entry in entries:
        if not isinstance(entry, dict):
            _finding(
                findings,
                "invalid",
                "work_package_read_entry_invalid",
                package_dir / "read-ledger.json",
                "read ledger entry 必须是 object",
                package_dir.name,
            )
            continue
        value = str(entry.get("path") or "")
        normalized = PurePosixPath(value.replace("\\", "/"))
        if (
            not value
            or PureWindowsPath(value).is_absolute()
            or normalized.is_absolute()
            or ".." in normalized.parts
        ):
            _finding(
                findings,
                "invalid",
                "work_package_read_path_invalid",
                package_dir / "read-ledger.json",
                f"Work Package read path 必须是安全相对路径: {value}",
                package_dir.name,
            )


def _inspect_quarantines(root, findings, stats):
    directory = root / "quarantine"
    if not directory.is_dir():
        return
    for path in sorted(item for item in directory.iterdir() if item.is_dir()):
        stats["knowledge_quarantines"] += 1
        receipt = _read_object(
            path / "receipt.json",
            findings,
            stats,
            missing_code="knowledge_quarantine_receipt_missing",
            corrupt_code="knowledge_quarantine_receipt_invalid",
        )
        if receipt is None:
            continue
        status = receipt.get("status")
        if (
            receipt.get("quarantine_id") != path.name
            or status not in {
                "prepared",
                "completed",
                "restored",
                "rolled_back",
            }
        ):
            _finding(
                findings,
                "invalid",
                "knowledge_quarantine_identity_invalid",
                path / "receipt.json",
                "Knowledge quarantine id 或 status 无效",
                path.name,
            )
            continue
        items = receipt.get("capabilities")
        if not isinstance(items, list) or not items:
            _finding(
                findings,
                "invalid",
                "knowledge_quarantine_entries_invalid",
                path / "receipt.json",
                "Knowledge quarantine capabilities 必须是非空 array",
                path.name,
            )
            continue
        for item in items:
            _inspect_quarantine_capability(
                root,
                path,
                status,
                item,
                findings,
            )
        _inspect_quarantine_catalog_snapshot(path, receipt, findings)
        if status == "prepared":
            _finding(
                findings,
                "invalid",
                "knowledge_quarantine_incomplete",
                path / "receipt.json",
                "Knowledge quarantine 停留在 prepared，必须显式恢复或回滚",
                path.name,
            )
        if status == "rolled_back":
            _finding(
                findings,
                "warning",
                "knowledge_quarantine_rolled_back",
                path / "receipt.json",
                "Knowledge quarantine 已回滚，保留回执供审计",
                path.name,
            )
        if status != "restored":
            _finding(
                findings,
                "warning",
                "knowledge_quarantine_present",
                path / "receipt.json",
                "隔离资产默认不应进入可移植 Knowledge 导出",
                path.name,
            )


def _inspect_quarantine_capability(
        root,
        directory,
        status,
        item,
        findings,
):
    if not isinstance(item, dict):
        _finding(
            findings,
            "invalid",
            "knowledge_quarantine_entry_invalid",
            directory / "receipt.json",
            "Knowledge quarantine capability entry 必须是 object",
            directory.name,
        )
        return
    capability_id = str(item.get("capability_id") or "")
    expected_hash = str(item.get("sha256") or "")
    try:
        active = _contained(root, root / str(item.get("path") or ""))
        quarantined = _contained(
            root,
            root / str(item.get("quarantine_path") or ""),
        )
    except ValueError:
        _finding(
            findings,
            "invalid",
            "knowledge_quarantine_path_invalid",
            directory / "receipt.json",
            "Knowledge quarantine capability 路径越界",
            capability_id or directory.name,
        )
        return
    expected_active = root / "capabilities" / f"{capability_id}.json"
    expected_quarantined = directory / "capabilities" / f"{capability_id}.json"
    if (
        not capability_id.startswith("capability-")
        or active != expected_active.resolve()
        or quarantined != expected_quarantined.resolve()
        or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None
    ):
        _finding(
            findings,
            "invalid",
            "knowledge_quarantine_entry_invalid",
            directory / "receipt.json",
            "Knowledge quarantine capability identity、path 或 SHA-256 无效",
            capability_id or directory.name,
        )
        return
    active_valid = active.is_file() and _sha256_file(active) == expected_hash
    quarantine_valid = (
        quarantined.is_file()
        and _sha256_file(quarantined) == expected_hash
    )
    if status == "completed":
        valid_placement = quarantine_valid and not active.exists()
    elif status in {"restored", "rolled_back"}:
        valid_placement = active_valid and not quarantined.exists()
    else:
        valid_placement = (
            (active_valid and not quarantined.exists())
            or (quarantine_valid and not active.exists())
        )
    if not valid_placement:
        _finding(
            findings,
            "invalid",
            "knowledge_quarantine_content_invalid",
            directory / "receipt.json",
            "Knowledge quarantine 文件位置或 SHA-256 与状态不一致",
            capability_id,
        )


def _inspect_quarantine_catalog_snapshot(directory, receipt, findings):
    exists = receipt.get("catalog_before_exists") is True
    snapshot = directory / "catalog-before.json"
    expected_hash = receipt.get("catalog_before_sha256")
    if not exists:
        if snapshot.exists():
            _finding(
                findings,
                "invalid",
                "knowledge_quarantine_catalog_snapshot_unexpected",
                snapshot,
                "Receipt 声明无原 catalog，但快照文件存在",
                directory.name,
            )
        return
    if not snapshot.is_file():
        _finding(
            findings,
            "invalid",
            "knowledge_quarantine_catalog_snapshot_missing",
            snapshot,
            "Receipt 声明存在原 catalog，但快照文件缺失",
            directory.name,
        )
        return
    if expected_hash is None:
        _finding(
            findings,
            "warning",
            "knowledge_quarantine_catalog_snapshot_legacy",
            snapshot,
            "旧回执未记录原 catalog SHA-256",
            directory.name,
        )
    elif (
        re.fullmatch(r"[0-9a-f]{64}", str(expected_hash)) is None
        or _sha256_file(snapshot) != expected_hash
    ):
        _finding(
            findings,
            "invalid",
            "knowledge_quarantine_catalog_snapshot_invalid",
            snapshot,
            "原 catalog 快照 SHA-256 无效",
            directory.name,
        )


def _inspect_temporary_files(root, findings, stats):
    if not root.is_dir():
        return
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.casefold() in {
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
            ".mp4",
            ".avi",
            ".mov",
        }:
            _finding(
                findings,
                "invalid",
                "raw_media_in_knowledge_store",
                path,
                "knowledge store 不允许 raw media",
            )
        if path.name.endswith((".migrating", ".tmp")):
            _finding(
                findings,
                "warning",
                "knowledge_temporary_file",
                path,
                "存在未清理的知识写入临时文件",
            )


def _read_object(
        path,
        findings,
        stats,
        *,
        required=True,
        missing_code=None,
        corrupt_code,
):
    path = Path(path)
    if not path.is_file():
        if required and missing_code:
            _finding(
                findings,
                "invalid",
                missing_code,
                path,
                "必需文件不存在",
            )
        return None
    stats["files_scanned"] += 1
    try:
        raw = path.read_bytes()
        if len(raw) > MAX_KNOWLEDGE_JSON_BYTES:
            raise ValueError("JSON 文件超过 1 MiB 安全上限")
        text = raw.decode("utf-8")

        def pairs(values):
            result = {}
            for key, item in values:
                if key in result:
                    raise ValueError(f"JSON 包含重复 key: {key}")
                result[key] = item
            return result

        value = json.loads(text, object_pairs_hook=pairs)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        _finding(
            findings,
            "invalid",
            corrupt_code,
            path,
            f"{type(error).__name__}: {error}",
        )
        return None
    if not isinstance(value, dict):
        _finding(
            findings,
            "invalid",
            corrupt_code,
            path,
            "JSON 顶层必须是 object",
        )
        return None
    return value


def _contained(root, path):
    root = Path(root).resolve()
    path = Path(path).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError(path) from error
    return path


def _contains_media(path, root):
    path = Path(path)
    root = Path(root)
    return (
        path.suffix.casefold()
        in {".png", ".jpg", ".jpeg", ".webp", ".mp4", ".avi", ".mov"}
        and root in path.parents
    )


def _sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _inspect_value_privacy(value, path, findings, record_id=None):
    text = "\n".join(_string_values(value))
    if PRIVATE_PATH.search(text):
        _finding(
            findings,
            "invalid",
            "knowledge_private_path",
            path,
            "knowledge 记录包含绝对私有路径或 UNC 路径",
            record_id,
        )
    if ABSOLUTE_PATH.search(text):
        _finding(
            findings,
            "invalid",
            "knowledge_absolute_path",
            path,
            "knowledge 记录包含绝对路径",
            record_id,
        )
    if CREDENTIAL.search(text):
        _finding(
            findings,
            "invalid",
            "knowledge_credential_text",
            path,
            "knowledge 记录包含凭据形态文本",
            record_id,
        )
    if SECRET_TOKEN.search(text):
        _finding(
            findings,
            "invalid",
            "knowledge_secret_token",
            path,
            "knowledge 记录包含 secret token 形态文本",
            record_id,
        )
    sensitive = sorted({
        key
        for key in _mapping_keys(value)
        if SENSITIVE_KEY.search(key)
    })
    if sensitive:
        _finding(
            findings,
            "invalid",
            "knowledge_sensitive_key",
            path,
            f"knowledge 记录包含敏感字段名: {sensitive}",
            record_id,
        )


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


def _finding(
        findings,
        severity,
        code,
        path,
        message,
        record_id=None,
):
    findings.append({
        "severity": severity,
        "code": code,
        "path": str(Path(path).resolve()),
        "record_id": record_id,
        "message": message,
    })


def _public_finding(finding, root):
    value = dict(finding)
    path = Path(str(value.get("path") or "")).resolve()
    try:
        value["path"] = path.relative_to(root).as_posix()
    except ValueError:
        digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:12]
        value["path"] = f"<outside-knowledge-root:{digest}>"
    value["message"] = _redact_public_text(value.get("message"))
    if value.get("record_id") is not None:
        value["record_id"] = _redact_public_text(value.get("record_id"))
    return value


def _redact_public_text(value):
    text = str(value or "")
    text = PRIVATE_PATH.sub("<private-path>", text)
    text = ABSOLUTE_PATH.sub("<private-path>", text)
    text = CREDENTIAL.sub("<credential>", text)
    return SECRET_TOKEN.sub("<credential>", text)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Inspect Recorder knowledge without modifying it",
    )
    parser.add_argument("recording_root")
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args(argv)
    report = inspect_knowledge_store(args.recording_root)
    print(json.dumps(
        report,
        ensure_ascii=False,
        separators=(",", ":") if args.compact else None,
        indent=None if args.compact else 2,
    ))
    return 1 if report["status"] == "invalid" else 0


if __name__ == "__main__":
    raise SystemExit(main())