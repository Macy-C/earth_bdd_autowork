from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath

from autowork_core.utils.debug_tools.recorder.code_manifest import (
    code_manifest_matches_transaction,
)
from autowork_core.utils.debug_tools.recorder.identity import stable_digest
from autowork_core.utils.debug_tools.recorder.generation_plan import (
    SUPPORTED_PLAN_VERSIONS,
    plan_artifact_identity_is_valid,
)
from autowork_core.utils.debug_tools.recorder.generation_transaction import (
    transaction_code_snapshot_matches,
)
from autowork_core.utils.debug_tools.recorder.knowledge_store import (
    capability_store_lock,
    ensure_knowledge_store,
    resolve_knowledge_path,
)
from autowork_core.utils.debug_tools.recorder.models import SCHEMA_VERSION
from autowork_core.utils.debug_tools.recorder.writer import write_json_atomic
from autowork_core.utils.debug_tools.recorder.request_repository import (
    request_identity_is_valid,
)
from autowork_core.utils.debug_tools.recorder.transaction_integrity import (
    completed_report_fingerprint,
    transaction_result_fingerprint,
)
from autowork_core.utils.debug_tools.recorder.workflow_state import (
    load_workflow_state,
)


def publish_plan_capabilities(
    session_dir,
    request,
    plan_artifact,
    *,
    accepted_transaction=None,
    eligible_step_ids=None,
):
    session_dir = Path(session_dir).resolve()
    output_root = _find_output_root(session_dir)
    knowledge_root = ensure_knowledge_store(output_root)
    capability_dir = knowledge_root / "capabilities"
    capability_dir.mkdir(parents=True, exist_ok=True)
    if (
        plan_artifact.get("plan_version") not in SUPPORTED_PLAN_VERSIONS
        or not _plan_is_publishable(
            plan_artifact,
            accepted_transaction=accepted_transaction,
        )
    ):
        raise ValueError(
            "Capability 只能由用户确认的 Plan，或用户 accepted 的完整事务发布"
        )
    step_plans = (plan_artifact.get("plan") or {}).get("steps") or {}
    request_steps = {
        step["id"]: step
        for step in (request.get("target") or {}).get("steps") or []
    }
    evidence_by_step = {
        (item.get("step") or {}).get("id"): item
        for item in request.get("evidence") or []
    }
    published = []
    eligible_step_ids = (
        {str(item) for item in eligible_step_ids}
        if eligible_step_ids is not None
        else None
    )
    for step_id, step_plan in step_plans.items():
        if (
            eligible_step_ids is not None
            and str(step_id) not in eligible_step_ids
        ):
            continue
        step = request_steps.get(step_id)
        evidence = evidence_by_step.get(step_id)
        if step is None or evidence is None:
            continue
        capability_id = "capability-" + stable_digest(
            (request.get("target") or {}).get("feature", {}).get("id"),
            step_id,
            plan_artifact.get("plan_fingerprint"),
            length=12,
        )
        capability_path = capability_dir / f"{capability_id}.json"
        provenance = {
            "context": "production",
            "producer": "recorder_generation",
            "confirmation_source": "user_adjustment",
        }
        transaction_id = None
        if accepted_transaction:
            transaction_id = accepted_transaction.get("transaction_id")
            provenance.update({
                "confirmation_source": "accepted_transaction_feedback",
                "runtime_verification": accepted_transaction.get(
                    "runtime_verification",
                    "not_run",
                ),
            })
        capability = {
            "schema_version": SCHEMA_VERSION,
            "capability_version": "2.0",
            "capability_id": capability_id,
            "published_at": datetime.now().isoformat(timespec="seconds"),
            "status": "confirmed",
            "feature": _portable_feature(
                (request.get("target") or {}).get("feature") or {}
            ),
            "scenario": (request.get("target") or {}).get("scenario"),
            "step": step,
            "plan": step_plan,
            "semantic_contract": _semantic_contract(
                request,
                step,
                step_plan,
                scenario_model=(
                    (plan_artifact.get("plan") or {}).get("scenario_model")
                    or {}
                ),
                runtime_verification=(
                    accepted_transaction.get("runtime_verification")
                    if accepted_transaction
                    else "not_run"
                ),
            ),
            "source": {
                "session_id": (request.get("session") or {}).get("id"),
                "session_path": session_dir.relative_to(output_root).as_posix(),
                "request_id": request.get("request_id"),
                "request_path": request.get("request_path"),
                "evidence_fingerprint": request.get("evidence_fingerprint"),
                "selected_take": _selected_take_id(
                    evidence.get("selected_take")
                ),
                "timeline_revision": evidence.get("timeline_revision"),
                "plan_id": plan_artifact.get("plan_id"),
                "plan_fingerprint": plan_artifact.get("plan_fingerprint"),
                "transaction_id": transaction_id,
                "runtime_code_snapshot_fingerprint": (
                    accepted_transaction.get(
                        "runtime_code_snapshot_fingerprint"
                    )
                    if accepted_transaction
                    else None
                ),
                "revision_seal": (
                    plan_artifact.get("source") or {}
                ).get("revision_seal"),
                "provenance": provenance,
            },
            "reuse_policy": {
                "search_existing_code_first": True,
                "reuse_page_object_and_locator_before_generating": True,
                "do_not_merge_evidence_across_runs_implicitly": True,
                "requires_regeneration_when_take_or_timeline_changes": True,
                "source_recording_may_be_retired": True,
            },
        }
        with capability_store_lock(output_root):
            write_json_atomic(capability_path, capability)
            _update_capability_catalog(
                output_root,
                capability,
                capability_path.relative_to(knowledge_root).as_posix(),
            )
        published.append(capability_path)
    return published


