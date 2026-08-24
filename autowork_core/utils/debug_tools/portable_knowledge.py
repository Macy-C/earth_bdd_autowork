from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath

from autowork_core.utils.debug_tools.ai_paths import (
    PORTABLE_KNOWLEDGE_SCHEMA_PATH,
    PROJECT_KNOWLEDGE_ROOT,
    PROJECT_PORTABLE_KNOWLEDGE_ROOT,
)
from autowork_core.utils.debug_tools.recorder.knowledge_audit import (
    ABSOLUTE_PATH,
    CREDENTIAL,
    PRIVATE_PATH,
    SECRET_TOKEN,
    SENSITIVE_KEY,
    inspect_knowledge_store,
)
from autowork_core.utils.debug_tools.recorder.knowledge_store import (
    capability_store_lock,
)
PORTABLE_KNOWLEDGE_VERSION = "1.0"
PORTABLE_ROOT = PROJECT_PORTABLE_KNOWLEDGE_ROOT
RECORDS_ROOT = PORTABLE_ROOT / "records"
SCHEMA_PATH = PORTABLE_KNOWLEDGE_SCHEMA_PATH
RECORD_ID = re.compile(r"^portable-knowledge-[0-9a-f]{32}$")
HASH = re.compile(r"^[0-9a-f]{64}$")
IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
MAX_RECORD_BYTES = 16 * 1024
MAX_OPERATIONS = 32
MAX_QUERY_ITEMS = 6
MAX_QUERY_BYTES = 8192
MAX_QUERY_STEPS = 32
MAX_QUERY_INPUT_BYTES = 4096
MAX_STORE_RECORDS = 5000
MAX_STORE_BYTES = 64 * 1024 * 1024
MAX_SOURCE_CAPABILITY_BYTES = 1024 * 1024
ALLOWED_PATH_ROOTS = (
    PurePosixPath("Bdd/steps"),
    PurePosixPath("Bdd/page_obj"),
    PurePosixPath("Bdd/locators"),
    PurePosixPath("Bdd/data"),
)
ALLOWED_SOURCES = {
    "literal",
    "observation",
    "observed",
    "step_text",
    "text_block",
    "unspecified",
}
SOURCE_PREFIXES = ("examples.", "data.", "context.", "table.")
REOPEN_WHEN = [
    "current_code_or_locator_hash_changes",
    "feature_scenario_or_step_identity_changes",
    "framework_generation_contract_changes",
    "current_evidence_or_user_instruction_conflicts",
]
TOP_LEVEL_FIELDS = {
    "portable_knowledge_version",
    "record_id",
    "authority",
    "kind",
    "content_trust",
    "instruction_authority",
    "provenance",
    "target",
    "implementation",
    "applicability",
    "content_hash",
}
PROVENANCE_FIELDS = {
    "source_capability_id",
    "source_capability_sha256",
    "confirmation_source",
    "plan_fingerprint",
    "revision_seal",
}
TARGET_FIELDS = {
    "feature_id",
    "feature_path",
    "feature_name",
    "scenario_id",
    "scenario_name",
    "step_id",
    "step_keyword",
    "step_text",
    "example_columns",
    "target_fingerprint",
}
IMPLEMENTATION_FIELDS = {
    "behavior_owner",
    "behavior_file",
    "page_object",
    "locator_file",
    "data_file",
    "operations",
}
OPERATION_FIELDS = {"op", "target", "source"}
APPLICABILITY_FIELDS = {
    "advisory_only",
    "requires_current_code_validation",
    "reopen_when",
}


def plan_portable_knowledge_sync(project_root):
    state = _build_sync_state(project_root)
    return _plan_from_state(state)


def _plan_from_state(state):
    plan = {
        "portable_knowledge_version": PORTABLE_KNOWLEDGE_VERSION,
        "action": "sync_confirmed_capabilities",
        "side_effect_free": True,
        "requires_user_confirmation": True,
        "automatic_stage": False,
        "automatic_commit": False,
        "automatic_push": False,
        "create": [
            {
                "record_id": item["record"]["record_id"],
                "path": item["path"],
                "content_hash": item["record"]["content_hash"],
                "source_capability_id": item["source_capability_id"],
                "source_capability_sha256": item["record"]["provenance"][
                    "source_capability_sha256"
                ],
                "preview": {
                    "target": item["record"]["target"],
                    "implementation": item["record"]["implementation"],
                    "content_trust": item["record"]["content_trust"],
                    "instruction_authority": item["record"][
                        "instruction_authority"
                    ],
                },
            }
            for item in state["create"]
        ],
        "existing": sorted(state["existing"]),
        "skipped": state["skipped"],
        "store": {
            "current_records": state["current_records"],
            "current_bytes": state["current_bytes"],
            "projected_records": state["projected_records"],
            "projected_bytes": state["projected_bytes"],
            "max_records": MAX_STORE_RECORDS,
            "max_bytes": MAX_STORE_BYTES,
        },
    }
    plan["plan_fingerprint"] = _hash(plan)
    return plan


