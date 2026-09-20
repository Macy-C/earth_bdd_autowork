from __future__ import annotations

import ast
import base64
import copy
import hashlib
import json
import os
import secrets
import subprocess
import time
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

import yaml

from config.paths import Paths
from autowork_core.utils.debug_tools.recorder.annotations import (
    annotation_snapshot_is_valid,
    build_annotation_snapshot,
    current_annotation_snapshot_for_request,
)
from autowork_core.utils.debug_tools.recorder.code_manifest import (
    build_code_manifest,
)
from autowork_core.utils.debug_tools.recorder.evidence_context import (
    evidence_item_ids,
    load_evidence_context,
)
from autowork_core.utils.debug_tools.recorder.generation_plan import (
    PLAN_VERSION,
    load_generation_plan,
)
from autowork_core.utils.debug_tools.recorder.generation_contract import (
    generation_contract_lease_matches,
)
from autowork_core.utils.debug_tools.recorder.generation_capsule import (
    generation_capsule_input_snapshot,
    generation_capsule_source_files,
    load_generation_capsule,
)
from autowork_core.utils.debug_tools.recorder.generation_workspace_projection import (
    load_generation_workspace_projection,
)
from autowork_core.utils.debug_tools.recorder.generation_job import (
    generation_job_lease_is_valid,
    load_generation_job,
)
from autowork_core.utils.debug_tools.recorder.generation_job_result import (
    publish_pretransaction_job_failure,
    publish_static_job_outcome,
)
from autowork_core.utils.debug_tools.recorder.generation_diff import (
    build_generation_diff,
    build_generation_diff_summary,
    generation_diff_pointer_is_valid,
    load_generation_diff,
)
from autowork_core.utils.debug_tools.recorder.generation_pic_policy import (
    snapshot_pic_policy,
    validate_generated_pic_usage,
    validate_pic_authorizations,
)
from autowork_core.utils.debug_tools.recorder.generation_policy import (
    snapshot_generation_policy,
    validate_generation_policy,
)
from autowork_core.utils.debug_tools.recorder.generation_file_lock import (
    GenerationFileConflict,
    acquire_generation_file_lease,
    commit_generation_file_lease,
    generation_file_lease_release_is_complete,
    generation_path_has_reparse_point,
    generation_file_lease_write_guard,
    generation_file_lease_publish_guard,
    find_committed_generation_file_lease_report,
    release_generation_file_lease,
    release_generation_file_lease_for_transaction,
    validate_generation_file_lease,
)
from autowork_core.utils.debug_tools.recorder.generation_validation import (
    run_generation_validations,
    snapshot_runtime_variable_calls,
    validate_implementation_resolution_snapshot,
    validate_owner_resolution_snapshot,
    validate_plan_conformance,
)
from autowork_core.utils.debug_tools.recorder.identity import stable_digest
from autowork_core.utils.debug_tools.recorder.implementation_manifest import (
    IMPLEMENTATION_MANIFEST_VERSION,
    build_implementation_packet,
    build_implementation_manifest,
    implementation_manifest_identity_is_valid,
    implementation_manifest_matches_transaction,
)
from autowork_core.utils.debug_tools.recorder.locator_reuse import (
    locator_mapping_fingerprint,
)
from autowork_core.utils.debug_tools.recorder.implementation_materializer import (
    build_implementation_scaffold_candidate,
    implementation_scaffold_candidate_audit,
    implementation_scaffold_candidate_matches,
    implementation_scaffold_candidate_workspace_audit,
    load_implementation_scaffold_candidate,
    persist_implementation_scaffold_candidate,
)
from autowork_core.utils.debug_tools.recorder.implementation_validation_ledger import (
    append_validation_attempt,
    snapshot_ai_editable_files,
    verify_validation_ledger,
)
from autowork_core.utils.debug_tools.recorder.models import SCHEMA_VERSION
from autowork_core.utils.debug_tools.recorder.project_memory import (
    record_transaction_completed,
    snapshot_files,
)
from autowork_core.utils.debug_tools.recorder.request_repository import (
    request_identity_is_valid,
    request_revision_matches,
    session_dir_for_request_path,
)
from autowork_core.utils.debug_tools.recorder.run_lock import RunWriteLock
from autowork_core.utils.debug_tools.recorder.runtime_risk_policy import (
    derive_runtime_risk_policy,
)
from autowork_core.utils.debug_tools.recorder.scope_binding import (
    validate_request_scope_binding,
)
from autowork_core.utils.debug_tools.recorder.reconciliation_repository import (
    load_generation_brief,
)
from autowork_core.utils.debug_tools.recorder.semantic_reconciler import (
    brief_matches_request,
)
from autowork_core.utils.debug_tools.recorder.workflow_state import (
    load_workflow_state,
    transition_generation_job,
)
from autowork_core.utils.debug_tools.recorder.workflow_service import inspect_workflow
from autowork_core.utils.debug_tools.recorder.transaction_integrity import (
    TRANSACTION_VERSION,
    completed_report_fingerprint,
    runtime_code_snapshot_fingerprint,
    transaction_result_fingerprint,
)
from autowork_core.utils.debug_tools.recorder.writer import write_json_atomic


IMPLEMENTATION_RECEIPT_VERSION = "2.1"
HOST_DELIVERY_OBSERVATION_VERSION = "1.0"
STAGE_TIMING_LEDGER_VERSION = "1.0"
STAGE_TIMING_ORDER = (
    "semantic_selection",
    "design",
    "implementation",
    "transaction",
    "runtime",
    "oracle",
)
ALLOWED_WRITE_ROOTS = (
    Path("Bdd/steps"),
    Path("Bdd/page_obj"),
    Path("Bdd/locators"),
    Path("Bdd/data"),
)
PROTECTED_WRITE_ROOTS = (
    Path("autowork_core"),
    Path("config"),
    Path(".github"),
    Path("docs"),
    Path("Bdd/test_features"),
)
PROTECTED_ROOT_FILES = (
    Path("behave.ini"),
    Path("README.md"),
    Path("requirements.txt"),
)
PROJECT_GUARD_EXCLUDED_ROOTS = (
    Path(".git"),
    Path(".copilot/recorder-routing"),
    Path(".copilot/recorder-runtime"),
    Path("artifacts"),
    Path("logs"),
    Path("framework_validation/output"),
    Path("resources/models"),
    Path("resources/ffmpeg"),
)


def prepare_generation_transaction(
        request_path,
        *,
        project_root=None,
        generation_job_lease=None,
        generation_job_claim_id=None,
        generation_job_expected_epoch=None,
    ):
    request_path = Path(request_path).resolve()
    request = _read_json(request_path)
    session_dir = session_dir_for_request_path(request_path, request)
    lock = RunWriteLock(session_dir).acquire()
    try:
        return _prepare_generation_transaction_locked(
            request_path,
            project_root=project_root,
            generation_job_lease=generation_job_lease,
            generation_job_claim_id=generation_job_claim_id,
            generation_job_expected_epoch=generation_job_expected_epoch,
        )
    finally:
        lock.release()


def _prepare_generation_transaction_locked(
        request_path,
        *,
        project_root=None,
    generation_job_lease=None,
    generation_job_claim_id=None,
    generation_job_expected_epoch=None,
    ):
    request_path = Path(request_path).resolve()
    request = _read_json(request_path)
    if request.get("request_version") != "3.0":
        raise ValueError("GenerationTransactionV3 只接受 RequestV3")
    if not request_identity_is_valid(request):
        raise ValueError("RequestV3 完整性校验失败")
    session_dir = session_dir_for_request_path(request_path, request)
    effective_project_root = Path(
        project_root or Paths.BASE_DIR
    ).resolve()
    existing = load_workflow_state(session_dir, request.get("request_id"))
    if not generation_job_lease:
        raise ValueError("当前Workflow必须使用Generation Job prepare入口")
    job_errors = _generation_job_context_errors(
        existing,
        request,
        generation_job_lease,
        claim_id=generation_job_claim_id,
        expected_epoch=generation_job_expected_epoch,
        expected_phase="implementation",
    )
    if job_errors:
        raise ValueError(
            "Generation Job prepare context无效: "
            + "; ".join(job_errors)
        )
    if existing.get("status") == "running":
        active = existing.get("active_transaction") or {}
        path = _transaction_path(session_dir, active.get("path"))
        if path and path.is_file():
            report = _read_json(path)
            if report.get("status") in {"aborting", "aborted"}:
                _complete_aborted_generation_transaction(
                    session_dir,
                    request,
                    existing,
                    path,
                    report,
                    effective_project_root,
                )
                existing = load_workflow_state(
                    session_dir,
                    request.get("request_id"),
                )
            else:
                resumed = _resume_generation_transaction(
                    session_dir,
                    request,
                    existing,
                    path,
                    report,
                    project_root=effective_project_root,
                )
                if resumed.get("status") != "transaction_superseded":
                    return resumed
                existing = resumed.get("workflow_state") or load_workflow_state(
                    session_dir,
                    request.get("request_id"),
                )
                generation_job_expected_epoch = (
                    existing.get("job_execution") or {}
                ).get("epoch")
    try:
        orphan = find_committed_generation_file_lease_report(
            effective_project_root,
            request.get("request_id"),
            generation_job_lease=generation_job_lease,
        )
    except GenerationFileConflict as error:
        return _block_stale(
            session_dir,
            request,
            existing,
            str(error),
        )
    if orphan is not None:
        path, report = orphan
        if report.get("status") in {"aborting", "aborted"}:
            _complete_aborted_generation_transaction(
                session_dir,
                request,
                existing,
                path,
                report,
                effective_project_root,
            )
            existing = load_workflow_state(
                session_dir,
                request.get("request_id"),
            )
        else:
            resumed = _resume_generation_transaction(
                session_dir,
                request,
                existing,
                path,
                report,
                project_root=effective_project_root,
            )
            if resumed.get("status") != "transaction_superseded":
                return resumed
            existing = resumed.get("workflow_state") or load_workflow_state(
                session_dir,
                request.get("request_id"),
            )
            generation_job_expected_epoch = (
                existing.get("job_execution") or {}
            ).get("epoch")
    try:
        if _release_stale_committed_generation_file_lease(
                effective_project_root,
                request,
                generation_job_lease,
        ):
            existing = load_workflow_state(
                session_dir,
                request.get("request_id"),
            )
            generation_job_expected_epoch = (
                existing.get("job_execution") or {}
            ).get("epoch")
    except GenerationFileConflict as error:
        return _block_stale(
            session_dir,
            request,
            existing,
            str(error),
        )
    state = inspect_workflow(
        request_path,
        write=True,
        preserve_transaction=False,
    )
    expected_ready = bool(
        state.get("status") == "running"
        and (state.get("job_execution") or {}).get("phase")
        == "implementation"
    )
    if not expected_ready:
        return {
            "transaction_version": TRANSACTION_VERSION,
            "status": state.get("status"),
            "request_id": request.get("request_id"),
            "request_path": str(request_path),
            "workflow_state": state,
            "errors": state.get("errors") or [],
            "warnings": state.get("warnings") or [],
        }
    plan = load_generation_plan(session_dir, state, request)
    if plan is None:
        return _block_missing_plan(session_dir, request, state)
    if (plan.get("source") or {}).get(
            "generation_job_lease"
    ) != generation_job_lease:
        raise ValueError("Generation Plan与current Job lease不一致")
    contract_lease = (plan.get("source") or {}).get(
        "generation_contract_lease"
    )
    if not generation_contract_lease_matches(
        session_dir,
        contract_lease,
    ):
        return _block_contract_changed(session_dir, request, state)
    brief_path = _resolve_session_artifact(
        session_dir,
        (state.get("brief") or {}).get("path"),
        "generation-briefs",
    )
    brief = load_generation_brief(brief_path)
    if not brief_matches_request(brief, request):
        return _block_stale(session_dir, request, state, "Brief 与 RequestV3 不一致")
    revision_seal = (state.get("revision") or {}).get("seal")
    if (plan.get("source") or {}).get("revision_seal") != revision_seal:
        return _block_stale(session_dir, request, state, "Plan 与当前 revision 不一致")
    project_root = Path(project_root or Paths.BASE_DIR).resolve()
    scope_binding_errors = validate_request_scope_binding(
        request,
        project_root=project_root,
    )
    if scope_binding_errors:
        return _block_stale(
            session_dir,
            request,
            state,
            "; ".join(scope_binding_errors),
        )
    try:
        generation_capsule = _generation_job_capsule(
            session_dir,
            state,
            generation_job_lease,
        )
        generation_input_snapshot = generation_capsule_input_snapshot(
            generation_capsule
        )
        generation_source_files = generation_capsule_source_files(
            generation_capsule
        )
        generation_workspace_projection = _generation_job_workspace_projection(
            session_dir,
            state,
            generation_job_lease,
        )
    except ValueError as error:
        return _block_stale(
            session_dir,
            request,
            state,
            str(error),
        )
    generation_symlinks = _snapshot_symlinks(generation_input_snapshot)
    if generation_symlinks:
        return _block_stale(
            session_dir,
            request,
            state,
            "generation roots包含符号链接: "
            f"{generation_symlinks}",
        )
    resolution_errors, _resolution_warnings = (
        validate_owner_resolution_snapshot(
            project_root,
            ((plan.get("plan") or {}).get("window_owners") or {}),
            brief,
            generation_input_snapshot=generation_input_snapshot,
        )
    )
    if resolution_errors:
        return _block_stale(
            session_dir,
            request,
            state,
            "; ".join(resolution_errors),
        )
    implementation_errors, _implementation_warnings = (
        validate_implementation_resolution_snapshot(
            project_root,
            plan,
            brief,
            generation_input_snapshot=generation_input_snapshot,
            reject_existing_create=True,
        )
    )
    if implementation_errors:
        return _block_stale(
            session_dir,
            request,
            state,
            "; ".join(implementation_errors),
        )
    pic_errors, pic_authorization_audit = validate_pic_authorizations(
        session_dir,
        request,
        plan,
    )
    if pic_errors:
        return _block_pic_policy(
            session_dir,
            request,
            state,
            pic_authorization_audit,
        )
    design_started_at = _now_millis()
    design_started_monotonic = time.monotonic()
    implementation_manifest = build_implementation_manifest(
        plan,
        brief,
        generation_input_snapshot,
        request_id=request.get("request_id"),
        allowed_write_roots=ALLOWED_WRITE_ROOTS,
        protected_write_roots=PROTECTED_WRITE_ROOTS,
        protected_root_files=PROTECTED_ROOT_FILES,
        generation_workspace_projection=generation_workspace_projection,
    )
    design_finished_at = _now_millis()
    design_duration_ms = _elapsed_ms(design_started_monotonic)
    if implementation_manifest.get("status") != "ready":
        return _block_stale(
            session_dir,
            request,
            state,
            "; ".join(
                implementation_manifest.get("errors")
                or ["Implementation Manifest 无法从Plan派生"]
            ),
        )
    git_state_audit = _normalize_git_allowed_change_state(
        project_root,
        implementation_manifest.get("allowed_changes") or (),
    )
    if git_state_audit["errors"]:
        return _block_stale(
            session_dir,
            request,
            state,
            "; ".join(git_state_audit["errors"]),
        )
    if git_state_audit["normalized"]:
        generation_symlinks = _snapshot_symlinks(generation_input_snapshot)
        if generation_symlinks:
            return _block_stale(
                session_dir,
                request,
                state,
                "generation roots包含符号链接: "
                f"{generation_symlinks}",
            )
        resolution_errors, _resolution_warnings = (
            validate_owner_resolution_snapshot(
                project_root,
                ((plan.get("plan") or {}).get("window_owners") or {}),
                brief,
                generation_input_snapshot=generation_input_snapshot,
            )
        )
        if resolution_errors:
            return _block_stale(
                session_dir,
                request,
                state,
                "; ".join(resolution_errors),
            )
        implementation_errors, _implementation_warnings = (
            validate_implementation_resolution_snapshot(
                project_root,
                plan,
                brief,
                generation_input_snapshot=generation_input_snapshot,
                reject_existing_create=True,
            )
        )
        if implementation_errors:
            return _block_stale(
                session_dir,
                request,
                state,
                "; ".join(implementation_errors),
            )
        implementation_manifest = build_implementation_manifest(
            plan,
            brief,
            generation_input_snapshot,
            request_id=request.get("request_id"),
            allowed_write_roots=ALLOWED_WRITE_ROOTS,
            protected_write_roots=PROTECTED_WRITE_ROOTS,
            protected_root_files=PROTECTED_ROOT_FILES,
            generation_workspace_projection=generation_workspace_projection,
        )
        if implementation_manifest.get("status") != "ready":
            return _block_stale(
                session_dir,
                request,
                state,
                "; ".join(
                    implementation_manifest.get("errors")
                    or ["Implementation Manifest 无法从Plan派生"]
                ),
            )
    annotation_lease, annotation_errors = _annotation_lease(
        request,
        plan,
    )
    if annotation_errors:
        return _block_stale(
            session_dir,
            request,
            state,
            "; ".join(annotation_errors),
        )

    now = datetime.now()
    transaction_started_at = now.isoformat(timespec="milliseconds")
    transaction_id = (
        f"transaction-{now.strftime('%Y%m%d-%H%M%S-%f')}-"
        f"{stable_digest(request['request_id'], now.isoformat(), length=8)}"
    )
    candidate, candidate_preflight = _preflight_implementation_candidate(
        project_root,
        implementation_manifest,
        generation_input_snapshot,
        generation_source_files,
        transaction_id=transaction_id,
        request=request,
        plan=plan,
        brief=brief,
        generation_workspace_projection=generation_workspace_projection,
    )
    if candidate_preflight["status"] == "failed":
        reason = next(iter(candidate_preflight["errors"]), None)
        return publish_pretransaction_job_failure(
            session_dir,
            request["request_id"],
            claim_id=generation_job_claim_id,
            expected_epoch=generation_job_expected_epoch,
            expected_phase="implementation",
            category="system_candidate_invalid",
            next_action="review_generation_failure",
            implementation_owner={
                "type": "system_candidate_preflight",
                "status": "failed",
                "reason": reason,
                "errors": list(candidate_preflight["errors"]),
                "candidate_fingerprint": candidate_preflight.get(
                    "candidate_fingerprint"
                ),
            },
        )
    output = (
        session_dir
        / "ai"
        / "generation-transactions"
        / transaction_id
        / "report.json"
    )
    protected_input_snapshot = _snapshot_protected_paths(project_root)
    project_guard_snapshot = _snapshot_project_guard(project_root)
    project_guard_symlinks = _snapshot_symlinks(project_guard_snapshot)
    if project_guard_symlinks:
        return _block_stale(
            session_dir,
            request,
            state,
            "project guard包含符号链接: "
            f"{project_guard_symlinks}",
        )
    report = {
        "schema_version": SCHEMA_VERSION,
        "transaction_version": TRANSACTION_VERSION,
        "transaction_id": transaction_id,
        "transaction_nonce": secrets.token_hex(16),
        "status": "running",
        "started_at": now.isoformat(timespec="seconds"),
        "completed_at": None,
        "stage_timing_ledger": _new_stage_timing_ledger(
            transaction_started_at=transaction_started_at,
            design_started_at=design_started_at,
            design_finished_at=design_finished_at,
            design_duration_ms=design_duration_ms,
        ),
        "request_id": request.get("request_id"),
        "request_path": str(request_path),
        "session_dir": str(session_dir),
        "project_root": str(project_root),
        "target": request.get("target") or {},
        "evidence_fingerprint": request.get("evidence_fingerprint"),
        "generation_contract_lease": contract_lease,
        "generation_job_lease": generation_job_lease,
        "generation_job_claim_id": generation_job_claim_id,
        "lease": {
            "revision": state.get("revision") or {},
            "brief": state.get("brief") or {},
            "plan": state.get("plan") or {},
            "annotation": annotation_lease,
        },
        "generation_brief": {
            **(state.get("brief") or {}),
            "path": str(brief_path),
        },
        "brief_path": str(brief_path),
        "generation_plan": {
            **(state.get("plan") or {}),
            "path": plan.get("plan_path") or (
                session_dir / (state.get("plan") or {})["path"]
            ).resolve().as_posix(),
        },
        "plan_path": str(
            (session_dir / (state.get("plan") or {})["path"]).resolve()
        ),
        "implementation_summary": _implementation_summary(plan),
        **(
            {"generation_workspace_projection": copy.deepcopy(
                (load_generation_job(
                    session_dir,
                    state.get("current_job") or {},
                ) or {}).get("generation_workspace_projection") or {}
            )}
            if generation_workspace_projection
            else {}
        ),
        "implementation_manifest": implementation_manifest,
        "implementation_packet": build_implementation_packet(
            implementation_manifest
        ),
        "candidate_preflight": candidate_preflight,
        "system_materialization": {"status": "pending"},
        "risk": state.get("risk") or {},
        "allowed_write_roots": [path.as_posix() for path in ALLOWED_WRITE_ROOTS],
        "generation_policy_baseline": snapshot_generation_policy(project_root),
        "generation_input_snapshot": generation_input_snapshot,
        "protected_input_snapshot": protected_input_snapshot,
        "project_guard_snapshot": project_guard_snapshot,
        "changed_files": [],
        "validations": {},
        "required_validations": [],
        "summary": "",
        "decision_trace": {},
        "evidence_audit": {"status": "pending", "decision_coverage": None},
        "generation_policy_audit": {"status": "pending"},
        "pic_authorization_audit": pic_authorization_audit,
        "pic_policy_baseline": snapshot_pic_policy(project_root),
        "pic_usage_audit": {"status": "pending"},
        "plan_conformance_audit": {"status": "pending"},
        "code_manifest": None,
        "implementation_diff": None,
        "lease_revision_audit": {"status": "pending"},
        "annotation_lease_audit": {"status": "pending"},
        "implementation_snapshot": [],
        "runtime_code_snapshot": None,
        "runtime_code_snapshot_fingerprint": None,
        "project_memory": {"recorded": [], "warnings": []},
        "errors": [],
        "warnings": [],
    }
    write_json_atomic(output, report)
    try:
        report["generation_file_lease"] = acquire_generation_file_lease(
            project_root,
            transaction_id=transaction_id,
            request_id=request.get("request_id"),
            report_path=output,
            target_files=implementation_manifest.get("allowed_changes") or (),
        )
        write_json_atomic(output, report)
        report["generation_file_lease"] = commit_generation_file_lease(
            project_root,
            report["generation_file_lease"],
        )
        write_json_atomic(output, report)
        try:
            report["generation_baseline"] = _capture_generation_baseline(
                project_root,
                implementation_manifest,
                generation_input_snapshot,
                lease=report["generation_file_lease"],
                output_path=output.parent / "generation-baseline.json",
                transaction_id=transaction_id,
            )
        except ValueError as error:
            release_generation_file_lease(
                project_root,
                report.get("generation_file_lease"),
            )
            output.unlink(missing_ok=True)
            return _block_commit_rebase_required(
                session_dir,
                request,
                state,
                str(error),
            )
        write_json_atomic(output, report)
        candidate_pointer = persist_implementation_scaffold_candidate(
            session_dir,
            transaction_id,
            candidate,
            output_dir=output.parent,
        )
        report["system_materialization"] = (
            implementation_scaffold_candidate_audit(
                candidate,
                candidate_pointer,
            )
        )
        write_json_atomic(output, report)
    except GenerationFileConflict as error:
        output.unlink(missing_ok=True)
        return _block_stale(session_dir, request, state, str(error))
    except Exception:
        release_generation_file_lease(
            project_root,
            report.get("generation_file_lease"),
        )
        output.unlink(missing_ok=True)
        raise
    pointer = _transaction_pointer(
        report,
        output.relative_to(session_dir).as_posix(),
    )
    try:
        transition_generation_job(
            session_dir,
            request["request_id"],
            job_id=generation_job_lease["job_id"],
            job_fingerprint=generation_job_lease["job_fingerprint"],
            claim_id=generation_job_claim_id,
            expected_epoch=generation_job_expected_epoch,
            expected_phase="implementation",
            phase="implementation",
            next_action="validate_generation_implementation",
            transaction=pointer,
        )
    except Exception:
        release_generation_file_lease(
            project_root,
            report.get("generation_file_lease"),
        )
        output.unlink(missing_ok=True)
        raise
    report["report_path"] = str(output)
    return report