def _plan_is_publishable(plan_artifact, *, accepted_transaction):
    source = plan_artifact.get("source") or {}
    if accepted_transaction is not None:
        return all((
            plan_artifact.get("status") in {"confirmed", "validated"},
            source.get("plan_origin") != "legacy_import",
            source.get("confirmation_source") != "legacy_import",
        ))
    return all((
        plan_artifact.get("status") == "confirmed",
        source.get("confirmation_source") == "user_adjustment",
    ))


def _semantic_contract(
        request,
        step,
        step_plan,
        *,
    scenario_model,
        runtime_verification,
):
    target = request.get("target") or {}
    feature = target.get("feature") or {}
    scenario = target.get("scenario") or {}
    specification = scenario.get("specification") or {}
    table = step.get("table") or {}
    return {
        "semantic_contract_version": "1.1",
        "authority": "user_confirmed",
        "feature_name": feature.get("name"),
        "rule": _semantic_rule(specification.get("rule")),
        "scenario_name": scenario.get("name"),
        "scenario_kind": scenario.get("kind"),
        "step_pattern": step.get("text"),
        "step_role": _step_role(
            step.get("semantic_type") or step.get("keyword")
        ),
        "scenario_model": _scenario_model_contract(
            scenario_model,
            step.get("id"),
        ),
        "example_parameters": sorted(
            str(key)
            for key in (scenario.get("example_values") or {})
        ),
        "table_columns": [
            str(item) for item in table.get("headings") or ()
        ],
        "implementation_location": sorted({
            str(operation.get("implementation_location") or "page_method")
            for operation in step_plan.get("operations") or ()
            if isinstance(operation, dict)
        }),
        "implementation_operations": [
            {
                key: operation.get(key)
                for key in ("op", "target", "source", "parameters")
                if operation.get(key) not in (None, {}, [])
            }
            for operation in step_plan.get("operations") or ()
            if isinstance(operation, dict)
        ],
        "runtime_verification": str(
            runtime_verification or "not_run"
        ),
    }


def _scenario_model_contract(model, step_id):
    model = model if isinstance(model, dict) else {}
    step_id = str(step_id or "")
    step_model = next((
        item
        for item in model.get("steps") or ()
        if str(item.get("step_id") or "") == step_id
    ), None)
    if step_model is None:
        return {}
    state_ids = set(
        step_model.get("consumes") or ()
    ) | set(
        step_model.get("produces") or ()
    ) | set(
        step_model.get("observes") or ()
    )
    transitions = [
        item
        for item in model.get("transitions") or ()
        if step_id in {
            str(item.get("from_step_id") or ""),
            str(item.get("to_step_id") or ""),
        }
    ]
    for transition in transitions:
        state_ids.update(transition.get("state_ids") or ())
    return {
        "model_version": model.get("model_version"),
        "mode": model.get("mode") or "state_model",
        "summary": model.get("summary"),
        "step": step_model,
        "states": [
            item
            for item in model.get("states") or ()
            if item.get("state_id") in state_ids
        ],
        "transitions": transitions,
    }