def sync_portable_knowledge(
        project_root,
        *,
        plan_fingerprint,
        user_confirmed=False,
):
    if user_confirmed is not True:
        raise PermissionError(
            "写入 Portable Knowledge 需要当前任务中的用户明确确认"
        )
    plan_fingerprint = str(plan_fingerprint or "").strip()
    if HASH.fullmatch(plan_fingerprint) is None:
        raise ValueError("Portable Knowledge plan_fingerprint 无效")
    root = Path(project_root).resolve()
    with capability_store_lock(root):
        state = _build_sync_state(root)
        current_plan = _plan_from_state(state)
        if current_plan["plan_fingerprint"] != plan_fingerprint:
            raise ValueError(
                "Portable Knowledge 同步计划已变化，请重新审阅 plan-sync"
            )
        records_root = _portable_records_root(root, create=True)
        written = []
        try:
            for item in state["create"]:
                path = _contained(
                    records_root,
                    records_root / f"{item['record']['record_id']}.json",
                )
                if path.exists():
                    existing = _read_record(path)
                    if existing != item["record"]:
                        raise ValueError(
                            f"Portable Knowledge immutable collision: {path.name}"
                        )
                    continue
                _write_record_atomic(path, item["record"])
                written.append(path.relative_to(root).as_posix())
            audit, _records = _audit_portable_state(root)
            if audit["status"] != "passed":
                raise ValueError(
                    f"Portable Knowledge 写入后审计失败: {audit['errors']}"
                )
        except Exception:
            for relative in reversed(written):
                path = _contained(root, root / relative)
                path.unlink(missing_ok=True)
            raise
        return {
            "portable_knowledge_version": PORTABLE_KNOWLEDGE_VERSION,
            "status": "completed",
            "written": written,
            "existing": current_plan["existing"],
            "skipped": state["skipped"],
            "automatic_stage": False,
            "automatic_commit": False,
            "automatic_push": False,
            "audit": audit,
        }


def audit_portable_knowledge(project_root):
    root = Path(project_root).resolve()
    report, _records = _audit_portable_state(root)
    return report


def _audit_portable_state(root):
    root = Path(root).resolve()
    errors = []
    warnings = []
    errors.extend(_portable_schema_errors(root))
    try:
        records_root = _portable_records_root(root, create=False)
    except ValueError:
        records_root = None
        errors.append("records_root_invalid")
    files = []
    if records_root is not None:
        for path in sorted(records_root.iterdir(), key=lambda item: item.name):
            if (
                path.is_symlink()
                or _is_link_or_reparse(path)
                or not path.is_file()
                or RECORD_ID.fullmatch(path.stem) is None
                or path.suffix != ".json"
            ):
                errors.append(f"records_entry_invalid:{path.name}")
                continue
            files.append(path)
    store_bytes = sum(path.stat().st_size for path in files)
    if len(files) > MAX_STORE_RECORDS:
        errors.append("records_count_exceeded")
    if store_bytes > MAX_STORE_BYTES:
        errors.append("records_bytes_exceeded")
    record_ids = set()
    records = []
    for path in files:
        try:
            value = _read_record(path)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            errors.append(
                f"record_unreadable:{path.name}:{type(error).__name__}"
            )
            continue
        record_errors = validate_portable_record(value)
        if path.name != f"{value.get('record_id')}.json":
            record_errors.append("record_filename_mismatch")
        if value.get("record_id") in record_ids:
            record_errors.append("record_id_duplicate")
        record_ids.add(value.get("record_id"))
        errors.extend(
            f"{path.name}:{error}"
            for error in record_errors
        )
        if not record_errors:
            records.append(value)
    return {
        "portable_knowledge_version": PORTABLE_KNOWLEDGE_VERSION,
        "status": "passed" if not errors else "invalid",
        "record_count": len(files),
        "record_bytes": store_bytes,
        "side_effect_free": True,
        "errors": errors,
        "warnings": warnings,
    }, records


def query_portable_knowledge(project_root, target, *, limit=MAX_QUERY_ITEMS):
    root = Path(project_root).resolve()
    if len(_canonical_bytes(target)) > MAX_QUERY_INPUT_BYTES:
        raise ValueError("Portable Knowledge query input 超过 4 KiB")
    steps = target.get("steps") if isinstance(target, dict) else []
    if not isinstance(steps, list) or len(steps) > MAX_QUERY_STEPS:
        raise ValueError("Portable Knowledge query steps 超限")
    audit, records = _audit_portable_state(root)
    if audit["status"] != "passed":
        raise ValueError(
            f"Portable Knowledge 不可查询: {audit['errors']}"
        )
    query = _query_target(target)
    ranked = []
    for value in records:
        score, reasons = _relevance(value["target"], query)
        if score <= 0:
            continue
        ranked.append((score, value["record_id"], reasons, value))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    items = []
    maximum = max(1, min(int(limit), MAX_QUERY_ITEMS))
    for score, _record_id, reasons, value in ranked[:maximum]:
        candidate = {
            "record_id": value["record_id"],
            "content_trust": value["content_trust"],
            "instruction_authority": value["instruction_authority"],
            "score": score,
            "reasons": reasons,
            "target": value["target"],
            "implementation": {
                **value["implementation"],
                "operations": value["implementation"]["operations"][:6],
            },
            "applicability": value["applicability"],
        }
        proposed = [*items, candidate]
        envelope = _query_result(proposed)
        if len(_public_json_bytes(envelope)) > MAX_QUERY_BYTES:
            break
        items = proposed
    return _query_result(items)