def _resume_generation_transaction(
        session_dir,
        request,
        state,
        report_path,
        report,
        *,
        project_root,
):
    errors = []
    frozen_root = Path(report.get("project_root") or "").resolve()
    if frozen_root != Path(project_root).resolve():
        errors.append("GenerationTransaction project_root 已变化")
    if report.get("request_id") != request.get("request_id"):
        errors.append("GenerationTransaction request_id 与当前Request不一致")

    if report.get("status") == "running":
        protocol_reason = _running_transaction_protocol_refresh_reason(
            session_dir,
            request,
            state,
            report,
        )
        if not errors and protocol_reason:
            return _supersede_running_generation_transaction(
                session_dir,
                request,
                state,
                report_path,
                report,
                project_root=project_root,
                reason=protocol_reason,
            )
        try:
            _validate_report_identity(report_path, report)
        except ValueError as error:
            errors.append(str(error))
        if (
            not errors
            and not generation_contract_lease_matches(
                session_dir,
                report.get("generation_contract_lease"),
            )
        ):
            return _supersede_running_generation_transaction(
                session_dir,
                request,
                state,
                report_path,
                report,
                project_root=project_root,
                reason="generation_contract_changed",
            )
        if not errors and _running_manifest_is_outdated(
                session_dir,
                request,
                state,
                report,
        ):
            return _supersede_running_generation_transaction(
                session_dir,
                request,
                state,
                report_path,
                report,
                project_root=project_root,
                reason="implementation_manifest_outdated",
            )
        if not errors and _project_guard_changed_paths(
                report.get("project_guard_snapshot") or {},
                _snapshot_project_guard(project_root),
        ):
            return _supersede_running_generation_transaction(
                session_dir,
                request,
                state,
                report_path,
                report,
                project_root=project_root,
                reason="project_guard_changed",
            )
        try:
            report["generation_file_lease"] = commit_generation_file_lease(
                project_root,
                report.get("generation_file_lease"),
            )
            write_json_atomic(report_path, report)
        except (TypeError, ValueError) as error:
            errors.append(str(error))
        errors.extend(validate_generation_file_lease(
            project_root,
            report.get("generation_file_lease"),
        ))
        try:
            candidate = load_implementation_scaffold_candidate(
                session_dir,
                (report.get("system_materialization") or {}).get(
                    "candidate"
                ),
                transaction_id=report.get("transaction_id"),
            )
            expected_audit = implementation_scaffold_candidate_audit(
                candidate,
                (report.get("system_materialization") or {}).get(
                    "candidate"
                ),
            )
            if report.get("system_materialization") != expected_audit:
                raise ValueError("Implementation candidate audit mismatch")
        except (OSError, TypeError, ValueError) as error:
            errors.append(
                "Implementation candidate recovery failed: "
                f"{type(error).__name__}: {error}"
            )
        if errors:
            return _block_stale(
                session_dir,
                request,
                state,
                "; ".join(errors),
            )
        pointer = _transaction_pointer(
            report,
            report_path.relative_to(session_dir).as_posix(),
        )
        if (state.get("active_transaction") or {}) != pointer:
            return _block_stale(
                session_dir,
                request,
                state,
                "Generation Job active Transaction pointer不一致",
            )
        report["report_path"] = str(report_path)
        return report

    try:
        _validate_terminal_report_identity(report_path, report)
    except ValueError as error:
        errors.append(str(error))
    if errors:
        return _block_stale(
            session_dir,
            request,
            state,
            "; ".join(errors),
        )
    try:
        with generation_file_lease_publish_guard(
            project_root,
            report.get("generation_file_lease"),
        ):
            report = _finalize_terminal_snapshot(
                report_path,
                report,
                project_root,
            )
            _transition_terminal_workflow(
                session_dir,
                request["request_id"],
                report_path,
                report,
            )
    except (TypeError, ValueError) as error:
        return _block_stale(
            session_dir,
            request,
            state,
            str(error),
        )
    report["report_path"] = str(report_path)
    return report


def _running_transaction_protocol_refresh_reason(
        session_dir,
        request,
        state,
        report,
    ):
    execution = state.get("job_execution") or {}
    context_errors = _generation_job_context_errors(
        state,
        request,
        report.get("generation_job_lease") or {},
        claim_id=execution.get("claim_id"),
        expected_epoch=execution.get("epoch"),
        expected_phase="implementation",
    )
    if any((
            context_errors,
            report.get("generation_job_claim_id")
            != execution.get("claim_id"),
    )):
        return None
    if report.get("transaction_version") != TRANSACTION_VERSION:
        return "transaction_protocol_changed"
    if not generation_contract_lease_matches(
            session_dir,
            report.get("generation_contract_lease"),
    ):
        return "generation_contract_changed"
    if not implementation_manifest_identity_is_valid(
            report.get("implementation_manifest")
    ):
        return "implementation_manifest_protocol_changed"
    return None


def _running_manifest_is_outdated(session_dir, request, state, report):
    brief, plan, artifact_errors = _load_frozen_artifacts(
        session_dir,
        request,
        state,
        report,
    )
    if artifact_errors:
        return False
    return not implementation_manifest_matches_transaction(
        report.get("implementation_manifest"),
        plan,
        brief,
        report.get("generation_input_snapshot") or {},
        request_id=request.get("request_id"),
        allowed_write_roots=ALLOWED_WRITE_ROOTS,
        protected_write_roots=PROTECTED_WRITE_ROOTS,
        protected_root_files=PROTECTED_ROOT_FILES,
        generation_workspace_projection=_report_workspace_projection(
            session_dir,
            report,
        ),
    )


def _supersede_running_generation_transaction(
        session_dir,
        request,
        state,
        report_path,
        report,
        *,
        project_root,
        reason,
    ):
    report = dict(report)
    superseded_at = datetime.now().isoformat(timespec="seconds")
    report["status"] = "superseded"
    report["superseded_at"] = superseded_at
    report["superseded_reason"] = str(reason or "outdated_transaction")
    report["completion_fingerprint"] = None
    report["result_fingerprint"] = transaction_result_fingerprint(report)
    write_json_atomic(report_path, report)
    release_generation_file_lease(
        project_root,
        report.get("generation_file_lease"),
    )
    execution = state.get("job_execution") or {}
    job_lease = report.get("generation_job_lease") or {}
    transition_generation_job(
        session_dir,
        request["request_id"],
        job_id=job_lease.get("job_id"),
        job_fingerprint=job_lease.get("job_fingerprint"),
        claim_id=execution.get("claim_id"),
        expected_epoch=execution.get("epoch"),
        expected_phase="implementation",
        phase="implementation",
        next_action="prepare_generation_transaction",
        clear_active_transaction=True,
    )
    return {
        "transaction_version": TRANSACTION_VERSION,
        "status": "transaction_superseded",
        "request_id": request.get("request_id"),
        "transaction_id": report.get("transaction_id"),
        "superseded_reason": report["superseded_reason"],
        "workflow_state": load_workflow_state(
            session_dir,
            request.get("request_id"),
        ),
        "errors": [],
        "warnings": [],
    }


def _release_stale_committed_generation_file_lease(
        project_root,
        request,
        generation_job_lease,
    ):
    orphan = find_committed_generation_file_lease_report(
        project_root,
        request.get("request_id"),
    )
    if orphan is None:
        return False
    report_path, report = orphan
    if (report.get("generation_job_lease") or {}) == generation_job_lease:
        return False
    reason = _stale_committed_lease_release_reason(
        report,
        generation_job_lease,
    )
    if reason is None:
        return False
    _supersede_committed_generation_transaction_report(
        project_root,
        report_path,
        report,
        reason=reason,
    )
    return True


def _stale_committed_lease_release_reason(report, generation_job_lease):
    if report.get("status") != "running":
        return None
    if (
            report.get("transaction_version") != TRANSACTION_VERSION
            or not report.get("candidate_preflight")
    ):
        return "transaction_protocol_changed"
    if (report.get("generation_job_lease") or {}) != generation_job_lease:
        return "replaced_generation_job"
    return None


def _supersede_committed_generation_transaction_report(
        project_root,
        report_path,
        report,
        *,
        reason,
    ):
    report = dict(report)
    superseded_at = datetime.now().isoformat(timespec="seconds")
    report["status"] = "superseded"
    report["superseded_at"] = superseded_at
    report["superseded_reason"] = str(reason or "outdated_transaction")
    report["completion_fingerprint"] = None
    report["result_fingerprint"] = transaction_result_fingerprint(report)
    write_json_atomic(report_path, report)
    release_generation_file_lease(
        project_root,
        report.get("generation_file_lease"),
    )


def _transaction_pointer(report, relative_path):
    return {
        "transaction_id": report.get("transaction_id"),
        "path": str(relative_path),
        "revision_seal": (


            (report.get("lease") or {}).get("revision") or {}
        ).get("seal"),
        "plan_fingerprint": (
            report.get("generation_plan") or {}
        ).get("plan_fingerprint"),
        "implementation_manifest_fingerprint": (
            report.get("implementation_manifest") or {}
        ).get("implementation_manifest_fingerprint"),
        "annotation_snapshot_fingerprint": (
            (report.get("lease") or {}).get("annotation") or {}
        ).get("snapshot_fingerprint"),
        "generation_job_lease_fingerprint": (
            report.get("generation_job_lease") or {}
        ).get("lease_fingerprint"),
    }


def _generation_job_context_errors(
        state,
        request,
        lease,
        *,
        claim_id,
        expected_epoch,


        expected_phase,
    ):
    if not generation_job_lease_is_valid(lease):
        return ["job_lease_invalid"]
    pointer = state.get("current_job") or {}
    execution = state.get("job_execution") or {}
    checks = {
        "workflow_version": state.get("workflow_state_version"),
        "request_id": lease.get("request_id") == request.get("request_id"),


        "job_id": lease.get("job_id") == pointer.get("job_id"),
        "job_fingerprint": lease.get("job_fingerprint")
        == pointer.get("job_fingerprint"),
        "job_nonce": lease.get("job_nonce") == pointer.get("nonce"),
        "profile_fingerprint": lease.get("profile_fingerprint")
        == pointer.get("profile_lease_fingerprint"),
        "claim_id": bool(claim_id)
        and claim_id == execution.get("claim_id"),
        "epoch": isinstance(expected_epoch, int)
        and expected_epoch == execution.get("epoch"),
        "phase": execution.get("phase") == expected_phase,
        "status": state.get("status") == "running",
    }
    return [name for name, passed in checks.items() if not passed]


def _generation_job_capsule(session_dir, state, lease):
    pointer = state.get("current_job") or {}
    job = load_generation_job(session_dir, pointer)
    if job is None:
        raise ValueError("Generation Job identity invalid")
    if (job.get("generation_capsule") or {}).get(
            "capsule_fingerprint"
    ) != lease.get("generation_capsule_fingerprint"):
        raise ValueError("Generation Job lease capsule mismatch")
    return load_generation_capsule(
        session_dir,
        job.get("generation_capsule") or {},
    )


def _generation_job_workspace_projection(session_dir, state, lease):
    pointer = state.get("current_job") or {}
    job = load_generation_job(session_dir, pointer)
    if job is None:
        raise ValueError("Generation Job identity invalid")
    if (job.get("generation_capsule") or {}).get(
            "capsule_fingerprint"
    ) != lease.get("generation_capsule_fingerprint"):
        raise ValueError("Generation Job lease capsule mismatch")
    projection_pointer = job.get("generation_workspace_projection") or {}
    if not projection_pointer:
        return None
    projection = load_generation_workspace_projection(
        session_dir,
        projection_pointer,
    )
    if projection.get("generation_input_snapshot_fingerprint") != (
            (job.get("generation_capsule") or {}).get(
                "generation_input_snapshot_fingerprint"
            )
    ):
        raise ValueError("Generation workspace projection与Capsule snapshot不一致")
    return projection