def _semantic_rule(value):
    value = value if isinstance(value, dict) else {}
    return {
        "name": value.get("name"),
        "description": list(value.get("description") or ()),
    }


def _step_role(keyword):
    return {
        "given": "precondition",
        "when": "business_action",
        "then": "business_assertion",
    }.get(str(keyword or "").strip().casefold(), "scenario_step")


def publish_accepted_transaction_capabilities(
    report_path,
    report,
    *,
    project_root=None,
    eligible_step_ids=None,
):
    session_dir, request, plan, runtime_verification = (
        validate_accepted_transaction_capability_source(
            report_path,
            report,
            project_root=project_root,
        )
    )
    return publish_plan_capabilities(
        session_dir,
        request,
        plan,
        accepted_transaction={
            "transaction_id": report.get("transaction_id"),
            "runtime_verification": runtime_verification,
            "runtime_code_snapshot_fingerprint": report.get(
                "runtime_code_snapshot_fingerprint"
            ),
        },
        eligible_step_ids=eligible_step_ids,
    )


def validate_accepted_transaction_capability_source(
        report_path,
        report,
        *,
        project_root=None,
):
    session_dir, request, plan, runtime_verification = (
        validate_completed_transaction_artifact_source(
            report_path,
            report,
            project_root=project_root,
        )
    )
    report_path = Path(report_path).resolve()
    state = load_workflow_state(session_dir, report.get("request_id"))
    result = state.get("last_result") or {}
    if any((
        state.get("status") != "completed",
        result.get("transaction_id") != report.get("transaction_id"),
        Path(result.get("report_path") or "").resolve() != report_path,
        result.get("status") != report.get("status"),
        result.get("completion_fingerprint")
        != report.get("completion_fingerprint"),
        result.get("result_fingerprint") != report.get("result_fingerprint"),
        plan.get("plan_fingerprint")
        != (state.get("plan") or {}).get("plan_fingerprint"),
    )):
        raise ValueError("Completed transaction report fingerprint 无效")
    plan_source = plan.get("source") or {}
    if (
        plan_source.get("plan_origin") == "legacy_import"
        or plan_source.get("confirmation_source") == "legacy_import"
    ):
        raise ValueError("legacy_import Plan 不能提升为当前 confirmed Capability")
    return session_dir, request, plan, runtime_verification


def validate_completed_transaction_artifact_source(
        report_path,
        report,
        *,
        project_root=None,
    ):
    report_path = Path(report_path).resolve()
    if report.get("status") not in {"completed", "completed_no_changes"}:
        raise ValueError("Transaction artifact source尚未完成")
    for audit_name in (
        "plan_conformance_audit",
        "evidence_audit",
        "generation_policy_audit",
    ):
        if (report.get(audit_name) or {}).get("status") != "passed":
            raise ValueError(f"Transaction artifact缺少通过审计: {audit_name}")

    session_dir = Path(report["session_dir"]).resolve()
    if (
        project_root is not None
        and Path(report.get("project_root") or "").resolve()
        != Path(project_root).resolve()
    ):
        raise ValueError("Transaction project_root 与当前项目不一致")
    declared_report_fingerprint = report.get("completion_fingerprint")
    actual_report_fingerprint = completed_report_fingerprint(report)
    declared_result_fingerprint = report.get("result_fingerprint")
    actual_result_fingerprint = transaction_result_fingerprint(report)
    if any((
        not declared_report_fingerprint,
        actual_report_fingerprint != declared_report_fingerprint,
        not declared_result_fingerprint,
        actual_result_fingerprint != declared_result_fingerprint,
    )):
        raise ValueError("Transaction artifact fingerprint 无效")
    request_path = _transaction_artifact_path(
        session_dir,
        report.get("request_path"),
        "requests",
    )
    plan_path = _transaction_artifact_path(
        session_dir,
        report.get("plan_path"),
        "plans",
    )
    request = json.loads(request_path.read_text(encoding="utf-8"))
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    expected_fingerprint = (
        ((report.get("lease") or {}).get("plan") or {}).get(
            "plan_fingerprint"
        )
    )
    lease = report.get("lease") or {}
    lease_plan_path = _transaction_artifact_path(
        session_dir,
        (lease.get("plan") or {}).get("path"),
        "plans",
    )
    if any((
        not request_identity_is_valid(request),
        request.get("request_id") != report.get("request_id"),
        request.get("target") != report.get("target"),
        not plan_artifact_identity_is_valid(plan),
        plan.get("request_id") != report.get("request_id"),
        plan.get("plan_fingerprint") != expected_fingerprint,
        (plan.get("source") or {}).get("revision_seal")
        != (lease.get("revision") or {}).get("seal"),
        lease_plan_path != plan_path,
    )):
        raise ValueError("Transaction Plan 与完成报告不一致")
    effective_project_root = Path(
        project_root or report.get("project_root") or ""
    ).resolve()
    if not code_manifest_matches_transaction(
            report.get("code_manifest"),
            request_id=request.get("request_id"),
            plan_fingerprint=plan.get("plan_fingerprint"),
            project_root=effective_project_root,
            plan_audit=report.get("plan_conformance_audit"),
    ):
        raise ValueError("Transaction Code Manifest 无效或绑定不一致")
    if not transaction_code_snapshot_matches(
        report,
        effective_project_root,
    ):
        raise ValueError("Transaction runtime code snapshot 已变化")
    focused = (
        (report.get("validations") or {}).get("focused_execution") or {}
    )
    runtime_verification = (
        "passed" if focused.get("status") == "passed" else "not_run"
    )
    return session_dir, request, plan, runtime_verification