def _query_result(items):
    return {
        "portable_knowledge_version": PORTABLE_KNOWLEDGE_VERSION,
        "content_trust": "untrusted_data",
        "instruction_authority": False,
        "advisory_only": True,
        "query_bounded": True,
        "max_items": MAX_QUERY_ITEMS,
        "max_bytes": MAX_QUERY_BYTES,
        "items": items,
    }


def validate_portable_record(value):
    errors = []
    if not isinstance(value, dict):
        return ["record_not_object"]
    _exact_fields(value, TOP_LEVEL_FIELDS, "record", errors)
    if value.get("portable_knowledge_version") != PORTABLE_KNOWLEDGE_VERSION:
        errors.append("record_version_invalid")
    if RECORD_ID.fullmatch(str(value.get("record_id") or "")) is None:
        errors.append("record_id_invalid")
    if value.get("authority") != "user_confirmed":
        errors.append("record_authority_invalid")
    if value.get("kind") != "confirmed_capability":
        errors.append("record_kind_invalid")
    if any((
        value.get("content_trust") != "untrusted_data",
        value.get("instruction_authority") is not False,
    )):
        errors.append("record_content_trust_invalid")
    provenance = value.get("provenance")
    if not isinstance(provenance, dict):
        errors.append("record_provenance_invalid")
        provenance = {}
    else:
        _exact_fields(provenance, PROVENANCE_FIELDS, "provenance", errors)
    if any((
        not _safe_text(provenance.get("source_capability_id"), 160),
        HASH.fullmatch(str(provenance.get("source_capability_sha256") or ""))
        is None,
        provenance.get("confirmation_source") != "user_adjustment",
        HASH.fullmatch(str(provenance.get("plan_fingerprint") or ""))
        is None,
        HASH.fullmatch(str(provenance.get("revision_seal") or "")) is None,
    )):
        errors.append("record_provenance_invalid")

    target = value.get("target")
    if not isinstance(target, dict):
        errors.append("record_target_invalid")
        target = {}
    else:
        _exact_fields(target, TARGET_FIELDS, "target", errors)
    for field in (
        "feature_id",
        "feature_path",
        "step_id",
        "step_text",
        "target_fingerprint",
    ):
        if not isinstance(target.get(field), str) or not target.get(field):
            errors.append(f"target_{field}_invalid")
    for field, maximum in (
        ("feature_id", 160),
        ("feature_path", 300),
        ("step_id", 160),
        ("step_text", 500),
        ("target_fingerprint", 64),
    ):
        if not _safe_text(target.get(field), maximum):
            errors.append(f"target_{field}_invalid")
    if not _safe_relative_path(target.get("feature_path"), roots=(
        PurePosixPath("Bdd/features"),
        PurePosixPath("Bdd/test_features"),
    )):
        errors.append("target_feature_path_not_portable")
    for field, maximum in (
        ("feature_name", 240),
        ("scenario_id", 160),
        ("scenario_name", 240),
        ("step_keyword", 40),
    ):
        item = target.get(field)
        if item is not None and not _safe_text(item, maximum):
            errors.append(f"target_{field}_invalid")
    columns = target.get("example_columns")
    if (
        not isinstance(columns, list)
        or len(columns) > 32
        or len(columns) != len(set(columns))
        or not all(_safe_identifier(item) for item in columns)
    ):
        errors.append("target_example_columns_invalid")
    target_body = {
        key: item for key, item in target.items() if key != "target_fingerprint"
    }
    if target.get("target_fingerprint") != _hash(target_body):
        errors.append("target_fingerprint_invalid")

    implementation = value.get("implementation")
    if not isinstance(implementation, dict):
        errors.append("record_implementation_invalid")
        implementation = {}
    else:
        _exact_fields(
            implementation,
            IMPLEMENTATION_FIELDS,
            "implementation",
            errors,
        )
    for field in ("behavior_file", "page_object", "locator_file", "data_file"):
        item = implementation.get(field)
        if item is not None and not _safe_relative_path(item):
            errors.append(f"implementation_{field}_invalid")
    behavior_owner = implementation.get("behavior_owner")
    if behavior_owner is not None and not _safe_text(behavior_owner, 160):
        errors.append("implementation_behavior_owner_invalid")
    operations = implementation.get("operations")
    if (
        not isinstance(operations, list)
        or not operations
        or len(operations) > MAX_OPERATIONS
    ):
        errors.append("implementation_operations_invalid")
        operations = []
    for operation in operations:
        if not isinstance(operation, dict):
            errors.append("operation_not_object")
            continue
        _exact_fields(operation, OPERATION_FIELDS, "operation", errors)
        if not _safe_identifier(operation.get("op")):
            errors.append("operation_op_invalid")
        target_name = operation.get("target")
        if target_name is not None and not _safe_text(target_name, 160):
            errors.append("operation_target_invalid")
        if not _safe_source(operation.get("source")):
            errors.append("operation_source_invalid")

    applicability = value.get("applicability")
    if not isinstance(applicability, dict):
        errors.append("record_applicability_invalid")
        applicability = {}
    else:
        _exact_fields(
            applicability,
            APPLICABILITY_FIELDS,
            "applicability",
            errors,
        )
    if any((
        applicability.get("advisory_only") is not True,
        applicability.get("requires_current_code_validation") is not True,
        applicability.get("reopen_when") != REOPEN_WHEN,
    )):
        errors.append("record_applicability_policy_invalid")

    body = {
        key: item
        for key, item in value.items()
        if key not in {"record_id", "content_hash"}
    }
    expected_hash = _hash(body)
    if value.get("content_hash") != expected_hash:
        errors.append("record_content_hash_invalid")
    if value.get("record_id") != f"portable-knowledge-{expected_hash[:32]}":
        errors.append("record_identity_mismatch")
    if len(_record_bytes(value)) > MAX_RECORD_BYTES:
        errors.append("record_size_exceeded")
    errors.extend(_privacy_errors(value))
    return list(dict.fromkeys(errors))