def _report_workspace_projection(session_dir, report):
    pointer = (report or {}).get("generation_workspace_projection") or {}
    if not pointer:
        return None
    return load_generation_workspace_projection(session_dir, pointer)


def _generation_job_input_snapshot(session_dir, state, lease):
    return generation_capsule_input_snapshot(
        _generation_job_capsule(session_dir, state, lease)
    )


def _preflight_implementation_candidate(
        project_root,
        manifest,
        generation_input_snapshot,
        source_files,
        *,
        transaction_id,
        request,
        plan,
        brief,
        generation_workspace_projection=None,
    ):
    allowed = sorted(manifest.get("allowed_changes") or ())
    ai_editable = sorted(manifest.get("ai_editable_changes") or ())
    ai_candidate_files = _plan_implementation_candidate_files(
        plan,
        ai_editable,
    )
    if set(ai_candidate_files) != set(ai_editable):
        candidate = build_implementation_scaffold_candidate(
            project_root,
            manifest,
            generation_input_snapshot,
            transaction_id=transaction_id,
        )
        return candidate, {
            "candidate_preflight_version": "1.0",
            "status": "not_applicable",
            "reason": "candidate_bundle_missing_ai_content",
            "candidate_fingerprint": candidate.get("candidate_fingerprint"),
            "file_count": len(candidate.get("files") or ()),
            "required_validations": [],
            "validations": {},
            "plan_conformance_audit": {
                "status": "not_evaluated",
                "checked_operations": 0,
            },
            "errors": [],
        }
    complete_candidate = allowed == sorted(
        set(manifest.get("system_owned_changes") or ())
        | set(ai_candidate_files)
    )
    errors = []
    candidate = None
    validations = {}
    required_validations = []
    plan_audit = {
        "status": "not_evaluated",
        "checked_operations": 0,
    }
    with TemporaryDirectory(prefix="bdd-autowork-candidate-preflight-") as value:
        staging_root = Path(value).resolve()
        try:
            _materialize_candidate_preflight_workspace_slice(
                staging_root,
                project_root,
                manifest,
                generation_input_snapshot,
                source_files,
                generation_workspace_projection,
            )
            candidate = build_implementation_scaffold_candidate(
                staging_root,
                manifest,
                generation_input_snapshot,
                transaction_id=transaction_id,
                candidate_files=[
                    ai_candidate_files[path]
                    for path in sorted(ai_candidate_files)
                ],
            )
            _materialize_candidate_preflight_workspace_slice(
                staging_root,
                project_root,
                manifest,
                generation_input_snapshot,
                [],
                generation_workspace_projection,
                candidate=candidate,
            )
            if not complete_candidate:
                return candidate, {
                    "candidate_preflight_version": "1.0",
                    "status": "not_applicable",
                    "reason": "candidate_bundle_is_not_complete",
                    "candidate_fingerprint": candidate.get(
                        "candidate_fingerprint"
                    ),
                    "file_count": len(candidate.get("files") or ()),
                    "required_validations": [],
                    "validations": {},
                    "plan_conformance_audit": {
                        "status": "not_evaluated",
                        "checked_operations": 0,
                    },
                    "errors": [],
                }
            if sorted(candidate.get("system_owned_files") or ()) != allowed:
                errors.append(
                    "Implementation candidate files do not cover the complete "
                    "allowed change scope"
                )
            _materialize_candidate_preflight_files(staging_root, candidate)
            changed = list(candidate.get("system_owned_files") or ())
            target = request.get("target") or {}
            source_feature = (target.get("feature") or {}).get(
                "source_relpath"
            )
            required_validations = _required_validations(changed)
            if not source_feature:
                required_validations = [
                    name for name in required_validations
                    if name != "step_scope"
                ]
            validations = run_generation_validations(
                staging_root,
                changed,
                source_feature=source_feature,
                plan_artifact=plan,
                target_steps=target.get("steps") or [],
                target_scenario=target.get("scenario") or {},
            )
            for name in required_validations:
                result = validations.get(name) or {}
                if result.get("status") != "passed":
                    details = list(result.get("errors") or ())
                    errors.append(
                        f"Candidate preflight {name} failed"
                        + (f": {'; '.join(details)}" if details else "")
                    )
            plan_errors, plan_audit = validate_plan_conformance(
                staging_root,
                changed,
                plan,
                request=request,
                brief=brief,
                generation_input_snapshot=generation_input_snapshot,
            )
            errors.extend(
                f"Candidate preflight Plan-to-Code: {error}"
                for error in plan_errors
            )
        except (OSError, TypeError, ValueError) as error:
            errors.append(
                "Candidate preflight could not be completed: "
                f"{type(error).__name__}: {error}"
            )
    audit = {
        "candidate_preflight_version": "1.0",
        "status": "failed" if errors else "passed",
        "candidate_fingerprint": (
            (candidate or {}).get("candidate_fingerprint")
        ),
        "file_count": len((candidate or {}).get("files") or ()),
        "required_validations": required_validations,
        "validations": validations,
        "plan_conformance_audit": plan_audit,
        "errors": errors,
    }
    return candidate, audit


def _plan_implementation_candidate_files(plan_artifact, ai_editable):
    plan = (plan_artifact or {}).get("plan") or {}
    files = {}
    for item in plan.get("implementation_candidate_files") or ():
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        if path:
            files[path] = dict(item)
    return {
        path: files[path]
        for path in sorted(set(ai_editable or ()))
        if path in files
    }


def _materialize_candidate_preflight_sources(staging_root, source_files):
    for source in source_files or ():
        if not isinstance(source, dict) or source.get("is_symlink") is not False:
            raise ValueError("Candidate preflight source must be a regular file")
        relative = _candidate_preflight_relative_path(source.get("path"))
        encoding = source.get("content_encoding")
        if encoding == "utf-8":
            content = str(source.get("content") or "").encode("utf-8")
        elif encoding == "base64":
            content = base64.b64decode(
                str(source.get("content_base64") or ""),
                validate=True,
            )
        else:
            raise ValueError(
                f"Candidate preflight source encoding is invalid: {relative}"
            )
        if hashlib.sha256(content).hexdigest() != source.get("sha256"):
            raise ValueError(
                f"Candidate preflight source hash mismatch: {relative}"
            )
        path = staging_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def _materialize_candidate_preflight_workspace_slice(
        staging_root,
        project_root,
        manifest,
        generation_input_snapshot,
        source_files,
        generation_workspace_projection,
        *,
        candidate=None,
    ):
    _materialize_candidate_preflight_sources(staging_root, source_files)
    projection_required = bool(
        generation_workspace_projection
        or (manifest.get("current_content_projection") or {})
    )
    if not projection_required:
        return
    if not isinstance(generation_workspace_projection, dict):
        raise ValueError("Candidate preflight workspace projection missing")
    _validate_preflight_workspace_projection(
        manifest,
        generation_input_snapshot,
        generation_workspace_projection,
    )
    paths = _preflight_workspace_slice_paths(
        manifest,
        generation_input_snapshot,
        generation_workspace_projection,
        candidate=candidate,
    )
    _materialize_preflight_snapshot_paths(
        staging_root,
        project_root,
        generation_input_snapshot,
        paths,
    )


def _validate_preflight_workspace_projection(
        manifest,
        generation_input_snapshot,
        generation_workspace_projection,
    ):
    expected = _fingerprint(generation_input_snapshot)
    observed = generation_workspace_projection.get(
        "generation_input_snapshot_fingerprint"
    )
    manifest_projection = manifest.get("current_content_projection") or {}
    if observed != expected:
        raise ValueError(
            "Candidate preflight workspace projection与input snapshot不一致"
        )
    if manifest_projection and any((
            manifest_projection.get("projection_fingerprint")
            != generation_workspace_projection.get("projection_fingerprint"),
            manifest_projection.get("generation_input_snapshot_fingerprint")
            != observed,
    )):
        raise ValueError(
            "Candidate preflight workspace projection与Manifest不一致"
        )


def _preflight_workspace_slice_paths(
        manifest,
        generation_input_snapshot,
        generation_workspace_projection,
        *,
        candidate=None,
    ):
    snapshot_files = (generation_input_snapshot or {}).get("files") or {}
    paths = set()
    for value in manifest.get("read_only_reuse") or ():
        paths.add(_candidate_preflight_relative_path(value).as_posix())
    for record in manifest.get("files") or ():
        if not isinstance(record, dict):
            continue
        path = _candidate_preflight_relative_path(record.get("path")).as_posix()
        if record.get("strategy") == "reuse" or path in snapshot_files:
            paths.add(path)
    candidate_paths = {
        _candidate_preflight_relative_path(item.get("path")).as_posix()
        for item in (candidate or {}).get("files") or ()
        if isinstance(item, dict) and item.get("path")
    }
    imported_paths = _candidate_local_import_paths(candidate)
    paths.update(path for path in imported_paths if path in snapshot_files)
    paths.update(_required_projection_package_markers(
        generation_workspace_projection,
        set(paths) | candidate_paths | imported_paths,
    ))
    paths.difference_update(candidate_paths)
    return sorted(paths)


def _required_projection_package_markers(
        generation_workspace_projection,
        relevant_paths,
    ):
    markers = (
        (generation_workspace_projection.get("required_structure_files") or {})
        .get("package_markers") or []
    )
    result = []
    for marker in markers:
        if not isinstance(marker, dict) or marker.get("status") != "present":
            continue
        path = _candidate_preflight_relative_path(marker.get("path")).as_posix()
        parent = str(PurePosixPath(path).parent)
        required_by = {
            str(item).replace("\\", "/")
            for item in marker.get("required_by") or []
        }
        if required_by & relevant_paths or any(
                str(item).startswith(parent + "/")
                for item in relevant_paths
        ):
            result.append(path)
    return result


def _candidate_local_import_paths(candidate):
    result = set()
    for item in (candidate or {}).get("files") or ():
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        if not path.endswith(".py"):
            continue
        try:
            tree = ast.parse(str(item.get("content") or ""), path)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    result.update(_module_candidate_paths(alias.name))
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                result.update(_module_candidate_paths(node.module or ""))
    return result


def _module_candidate_paths(module):
    module = str(module or "")
    if not module.startswith("Bdd."):
        return set()
    path = module.replace(".", "/")
    return {path + ".py", path + "/__init__.py"}


def _materialize_preflight_snapshot_paths(
        staging_root,
        project_root,
        generation_input_snapshot,
        paths,
    ):
    snapshot_files = (generation_input_snapshot or {}).get("files") or {}
    for relative in sorted(set(paths or [])):
        baseline = snapshot_files.get(relative)
        if not isinstance(baseline, dict):
            raise ValueError(
                "Candidate preflight workspace projection path missing from snapshot: "
                f"{relative}"
            )
        if baseline.get("is_symlink") is True:
            raise ValueError(
                "Candidate preflight workspace projection path is symlink: "
                f"{relative}"
            )
        expected_sha256 = baseline.get("sha256")
        expected_size = baseline.get("size")
        if not isinstance(expected_sha256, str) or not isinstance(expected_size, int):
            raise ValueError(
                "Candidate preflight workspace projection snapshot invalid: "
                f"{relative}"
            )
        source_path = _generation_target_path(project_root, relative)
        content = _preflight_workspace_slice_content(source_path, relative)
        _verify_preflight_workspace_slice_content(
            relative,
            content,
            expected_sha256,
            expected_size,
        )
        staged_path = staging_root / _candidate_preflight_relative_path(relative)
        if staged_path.exists() or staged_path.is_symlink():
            staged_content = _preflight_workspace_slice_content(
                staged_path,
                relative,
            )
            _verify_preflight_workspace_slice_content(
                relative,
                staged_content,
                expected_sha256,
                expected_size,
            )
            continue
        staged_path.parent.mkdir(parents=True, exist_ok=True)
        staged_path.write_bytes(content)


def _preflight_workspace_slice_content(path, relative):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(
            "Candidate preflight workspace projection path changed type: "
            f"{relative}"
        )
    if not path.is_file():
        raise ValueError(
            "Candidate preflight workspace projection path missing after freeze: "
            f"{relative}"
        )
    return path.read_bytes()


def _verify_preflight_workspace_slice_content(
        relative,
        content,
        expected_sha256,
        expected_size,
    ):
    if hashlib.sha256(content).hexdigest() != expected_sha256 or len(content) != expected_size:
        raise ValueError(
            "Candidate preflight workspace projection path drifted: "
            f"{relative}"
        )


def _fingerprint(value):
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _materialize_candidate_preflight_files(staging_root, candidate):
    for item in (candidate or {}).get("files") or ():
        relative = _candidate_preflight_relative_path(item.get("path"))
        content = str(item.get("content") or "").encode("utf-8")
        if hashlib.sha256(content).hexdigest() != item.get("sha256"):
            raise ValueError(
                f"Candidate preflight file hash mismatch: {relative}"
            )
        path = staging_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def _candidate_preflight_relative_path(value):
    path = Path(str(value or "").replace("\\", "/"))
    if not str(value or "") or path.is_absolute() or ".." in path.parts:
        raise ValueError("Candidate preflight path is invalid")
    return path


def _now_millis():
    return datetime.now().isoformat(timespec="milliseconds")


def _elapsed_ms(started_monotonic):
    return max(0, int((time.monotonic() - started_monotonic) * 1000))


def _new_stage_timing_ledger(
        *,
        transaction_started_at,
        design_started_at,
        design_finished_at,
        design_duration_ms,
):
    return {
        "stage_timing_ledger_version": STAGE_TIMING_LEDGER_VERSION,
        "stages": {
            "semantic_selection": {
                "source": "independent_oracle_required",
            },
            "design": {
                "source": "implementation_manifest",
                "started_at": design_started_at,
                "finished_at": design_finished_at,
                "duration_ms": int(design_duration_ms),
            },
            "implementation": {
                "source": "implementation_validation_ledger",
            },
            "transaction": {
                "source": "generation_transaction",
                "started_at": transaction_started_at,
            },
            "runtime": {
                "source": "bound_run_result",
            },
            "oracle": {
                "source": "independent_business_oracle",
            },
        },
    }


def _update_stage_timing(
        report,
        stage,
        *,
        source,
        started_at=None,
        finished_at=None,
        duration_ms=None,
):
    ledger = _stage_timing_ledger(report)
    stages = ledger["stages"]
    current = stages.get(stage)
    entry = dict(current if isinstance(current, dict) else {})
    entry["source"] = str(source or entry.get("source") or "")
    if started_at is not None:
        entry["started_at"] = str(started_at)
    if finished_at is not None:
        entry["finished_at"] = str(finished_at)
    if duration_ms is not None:
        entry["duration_ms"] = max(0, int(duration_ms))
    stages[str(stage)] = entry
    return ledger


def _stage_timing_ledger(report):
    existing = report.get("stage_timing_ledger") or {}
    existing_stages = (
        existing.get("stages")
        if isinstance(existing, dict)
        else {}
    )
    stages = dict(existing_stages if isinstance(existing_stages, dict) else {})
    defaults = _new_stage_timing_ledger(
        transaction_started_at=str(report.get("started_at") or ""),
        design_started_at="",
        design_finished_at="",
        design_duration_ms=0,
    )["stages"]
    ordered = {}
    for name in STAGE_TIMING_ORDER:
        current = stages.get(name)
        ordered[name] = {
            **defaults[name],
            **(current if isinstance(current, dict) else {}),
        }
    return {
        "stage_timing_ledger_version": STAGE_TIMING_LEDGER_VERSION,
        "stages": ordered,
    }


def _complete_transaction_timing(report, finished_at):
    stages = (report.get("stage_timing_ledger") or {}).get("stages") or {}
    transaction = stages.get("transaction") or {}
    started_at = transaction.get("started_at") or report.get("started_at")
    return _update_stage_timing(
        report,
        "transaction",
        source="generation_transaction",
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=_duration_between_ms(started_at, finished_at),
    )


def _duration_between_ms(started_at, finished_at):
    try:
        started = datetime.fromisoformat(str(started_at))
        finished = datetime.fromisoformat(str(finished_at))
    except (TypeError, ValueError):
        return None
    return max(0, int((finished - started).total_seconds() * 1000))


def _transition_terminal_workflow(
        session_dir,
        request_id,
        report_path,
        report,
):
    return publish_static_job_outcome(
        session_dir,
        request_id,
        report_path,
        report,
    )


def finish_generation_transaction(
        report_path,
        *,
    changed_files=None,
    derive_changed_files=False,
    validate_only=False,
        summary="",
        project_root=None,
    generation_job_claim_id=None,
    generation_job_expected_epoch=None,
):
    report_path = Path(report_path).resolve()
    session_dir = _session_dir_for_transaction_report_path(report_path)
    lock = RunWriteLock(session_dir).acquire()
    try:
        try:
            return _finish_generation_transaction_locked(
                report_path,
                changed_files=changed_files,
                derive_changed_files=derive_changed_files,
                validate_only=validate_only,
                summary=summary,
                project_root=project_root,
                generation_job_claim_id=generation_job_claim_id,
                generation_job_expected_epoch=(
                    generation_job_expected_epoch
                ),
            )
        except Exception:
            if validate_only:
                raise
            report = _read_json(report_path)
            _fail_running_transaction_after_exception(
                report_path,
                report,
                project_root=project_root,
            )
            raise
    finally:
        lock.release()


def abort_generation_transaction(
        report_path,
        *,
        reason,
        project_root=None,
    generation_job_claim_id=None,
    generation_job_expected_epoch=None,
    allow_project_guard_drift=False,
    ):
    report_path = Path(report_path).resolve()
    session_dir = _session_dir_for_transaction_report_path(report_path)
    lock = RunWriteLock(session_dir).acquire()
    try:
        return _abort_generation_transaction_locked(
            report_path,
            reason=reason,
            project_root=project_root,
            generation_job_claim_id=generation_job_claim_id,
            generation_job_expected_epoch=generation_job_expected_epoch,
            allow_project_guard_drift=allow_project_guard_drift,
        )
    finally:
        lock.release()