def _transaction_artifact_path(session_dir, value, directory):
    path = Path(str(value or ""))
    path = path.resolve() if path.is_absolute() else (session_dir / path).resolve()
    expected = (session_dir / "ai" / directory).resolve()
    try:
        path.relative_to(expected)
    except ValueError as error:
        raise ValueError(
            f"Transaction artifact 路径越界: {path}"
        ) from error
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def _portable_feature(feature):
    feature = dict(feature)
    source = str(feature.get("source_relpath") or "").strip()
    if source and (
        PureWindowsPath(source).is_absolute()
        or PurePosixPath(source).is_absolute()
        or ".." in PurePosixPath(source.replace("\\", "/")).parts
    ):
        feature["source_relpath"] = None
    return feature


def load_capability_catalog(output_root):
    output_root = Path(output_root).resolve()
    with capability_store_lock(output_root):
        return _load_capability_catalog_unlocked(output_root)


def _load_capability_catalog_unlocked(output_root):
    _migrate_legacy_capabilities(output_root)
    path = _capability_catalog_path(output_root)
    if not path.exists():
        return {
            "schema_version": SCHEMA_VERSION,
            "updated_at": None,
            "capabilities": [],
        }
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {"capabilities": []}
    except Exception:
        return {
            "schema_version": SCHEMA_VERSION,
            "updated_at": None,
            "capabilities": [],
        }


def search_capabilities(output_root, query):
    query = str(query or "").strip().casefold()
    entries = load_capability_catalog(output_root).get("capabilities") or []
    if not query:
        return entries
    return [
        entry
        for entry in entries
        if query in " ".join(
            str(value or "")
            for value in (
                entry.get("capability_id"),
                (entry.get("feature") or {}).get("name"),
                (entry.get("scenario") or {}).get("name"),
                (entry.get("step") or {}).get("text"),
                entry.get("path"),
            )
        ).casefold()
    ]


def load_capability(output_root, capability):
    output_root = Path(output_root).resolve()
    with capability_store_lock(output_root):
        catalog = _load_capability_catalog_unlocked(output_root)
        if isinstance(capability, dict):
            capability_id = str(capability.get("capability_id") or "")
        else:
            capability_id = str(capability or "")
        entry = next((
            item
            for item in catalog.get("capabilities") or ()
            if str(item.get("capability_id") or "") == capability_id
        ), None)
        if entry is None:
            raise KeyError(f"Capability 不存在: {capability_id}")
        path = resolve_capability_path(output_root, entry)
        value = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(value, dict)
            or str(value.get("capability_id") or "") != capability_id
        ):
            raise ValueError(f"Capability detail 身份不一致: {path}")
        projected = dict(value)
        for field in (
            "status",
            "stale_reason",
            "current_take_id",
            "current_timeline_revision",
        ):
            if field in entry:
                projected[field] = entry[field]
            else:
                projected.pop(field, None)
        return projected