def validate_portable_knowledge_schema(value):
    if not isinstance(value, dict):
        return ["portable_knowledge_schema_not_object"]
    if value != portable_knowledge_schema():
        return ["portable_knowledge_schema_contract_mismatch"]
    return []


def portable_knowledge_schema():
    nullable_text = {"type": ["string", "null"], "maxLength": 240}
    portable_path = {
        "type": ["string", "null"],
        "maxLength": 300,
        "pattern": (
            "^Bdd/(?:steps|page_obj|locators|data)/"
            "(?!.*(?:^|/)\\.\\.(?:/|$))[^\\r\\n/][^\\r\\n]*$"
        ),
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://bdd-autowork.local/ai/portable-knowledge.schema.json",
        "title": "BDD Autowork Portable Knowledge Record",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "portable_knowledge_version",
            "record_id",
            "authority",
            "kind",
            "content_trust",
            "instruction_authority",
            "provenance",
            "target",
            "implementation",
            "applicability",
            "content_hash",
        ],
        "properties": {
            "portable_knowledge_version": {
                "const": PORTABLE_KNOWLEDGE_VERSION,
            },
            "record_id": {"type": "string", "pattern": RECORD_ID.pattern},
            "authority": {"const": "user_confirmed"},
            "kind": {"const": "confirmed_capability"},
            "content_trust": {"const": "untrusted_data"},
            "instruction_authority": {"const": False},
            "provenance": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "source_capability_id",
                    "source_capability_sha256",
                    "confirmation_source",
                    "plan_fingerprint",
                    "revision_seal",
                ],
                "properties": {
                    "source_capability_id": {
                        "type": "string",
                        "maxLength": 160,
                    },
                    "source_capability_sha256": {
                        "type": "string",
                        "pattern": HASH.pattern,
                    },
                    "confirmation_source": {"const": "user_adjustment"},
                    "plan_fingerprint": {
                        "type": "string",
                        "pattern": HASH.pattern,
                    },
                    "revision_seal": {
                        "type": "string",
                        "pattern": HASH.pattern,
                    },
                },
            },
            "target": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "feature_id",
                    "feature_path",
                    "feature_name",
                    "scenario_id",
                    "scenario_name",
                    "step_id",
                    "step_keyword",
                    "step_text",
                    "example_columns",
                    "target_fingerprint",
                ],
                "properties": {
                    "feature_id": {"type": "string", "maxLength": 160},
                    "feature_path": {
                        "type": "string",
                        "maxLength": 300,
                        "pattern": (
                            "^Bdd/(?:features|test_features)/"
                            "(?!.*(?:^|/)\\.\\.(?:/|$))"
                            "[^\\r\\n/][^\\r\\n]*$"
                        ),
                    },
                    "feature_name": nullable_text,
                    "scenario_id": nullable_text,
                    "scenario_name": nullable_text,
                    "step_id": {"type": "string", "maxLength": 160},
                    "step_keyword": {
                        "type": ["string", "null"],
                        "maxLength": 40,
                    },
                    "step_text": {"type": "string", "maxLength": 500},
                    "example_columns": {
                        "type": "array",
                        "maxItems": 32,
                        "uniqueItems": True,
                        "items": {
                            "type": "string",
                            "pattern": IDENTIFIER.pattern,
                        },
                    },
                    "target_fingerprint": {
                        "type": "string",
                        "pattern": HASH.pattern,
                    },
                },
            },
            "implementation": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "behavior_owner",
                    "behavior_file",
                    "page_object",
                    "locator_file",
                    "data_file",
                    "operations",
                ],
                "properties": {
                    "behavior_owner": {
                        "type": ["string", "null"],
                        "maxLength": 160,
                    },
                    "behavior_file": portable_path,
                    "page_object": portable_path,
                    "locator_file": portable_path,
                    "data_file": portable_path,
                    "operations": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": MAX_OPERATIONS,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["op", "target", "source"],
                            "properties": {
                                "op": {
                                    "type": "string",
                                    "pattern": IDENTIFIER.pattern,
                                },
                                "target": {
                                    "type": ["string", "null"],
                                    "maxLength": 160,
                                },
                                "source": {
                                    "type": "string",
                                    "maxLength": 180,
                                    "anyOf": [
                                        {"enum": sorted(ALLOWED_SOURCES)},
                                        {
                                            "pattern": (
                                                "^(?:examples|data|context|table)\\."
                                                + IDENTIFIER.pattern[1:-1]
                                                + "$"
                                            ),
                                        },
                                    ],
                                },
                            },
                        },
                    },
                },
            },
            "applicability": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "advisory_only",
                    "requires_current_code_validation",
                    "reopen_when",
                ],
                "properties": {
                    "advisory_only": {"const": True},
                    "requires_current_code_validation": {"const": True},
                    "reopen_when": {"const": list(REOPEN_WHEN)},
                },
            },
            "content_hash": {"type": "string", "pattern": HASH.pattern},
        },
    }