def _abort_generation_transaction_locked(
        report_path,
        *,
        reason,
        project_root=None,
    generation_job_claim_id=None,
    generation_job_expected_epoch=None,
    allow_project_guard_drift=False,
    allow_generation_root_drift=False,
    ):
    report_path = Path(report_path).resolve()
    session_dir = _session_dir_for_transaction_report_path(report_path)
    report = _read_json(report_path)
    if report.get("status") in {"aborting", "aborted"}:
        _validate_terminal_report_identity(report_path, report)
        request_path = Path(report["request_path"]).resolve()
        request = _read_json(request_path)
        state = load_workflow_state(
            session_dir,
            request.get("request_id"),
        )
        root = Path(
            project_root or report.get("project_root") or ""
        ).resolve()
        return _complete_aborted_generation_transaction(
            session_dir,
            request,
            state,
            report_path,
            report,
            root,
        )
    _validate_report_identity(report_path, report)
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("Abort reason is required")
    if len(reason) > 500:
        raise ValueError("Abort reason exceeds 500 characters")
    request_path = Path(report["request_path"]).resolve()
    request = _read_json(request_path)
    if not request_identity_is_valid(request):
        raise ValueError("RequestV3 完整性校验失败")
    state = load_workflow_state(session_dir, request.get("request_id"))
    job_errors = _generation_job_context_errors(
        state,
        request,
        report.get("generation_job_lease") or {},
        claim_id=generation_job_claim_id,
        expected_epoch=generation_job_expected_epoch,
        expected_phase="implementation",
    )
    if generation_job_claim_id != report.get("generation_job_claim_id"):
        job_errors.append("report_claim_id")
    if job_errors:
        raise ValueError(
            "Generation Job abort context无效: "
            + "; ".join(job_errors)
        )
    active = state.get("active_transaction") or {}
    if any((
        state.get("status") != "running",
        active.get("transaction_id") != report.get("transaction_id"),
        active.get("implementation_manifest_fingerprint")
        != (report.get("implementation_manifest") or {}).get(
            "implementation_manifest_fingerprint"
        ),
    )):
        raise ValueError("Workflow State 与 GenerationTransactionV3 不一致")
    root = Path(project_root or report.get("project_root") or "").resolve()
    if root != Path(report.get("project_root") or "").resolve():
        raise ValueError("GenerationTransactionV3 project_root 不一致")
    lease_errors = validate_generation_file_lease(
        root,
        report.get("generation_file_lease"),
    )
    if lease_errors:
        raise ValueError(
            "Generation transaction lease invalid: "
            + "; ".join(lease_errors)
        )
    try:
        _load_generation_baseline(report_path, report)
    except ValueError as error:
        raise ValueError(
            "Generation transaction does not support safe abort: "
            f"{error}"
        ) from error
    changed = set(_changed_snapshot_paths(
        report.get("generation_input_snapshot") or {},
        _snapshot_generation_roots(
            root,
            exact_files=(
                (report.get("generation_input_snapshot") or {}).get(
                    "exact_files"
                ) or ()
            ),
        ),
    ))
    allowed = set(
        (report.get("implementation_manifest") or {}).get(
            "allowed_changes"
        ) or ()
    )
    unexpected = sorted(changed - allowed)
    if unexpected and not allow_generation_root_drift:
        raise ValueError(
            f"Abort found generation changes outside transaction: {unexpected}"
        )
    project_guard_drifted = bool(_project_guard_changed_paths(
        report.get("project_guard_snapshot") or {},
        _snapshot_project_guard(root),
    ))
    if project_guard_drifted and not allow_project_guard_drift:
        raise ValueError("Abort found project changes outside generation roots")
    requested_at = datetime.now().isoformat(timespec="seconds")
    report.update({
        "status": "aborting",
        "completed_at": None,
        "changed_files": [],
        "abort": {
            "abort_version": "1.0",
            "reason": reason,
            "requested_at": requested_at,
            "phase": "intent_recorded",
            "draft_archive": None,
            "restored_files": [],
            "system_materialization_rolled_back": False,
            "lease_released": False,
            "project_guard_drift_allowed": bool(
                allow_project_guard_drift
            ),
            "project_guard_drift_detected": bool(project_guard_drifted),
            "generation_root_drift_allowed": bool(
                allow_generation_root_drift
            ),
            "generation_root_drift_detected": bool(unexpected),
            "generation_root_drift_paths": unexpected,
        },
        "summary": reason,
    })
    report.pop("completion_fingerprint", None)
    report.pop("implementation_receipt", None)
    report["result_fingerprint"] = transaction_result_fingerprint(report)
    write_json_atomic(report_path, report)
    return _complete_aborted_generation_transaction(
        session_dir,
        request,
        state,
        report_path,
        report,
        root,
    )


def _complete_aborted_generation_transaction(
        session_dir,
        request,
        state,
        report_path,
        report,
        project_root,
    ):
    report_path = Path(report_path).resolve()
    project_root = Path(project_root).resolve()
    _validate_terminal_report_identity(report_path, report)
    if report.get("status") not in {"aborting", "aborted"}:
        raise ValueError("Generation transaction is not aborting")
    if any((
        not request_identity_is_valid(request),
        report.get("request_id") != request.get("request_id"),
        project_root != Path(report.get("project_root") or "").resolve(),
    )):
        raise ValueError("Aborted GenerationTransaction identity invalid")
    active = state.get("active_transaction") or {}
    last_result = state.get("last_result") or {}
    if not (
        state.get("status") == "running"
        and active.get("transaction_id") == report.get("transaction_id")
        or state.get("status") == "ready"
        and last_result.get("transaction_id") == report.get("transaction_id")
        and last_result.get("status") == "aborted"
    ):
        raise ValueError("Abort recovery Workflow State mismatch")
    abort = report.get("abort") or {}
    if any((
        abort.get("abort_version") != "1.0",
        not str(abort.get("reason") or "").strip(),
    )):
        raise ValueError("Abort recovery intent invalid")
    _load_generation_baseline(report_path, report)
    changed = set(_changed_snapshot_paths(
        report.get("generation_input_snapshot") or {},
        _snapshot_generation_roots(
            project_root,
            exact_files=(
                (report.get("generation_input_snapshot") or {}).get(
                    "exact_files"
                ) or ()
            ),
        ),
    ))
    allowed = set(
        (report.get("implementation_manifest") or {}).get(
            "allowed_changes"
        ) or ()
    )
    unexpected = sorted(changed - allowed)
    allow_generation_root_drift = bool(
        (report.get("abort") or {}).get("generation_root_drift_allowed")
    )
    if unexpected and not allow_generation_root_drift:
        raise ValueError(
            f"Abort recovery found changes outside transaction: {unexpected}"
        )
    allow_project_guard_drift = bool(
        (report.get("abort") or {}).get("project_guard_drift_allowed")
    )
    project_guard_drifted = bool(_project_guard_changed_paths(
        report.get("project_guard_snapshot") or {},
        _snapshot_project_guard(project_root),
    ))
    if project_guard_drifted and not allow_project_guard_drift:
        raise ValueError("Abort recovery project guard mismatch")
    if report.get("status") == "aborting":
        archive = _archive_ai_implementation(
            project_root,
            report,
            report_path.parent / "aborted-implementation.zip",
        )
        report = _write_abort_progress(
            report_path,
            report,
            phase="draft_archived",
            draft_archive=archive,
        )
        report = _write_abort_progress(
            report_path,
            report,
            phase="workspace_revert_delegated_to_host",
            system_materialization_rolled_back=False,
        )
        restored = []
        report = _write_abort_progress(
            report_path,
            report,
            phase="workspace_revert_delegated_to_host",
            restored_files=restored,
        )
    _validate_aborted_implementation_archive(
        report_path.parent / "aborted-implementation.zip",
        report,
    )
    project_guard_drifted = bool(_project_guard_changed_paths(
        report.get("project_guard_snapshot") or {},
        _snapshot_project_guard(project_root),
    ))
    if project_guard_drifted and not allow_project_guard_drift:
        raise ValueError("Abort recovery project guard mismatch")
    lease = report.get("generation_file_lease")
    if not generation_file_lease_release_is_complete(project_root, lease):
        lease_errors = validate_generation_file_lease(project_root, lease)
        if lease_errors:
            raise ValueError(
                "Abort recovery lease invalid: " + "; ".join(lease_errors)
            )
        release_generation_file_lease(project_root, lease)
    if not generation_file_lease_release_is_complete(project_root, lease):
        raise ValueError("Abort recovery lease release incomplete")
    if (
        report.get("status") != "aborted"
        or (report.get("abort") or {}).get("lease_released") is not True
    ):
        report = dict(report)
        completed_at = datetime.now()
        report["status"] = "aborted"
        report["completed_at"] = completed_at.isoformat(timespec="seconds")
        report["abort"] = {
            **(report.get("abort") or {}),
            "phase": "completed",
            "aborted_at": completed_at.isoformat(timespec="seconds"),
            "lease_released": True,
            "project_guard_drift_detected": bool(project_guard_drifted),
        }
        report["stage_timing_ledger"] = _complete_transaction_timing(
            report,
            completed_at.isoformat(timespec="milliseconds"),
        )
        report["result_fingerprint"] = transaction_result_fingerprint(report)
        write_json_atomic(report_path, report)
    publish_static_job_outcome(
        session_dir,
        request["request_id"],
        report_path,
        report,
    )
    report["report_path"] = str(report_path)
    return report


def _write_abort_progress(report_path, report, *, phase, **updates):
    value = dict(report)
    value["abort"] = {
        **(value.get("abort") or {}),
        **updates,
        "phase": phase,
    }
    value["result_fingerprint"] = transaction_result_fingerprint(value)
    write_json_atomic(report_path, value)
    return value