def mark_capabilities_stale(
        output_root,
        session_id,
        step_id,
        *,
        reason,
        affected_take_id=None,
        affected_timeline_revision=None,
        current_take_id=None,
        current_timeline_revision=None,
):
    output_root = Path(output_root).resolve()
    catalog_path = _capability_catalog_path(output_root)
    with capability_store_lock(output_root):
        catalog = _load_capability_catalog_unlocked(output_root)
        changed = False
        for entry in catalog.get("capabilities") or []:
            source = entry.get("source") or {}
            step = entry.get("step") or {}
            if source.get("session_id") != session_id or step.get("id") != step_id:
                continue
            if (
                affected_take_id is not None
                and _selected_take_id(source.get("selected_take"))
                != str(affected_take_id)
            ):
                continue
            if (
                affected_timeline_revision is not None
                and str(source.get("timeline_revision") or "")
                != str(affected_timeline_revision)
            ):
                continue
            entry["status"] = "stale"
            entry["stale_reason"] = str(reason)
            entry["current_take_id"] = current_take_id
            entry["current_timeline_revision"] = current_timeline_revision
            changed = True
        if changed:
            catalog["updated_at"] = datetime.now().isoformat(timespec="seconds")
            write_json_atomic(catalog_path, catalog)
        return changed


def _selected_take_id(value):
    if isinstance(value, dict):
        value = value.get("id")
    return str(value or "")


def _update_capability_catalog(output_root, capability, relative_path):
    catalog_path = _capability_catalog_path(output_root)
    catalog = _load_capability_catalog_unlocked(output_root)
    entry = {
        "capability_id": capability["capability_id"],
        "path": relative_path,
        "published_at": capability["published_at"],
        "status": capability["status"],
        "feature": capability["feature"],
        "scenario": capability["scenario"],
        "step": capability["step"],
        "semantic_contract": capability.get("semantic_contract") or {},
        "source": capability["source"],
    }
    entries = [
        item
        for item in catalog.get("capabilities") or []
        if item.get("capability_id") != entry["capability_id"]
    ]
    entries.append(entry)
    entries.sort(
        key=lambda item: (
            item.get("published_at") or "",
            item.get("capability_id") or "",
        ),
        reverse=True,
    )
    catalog.update({
        "schema_version": SCHEMA_VERSION,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "capabilities": entries,
    })
    write_json_atomic(catalog_path, catalog)


def resolve_capability_path(output_root, entry):
    return resolve_knowledge_path(output_root, entry.get("path"))


def _capability_catalog_path(output_root):
    return (
        ensure_knowledge_store(output_root)
        / "capabilities"
        / "catalog.json"
    )


def _migrate_legacy_capabilities(output_root):
    output_root = Path(output_root).resolve()
    knowledge_root = ensure_knowledge_store(output_root)
    catalog_path = knowledge_root / "capabilities" / "catalog.json"
    if catalog_path.exists():
        return
    legacy_catalog_path = output_root / "capabilities.json"
    if not legacy_catalog_path.is_file():
        return
    try:
        legacy = json.loads(legacy_catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    migrated = []
    for entry in legacy.get("capabilities") or ():
        capability_id = str(entry.get("capability_id") or "")
        relative = entry.get("path")
        if not capability_id or not relative:
            continue
        source = (output_root / str(relative)).resolve()
        try:
            source.relative_to(output_root)
        except ValueError:
            continue
        try:
            capability = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        target = knowledge_root / "capabilities" / f"{capability_id}.json"
        write_json_atomic(target, capability)
        migrated.append({
            **entry,
            "path": target.relative_to(knowledge_root).as_posix(),
        })
    write_json_atomic(catalog_path, {
        "schema_version": legacy.get("schema_version") or SCHEMA_VERSION,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "capabilities": migrated,
        "migration": {
            "source": "recording_sessions/capabilities.json",
            "migrated_count": len(migrated),
        },
    })


def _find_output_root(session_dir):
    for parent in session_dir.parents:
        if (parent / "catalog.json").exists():
            return parent
    raise FileNotFoundError(
        f"无法从会话定位 recording_sessions 根目录: {session_dir}"
    )