def _build_sync_state(project_root):
    root = Path(project_root).resolve()
    schema_errors = _portable_schema_errors(root)
    if schema_errors:
        raise ValueError(
            f"Portable Knowledge schema 无效: {schema_errors}"
        )
    recording_root = root / "artifacts" / "recording_sessions"
    audit = inspect_knowledge_store(recording_root)
    if audit["summary"]["invalid_count"]:
        raise ValueError(
            f"Active Knowledge 无效，禁止同步: {audit['findings']}"
        )
    catalog_path = root / PROJECT_KNOWLEDGE_ROOT / "capabilities/catalog.json"
    if (catalog_path.exists() or catalog_path.is_symlink()) and _is_link_or_reparse(
        catalog_path
    ):
        raise ValueError("Capability catalog 不能是链接或 reparse point")
    if not catalog_path.is_file():
        catalog = {"capabilities": []}
    else:
        catalog = _read_object(catalog_path)
    entries = catalog.get("capabilities")
    if not isinstance(entries, list):
        raise ValueError("Capability catalog.capabilities 必须是 array")
    records_root = _portable_records_root(root, create=False)
    existing_by_id = {}
    portable_audit, existing_records = _audit_portable_state(root)
    if portable_audit["status"] != "passed":
        raise ValueError(
            f"现有 Portable Knowledge 无效: {portable_audit['errors']}"
        )
    for value in existing_records:
        existing_by_id[value["record_id"]] = value
    create = []
    existing = []
    skipped = []
    knowledge_root = (root / PROJECT_KNOWLEDGE_ROOT).resolve()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Capability catalog entry 必须是 object")
        capability_id = str(entry.get("capability_id") or "")
        if entry.get("status") != "confirmed":
            skipped.append({
                "capability_id": capability_id,
                "reason": "capability_not_confirmed",
            })
            continue
        path = _contained(
            knowledge_root,
            knowledge_root / str(entry.get("path") or ""),
        )
        if _is_link_or_reparse(path):
            raise ValueError("Capability 文件不能是链接或 reparse point")
        capability, capability_sha256 = _read_source_capability(path)
        capability["_source_sha256"] = capability_sha256
        if capability_id != capability.get("capability_id"):
            raise ValueError("Capability catalog/detail id 不一致")
        provenance = (capability.get("source") or {}).get("provenance") or {}
        source = capability.get("source") or {}
        if any((
            provenance.get("context") != "production",
            provenance.get("producer") != "recorder_generation",
            provenance.get("confirmation_source") != "user_adjustment",
            HASH.fullmatch(str(source.get("plan_fingerprint") or "")) is None,
            HASH.fullmatch(str(source.get("revision_seal") or "")) is None,
        )):
            skipped.append({
                "capability_id": capability_id,
                "reason": "capability_confirmation_not_trusted",
            })
            continue
        record = _record_from_capability(capability)
        record_errors = validate_portable_record(record)
        if record_errors:
            raise ValueError(
                f"Capability 不能生成 Portable Knowledge: "
                f"{capability_id}: {record_errors}"
            )
        record_id = record["record_id"]
        if record_id in existing_by_id:
            if existing_by_id[record_id] != record:
                raise ValueError(
                    f"Portable Knowledge immutable collision: {record_id}"
                )
            existing.append(record_id)
            continue
        create.append({
            "record": record,
            "path": (RECORDS_ROOT / f"{record_id}.json").as_posix(),
            "source_capability_id": capability_id,
        })
    create.sort(key=lambda item: item["record"]["record_id"])
    skipped.sort(key=lambda item: (item["capability_id"], item["reason"]))
    projected_records = portable_audit["record_count"] + len(create)
    projected_bytes = portable_audit["record_bytes"] + sum(
        len(_record_bytes(item["record"])) for item in create
    )
    if projected_records > MAX_STORE_RECORDS:
        raise ValueError("Portable Knowledge projected record count 超限")
    if projected_bytes > MAX_STORE_BYTES:
        raise ValueError("Portable Knowledge projected bytes 超限")
    return {
        "create": create,
        "existing": existing,
        "skipped": skipped,
        "current_records": portable_audit["record_count"],
        "current_bytes": portable_audit["record_bytes"],
        "projected_records": projected_records,
        "projected_bytes": projected_bytes,
    }


