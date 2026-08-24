from __future__ import annotations

import argparse
import copy
import json
from datetime import datetime
from pathlib import Path

from autowork_core.utils.debug_tools.recorder.bundle_validator import (
    validate_ai_bundle,
)
from autowork_core.utils.debug_tools.recorder.evidence_context import (
    build_evidence_context,
)
from autowork_core.utils.debug_tools.recorder.evidence_recovery import (
    write_request_recovery_report,
)
from autowork_core.utils.debug_tools.recorder.generation_contract import (
    build_generation_contract,
)
from autowork_core.utils.debug_tools.recorder.identity import stable_digest
from autowork_core.utils.debug_tools.recorder.models import SCHEMA_VERSION
from autowork_core.utils.debug_tools.recorder.project_memory import (
    write_request_memory_context,
)
from autowork_core.utils.debug_tools.recorder.request_repository import (
    artifact_hashes,
    evidence_fingerprint,
    index_generation_request,
    request_revision_snapshot,
    request_scope_key,
    resolve_session_path,
    session_dir_for_request_path,
)
from autowork_core.utils.debug_tools.recorder.workflow_service import (
    inspect_workflow,
    submit_generation_plan,
)
from autowork_core.utils.debug_tools.recorder.writer import write_json_atomic


LEGACY_IMPORT_VERSION = "1.0"
_RUNTIME_FIELDS = {
    "adjustment",
    "generation_brief",
    "interview",
    "interview_command",
    "stale",
    "understanding",
}


def import_legacy_request(request_path):
    source_path = Path(request_path).resolve()
    source = _read_json(source_path)
    session_dir = session_dir_for_request_path(source_path, source)
    legacy_fields = sorted(_RUNTIME_FIELDS & set(source))
    if source.get("request_version") == "3.0" and not legacy_fields:
        raise ValueError("该请求已经是无运行态字段的 RequestV3，无需迁移")

    contract = _ensure_current_contract(session_dir)
    readiness = validate_ai_bundle(session_dir)
    target_step_ids = {
        step.get("id")
        for step in (source.get("target") or {}).get("steps") or []
        if step.get("id")
    }
    reviews = [
        item
        for item in readiness.get("review_required") or []
        if item.get("step_id") in target_step_ids | {None}
    ]
    request_id = "request_legacy_v3_" + stable_digest(
        source.get("request_id"),
        source.get("evidence_fingerprint"),
        contract.get("contract_hash"),
        length=16,
    )
    output = session_dir / "ai" / "requests" / f"{request_id}.json"
    request = {
        "schema_version": SCHEMA_VERSION,
        "request_version": "3.0",
        "request_id": request_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "session": copy.deepcopy(source.get("session") or {}),
        "target": copy.deepcopy(source.get("target") or {}),
        "readiness": {
            "bundle_valid": bool(readiness.get("bundle_valid")),
            "target_generation_ready": not reviews,
            "session_generation_ready": bool(readiness.get("generation_ready")),
            "session_warnings": list(readiness.get("warnings") or []),
            "target_review_required": copy.deepcopy(reviews),
        },
        "generation_contract": "ai/generation-contract.json",
        "framework_contract": {
            "generation_contract_version": contract.get(
                "generation_contract_version"
            ),
            "framework_contract_version": (
                contract.get("framework_contract") or {}
            ).get("version"),
            "contract_hash": contract.get("contract_hash"),
            "api_signature_hash": (
                contract.get("framework_contract") or {}
            ).get("api_signature_hash"),
        },
        "artifact_repair": copy.deepcopy(source.get("artifact_repair") or {}),
        "evidence": _refresh_evidence_hashes(
            session_dir,
            source.get("evidence") or [],
        ),
        "instructions": [
            "This immutable RequestV3 was materialized by legacy_import.",
            "Use Workflow State, GenerationPlanV4.2, and GenerationTransactionV3 only.",
            "Legacy interview, understanding, preflight, and run state are advisory history only.",
        ],
        "legacy_source": {
            "import_version": LEGACY_IMPORT_VERSION,
            "request_id": source.get("request_id"),
            "request_version": source.get("request_version"),
            "evidence_fingerprint": source.get("evidence_fingerprint"),
            "path": source_path.relative_to(session_dir).as_posix(),
            "runtime_fields_found": legacy_fields,
        },
        "request_path": output.relative_to(session_dir).as_posix(),
        "request_path_absolute": str(output),
    }
    write_request_recovery_report(session_dir, request)
    _attach_memory_context(session_dir, request)
    context = build_evidence_context(session_dir, request, write=True)
    context_path = Path(context["context_path"])
    request["evidence_context"] = {
        "available": True,
        "version": context.get("evidence_context_version"),
        "path": context_path.relative_to(session_dir).as_posix(),
        "context_fingerprint": context.get("context_fingerprint"),
        "minimum_decision_evidence_ids": context.get(
            "minimum_decision_evidence_ids"
        ) or [],
        "coverage": context.get("coverage") or {},
    }
    request["request_scope"] = request_scope_key(target_step_ids)
    request["evidence_fingerprint"] = evidence_fingerprint(request)
    request["revision_snapshot"] = request_revision_snapshot(session_dir, request)
    index_generation_request(session_dir, request)
    state = inspect_workflow(output, write=True)

    plan, plan_source, warnings = _read_legacy_plan(
        session_dir,
        source,
    )
    imported_plan = None
    if plan:
        try:
            imported_plan = submit_generation_plan(
                output,
                plan,
                note=f"Imported once from {plan_source}.",
                confirmation_source="legacy_import",
            )
            state = inspect_workflow(output, write=True)
        except Exception as error:
            warnings.append(
                f"旧确认计划未通过当前 GenerationPlanV3 校验: "
                f"{type(error).__name__}: {error}"
            )

    report = {
        "schema_version": SCHEMA_VERSION,
        "legacy_import_version": LEGACY_IMPORT_VERSION,
        "imported_at": datetime.now().isoformat(timespec="seconds"),
        "status": "imported",
        "source_request": str(source_path),
        "request_id": request_id,
        "request_path": str(output),
        "runtime_fields_removed": legacy_fields,
        "legacy_plan_source": plan_source,
        "plan_id": (imported_plan or {}).get("plan_id"),
        "workflow_status": state.get("status"),
        "warnings": warnings,
    }
    report_path = session_dir / "ai" / "legacy-imports" / f"{request_id}.json"
    write_json_atomic(report_path, report)
    report["report_path"] = str(report_path)
    return report