def _finish_generation_transaction_locked(
        report_path,
        *,
    changed_files=None,
    derive_changed_files=False,
        validate_only=False,
        summary="",
        project_root=None,
        generation_job_claim_id=None,
        generation_job_expected_epoch=None,
    ):
    report_path = Path(report_path).resolve()
    validation_started_at = _now_millis()
    validation_started_monotonic = time.monotonic()
    report = _read_json(report_path)
    _validate_report_identity(report_path, report)
    request_path = Path(report["request_path"]).resolve()
    request = _read_json(request_path)
    if not request_identity_is_valid(request):
        raise ValueError("RequestV3 完整性校验失败")
    session_dir = session_dir_for_request_path(request_path, request)
    state = load_workflow_state(session_dir, request.get("request_id"))
    job_errors = _generation_job_context_errors(
        state,
        request,
        report.get("generation_job_lease") or {},
        claim_id=generation_job_claim_id,
        expected_epoch=generation_job_expected_epoch,
        expected_phase="implementation",
    )
    if generation_job_claim_id != report.get("generation_job_claim_id"):
        job_errors.append("report_claim_id")
    if job_errors:
        raise ValueError(
            "Generation Job implementation context无效: "
            + "; ".join(job_errors)
        )
    active = state.get("active_transaction") or {}
    if any((
        state.get("status") != "running",
        active.get("transaction_id") != report.get("transaction_id"),
        active.get("revision_seal")
        != ((report.get("lease") or {}).get("revision") or {}).get("seal"),
        active.get("annotation_snapshot_fingerprint")
        != ((report.get("lease") or {}).get("annotation") or {}).get(
            "snapshot_fingerprint"
        ),
        active.get("implementation_manifest_fingerprint")
        != (report.get("implementation_manifest") or {}).get(
            "implementation_manifest_fingerprint"
        ),
    )):
        raise ValueError("Workflow State 与 GenerationTransactionV3 不一致")

    project_root = Path(project_root or Paths.BASE_DIR).resolve()
    frozen_project_root = Path(report.get("project_root") or "").resolve()
    if project_root != frozen_project_root:
        raise ValueError(
            "GenerationTransactionV3 project_root 不一致: "
            f"{project_root} != {frozen_project_root}"
        )
    if derive_changed_files:
        if changed_files is not None:
            raise ValueError(
                "derive_changed_files不能与changed_files同时提交"
            )
        reported = None
        scope_errors = []
    else:
        if changed_files is None:
            raise ValueError("changed_files必须显式提交")
        reported, scope_errors = _normalize_changed_files(
            project_root,
            changed_files,
        )
    scope_errors.extend(validate_request_scope_binding(
        request,
        project_root=project_root,
    ))
    scope_errors.extend(validate_generation_file_lease(
        project_root,
        report.get("generation_file_lease"),
    ))
    materialization_errors = []
    implementation_manifest = report.get("implementation_manifest") or {}
    git_state_audit = _normalize_git_allowed_change_state(
        project_root,
        implementation_manifest.get("allowed_changes") or (),
    )
    scope_errors.extend(git_state_audit["errors"])
    system_materialization = report.get("system_materialization") or {}
    workspace_candidate_audit = {
        "status": "not_prepared",
        "matches": False,
        "files": [],
    }
    if system_materialization.get("status") == "candidate_prepared":
        workspace_candidate_audit = _implementation_candidate_workspace_audit(
            report_path,
            report,
            project_root,
        )
        materialization_errors.extend(
            _implementation_candidate_audit_errors(workspace_candidate_audit)
        )
    elif derive_changed_files:
        materialization_errors.append(
            "Implementation candidate was not prepared"
        )
    changed, change_errors, change_audit = _actual_generation_changes(
        project_root,
        report.get("generation_input_snapshot") or {},
        reported,
        derive_reported=derive_changed_files,
        system_owned=(
            report.get("implementation_manifest") or {}
        ).get("system_owned_changes") or (),
    )
    reported = list(change_audit.get("reported") or ())
    manifest_allowed = set(
        implementation_manifest.get("allowed_changes") or ()
    )
    manifest_read_only = set(
        implementation_manifest.get("read_only_reuse") or ()
    )
    undeclared_changes = sorted(set(changed) - manifest_allowed)
    read_only_changes = sorted(set(changed) & manifest_read_only)
    if undeclared_changes:
        scope_errors.append(
            "生成事务修改了Implementation Manifest范围外文件: "
            f"{undeclared_changes}"
        )
    if read_only_changes:
        scope_errors.append(
            "生成事务修改了Implementation Manifest只读复用文件: "
            f"{read_only_changes}"
        )
    git_state_audit = _normalize_git_allowed_change_state(
        project_root,
        manifest_allowed,
    )
    scope_errors.extend(git_state_audit["errors"])
    scope_errors.extend(_protected_locator_key_errors(
        project_root,
        implementation_manifest.get("protected_locator_keys") or (),
    ))
    change_audit["implementation_manifest"] = {
        "allowed_changes": sorted(manifest_allowed),
        "read_only_reuse": sorted(manifest_read_only),
        "protected_locator_keys": list(
            implementation_manifest.get("protected_locator_keys") or ()
        ),
        "undeclared_changes": undeclared_changes,
        "read_only_changes": read_only_changes,
    }
    protected_changes = _changed_snapshot_paths(
        report.get("protected_input_snapshot") or {},
        _snapshot_protected_paths(project_root),
    )
    if protected_changes:
        scope_errors.append(
            f"生成事务修改了受保护文件: {protected_changes}"
        )
    change_audit["protected_changes"] = protected_changes
    guard_baseline = report.get("project_guard_snapshot") or {}
    if guard_baseline.get("snapshot_version") != "1.0":
        scope_errors.append("Generation transaction 缺少项目guard快照")
        guard_changes = []
    else:
        guard_changes = _project_guard_changed_paths(
            guard_baseline,
            _snapshot_project_guard(project_root),
        )
        if guard_changes:
            scope_errors.append(
                "生成事务修改了generation scope外项目文件: "
                f"{guard_changes}"
            )
    change_audit["project_guard_changes"] = guard_changes
    revision_matches, current_revision = request_revision_matches(
        session_dir,
        request,
        ((report.get("lease") or {}).get("revision") or {}),
    )
    lease_errors = [] if revision_matches else [
        "生成期间 selected Take、timeline、Evidence Graph 或Annotation已变化"
    ]
    if not generation_contract_lease_matches(
        session_dir,
        report.get("generation_contract_lease"),
    ):
        lease_errors.append("生成期间Generation Contract已变化")
    brief, plan, artifact_errors = _load_frozen_artifacts(
        session_dir,
        request,
        state,
        report,
    )
    artifact_errors.extend(materialization_errors)
    if not implementation_manifest_matches_transaction(
            report.get("implementation_manifest"),
            plan,
            brief,
            report.get("generation_input_snapshot") or {},
            request_id=request.get("request_id"),
            allowed_write_roots=ALLOWED_WRITE_ROOTS,
            protected_write_roots=PROTECTED_WRITE_ROOTS,
            protected_root_files=PROTECTED_ROOT_FILES,
            generation_workspace_projection=_report_workspace_projection(
                session_dir,
                report,
            ),
        ):
        artifact_errors.append(
            "Implementation Manifest 身份无效或与冻结Plan/Brief不一致"
        )
    current_annotation = {}
    annotation_errors = []
    try:
        current_annotation = current_annotation_snapshot_for_request(
            session_dir,
            request,
        )
        expected_annotation, expected_annotation_errors = _annotation_lease(
            request,
            plan,
        )
        annotation_errors.extend(expected_annotation_errors)
        if expected_annotation != (
                (report.get("lease") or {}).get("annotation") or {}
        ):
            annotation_errors.append(
                "GenerationTransaction Annotation lease与Plan/Request不一致"
            )
        if current_annotation.get("snapshot_fingerprint") != (
                expected_annotation.get("snapshot_fingerprint")
        ):
            annotation_errors.append(
                "生成期间Annotation snapshot已变化"
            )
    except Exception as error:
        annotation_errors.append(
            f"当前Annotation snapshot无法验证: {type(error).__name__}: {error}"
        )
    lease_errors.extend(annotation_errors)
    validations = run_generation_validations(
        project_root,
        changed,
        plan_artifact=plan,
        target_steps=(request.get("target") or {}).get("steps") or [],
        target_scenario=(request.get("target") or {}).get("scenario") or {},
        source_feature=(
            ((request.get("target") or {}).get("feature") or {}).get(
                "source_relpath"
            )
        ),
    )
    required = _required_validations(changed)
    validation_errors = [
        f"必需验证未通过: {name}"
        for name in required
        if (validations.get(name) or {}).get("status") != "passed"
    ]
    validation_warnings = [
        f"{name}: {warning}"
        for name, value in validations.items()
        for warning in (value or {}).get("warnings") or []
    ]
    policy_errors, policy_audit = validate_generation_policy(
        project_root,
        changed,
        report.get("generation_policy_baseline") or {},
    )
    plan_errors, plan_audit = validate_plan_conformance(
        project_root,
        changed,
        plan,
        request=request,
        brief=brief,
        generation_input_snapshot=(
            report.get("generation_input_snapshot") or {}
        ),
    )
    pic_errors, pic_usage_audit = validate_generated_pic_usage(
        project_root,
        changed,
        report.get("pic_authorization_audit") or {},
        report.get("pic_policy_baseline") or {},
    )
    trace = _decision_trace(brief, plan)
    memory_warnings = [
        f"AI 经验追踪: {message}"
        for message in (
            (trace.get("memory_trace") or {}).get("warnings") or []
        )
    ]
    evidence_errors, evidence_audit = _validate_evidence_trace(
        session_dir,
        request,
        trace,
        changed,
    )
    code_manifest = build_code_manifest(
        project_root,
        plan_audit,
        request_id=request.get("request_id"),
        plan_fingerprint=plan.get("plan_fingerprint"),
    )
    manifest_errors = list(code_manifest.get("errors") or ())
    if (
        code_manifest.get("status") != "passed"
        and not plan_errors
        and not manifest_errors
    ):
        manifest_errors.append("Code Manifest 无法从Plan-to-Code audit投影")
    plan_errors = [*plan_errors, *manifest_errors]
    if (
        not validate_only
        and (report.get("implementation_manifest") or {}).get(
            "implementation_manifest_version"
        ) == IMPLEMENTATION_MANIFEST_VERSION
    ):
        ledger_pointer = report.get("implementation_validation_ledger") or {}
        ledger, ledger_errors = verify_validation_ledger(
            ledger_pointer.get("path") or "",
            transaction_id=report.get("transaction_id"),
            manifest_fingerprint=(
                report.get("implementation_manifest") or {}
            ).get("implementation_manifest_fingerprint"),
        )
        if ledger is None:
            artifact_errors.extend(ledger_errors)
        else:
            attempts = ledger.get("attempts") or []
            latest_attempt = attempts[-1] if attempts else {}
            if any((
                ledger_pointer.get("head_fingerprint")
                != ledger.get("head_fingerprint"),
                ledger_pointer.get("fingerprint")
                != ledger.get("fingerprint"),
                latest_attempt.get("status") != "valid",
                latest_attempt.get("source_snapshot")
                != snapshot_ai_editable_files(
                    project_root,
                    report.get("implementation_manifest") or {},
                ),
            )):
                artifact_errors.append(
                    "Latest implementation preflight is missing, invalid, "
                    "tampered, or stale"
                )
    errors = (
        change_errors
        + scope_errors
        + lease_errors
        + artifact_errors
        + policy_errors
        + validation_errors
        + plan_errors
        + pic_errors
        + evidence_errors
    )
    status = _completion_status(
        changed,
        change_errors=change_errors,
        scope_errors=scope_errors,
        lease_errors=lease_errors,
        artifact_errors=artifact_errors,
        policy_errors=policy_errors,
        validation_errors=validation_errors,
        plan_errors=plan_errors,
        pic_errors=pic_errors,
        evidence_errors=evidence_errors,
    )
    if validate_only:
        validation_result = _implementation_validation_result(
            report,
            status=status,
            changed=changed,
            system_materialization=system_materialization,
            validations=validations,
            plan_audit=plan_audit,
            evidence_audit=evidence_audit,
            policy_audit=policy_audit,
            pic_usage_audit=pic_usage_audit,
            workspace_candidate_audit=workspace_candidate_audit,
            errors=errors,
        )
        manifest = report.get("implementation_manifest") or {}
        ledger_path = report_path.parent / "implementation-validation-ledger.json"
        attempt, ledger = append_validation_attempt(
            ledger_path,
            transaction_id=report.get("transaction_id"),
            manifest_fingerprint=manifest.get(
                "implementation_manifest_fingerprint"
            ),
            source_snapshot=snapshot_ai_editable_files(
                project_root,
                manifest,
            ),
            status=validation_result["status"],
            issues=validation_result["issues"],
            expected_pointer=report.get(
                "implementation_validation_ledger"
            ) or {},
        )
        report["implementation_validation_ledger"] = {
            "path": str(ledger_path),
            "head_fingerprint": ledger.get("head_fingerprint"),
            "fingerprint": ledger.get("fingerprint"),
            "attempt_count": len(ledger.get("attempts") or ()),
            "latest_status": attempt.get("status"),
            "retry_attention_required": (
                _validation_retry_attention_required(ledger)
            ),
        }
        report["stage_timing_ledger"] = _update_stage_timing(
            report,
            "implementation",
            source="implementation_validation_ledger",
            started_at=validation_started_at,
            finished_at=_now_millis(),
            duration_ms=_elapsed_ms(validation_started_monotonic),
        )
        report["host_delivery_observation"] = _host_delivery_observation(
            project_root,
            changed,
            validation_started_at=validation_started_at,
        )
        report["system_materialization"] = system_materialization
        report["workspace_candidate_audit"] = workspace_candidate_audit
        write_json_atomic(report_path, report)
        validation_result["attempt"] = attempt
        return validation_result
    runtime_code_snapshot = snapshot_runtime_code(project_root)
    runtime_risk_policy = derive_runtime_risk_policy(
        project_root,
        report.get("implementation_manifest") or {},
    )
    completed_at = datetime.now()
    transaction_finished_at = completed_at.isoformat(timespec="milliseconds")
    unresolved_issues = _generation_unresolved_issues(
        plan,
        report.get("implementation_manifest") or {},
    )
    report.update({
        "status": status,
        "completed_at": completed_at.isoformat(timespec="seconds"),
        "unresolved_issues": unresolved_issues,
        "execution_outcome": _execution_outcome(
            request,
            status,
            unresolved_issues=unresolved_issues,
        ),
        "changed_files": changed,
        "reported_changed_files": reported,
        "change_set_audit": change_audit,
        "validations": validations,
        "required_validations": required,
        "summary": str(summary or (plan.get("plan") or {}).get("summary") or ""),
        "decision_trace": trace,
        "lease_revision_audit": {
            "status": "passed" if revision_matches else "failed",
            "expected": (report.get("lease") or {}).get("revision") or {},
            "current": current_revision,
        },
        "annotation_lease_audit": {
            "status": "passed" if not annotation_errors else "failed",
            "expected": (report.get("lease") or {}).get("annotation") or {},
            "current": {
                "snapshot_fingerprint": current_annotation.get(
                    "snapshot_fingerprint"
                ),
                "required_annotation_ids_by_step": current_annotation.get(
                    "required_annotation_ids_by_step"
                ) or {},
            },
            "errors": annotation_errors,
        },
        "generation_policy_audit": policy_audit,
        "pic_usage_audit": pic_usage_audit,
        "plan_conformance_audit": plan_audit,
        "code_manifest": code_manifest,
        "evidence_audit": evidence_audit,
        "workspace_candidate_audit": workspace_candidate_audit,
        "implementation_snapshot": snapshot_files(
            _implementation_snapshot_paths(
                implementation_manifest,
                changed,
            ),
            project_root=project_root,
        ),
        "runtime_code_snapshot": runtime_code_snapshot,
        "runtime_code_snapshot_fingerprint": (
            runtime_code_snapshot_fingerprint(runtime_code_snapshot)
        ),
        "runtime_risk_policy": runtime_risk_policy,
        "terminal_snapshot_audit": {"status": "pending"},
        "warnings": [
            *(report.get("warnings") or []),
            *validation_warnings,
            *memory_warnings,
            *((plan_audit or {}).get("warnings") or []),
        ],
        "errors": errors,
    })
    report["host_delivery_observation"] = _host_delivery_observation(
        project_root,
        changed,
        validation_started_at=validation_started_at,
    )
    report["git_diff_visibility"] = _git_diff_visibility_audit(
        project_root,
        report,
        changed,
    )
    if report["git_diff_visibility"].get("status") in {"failed", "partial"}:
        report["warnings"] = [
            *(report.get("warnings") or []),
            "Git diff visibility intent-to-add incomplete; generated files "
            "remain valid but new-file diff display may require manual git add -N.",
        ]
    report["stage_timing_ledger"] = _complete_transaction_timing(
        report,
        transaction_finished_at,
    )
    report["implementation_receipt"] = _implementation_receipt(report)
    if status in {"completed", "completed_no_changes"}:
        report["completion_fingerprint"] = completed_report_fingerprint(
            report
        )
    report["result_fingerprint"] = transaction_result_fingerprint(report)
    write_json_atomic(report_path, report)
    with generation_file_lease_publish_guard(
        project_root,
        report.get("generation_file_lease"),
    ):
        report = _finalize_terminal_snapshot(
            report_path,
            report,
            project_root,
        )
        _transition_terminal_workflow(
            session_dir,
            request["request_id"],
            report_path,
            report,
        )
    report["report_path"] = str(report_path)
    return report


def _finalize_terminal_snapshot(report_path, report, project_root):
    report = dict(report)
    project_root = Path(project_root).resolve()
    expected_runtime = report.get("runtime_code_snapshot") or {}
    expected_implementation = report.get("implementation_snapshot") or []
    current_runtime = snapshot_runtime_code(project_root)
    current_implementation = snapshot_files(
        _implementation_snapshot_paths(
            report.get("implementation_manifest") or {},
            report.get("changed_files") or (),
        ),
        project_root=project_root,
    )
    matches = bool(
        current_runtime == expected_runtime
        and current_implementation == expected_implementation
    )
    report["terminal_snapshot_audit"] = {
        "status": "passed" if matches else "failed",
        "expected_runtime_code_snapshot_fingerprint": (
            runtime_code_snapshot_fingerprint(expected_runtime)
        ),
        "current_runtime_code_snapshot_fingerprint": (
            runtime_code_snapshot_fingerprint(current_runtime)
        ),
        "implementation_snapshot_matches": (
            current_implementation == expected_implementation
        ),
    }
    if not matches:
        report["status"] = "stale_during_generation"
        report["runtime_code_snapshot"] = current_runtime
        report["runtime_code_snapshot_fingerprint"] = (
            runtime_code_snapshot_fingerprint(current_runtime)
        )
        report["implementation_snapshot"] = current_implementation
        report["errors"] = [
            *(report.get("errors") or ()),
            "Implementation changed during terminal snapshot finalization",
        ]
        report.pop("completion_fingerprint", None)
    report["implementation_receipt"] = _implementation_receipt(report)
    if report.get("status") in {"completed", "completed_no_changes"}:
        report["implementation_diff"] = build_generation_diff(
            project_root,
            report_path,
            report,
        )
        report["implementation_diff_summary"] = build_generation_diff_summary(
            Path(report.get("session_dir") or report_path.parents[3]),
            report["implementation_diff"],
        )
        _record_memory(report_path, report)
        report["completion_fingerprint"] = completed_report_fingerprint(
            report
        )
    report["result_fingerprint"] = transaction_result_fingerprint(report)
    write_json_atomic(report_path, report)
    return report


def _implementation_receipt(report):
    manifest = report.get("implementation_manifest") or {}
    materialization = report.get("system_materialization") or {}
    materialization_commit = report.get("system_materialization_commit") or {}
    workspace_candidate_audit = report.get("workspace_candidate_audit") or {}
    delivery_write_channel = (
        "system_materializer"
        if materialization_commit.get("status") == "materialized"
        else "host_native_edit"
        if workspace_candidate_audit.get("matches") is True
        and report.get("changed_files")
        else "none"
    )
    value = {
        "implementation_receipt_version": IMPLEMENTATION_RECEIPT_VERSION,
        "owner": "generation_transaction",
        "request_id": report.get("request_id"),
        "plan_id": manifest.get("plan_id"),
        "plan_fingerprint": manifest.get("plan_fingerprint"),
        "transaction_id": report.get("transaction_id"),
        "implementation_manifest_id": manifest.get(
            "implementation_manifest_id"
        ),
        "implementation_manifest_fingerprint": manifest.get(
            "implementation_manifest_fingerprint"
        ),
        "status": report.get("status"),
        "changed_files": list(report.get("changed_files") or ()),
        "ai_editable_changes": list(
            manifest.get("ai_editable_changes") or ()
        ),
        "system_owned_changes": list(
            manifest.get("system_owned_changes") or ()
        ),
        "materialization_status": materialization.get("status"),
        "materialization_commit_status": materialization_commit.get(
            "status"
        ),
        "materialization_commit": dict(materialization_commit),
        "git_diff_visibility": dict(report.get("git_diff_visibility") or {}),
        "delivery_write_channel": delivery_write_channel,
        "workspace_candidate_audit_status": workspace_candidate_audit.get(
            "status"
        ),
        "workspace_candidate_matches": workspace_candidate_audit.get(
            "matches"
        ) is True,
        "validation_ledger": dict(
            report.get("implementation_validation_ledger") or {}
        ),
        "summary": str(report.get("summary") or ""),
    }
    value["fingerprint"] = _receipt_fingerprint(value)
    return value