def _record_from_capability(capability):
    feature = capability.get("feature") or {}
    scenario = capability.get("scenario") or {}
    step = capability.get("step") or {}
    plan = capability.get("plan") or {}
    source = capability.get("source") or {}
    feature_path = _portable_feature_path(feature.get("source_relpath"))
    if feature_path is None:
        raise ValueError("Capability Feature path 不可移植")
    operations = []
    for item in plan.get("operations") or []:
        if not isinstance(item, dict):
            continue
        operations.append({
            "op": _required_text(item.get("op"), "operation.op", 80),
            "target": _optional_text(item.get("target"), 160),
            "source": _normalized_source(
                item.get("source"),
                item.get("parameters"),
            ),
        })
    if not operations or len(operations) > MAX_OPERATIONS:
        raise ValueError("Capability operations 数量无效")
    target = {
        "feature_id": _required_text(feature.get("id"), "feature.id", 160),
        "feature_path": feature_path,
        "feature_name": _optional_text(feature.get("name"), 240),
        "scenario_id": _optional_text(scenario.get("id"), 160),
        "scenario_name": _optional_text(scenario.get("name"), 240),
        "step_id": _required_text(step.get("id"), "step.id", 160),
        "step_keyword": _optional_text(step.get("keyword"), 40),
        "step_text": _step_template(
            step.get("text"),
            scenario.get("example_values"),
        ),
        "example_columns": sorted(
            str(key)
            for key in (scenario.get("example_values") or {})
            if _safe_identifier(key)
        )[:32],
    }
    target["target_fingerprint"] = _hash(target)
    body = {
        "portable_knowledge_version": PORTABLE_KNOWLEDGE_VERSION,
        "authority": "user_confirmed",
        "kind": "confirmed_capability",
        "content_trust": "untrusted_data",
        "instruction_authority": False,
        "provenance": {
            "source_capability_id": capability["capability_id"],
            "source_capability_sha256": capability["_source_sha256"],
            "confirmation_source": "user_adjustment",
            "plan_fingerprint": source["plan_fingerprint"],
            "revision_seal": source["revision_seal"],
        },
        "target": target,
        "implementation": {
            "behavior_owner": _optional_text(plan.get("behavior_owner"), 160),
            "behavior_file": _portable_implementation_path(
                plan.get("behavior_file")
            ),
            "page_object": _portable_implementation_path(
                plan.get("page_object")
            ),
            "locator_file": _portable_implementation_path(
                plan.get("locator_file")
            ),
            "data_file": _portable_implementation_path(plan.get("data_file")),
            "operations": operations,
        },
        "applicability": {
            "advisory_only": True,
            "requires_current_code_validation": True,
            "reopen_when": list(REOPEN_WHEN),
        },
    }
    content_hash = _hash(body)
    return {
        **body,
        "record_id": f"portable-knowledge-{content_hash[:32]}",
        "content_hash": content_hash,
    }


def _portable_records_root(root, *, create):
    root = Path(root).resolve()
    project_ai_root = root / PROJECT_PORTABLE_KNOWLEDGE_ROOT.parent
    portable_root = root / PORTABLE_ROOT
    records_root = portable_root / "records"
    for path in (project_ai_root, portable_root, records_root):
        if (path.exists() or path.is_symlink()) and _is_link_or_reparse(path):
            raise ValueError(
                f"Portable Knowledge 路径不能是链接或 reparse point: {path.name}"
            )
        try:
            path.resolve().relative_to(root)
        except ValueError as error:
            raise ValueError("Portable Knowledge 路径越界") from error
    if create:
        records_root.mkdir(parents=True, exist_ok=True)
    elif not records_root.is_dir():
        if records_root.exists() or records_root.is_symlink():
            raise ValueError("Portable Knowledge records root 不是目录")
        return None
    return records_root.resolve()


def _portable_schema_errors(root):
    path = Path(root).resolve() / SCHEMA_PATH
    try:
        value = _read_object(path)
    except (OSError, json.JSONDecodeError, ValueError):
        return ["portable_knowledge_schema_unreadable"]
    return validate_portable_knowledge_schema(value)


def _portable_feature_path(value):
    if not _safe_relative_path(value, roots=(
        PurePosixPath("Bdd/features"),
        PurePosixPath("Bdd/test_features"),
    )):
        return None
    return str(value).replace("\\", "/")


def _portable_implementation_path(value):
    if value in {None, ""}:
        return None
    if not _safe_relative_path(value):
        raise ValueError(f"实现路径不可移植: {value}")
    return str(value).replace("\\", "/")


def _safe_relative_path(value, *, roots=ALLOWED_PATH_ROOTS):
    text = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(text)
    if (
        not text
        or PureWindowsPath(text).is_absolute()
        or path.is_absolute()
        or ".." in path.parts
    ):
        return False
    return any(root in path.parents for root in roots)


def _normalized_source(source, parameters):
    value = str(source or "").strip()
    if not value and isinstance(parameters, dict):
        value = str(
            parameters.get("value_source")
            or parameters.get("expected_source")
            or ""
        ).strip()
    if not value:
        return "unspecified"
    return value if _safe_source(value) else "unspecified"