def _read_legacy_plan(session_dir, source):
    warnings = []
    adjustment = source.get("adjustment") or {}
    if adjustment.get("status") == "confirmed" and adjustment.get("path"):
        value = _read_declared_artifact(
            session_dir,
            adjustment.get("path"),
            "adjustments",
            warnings,
        )
        if value and isinstance(value.get("plan"), dict):
            return value["plan"], "overall_adjustment", warnings

    understanding = source.get("understanding") or {}
    if understanding.get("status") == "confirmed" and understanding.get("path"):
        value = _read_declared_artifact(
            session_dir,
            understanding.get("path"),
            "understandings",
            warnings,
        )
        proposal = (value or {}).get("proposal") or {}
        implementation = proposal.get("implementation") or {}
        if _has_structured_operations(implementation):
            return {
                "summary": proposal.get("summary") or "Imported legacy understanding.",
                "steps": implementation,
            }, "understanding", warnings
        warnings.append("旧 understanding 不含完整结构化 operations，未迁移为 PlanV4.2")

    interview = source.get("interview") or {}
    if interview.get("status") == "resolved":
        warnings.append("旧 interview 仅保留在迁移报告中，不作为 V3 计划或状态")
    return None, None, warnings


def _read_declared_artifact(
        session_dir,
        value,
        expected_directory,
        warnings,
):
    try:
        path = resolve_session_path(session_dir, value)
        root = (Path(session_dir) / "ai" / expected_directory).resolve()
        path.relative_to(root)
        return _read_json(path)
    except Exception as error:
        warnings.append(
            f"旧 {expected_directory} artifact 无法读取: "
            f"{type(error).__name__}: {error}"
        )
        return None


def _has_structured_operations(implementation):
    return any(
        isinstance(operation, dict) and operation.get("op")
        for step in dict(implementation or {}).values()
        if isinstance(step, dict)
        for operation in step.get("operations") or []
    )


def _refresh_evidence_hashes(session_dir, evidence_items):
    result = []
    for item in evidence_items:
        item = copy.deepcopy(item)
        item["artifact_hashes"] = artifact_hashes(
            session_dir,
            item.get("artifacts") or {},
        )
        result.append(item)
    return result


def _attach_memory_context(session_dir, request):
    try:
        path, context = write_request_memory_context(session_dir, request)
        request["memory_context"] = {
            "available": True,
            "path": path.relative_to(session_dir).as_posix(),
            "revision": (context.get("journal") or {}).get("revision"),
            "event_count": (context.get("journal") or {}).get("event_count", 0),
            "relevant_count": context.get("relevant_count", 0),
            "warnings": context.get("warnings") or [],
        }
    except Exception as error:
        request["memory_context"] = {
            "available": False,
            "path": None,
            "warnings": [
                f"项目记忆上下文不可用: {type(error).__name__}: {error}"
            ],
        }


def _ensure_current_contract(session_dir):
    manifest = _read_json(Path(session_dir) / "manifest.json")
    contract = build_generation_contract(manifest)
    write_json_atomic(
        Path(session_dir) / "ai" / "generation-contract.json",
        contract,
    )
    return contract


def _read_json(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 必须是 object: {path}")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Import one legacy Recorder request into RequestV3"
    )
    parser.add_argument("request_path")
    args = parser.parse_args(argv)
    result = import_legacy_request(args.request_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