def _git_diff_visibility_audit(project_root, report, changed_files):
    audit = {
        "git_diff_visibility_version": "1.0",
        "status": "not_applicable",
        "mode": "git_intent_to_add",
        "files": [],
        "skipped": [],
        "errors": [],
    }
    changed = set(str(item).replace("\\", "/") for item in changed_files or ())
    materialization_commit = report.get("system_materialization_commit") or {}
    if materialization_commit.get("status") != "materialized":
        audit["status"] = "no_materialization_commit"
        return audit
    written = set(
        str(item).replace("\\", "/")
        for item in materialization_commit.get("written_files") or ()
    )
    if not changed or not written:
        audit["status"] = "no_new_files"
        return audit
    manifest = report.get("implementation_manifest") or {}
    allowed = set(
        str(item).replace("\\", "/")
        for item in manifest.get("allowed_changes") or ()
    )
    journal_path = Path(str(materialization_commit.get("journal_path") or ""))
    try:
        journal = _read_json(journal_path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        audit["status"] = "failed"
        audit["errors"].append(
            "materialization_journal_unavailable: "
            f"{type(error).__name__}: {error}"
        )
        return audit
    candidates = []
    for item in journal.get("files") or ():
        if not isinstance(item, dict):
            continue
        relative = str(item.get("path") or "").replace("\\", "/")
        if not relative:
            continue
        if relative not in allowed:
            audit["skipped"].append({"path": relative, "reason": "not_allowed"})
            continue
        if relative not in changed or relative not in written:
            audit["skipped"].append({"path": relative, "reason": "not_written_change"})
            continue
        if item.get("original_sha256") is not None:
            audit["skipped"].append({"path": relative, "reason": "tracked_or_existing_file"})
            continue
        if item.get("byte_exact"):
            audit["skipped"].append({"path": relative, "reason": "byte_exact"})
            continue
        candidates.append(relative)
    candidates = sorted(set(candidates))
    if not candidates:
        audit["status"] = "no_new_files"
        return audit
    project_root = Path(project_root).resolve()
    git = ["git", "-C", str(project_root), "-c", "core.quotepath=false"]
    try:
        inside = subprocess.run(
            [*git, "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, ValueError) as error:
        audit["status"] = "not_applicable"
        audit["errors"].append(f"git_unavailable: {type(error).__name__}: {error}")
        return audit
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        audit["status"] = "not_applicable"
        return audit
    untracked = _git_untracked_paths(git, candidates, audit)
    if not untracked:
        audit["status"] = "no_untracked_new_files"
        return audit
    applied = []
    for chunk in _path_chunks(untracked):
        result = subprocess.run(
            [*git, "add", "-N", "--", *chunk],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            audit["errors"].append(
                "git_add_intent_to_add_failed: "
                + (result.stderr or result.stdout or "").strip()
            )
            continue
        applied.extend(chunk)
    applied = sorted(set(applied))
    audit["files"] = applied
    if not applied:
        audit["status"] = "failed"
        return audit
    _verify_git_intent_to_add(git, applied, audit)
    audit["status"] = "partial" if audit["errors"] else "applied"
    return audit


def _git_untracked_paths(git, paths, audit):
    status = subprocess.run(
        [*git, "status", "--porcelain=v1", "-uall", "--", *paths],
        capture_output=True,
        text=True,
        check=False,
    )
    if status.returncode != 0:
        audit["errors"].append(
            "git_status_failed: " + (status.stderr or status.stdout or "").strip()
        )
        return []
    path_set = set(paths)
    untracked = []
    seen = set()
    for line in status.stdout.splitlines():
        if len(line) < 4:
            continue
        state = line[:2]
        relative = line[3:].replace("\\", "/")
        if " -> " in relative:
            relative = relative.rsplit(" -> ", 1)[-1]
        if relative not in path_set:
            continue
        seen.add(relative)
        if state == "??":
            untracked.append(relative)
        else:
            audit["skipped"].append({
                "path": relative,
                "reason": "not_untracked",
                "git_status": state,
            })
    for relative in sorted(path_set - seen):
        audit["skipped"].append({
            "path": relative,
            "reason": "not_reported_by_git_status",
        })
    return sorted(set(untracked))


def _verify_git_intent_to_add(git, paths, audit):
    worktree = subprocess.run(
        [*git, "diff", "--name-status", "--", *paths],
        capture_output=True,
        text=True,
        check=False,
    )
    if worktree.returncode not in {0, 1}:
        audit["errors"].append(
            "git_diff_visibility_check_failed: "
            + (worktree.stderr or worktree.stdout or "").strip()
        )
        return
    visible = set()
    for line in worktree.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0] == "A":
            visible.add(parts[-1].replace("\\", "/"))
    missing = sorted(set(paths) - visible)
    if missing:
        audit["errors"].append(
            "git_diff_visibility_missing_paths: " + ", ".join(missing)
        )
    cached = subprocess.run(
        [*git, "diff", "--cached", "--name-only", "--", *paths],
        capture_output=True,
        text=True,
        check=False,
    )
    if cached.returncode not in {0, 1}:
        audit["errors"].append(
            "git_cached_visibility_check_failed: "
            + (cached.stderr or cached.stdout or "").strip()
        )
        return
    staged = [line.strip() for line in cached.stdout.splitlines() if line.strip()]
    if staged:
        audit["errors"].append(
            "git_intent_to_add_staged_content: " + ", ".join(staged)
        )


def _path_chunks(paths, size=100):
    values = list(paths or [])
    for index in range(0, len(values), size):
        yield values[index:index + size]


def _host_delivery_observation(
        project_root,
        changed_files,
        *,
        validation_started_at,
):
    root = Path(project_root).resolve()
    changed = [str(item) for item in changed_files or ()]
    observed = []
    missing = []
    errors = []
    for relative in changed:
        try:
            target = (root / relative).resolve()
            target.relative_to(root)
            if not target.is_file():
                missing.append(relative)
                continue
            stat = target.stat()
        except (OSError, ValueError) as error:
            errors.append({"path": relative, "error": str(error)})
            continue
        observed.append({
            "path": relative,
            "mtime_epoch_ms": int(stat.st_mtime * 1000),
            "mtime_at": _timestamp_iso(stat.st_mtime),
        })
    observed.sort(key=lambda item: item["mtime_epoch_ms"])
    if not changed:
        status = "not_required"
    elif len(observed) == len(changed) and not errors:
        status = "observed"
    elif observed:
        status = "partial"
    else:
        status = "not_observed"
    first = observed[0] if observed else {}
    last = observed[-1] if observed else {}
    validation_started_epoch_ms = _datetime_epoch_ms(validation_started_at)
    last_write_to_validation_start_ms = None
    if validation_started_epoch_ms is not None and last:
        last_write_to_validation_start_ms = max(
            0,
            validation_started_epoch_ms - int(last["mtime_epoch_ms"]),
        )
    return {
        "host_delivery_observation_version": HOST_DELIVERY_OBSERVATION_VERSION,
        "status": status,
        "source": "filesystem_stat_after_validation",
        "validation_started_at": validation_started_at,
        "file_count": len(changed),
        "observed_file_count": len(observed),
        "missing_file_count": len(missing),
        "first_target_path": first.get("path"),
        "first_target_mtime_at": first.get("mtime_at"),
        "last_target_path": last.get("path"),
        "last_target_mtime_at": last.get("mtime_at"),
        "target_write_spread_ms": (
            int(last["mtime_epoch_ms"]) - int(first["mtime_epoch_ms"])
            if first and last else None
        ),
        "last_write_to_validation_start_ms": last_write_to_validation_start_ms,
        "missing_targets": missing[:20],
        "errors": errors[:20],
    }


def _timestamp_iso(timestamp):
    return datetime.fromtimestamp(timestamp).isoformat(timespec="milliseconds")


def _datetime_epoch_ms(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(parsed.timestamp() * 1000)


def _receipt_fingerprint(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _implementation_validation_result(
        report,
        *,
        status,
        changed,
        system_materialization,
        validations,
        plan_audit,
        evidence_audit,
        policy_audit,
        pic_usage_audit,
        workspace_candidate_audit,
        errors,
    ):
    issues = [_implementation_validation_issue(error) for error in errors]
    return {
        "implementation_validation_version": "1.0",
        "status": (
            "valid"
            if status in {"completed", "completed_no_changes"}
            else "invalid"
        ),
        "transaction_id": report.get("transaction_id"),
        "request_id": report.get("request_id"),
        "projected_transaction_status": status,
        "changed_files": list(changed),
        "ai_editable_changes": list(
            (report.get("implementation_manifest") or {}).get(
                "ai_editable_changes"
            ) or ()
        ),
        "system_materialization": system_materialization,
        "validations": validations,
        "plan_conformance_audit": plan_audit,
        "evidence_audit": evidence_audit,
        "generation_policy_audit": policy_audit,
        "pic_usage_audit": pic_usage_audit,
        "workspace_candidate_audit": workspace_candidate_audit,
        "issues": issues,
    }


def _implementation_validation_issue(error):
    message = str(error)
    if "证据" in message or "Evidence" in message:
        category = "evidence"
        code = "evidence_invalid"
    elif "revision" in message or "lease" in message or "已变化" in message:
        category = "evidence"
        code = "stale_generation"
    elif "PIC" in message or "策略" in message or "policy" in message:
        category = "policy"
        code = "policy_violation"
    elif "scope" in message or "范围外" in message or "受保护" in message:
        category = "technical"
        code = "scope_violation"
    elif "locator" in message.casefold():
        category = "technical"
        code = "locator_invalid"
    elif "Python" in message or "python" in message:
        category = "technical"
        code = "python_invalid"
    elif "计划" in message or "Plan" in message:
        category = "technical"
        code = "plan_conformance"
    else:
        category = "technical"
        code = "implementation_invalid"
    return {
        "stage": "implementation_validation",
        "category": category,
        "code": code,
        "message": message,
        "ai_repairable": category == "technical",
        "user_action_required": category in {"evidence", "business_authority"},
    }


def _validation_retry_attention_required(ledger):
    attempts = list((ledger or {}).get("attempts") or [])
    for attempt in attempts[:-1]:
        if attempt.get("status") == "valid":
            continue
        if not _expected_pre_apply_validation_attempt(attempt):
            return True
    return False


def _expected_pre_apply_validation_attempt(attempt):
    issues = [
        issue for issue in attempt.get("issues") or []
        if isinstance(issue, dict)
    ]
    return bool(issues) and all(
        _expected_pre_apply_validation_issue(issue)
        for issue in issues
    )


def _expected_pre_apply_validation_issue(issue):
    message = str(issue.get("message") or "")
    return any(marker in message for marker in (
        "Implementation candidate file missing",
        "behavior_file 不存在",
        "Page Object 不存在",
        "root locator 文件不存在",
        "缺少 owned Page/View 的有序计划调用",
        "生成代码缺少计划值",
    ))


def _fail_running_transaction_after_exception(
        report_path,
        report,
        *,
        project_root=None,
):
    root = Path(
        project_root
        or report.get("project_root")
        or Paths.BASE_DIR
    ).resolve()
    job_bound = bool(report.get("generation_job_lease"))
    release_lease = not job_bound
    try:
        if report.get("status") != "running":
            try:
                _validate_recoverable_terminal_report_identity(
                    report_path,
                    report,
                )
                with generation_file_lease_publish_guard(
                    root,
                    report.get("generation_file_lease"),
                ):
                    report = _finalize_terminal_snapshot(
                        Path(report_path),
                        report,
                        root,
                    )
                    _transition_terminal_workflow(
                        report.get("session_dir")
                        or Path(report_path).parents[3],
                        report.get("request_id"),
                        Path(report_path),
                        report,
                    )
                release_lease = True
            except Exception:
                pass
            return
        report = dict(report)
        completed_at = datetime.now()
        report.update({
            "status": "failed",
            "completed_at": completed_at.isoformat(timespec="seconds"),
            "errors": [
                *(report.get("errors") or ()),
                "Generation transaction 因未处理异常终止",
            ],
        })
        report["stage_timing_ledger"] = _complete_transaction_timing(
            report,
            completed_at.isoformat(timespec="milliseconds"),
        )
        report.pop("completion_fingerprint", None)
        report["result_fingerprint"] = transaction_result_fingerprint(
            report
        )
        try:
            write_json_atomic(report_path, report)
        except Exception:
            return
        try:
            _transition_terminal_workflow(
                report.get("session_dir") or Path(report_path).parents[3],
                report.get("request_id"),
                Path(report_path),
                report,
            )
            release_lease = True
        except Exception:
            pass
    finally:
        if release_lease:
            release_generation_file_lease_for_transaction(
                root,
                Path(report_path).parent.name,
            )


def _load_frozen_artifacts(session_dir, request, state, report):
    errors = []
    brief = {}
    plan = {}
    try:
        brief_path = _resolve_session_artifact(
            session_dir,
            ((report.get("lease") or {}).get("brief") or {}).get("path"),
            "generation-briefs",
        )
        brief = load_generation_brief(brief_path)
        if not brief_matches_request(brief, request):
            errors.append("Generation Brief 与 RequestV3 不一致")
        expected = ((report.get("lease") or {}).get("brief") or {}).get(
            "brief_fingerprint"
        )
        if brief.get("brief_fingerprint") != expected:
            errors.append("Generation Brief 指纹在事务期间变化")
    except Exception as error:
        errors.append(f"Generation Brief 无法读取: {type(error).__name__}: {error}")
    try:
        plan = load_generation_plan(session_dir, state, request) or {}
        expected = ((report.get("lease") or {}).get("plan") or {}).get(
            "plan_fingerprint"
        )
        if not plan or plan.get("plan_fingerprint") != expected:
            errors.append("Generation Plan 指纹在事务期间变化")
    except Exception as error:
        errors.append(f"Generation Plan 无法读取: {type(error).__name__}: {error}")
    return brief, plan, errors


def _annotation_lease(request, plan):
    snapshot = request.get("annotation_snapshot")
    if snapshot is None:
        snapshot = build_annotation_snapshot(
            (request.get("target") or {}).get("steps") or []
        )
    if not annotation_snapshot_is_valid(snapshot):
        return {}, ["Request Annotation snapshot无效"]
    trace = ((plan or {}).get("plan") or {}).get("annotation_trace")
    required = snapshot.get("required_annotation_ids_by_step") or {}
    if snapshot.get("references"):
        if not isinstance(trace, dict):
            return {}, ["Plan缺少annotation_trace"]
        if any((
            trace.get("snapshot_fingerprint")
            != snapshot.get("snapshot_fingerprint"),
            trace.get("required_annotation_ids_by_step") != required,
        )):
            return {}, ["Plan annotation_trace与Request snapshot不一致"]
    elif trace and any((
        trace.get("references"),
        trace.get("required_annotation_ids_by_step"),
    )):
        return {}, ["无Annotation Request包含非空Plan annotation_trace"]
    return {
        "annotation_snapshot_version": snapshot.get(
            "annotation_snapshot_version"
        ),
        "snapshot_fingerprint": snapshot.get("snapshot_fingerprint"),
        "required_annotation_ids_by_step": required,
        "required_annotation_count": sum(
            len(annotation_ids)
            for annotation_ids in required.values()
        ),
    }, []


def _decision_trace(brief, plan):
    claims = []
    used = []
    actions = {
        (str(action.get("step_id") or ""), str(action.get("id"))): action
        for action in brief.get("actions") or []
        if action.get("id")
    }
    operation_decisions = []
    behavior_decisions = []
    ambiguity_decisions = []
    reuse_used = []
    uncertainties = list(
        (plan.get("plan") or {}).get("uncertainties") or []
    )
    for step_id, step in (
        (plan.get("plan") or {}).get("steps") or {}
    ).items():
        behavior_resolution = step.get("behavior_resolution") or {}
        covered_action_ids = step.get("covered_action_ids") or []
        if behavior_resolution.get("strategy") == "reuse":
            evidence_ids = list(dict.fromkeys(
                evidence_id
                for action_id in covered_action_ids
                for evidence_id in (
                    actions.get(
                        (str(step_id), str(action_id)),
                        {},
                    ).get("evidence") or []
                )
            ))
            used.extend(evidence_ids)
            claims.append({
                "claim_id": f"existing-behavior-{len(claims) + 1:03d}",
                "statement": (
                    "Reuse exact existing Step behavior "
                    f"{step.get('behavior_file') or ''}"
                ).strip(),
                "evidence_ids": evidence_ids,
            })
            behavior_decisions.append({
                "step_id": step_id,
                "behavior_file": step.get("behavior_file"),
                "behavior_resolution": behavior_resolution,
                "covered_action_ids": covered_action_ids,
            })
            if behavior_resolution.get("candidate_id"):
                reuse_used.append(behavior_resolution["candidate_id"])
        for index, operation in enumerate(step.get("operations") or [], start=1):
            evidence_ids = list(dict.fromkeys([
                *(operation.get("evidence_ids") or []),
                *(
                    evidence_id
                    for action_id in operation.get("action_ids") or []
                    for evidence_id in (
                        actions.get(
                            (str(step_id), str(action_id)),
                            {},
                        ).get("evidence") or []
                    )
                ),
            ]))
            used.extend(evidence_ids)
            claims.append({
                "claim_id": f"plan-operation-{len(claims) + 1:03d}",
                "statement": (
                    f"{operation.get('op')} {operation.get('target') or ''}"
                ).strip(),
                "evidence_ids": evidence_ids,
            })
            operation_decisions.append({
                "step_operation": index,
                "step_id": step_id,
                "op": operation.get("op"),
                "target": operation.get("target"),
                "action_ids": operation.get("action_ids") or [],
                "target_action_id": operation.get("target_action_id"),
                "value_action_ids": operation.get("value_action_ids") or [],
                "value_provenance": operation.get("value_provenance") or {},
                "target_fingerprint": operation.get("target_fingerprint"),
                "effect_ids": operation.get("effect_ids") or [],
                "decision_ids": operation.get("decision_ids") or [],
                "confidence": operation.get("confidence"),
                "implementation_location": operation.get(
                    "implementation_location"
                ) or "page_method",
                "implementation_method": operation.get(
                    "implementation_method"
                ),
                "implementation_resolution": operation.get(
                    "implementation_resolution"
                ) or {},
                "uncertainty": operation.get("uncertainty"),
                "reuse_reference": operation.get("reuse_reference"),
            })
            if operation.get("uncertainty") is not None:
                uncertainties.append(operation["uncertainty"])
            if operation.get("reuse_reference"):
                reuse_used.append(operation["reuse_reference"])
        for relationship in step.get("action_relationships") or []:
            source_action_id = str(
                relationship.get("source_action_id") or ""
            )
            source = actions.get((str(step_id), source_action_id)) or {}
            evidence_ids = list(dict.fromkeys(
                source.get("evidence") or []
            ))
            used.extend(evidence_ids)
            claims.append({
                "claim_id": f"action-relationship-{len(claims) + 1:03d}",
                "statement": (
                    f"{relationship.get('kind')} {source_action_id} -> "
                    f"{relationship.get('consumer_action_id') or ''}"
                ).strip(),
                "evidence_ids": evidence_ids,
            })
        for action_id in step.get("ignored_action_ids") or []:
            action = actions.get((str(step_id), str(action_id))) or {}
            evidence_ids = list(action.get("evidence") or [])
            used.extend(evidence_ids)
            claims.append({
                "claim_id": f"ignored-action-{len(claims) + 1:03d}",
                "statement": f"Ignore recorded action {action_id}",
                "evidence_ids": evidence_ids,
            })
    ambiguities = {
        str(item.get("ambiguity_id") or ""): item
        for item in brief.get("ambiguities") or []
        if item.get("ambiguity_id")
    }
    for resolution in (
        (plan.get("plan") or {}).get("ambiguity_resolutions") or []
    ):
        ambiguity_id = str(resolution.get("ambiguity_id") or "")
        ambiguity = ambiguities.get(ambiguity_id) or {}
        evidence_ids = list(dict.fromkeys(
            resolution.get("evidence_ids") or []
        ))
        used.extend(evidence_ids)
        claims.append({
            "claim_id": f"ambiguity-{len(claims) + 1:03d}",
            "statement": (
                f"Resolve {ambiguity.get('code') or ambiguity_id} as "
                f"{resolution.get('outcome') or ''}"
            ).strip(),
            "evidence_ids": evidence_ids,
        })
        ambiguity_decisions.append({
            "ambiguity_id": ambiguity_id,
            "code": ambiguity.get("code"),
            "routing": ambiguity.get("routing"),
            "step_id": ambiguity.get("step_id"),
            "outcome": resolution.get("outcome"),
            "action_ids": resolution.get("action_ids") or [],
            "evidence_ids": evidence_ids,
            "candidate_id": resolution.get("candidate_id"),
            "decision_ids": resolution.get("decision_ids") or [],
            "reason": resolution.get("reason"),
            "facts": ambiguity.get("facts") or {},
        })
    available = {
        evidence_id
        for action in actions.values()
        for evidence_id in action.get("evidence") or []
    }
    evidence_used = list(dict.fromkeys(used))
    memory_trace = _memory_trace_audit(brief, plan)
    return {
        "summary": (plan.get("plan") or {}).get("summary") or "",
        "scenario_model": (
            (plan.get("plan") or {}).get("scenario_model") or {}
        ),
        "memories_used": [
            item["memory_id"]
            for item in memory_trace.get("applied") or []
        ],
        "memories_rejected": [
            item["memory_id"]
            for item in memory_trace.get("dismissed") or []
        ],
        "memory_trace": memory_trace,
        "insights": [],
        "uncertainties": uncertainties,
        "decisions": (plan.get("plan") or {}).get("decision_trace") or [],
        "operation_decisions": operation_decisions,
        "behavior_decisions": behavior_decisions,
        "ambiguity_decisions": ambiguity_decisions,
        "pic_authorizations": (
            plan.get("plan") or {}
        ).get("pic_authorizations") or [],
        "reuse_used": reuse_used,
        "claims": claims,
        "evidence_used": evidence_used,
        "evidence_skipped": [
            {
                "evidence_id": evidence_id,
                "reason": "not_required_by_validated_plan",
            }
            for evidence_id in sorted(available - set(evidence_used))
        ],
        "brief_fingerprint": brief.get("brief_fingerprint"),
        "plan_fingerprint": plan.get("plan_fingerprint"),
    }


def _implementation_summary(plan_artifact):
    plan = (plan_artifact or {}).get("plan") or {}
    methods = {}
    for step in (plan.get("steps") or {}).values():
        for operation in (step or {}).get("operations") or []:
            method = str(operation.get("implementation_method") or "")
            resolution = operation.get("implementation_resolution") or {}
            if not method or method in methods:
                continue
            methods[method] = {
                "method": method,
                "strategy": resolution.get("strategy"),
                "candidate_id": resolution.get("candidate_id"),
                "reason": resolution.get("reason"),
            }
    counts = {"reuse": 0, "modify": 0, "create": 0}
    for item in methods.values():
        if item["strategy"] in counts:
            counts[item["strategy"]] += 1
    return {**counts, "methods": list(methods.values())}


def _memory_trace_audit(brief, plan):
    digest = brief.get("memory_digest") or {}
    available = {
        str(item.get("memory_id")): item
        for item in digest.get("items") or []
        if item.get("memory_id")
    }
    normalized_plan = plan.get("plan") or {}
    trace_declared = "memory_trace" in normalized_plan
    declared = normalized_plan.get("memory_trace") or {}
    applied = _memory_trace_entries(declared.get("applied"))
    dismissed = _memory_trace_entries(declared.get("dismissed"))
    declared_ids = {
        item["memory_id"]
        for item in [*applied, *dismissed]
    }
    overlap = sorted(
        {item["memory_id"] for item in applied}
        & {item["memory_id"] for item in dismissed}
    )
    unknown = sorted(declared_ids - set(available))
    warnings = []
    if unknown:
        warnings.append(
            f"忽略冻结 digest 之外的 memory: {unknown}"
        )
    if overlap:
        warnings.append(
            f"忽略同时采用和拒绝的 memory: {overlap}"
        )
    valid_ids = set(available) - set(unknown) - set(overlap)
    applied = [
        item for item in applied
        if item["memory_id"] in valid_ids
    ]
    dismissed = [
        item for item in dismissed
        if item["memory_id"] in valid_ids
    ]
    if unknown or overlap:
        status = "invalid"
    elif trace_declared:
        status = "passed"
    elif available:
        status = "invalid"
        warnings.append(
            "Brief 含相关经验，但 Plan 缺少memory_trace评估"
        )
    else:
        status = "not_available"
    return {
        "status": status,
        "digest_fingerprint": digest.get("digest_fingerprint"),
        "journal_revision": digest.get("journal_revision"),
        "available_memory_ids": sorted(available),
        "applied": applied,
        "dismissed": dismissed,
        "warnings": warnings,
    }


def _memory_trace_entries(values):
    result = []
    seen = set()
    for value in values or []:
        if not isinstance(value, dict):
            continue
        memory_id = str(value.get("memory_id") or "").strip()
        if not memory_id or memory_id in seen:
            continue
        item = {"memory_id": memory_id}
        reason = str(value.get("reason") or "").strip()
        if reason:
            item["reason"] = reason[:96]
        result.append(item)
        seen.add(memory_id)
    return result


def _protected_locator_key_errors(project_root, records):
    project_root = Path(project_root).resolve()
    errors = []
    seen = set()
    for record in records or ():
        if not isinstance(record, dict):
            errors.append("Implementation Manifest protected_locator_key无效")
            continue
        path_value = str(record.get("file") or "")
        key = str(record.get("key") or "")
        expected = str(record.get("locator_fingerprint") or "")
        identity = (path_value, key)
        if not path_value or not key or not expected or identity in seen:
            errors.append("Implementation Manifest protected_locator_key无效")
            continue
        seen.add(identity)
        path = _generation_target_path(project_root, path_value)
        try:
            value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, UnicodeError, yaml.YAMLError) as error:
            errors.append(
                "无法验证复用Locator key: "
                f"{path_value}:{key}: {type(error).__name__}"
            )
            continue
        locator = value.get(key) if isinstance(value, dict) else None
        if not isinstance(locator, dict):
            errors.append(
                f"复用Locator key缺失或无效: {path_value}:{key}"
            )
            continue
        try:
            actual = locator_mapping_fingerprint(locator)
        except ValueError:
            actual = ""
        if actual != expected:
            errors.append(
                f"复用Locator key已被修改: {path_value}:{key}"
            )
    return errors


def _validate_evidence_trace(session_dir, request, trace, changed_files):
    declared = request.get("evidence_context") or {}
    if not declared.get("available") or not declared.get("path"):
        return ["RequestV3 缺少 Evidence Context"], {
            "status": "invalid_context",
            "decision_coverage": 0.0,
        }
    try:
        path = _resolve_session_artifact(
            session_dir,
            declared.get("path"),
            "evidence-context",
        )
        context = load_evidence_context(path)
    except Exception as error:
        return [f"Evidence Context 无法读取: {type(error).__name__}: {error}"], {
            "status": "invalid_context",
            "decision_coverage": 0.0,
        }
    if (
        declared.get("context_fingerprint")
        and declared.get("context_fingerprint") != context.get("context_fingerprint")
    ):
        return ["Evidence Context 指纹与 RequestV3 不一致"], {
            "status": "stale_context",
            "decision_coverage": 0.0,
        }
    available = evidence_item_ids(context)
    cited = {
        evidence_id
        for claim in trace.get("claims") or []
        for evidence_id in claim.get("evidence_ids") or []
    }
    invalid = sorted(cited - available)
    minimum = set(context.get("minimum_decision_evidence_ids") or [])
    missing = sorted(minimum - cited) if changed_files else []
    errors = []
    if invalid:
        errors.append(f"Plan 引用未知 Evidence ID: {invalid}")
    if missing:
        errors.append(f"Plan 未覆盖最小决策证据: {missing}")
    denominator = len(minimum)
    coverage = 1.0 if not denominator else len(minimum & cited) / denominator
    return errors, {
        "status": "passed" if not errors else "failed",
        "decision_coverage": round(coverage, 4),
        "available_evidence_count": len(available),
        "cited_evidence_count": len(cited),
        "missing_minimum_evidence_ids": missing,
        "invalid_evidence_ids": invalid,
    }


def _normalize_changed_files(project_root, values):
    changed = []
    errors = []
    for value in values or []:
        path = Path(value)
        absolute = path.resolve() if path.is_absolute() else (project_root / path).resolve()
        try:
            relative = absolute.relative_to(project_root)
        except ValueError:
            errors.append(f"生成修改超出项目根目录: {value}")
            continue
        normalized = relative.as_posix()
        if not any(relative == root or root in relative.parents for root in ALLOWED_WRITE_ROOTS):
            errors.append(f"生成修改超出允许范围: {normalized}")
        changed.append(normalized)
    return list(dict.fromkeys(changed)), errors


def _snapshot_generation_roots(project_root, *, exact_files=()):
    return _snapshot_paths(
        project_root,
        ALLOWED_WRITE_ROOTS,
        exact_files=tuple(Path(path) for path in exact_files or ()),
    )


def _capture_generation_baseline(
        project_root,
        manifest,
        generation_input_snapshot,
        *,
        lease,
        output_path,
        transaction_id,
    ):
    project_root = Path(project_root).resolve()
    output_path = Path(output_path).resolve()
    files = []
    with generation_file_lease_write_guard(project_root, lease):
        for relative in sorted(manifest.get("allowed_changes") or ()):
            path = _generation_target_path(project_root, relative)
            expected = (generation_input_snapshot.get("files") or {}).get(
                relative
            )
            if path.exists() and not path.is_file():
                raise ValueError(
                    f"Generation baseline target is not a file: {relative}"
                )
            content = path.read_bytes() if path.is_file() else None
            if bool(content is not None) != bool(expected is not None):
                raise ValueError(
                    f"Generation baseline existence drifted: {relative}"
                )
            if content is not None and any((
                hashlib.sha256(content).hexdigest() != expected.get("sha256"),
                len(content) != expected.get("size"),
            )):
                raise ValueError(
                    f"Generation baseline content drifted: {relative}"
                )
            files.append({
                "path": relative,
                "exists": content is not None,
                "sha256": (
                    hashlib.sha256(content).hexdigest()
                    if content is not None
                    else None
                ),
                "size": len(content) if content is not None else 0,
                "content_base64": (
                    base64.b64encode(content).decode("ascii")
                    if content is not None
                    else None
                ),
            })
    value = {
        "generation_baseline_version": "1.0",
        "transaction_id": transaction_id,
        "implementation_manifest_fingerprint": manifest.get(
            "implementation_manifest_fingerprint"
        ),
        "files": files,
    }
    value["fingerprint"] = _receipt_fingerprint(value)
    write_json_atomic(output_path, value)
    return {
        "path": str(output_path),
        "fingerprint": value["fingerprint"],
        "file_count": len(files),
    }


def _generation_target_path(project_root, relative):
    project_root = Path(project_root).resolve()
    value = str(relative or "")
    candidate = Path(value)
    if not value or candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"Generation target path invalid: {relative}")
    path = (project_root / candidate).resolve()
    path.relative_to(project_root)
    if generation_path_has_reparse_point(project_root, candidate.as_posix()):
        raise ValueError(f"Generation target is a reparse point: {relative}")
    return path


def _load_generation_baseline(report_path, report):
    pointer = report.get("generation_baseline") or {}
    path = Path(str(pointer.get("path") or "")).resolve()
    expected_path = (
        Path(report_path).resolve().parent / "generation-baseline.json"
    )
    if path != expected_path:
        raise ValueError("Generation baseline path invalid")
    value = _read_json(path)
    payload = {
        key: item for key, item in value.items()
        if key != "fingerprint"
    }
    manifest = report.get("implementation_manifest") or {}
    if any((
        value.get("generation_baseline_version") != "1.0",
        value.get("transaction_id") != report.get("transaction_id"),
        value.get("implementation_manifest_fingerprint")
        != manifest.get("implementation_manifest_fingerprint"),
        value.get("fingerprint") != _receipt_fingerprint(payload),
        pointer.get("fingerprint") != value.get("fingerprint"),
    )):
        raise ValueError("Generation baseline identity invalid")
    files = value.get("files")
    if (
        not isinstance(files, list)
        or [item.get("path") for item in files if isinstance(item, dict)]
        != sorted(manifest.get("allowed_changes") or ())
    ):
        raise ValueError("Generation baseline scope invalid")
    for item in files:
        content_value = item.get("content_base64")
        if item.get("exists") is True:
            try:
                content = base64.b64decode(
                    str(content_value).encode("ascii"),
                    validate=True,
                )
            except (ValueError, UnicodeError) as error:
                raise ValueError(
                    "Generation baseline content invalid"
                ) from error
            if any((
                hashlib.sha256(content).hexdigest() != item.get("sha256"),
                len(content) != item.get("size"),
            )):
                raise ValueError("Generation baseline content mismatch")
        elif any((
            content_value is not None,
            item.get("sha256") is not None,
            item.get("size") not in {0, None},
        )):
            raise ValueError("Generation baseline missing-file record invalid")
    return value


def _archive_ai_implementation(project_root, report, archive_path):
    project_root = Path(project_root).resolve()
    archive_path = Path(archive_path).resolve()
    if archive_path.exists():
        result = _validate_aborted_implementation_archive(
            archive_path,
            report,
        )
        current = _ai_implementation_records(project_root, report)
        baseline = _load_generation_baseline(
            archive_path.parent / "report.json",
            report,
        )
        baseline_by_path = {
            item["path"]: {
                "path": item["path"],
                "exists": item.get("exists") is True,
                "sha256": item.get("sha256"),
                "size": item.get("size") or 0,
            }
            for item in baseline["files"]
            if item["path"] in set(
                (report.get("implementation_manifest") or {}).get(
                    "ai_editable_changes"
                ) or ()
            )
        }
        archived_by_path = {
            item["path"]: item for item in result["files"]
        }
        if any(
            _record_key(item) not in {
                _record_key(archived_by_path.get(item["path"])),
                _record_key(baseline_by_path.get(item["path"])),
            }
            for item in current
        ):
            raise ValueError(
                "Aborted implementation archive does not match current AI draft"
            )
        return result
    manifest = report.get("implementation_manifest") or {}
    records = _ai_implementation_records(project_root, report)
    temporary = archive_path.with_suffix(".tmp")
    temporary.unlink(missing_ok=True)
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as output:
            for record in records:
                relative = record["path"]
                path = _generation_target_path(project_root, relative)
                content = path.read_bytes() if path.is_file() else None
                if content is not None:
                    output.writestr(f"files/{relative}", content)
            archive_manifest = {
                "aborted_implementation_archive_version": "1.0",
                "transaction_id": report.get("transaction_id"),
                "implementation_manifest_fingerprint": manifest.get(
                    "implementation_manifest_fingerprint"
                ),
                "files": records,
            }
            archive_manifest["fingerprint"] = _receipt_fingerprint(
                archive_manifest
            )
            output.writestr(
                "archive.json",
                json.dumps(archive_manifest, ensure_ascii=False, indent=2),
            )
        os.replace(temporary, archive_path)
    finally:
        temporary.unlink(missing_ok=True)
    content = archive_path.read_bytes()
    return {
        "path": str(archive_path),
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
        "files": records,
    }


def _ai_implementation_records(project_root, report):
    project_root = Path(project_root).resolve()
    manifest = report.get("implementation_manifest") or {}
    records = []
    for relative in sorted(manifest.get("ai_editable_changes") or ()):
        path = _generation_target_path(project_root, relative)
        content = path.read_bytes() if path.is_file() else None
        records.append({
            "path": relative,
            "exists": content is not None,
            "sha256": (
                hashlib.sha256(content).hexdigest()
                if content is not None
                else None
            ),
            "size": len(content) if content is not None else 0,
        })
    return records


def _record_key(record):
    record = record or {}
    return (
        str(record.get("path") or ""),
        record.get("exists") is True,
        record.get("sha256"),
        int(record.get("size") or 0),
    )


def _validate_aborted_implementation_archive(archive_path, report):
    archive_path = Path(archive_path).resolve()
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            value = json.loads(archive.read("archive.json").decode("utf-8"))
            files = value.get("files")
            if not isinstance(files, list):
                raise ValueError("Aborted implementation archive files invalid")
            manifest = report.get("implementation_manifest") or {}
            expected_paths = sorted(manifest.get("ai_editable_changes") or ())
            actual_paths = [
                item.get("path")
                for item in files
                if isinstance(item, dict)
            ]
            if actual_paths != expected_paths or len(actual_paths) != len(files):
                raise ValueError("Aborted implementation archive scope invalid")
            expected_members = {"archive.json"}
            for item in files:
                if item.get("exists") is not True:
                    continue
                member = f"files/{item['path']}"
                expected_members.add(member)
                content = archive.read(member)
                if any((
                    hashlib.sha256(content).hexdigest()
                    != item.get("sha256"),
                    len(content) != item.get("size"),
                )):
                    raise ValueError("Aborted implementation archive drifted")
            if set(archive.namelist()) != expected_members:
                raise ValueError("Aborted implementation archive members invalid")
    except (OSError, KeyError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Aborted implementation archive invalid") from error
    payload = {
        key: item for key, item in value.items()
        if key != "fingerprint"
    }
    manifest = report.get("implementation_manifest") or {}
    if any((
        value.get("aborted_implementation_archive_version") != "1.0",
        value.get("transaction_id") != report.get("transaction_id"),
        value.get("implementation_manifest_fingerprint")
        != manifest.get("implementation_manifest_fingerprint"),
        value.get("fingerprint") != _receipt_fingerprint(payload),
    )):
        raise ValueError("Aborted implementation archive identity invalid")
    content = archive_path.read_bytes()
    return {
        "path": str(archive_path),
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
        "files": value.get("files") or [],
    }


def snapshot_runtime_code(project_root):
    return _snapshot_generation_roots(project_root)


def _implementation_snapshot_paths(manifest, changed_files):
    manifest = manifest if isinstance(manifest, dict) else {}
    return sorted({
        *(
            str(path)
            for path in changed_files or ()
            if str(path)
        ),
        *(
            str(path)
            for path in manifest.get("allowed_changes") or ()
            if str(path)
        ),
        *(
            str(path)
            for path in manifest.get("read_only_reuse") or ()
            if str(path)
        ),
    })


def _implementation_candidate_matches(report_path, report, project_root):
    return _implementation_candidate_workspace_audit(
        report_path,
        report,
        project_root,
    )["matches"]


def _implementation_candidate_workspace_audit(
        report_path,
        report,
        project_root,
    ):
    try:
        candidate = load_implementation_scaffold_candidate(
            _session_dir_for_transaction_report_path(report_path),
            (report.get("system_materialization") or {}).get("candidate"),
            transaction_id=report.get("transaction_id"),
        )
    except (OSError, TypeError, ValueError) as error:
        return {
            "status": "invalid_candidate",
            "matches": False,
            "files": [],
            "error": f"{type(error).__name__}: {error}",
        }
    manifest = report.get("implementation_manifest") or {}
    if any((
        candidate.get("manifest_id")
        != manifest.get("implementation_manifest_id"),
        candidate.get("manifest_fingerprint")
        != manifest.get("implementation_manifest_fingerprint"),
    )):
        return {
            "status": "manifest_mismatch",
            "matches": False,
            "files": [],
            "error": "Implementation candidate与Manifest不一致",
        }
    return implementation_scaffold_candidate_workspace_audit(
        project_root,
        candidate,
    )


def _implementation_candidate_audit_errors(audit):
    errors = []
    for item in audit.get("files") or ():
        if item.get("status") == "matches":
            continue
        detail = (
            f"expected_sha256={item.get('expected_sha256')}, "
            f"actual_sha256={item.get('actual_sha256')}"
        )
        errors.append(
            "Implementation candidate file "
            f"{item.get('status')}: {item.get('path')} ({detail})"
        )
    if not errors and not audit.get("matches"):
        errors.append(
            "Implementation candidate audit failed: "
            f"{audit.get('error') or audit.get('status')}"
        )
    return errors


def transaction_code_snapshot_matches(report, project_root):
    if not isinstance(report, dict):
        return False
    project_root = Path(project_root).resolve()
    runtime_snapshot = report.get("runtime_code_snapshot")
    if (
        not isinstance(runtime_snapshot, dict)
        or report.get("runtime_code_snapshot_fingerprint")
        != runtime_code_snapshot_fingerprint(runtime_snapshot)
        or snapshot_runtime_code(project_root) != runtime_snapshot
    ):
        return False
    implementation_snapshot = report.get("implementation_snapshot")
    if not isinstance(implementation_snapshot, list):
        return False
    paths = []
    for item in implementation_snapshot:
        if not isinstance(item, dict) or not item.get("path"):
            return False
        paths.append(item["path"])
    return snapshot_files(paths, project_root=project_root) == (
        implementation_snapshot
    )


def _snapshot_protected_paths(project_root):
    return _snapshot_paths(
        project_root,
        PROTECTED_WRITE_ROOTS,
        exact_files=PROTECTED_ROOT_FILES,
    )


def _snapshot_project_guard(project_root):
    project_root = Path(project_root).resolve()
    files = {}
    for path in sorted(
        item
        for item in project_root.rglob("*")
        if item.is_file() or item.is_symlink()
    ):
        relative_path = path.relative_to(project_root)
        if any(
            relative_path == root or root in relative_path.parents
            for root in (*ALLOWED_WRITE_ROOTS, *PROJECT_GUARD_EXCLUDED_ROOTS)
        ):
            continue
        if "__pycache__" in relative_path.parts or path.suffix.casefold() in {
            ".pyc",
            ".pyo",
        }:
            continue
        relative = relative_path.as_posix()
        files[relative] = _snapshot_file_record(path)
    return {
        "snapshot_version": "1.0",
        "excluded_roots": [
            path.as_posix()
            for path in (*ALLOWED_WRITE_ROOTS, *PROJECT_GUARD_EXCLUDED_ROOTS)
        ],
        "files": files,
    }


def _project_guard_changed_paths(before, after):
    return _changed_snapshot_paths(
        _filter_project_guard_snapshot(before),
        _filter_project_guard_snapshot(after),
    )


def _filter_project_guard_snapshot(snapshot):
    files = {}
    for relative, record in (snapshot.get("files") or {}).items():
        relative_path = Path(str(relative))
        if any(
            relative_path == root or root in relative_path.parents
            for root in PROJECT_GUARD_EXCLUDED_ROOTS
        ):
            continue
        files[str(relative).replace("\\", "/")] = record
    return {
        "snapshot_version": snapshot.get("snapshot_version"),
        "files": files,
    }


def _snapshot_paths(project_root, roots, *, exact_files):
    project_root = Path(project_root).resolve()
    files = {}
    for root in roots:
        directory = project_root / root
        if not directory.exists():
            continue
        for path in sorted(
            item
            for item in directory.rglob("*")
            if item.is_file() or item.is_symlink()
        ):
            if "__pycache__" in path.parts or path.suffix.casefold() in {
                ".pyc",
                ".pyo",
            }:
                continue
            relative = path.relative_to(project_root).as_posix()
            record = _snapshot_file_record(path)
            if not path.is_symlink() and path.suffix.casefold() == ".py":
                calls = snapshot_runtime_variable_calls(path)
                if calls:
                    record["runtime_variable_calls"] = calls
            files[relative] = record
    for relative in exact_files:
        path = project_root / relative
        if path.is_file() or path.is_symlink():
            record = _snapshot_file_record(path)
            if not path.is_symlink() and path.suffix.casefold() == ".py":
                calls = snapshot_runtime_variable_calls(path)
                if calls:
                    record["runtime_variable_calls"] = calls
            files[relative.as_posix()] = record
    return {
        "snapshot_version": "1.0",
        "roots": [path.as_posix() for path in roots],
        "exact_files": [path.as_posix() for path in exact_files],
        "files": files,
    }


def _actual_generation_changes(
        project_root,
        baseline,
        reported,
        *,
        derive_reported=False,
    system_owned=(),
    ):
    errors = []
    reported = list(reported or ())
    if baseline.get("snapshot_version") != "1.0":
        return [], ["Generation transaction 缺少有效输入快照"], {
            "status": "failed",
            "reported_source": (
                "derived_from_baseline"
                if derive_reported
                else "caller"
            ),
            "actual": [],
            "reported": list(reported),
            "unreported": [],
            "falsely_reported": list(reported),
        }
    current = _snapshot_generation_roots(
        project_root,
        exact_files=baseline.get("exact_files") or (),
    )
    symlinks = _snapshot_symlinks(current)
    if symlinks:
        errors.append(f"generation roots包含符号链接: {symlinks}")
    actual = _changed_snapshot_paths(baseline, current)
    if derive_reported:
        reported = list(actual)
    else:
        actual_set = set(actual)
        reported.extend(
            sorted(actual_set & set(system_owned or ()))
        )
    reported_set = set(reported)
    actual_set = set(actual)
    unreported = sorted(actual_set - reported_set)
    falsely_reported = sorted(reported_set - actual_set)
    if unreported:
        errors.append(f"生成修改未申报: {unreported}")
    if falsely_reported:
        errors.append(f"申报文件在事务中未变化: {falsely_reported}")
    return actual, errors, {
        "status": "passed" if not errors else "failed",
        "reported_source": (
            "derived_from_baseline"
            if derive_reported
            else "caller"
        ),
        "actual": actual,
        "reported": sorted(reported_set),
        "unreported": unreported,
        "falsely_reported": falsely_reported,
    }


def _changed_snapshot_paths(before, after):
    before_files = before.get("files") or {}
    after_files = after.get("files") or {}
    return sorted(
        path
        for path in set(before_files) | set(after_files)
        if before_files.get(path) != after_files.get(path)
    )


def _git_allowed_change_state_errors(project_root, paths):
    return _normalize_git_allowed_change_state(project_root, paths)["errors"]


def _normalize_git_allowed_change_state(project_root, paths):
    paths = sorted({str(path).replace("\\", "/") for path in paths or ()})
    if not paths:
        return {"status": "not_applicable", "normalized": [], "errors": []}
    project_root = Path(project_root).resolve()
    git = ["git", "-C", str(project_root), "-c", "core.quotepath=false"]
    try:
        inside = subprocess.run(
            [*git, "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, ValueError):
        return {"status": "not_applicable", "normalized": [], "errors": []}
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return {"status": "not_applicable", "normalized": [], "errors": []}
    tracked = set()
    head = subprocess.run(
        [*git, "ls-tree", "-r", "--name-only", "HEAD", "--", *paths],
        capture_output=True,
        text=True,
        check=False,
    )
    if head.returncode == 0:
        tracked = {line.strip() for line in head.stdout.splitlines() if line.strip()}
    status = subprocess.run(
        [*git, "status", "--porcelain=v1", "-uall", "--", *paths],
        capture_output=True,
        text=True,
        check=False,
    )
    if status.returncode != 0:
        return {
            "status": "failed",
            "normalized": [],
            "errors": ["无法读取Git工作区状态，拒绝生成写入目标"],
        }
    errors = []
    normalized = []
    path_set = set(paths)
    for line in status.stdout.splitlines():
        if len(line) < 4:
            continue
        state = line[:2]
        relative = line[3:]
        if " -> " in relative:
            relative = relative.rsplit(" -> ", 1)[-1]
        if relative not in path_set:
            continue
        index_state, worktree_state = state[0], state[1]
        if state == "??":
            continue
        if index_state == "D" and relative in tracked:
            error = _run_git_normalization(
                git,
                ["restore", "--staged", "--", relative],
                relative,
            )
            if error:
                errors.append(error)
                continue
            path = project_root / relative
            if path.exists():
                normalized.append({
                    "path": relative,
                    "action": "unstage_tracked_delete",
                })
            else:
                normalized.append({
                    "path": relative,
                    "action": "unstage_user_deleted_tracked_file",
                })
        elif index_state != " ":
            errors.append(
                "生成目标存在Git暂存区变更，拒绝生成: "
                f"{relative} status={state}"
            )
        elif worktree_state == "D" and relative in tracked:
            normalized.append({
                "path": relative,
                "action": "accept_user_deleted_tracked_file",
            })
    return {
        "status": "normalized" if normalized and not errors else (
            "failed" if errors else "clean"
        ),
        "normalized": normalized,
        "errors": errors,
    }


def _run_git_normalization(git, arguments, relative):
    try:
        result = subprocess.run(
            [*git, *arguments],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, ValueError) as error:
        return f"无法修复Git生成目标状态: {relative}: {type(error).__name__}"
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "").strip()
        return f"无法修复Git生成目标状态: {relative}: {message}"
    return None


def _snapshot_file_record(path):
    path = Path(path)
    if path.is_symlink():
        try:
            target = str(path.readlink())
        except OSError:
            target = None
        return {
            "is_symlink": True,
            "link_target": target,
        }
    return {
        "sha256": _sha256_file(path),
        "size": path.stat().st_size,
    }


def _snapshot_symlinks(snapshot):
    return sorted(
        str(path)
        for path, record in (snapshot.get("files") or {}).items()
        if isinstance(record, dict) and record.get("is_symlink") is True
    )


def _sha256_file(path):
    import hashlib

    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _required_validations(changed_files):
    paths = [Path(value) for value in changed_files]
    required = set()
    if any(path.suffix.casefold() == ".py" for path in paths):
        required.add("python_compile")
    if any(
        Path("Bdd/locators") in path.parents
        and path.suffix.casefold() in {".yaml", ".yml"}
        for path in paths
    ):
        required.add("locator_compile")
    if any(Path("Bdd/steps") in path.parents for path in paths):
        required.add("step_scope")
    if any(
        Path("Bdd/data") in path.parents
        and path.suffix.casefold() in {".yaml", ".yml"}
        for path in paths
    ):
        required.add("data_content")
    return sorted(required)


def _execution_outcome(
        request,
        transaction_status,
        *,
        unresolved_issues=(),
    ):
    execution = dict(request.get("execution") or {})
    completed = transaction_status in {"completed", "completed_no_changes"}
    if completed and unresolved_issues:
        return {
            "execution_outcome_version": "1.0",
            "static_status": "generated_with_issues",
            "runtime_status": "runtime_blocked",
            "status": "generated_with_issues/runtime_blocked",
            "execution_mode": execution.get("mode") or "not_configured",
            "runtime_policy": execution.get("runtime_policy") or "static_only",
            "unresolved_issue_count": len(unresolved_issues),
        }
    runtime_allowed = execution.get("runtime_policy") == "allowed"
    static_status = "static_validated" if completed else "static_validation_failed"
    runtime_status = "runtime_pending" if completed and runtime_allowed else "runtime_not_run"
    return {
        "execution_outcome_version": "1.0",
        "static_status": static_status,
        "runtime_status": runtime_status,
        "status": f"{static_status}/{runtime_status}",
        "execution_mode": execution.get("mode") or "not_configured",
        "runtime_policy": execution.get("runtime_policy") or "static_only",
    }


def _plan_unresolved_issues(plan_artifact):
    plan = (plan_artifact or {}).get("plan") or {}
    return [
        dict(issue)
        for step in (plan.get("steps") or {}).values()
        if isinstance(step, dict)
        for issue in step.get("unresolved_issues") or ()
        if isinstance(issue, dict)
    ]


def _generation_unresolved_issues(plan_artifact, implementation_manifest):
    result = []
    seen = set()
    for issue in [
            *_plan_unresolved_issues(plan_artifact),
            *(
                dict(item)
                for item in (implementation_manifest or {}).get(
                    "unresolved_issues"
                ) or ()
                if isinstance(item, dict)
            ),
    ]:
        identity = str(issue.get("issue_id") or "") or json.dumps(
            issue,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if identity in seen:
            continue
        seen.add(identity)
        result.append(issue)
    return result


def _completion_status(
        changed,
        *,
    change_errors,
        scope_errors,
        lease_errors,
        artifact_errors,
        policy_errors,
        validation_errors,
        plan_errors,
        pic_errors,
        evidence_errors,
):
    if change_errors:
        return "change_set_mismatch"
    if scope_errors:
        return "scope_violation"
    if lease_errors or artifact_errors:
        return "stale_during_generation"
    if policy_errors or pic_errors:
        return "policy_violation"
    if validation_errors:
        return "failed_validation"
    if plan_errors:
        return "failed_plan_conformance"
    if evidence_errors:
        return "failed_evidence_audit"
    return "completed" if changed else "completed_no_changes"


def _validate_report_identity(report_path, report):
    _validate_report_static_identity(report_path, report)
    if report.get("status") != "running":
        raise ValueError(f"事务不能重复完成: status={report.get('status')}")


def _validate_terminal_report_identity(report_path, report):
    _validate_report_static_identity(report_path, report)
    status = str(report.get("status") or "")
    if status == "running":
        raise ValueError("事务报告尚未进入终态")
    declared_result = report.get("result_fingerprint")
    if (
            not declared_result
            or transaction_result_fingerprint(report) != declared_result
    ):
        raise ValueError("GenerationTransaction 终态结果指纹无效")
    if status in {"completed", "completed_no_changes"}:
        declared_completion = report.get("completion_fingerprint")
        if (
                not declared_completion
                or completed_report_fingerprint(report)
                != declared_completion
        ):
            raise ValueError("GenerationTransaction 完成报告指纹无效")
        pointer = report.get("implementation_diff")
        if not generation_diff_pointer_is_valid(
                pointer,
                transaction_id=report.get("transaction_id"),
        ):
            raise ValueError("GenerationTransaction implementation diff无效")
        load_generation_diff(
            report.get("session_dir"),
            pointer,
            offset=0,
            limit=1,
        )


def _validate_recoverable_terminal_report_identity(report_path, report):
    if (report.get("terminal_snapshot_audit") or {}).get("status") != "pending":
        _validate_terminal_report_identity(report_path, report)
        return
    _validate_report_static_identity(report_path, report)
    if report.get("status") not in {"completed", "completed_no_changes"}:
        raise ValueError("GenerationTransaction恢复终态无效")
    if any((
        report.get("implementation_diff") is not None,
        transaction_result_fingerprint(report)
        != report.get("result_fingerprint"),
        completed_report_fingerprint(report)
        != report.get("completion_fingerprint"),
    )):
        raise ValueError("GenerationTransaction恢复终态身份无效")


def _validate_report_static_identity(report_path, report):
    required = (
        "transaction_id",
        "transaction_nonce",
        "request_id",
        "request_path",
        "session_dir",
        "project_root",
        "lease",
        "implementation_manifest",
        "candidate_preflight",
        "generation_job_lease",
        "generation_job_claim_id",
    )
    missing = [name for name in required if not report.get(name)]
    if report.get("transaction_version") != TRANSACTION_VERSION or missing:
        raise ValueError(f"GenerationTransactionV3 身份无效: missing={missing}")
    if not implementation_manifest_identity_is_valid(
            report.get("implementation_manifest")
        ):
        raise ValueError("GenerationTransactionV3 Implementation Manifest无效")
    root = (
        Path(report["session_dir"]) / "ai" / "generation-transactions"
    ).resolve()
    try:
        relative = report_path.relative_to(root)
    except ValueError as error:
        raise ValueError(f"事务报告路径越界: {report_path}") from error
    if (
        len(relative.parts) != 2
        or relative.parts[0] != report.get("transaction_id")
        or not relative.parts[0].startswith("transaction-")
        or relative.parts[1] != "report.json"
    ):
        raise ValueError("事务报告路径与 transaction_id 不一致")


def _session_dir_for_transaction_report_path(report_path):
    report_path = Path(report_path).resolve()
    if any((
        report_path.name != "report.json",
        len(report_path.parents) < 4,
        not report_path.parent.name.startswith("transaction-"),
        report_path.parents[1].name != "generation-transactions",
        report_path.parents[2].name != "ai",
    )):
        raise ValueError(
            f"GenerationTransaction report 路径无效: {report_path}"
        )
    return report_path.parents[3]


def _resolve_session_artifact(
        session_dir,
        value,
        expected_directory,
):
    if not value:
        raise ValueError(f"缺少 artifact path: {expected_directory}")
    session_dir = Path(session_dir).resolve()
    path = Path(value)
    path = path.resolve() if path.is_absolute() else (session_dir / path).resolve()
    root = (session_dir / "ai" / expected_directory).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError(f"artifact 路径越界: {path}") from error
    return path


def _transaction_path(session_dir, value):
    if not value:
        return None
    try:
        return _resolve_session_artifact(
            session_dir,
            value,
            "generation-transactions",
        )
    except ValueError:
        return None


def _block_missing_plan(session_dir, request, state):
    return _job_block_result(
        request,
        state,
        "missing_generation_plan",
        [f"Workflow implementation阶段缺少有效GenerationPlanV{PLAN_VERSION}"],
    )


def _block_stale(session_dir, request, state, reason):
    return _job_block_result(
        request,
        state,
        "stale_during_generation",
        [reason],
    )


def _block_contract_changed(session_dir, request, state):
    return _job_block_result(
        request,
        state,
        "generation_contract_changed",
        ["Generation Contract在Job期间已变化"],
    )


def _block_commit_rebase_required(session_dir, request, state, reason):
    return _job_block_result(
        request,
        state,
        "commit_rebase_required",
        [reason],
    )


def _block_pic_policy(session_dir, request, state, audit):
    result = _job_block_result(
        request,
        state,
        "invalid_pic_authorization",
        audit.get("errors") or ["PIC authorization无效"],
    )
    result["pic_authorization_audit"] = audit
    return result


def _job_block_result(request, state, category, errors):
    return {
        "transaction_version": TRANSACTION_VERSION,
        "status": "job_blocked",
        "job_failure_category": category,
        "request_id": request.get("request_id"),
        "workflow_state": state,
        "errors": [str(item) for item in errors],
        "warnings": [],
    }


def _record_memory(report_path, report):
    try:
        events = record_transaction_completed(report_path, report)
        report["project_memory"] = {
            "recorded": [event["memory_id"] for event in events],
            "warnings": [],
        }
    except Exception as error:
        report["project_memory"] = {
            "recorded": [],
            "warnings": [
                f"生成结果未写入项目记忆: {type(error).__name__}: {error}"
            ],
        }


def _read_json(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 必须是 object: {path}")
    return value