def _safe_source(value):
    if not isinstance(value, str):
        return False
    return value in ALLOWED_SOURCES or any(
        value.startswith(prefix) and _safe_identifier(value[len(prefix):])
        for prefix in SOURCE_PREFIXES
    )


def _step_template(value, example_values):
    text = _required_text(value, "step.text", 500)
    pairs = [
        (str(key), str(item))
        for key, item in (example_values or {}).items()
        if _safe_identifier(str(key)) and item not in {None, ""}
    ]
    pairs.sort(key=lambda pair: (-len(pair[1]), pair[0]))
    for key, item in pairs:
        text = text.replace(item, f"<{key}>")
    if _dangerous_text(text):
        raise ValueError("Step text 包含不可移植路径、凭据或指令形态内容")
    return text


def _dangerous_text(value):
    text = str(value or "")
    if any((
        PRIVATE_PATH.search(text),
        ABSOLUTE_PATH.search(text),
        CREDENTIAL.search(text),
        SECRET_TOKEN.search(text),
    )):
        return True
    for token in re.findall(r"[^\s,;]+", text):
        token = token.strip("\"'`()[]{}<>")
        path = PurePosixPath(token.replace("\\", "/"))
        if PureWindowsPath(token).is_absolute() or path.is_absolute():
            return True
    return False


def _safe_identifier(value):
    return isinstance(value, str) and IDENTIFIER.fullmatch(value) is not None


def _safe_text(value, maximum):
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= maximum
        and "\n" not in value
        and "\r" not in value
    )


def _required_text(value, label, maximum):
    text = str(value or "").strip()
    if not _safe_text(text, maximum):
        raise ValueError(f"{label} 无效")
    return text


def _optional_text(value, maximum):
    if value in {None, ""}:
        return None
    return _required_text(value, "optional text", maximum)


def _privacy_errors(value):
    errors = []
    text = "\n".join(_string_values(value))
    if PRIVATE_PATH.search(text):
        errors.append("record_private_path")
    if ABSOLUTE_PATH.search(text):
        errors.append("record_absolute_path")
    if CREDENTIAL.search(text):
        errors.append("record_credential_text")
    if SECRET_TOKEN.search(text):
        errors.append("record_secret_token")
    if _dangerous_text(text):
        errors.append("record_dangerous_text")
    keys = sorted({
        key for key in _mapping_keys(value) if SENSITIVE_KEY.fullmatch(key)
    })
    if keys:
        errors.append("record_sensitive_key")
    return errors


def _query_target(target):
    target = target if isinstance(target, dict) else {}
    feature = target.get("feature") or {}
    scenario = target.get("scenario") or {}
    steps = target.get("steps") or []
    return {
        "feature_id": feature.get("id"),
        "feature_path": feature.get("source_relpath"),
        "feature_name": feature.get("name"),
        "scenario_id": scenario.get("id"),
        "scenario_name": scenario.get("name"),
        "step_ids": [item.get("id") for item in steps if isinstance(item, dict)],
        "step_texts": [
            item.get("text") for item in steps if isinstance(item, dict)
        ],
    }


def _relevance(target, query):
    score = 0
    reasons = []
    if target.get("step_id") in set(query.get("step_ids") or ()):
        score += 100
        reasons.append("same_step_id")
    if target.get("feature_id") == query.get("feature_id"):
        score += 60
        reasons.append("same_feature_id")
    if target.get("feature_path") == query.get("feature_path"):
        score += 50
        reasons.append("same_feature_path")
    query_scenario_id = query.get("scenario_id")
    target_scenario_id = target.get("scenario_id")
    if query_scenario_id and target_scenario_id:
        if query_scenario_id == target_scenario_id:
            score += 45
            reasons.append("same_scenario_id")
        else:
            score -= 30
            reasons.append("different_scenario_id")
    if (
        query.get("scenario_name")
        and target.get("scenario_name") == query.get("scenario_name")
    ):
        score += 20
        reasons.append("same_scenario_name")
    if target.get("step_text") in set(query.get("step_texts") or ()):
        score += 80
        reasons.append("same_step_text")
    record_tokens = _tokens([
        target.get("feature_name"),
        target.get("scenario_name"),
        target.get("step_text"),
    ])
    query_tokens = _tokens([
        query.get("feature_name"),
        query.get("scenario_name"),
        query.get("step_texts"),
    ])
    shared = sorted(record_tokens & query_tokens)
    if shared:
        score += min(30, len(shared) * 5)
        reasons.append("shared_terms:" + ",".join(shared[:6]))
    return score, reasons


def _tokens(value):
    text = _flatten_text(value).casefold()
    result = {
        item for item in re.findall(r"[a-z0-9_]+", text) if len(item) > 1
    }
    for group in re.findall(r"[\u4e00-\u9fff]+", text):
        result.update(
            group[index:index + 2]
            for index in range(max(1, len(group) - 1))
        )
    return result


def _flatten_text(value):
    if isinstance(value, (list, tuple, set)):
        return " ".join(_flatten_text(item) for item in value)
    if value is None:
        return ""
    return str(value)


def _exact_fields(value, expected, label, errors):
    if set(value) != expected:
        errors.append(f"{label}_fields_invalid")


def _read_object(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 顶层必须是 object: {path}")
    return value


def _read_record(path):
    path = Path(path)
    if path.stat().st_size > MAX_RECORD_BYTES:
        raise ValueError(f"Portable Knowledge 文件过大: {path.name}")
    raw = _read_limited(path, MAX_RECORD_BYTES)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"Portable Knowledge 不是 UTF-8: {path.name}") from error

    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError(
                    f"Portable Knowledge 包含重复 JSON key: {path.name}"
                )
            result[key] = value
        return result

    try:
        value = json.loads(text, object_pairs_hook=pairs)
    except json.JSONDecodeError as error:
        raise ValueError(f"Portable Knowledge JSON 无效: {path.name}") from error
    if not isinstance(value, dict):
        raise ValueError(f"Portable Knowledge 顶层必须是 object: {path.name}")
    if raw != _record_bytes(value):
        raise ValueError(
            f"Portable Knowledge 文件不是唯一 canonical 字节: {path.name}"
        )
    return value


def _read_source_capability(path):
    path = Path(path)
    if path.stat().st_size > MAX_SOURCE_CAPABILITY_BYTES:
        raise ValueError(f"Capability 文件超过安全上限: {path.name}")
    raw = _read_limited(path, MAX_SOURCE_CAPABILITY_BYTES)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"Capability 不是 UTF-8: {path.name}") from error

    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError(f"Capability 包含重复 JSON key: {path.name}")
            result[key] = value
        return result

    try:
        value = json.loads(text, object_pairs_hook=pairs)
    except json.JSONDecodeError as error:
        raise ValueError(f"Capability JSON 无效: {path.name}") from error
    if not isinstance(value, dict):
        raise ValueError(f"Capability 顶层必须是 object: {path.name}")
    return value, hashlib.sha256(raw).hexdigest()


def _write_record_atomic(path, value):
    path = Path(path)
    content = _record_bytes(value)
    if len(content) > MAX_RECORD_BYTES:
        raise ValueError(f"Portable Knowledge 文件过大: {path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _contained(root, path):
    root = Path(root).resolve()
    path = Path(path).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError("portable_path_outside_project") from error
    return path


def _is_link_or_reparse(path):
    path = Path(path)
    if path.is_symlink():
        return True
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _hash(value):
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _read_limited(path, maximum):
    with Path(path).open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError(f"文件超过安全上限: {Path(path).name}")
    return raw


def _canonical_bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _record_bytes(value):
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
        + "\n"
    ).encode("utf-8")


def _public_json_bytes(value):
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")


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


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Manage immutable portable project knowledge",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan-sync")
    sync = commands.add_parser("sync")
    sync.add_argument("--plan-fingerprint", required=True)
    sync.add_argument("--user-confirmed", action="store_true")
    audit = commands.add_parser("audit")
    query = commands.add_parser("query")
    query.add_argument("--feature-id")
    query.add_argument("--feature-path")
    query.add_argument("--feature-name")
    query.add_argument("--scenario-id")
    query.add_argument("--scenario-name")
    query.add_argument("--step-id", action="append", default=[])
    query.add_argument("--step-text", action="append", default=[])
    query.add_argument("--limit", type=int, default=MAX_QUERY_ITEMS)
    for command in (plan, sync, audit, query):
        command.add_argument("--project-root", default=".")
    args = parser.parse_args(argv)
    try:
        if args.command == "plan-sync":
            result = plan_portable_knowledge_sync(args.project_root)
        elif args.command == "sync":
            result = sync_portable_knowledge(
                args.project_root,
                plan_fingerprint=args.plan_fingerprint,
                user_confirmed=args.user_confirmed,
            )
        elif args.command == "audit":
            result = audit_portable_knowledge(args.project_root)
        else:
            result = query_portable_knowledge(
                args.project_root,
                {
                    "feature": {
                        "id": args.feature_id,
                        "source_relpath": args.feature_path,
                        "name": args.feature_name,
                    },
                    "scenario": {
                        "id": args.scenario_id,
                        "name": args.scenario_name,
                    },
                    "steps": [
                        {"id": item} for item in args.step_id
                    ] + [
                        {"text": item} for item in args.step_text
                    ],
                },
                limit=args.limit,
            )
    except (OSError, ValueError, PermissionError) as error:
        message = _redacted_error(error)
        print(json.dumps({
            "portable_knowledge_version": PORTABLE_KNOWLEDGE_VERSION,
            "status": "error",
            "error_code": type(error).__name__,
            "message": message,
        }, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") not in {"invalid", "blocked"} else 1


def _redacted_error(error):
    text = str(error or "portable_knowledge_error")
    text = PRIVATE_PATH.sub("<private-path>", text)
    text = ABSOLUTE_PATH.sub("<private-path>", text)
    text = CREDENTIAL.sub("<credential>", text)
    text = SECRET_TOKEN.sub("<credential>", text)
    text = re.sub(r"[A-Za-z]:[\\/][^\s,;]+", "<private-path>", text)
    return text[:500]


if __name__ == "__main__":
    raise SystemExit(main())