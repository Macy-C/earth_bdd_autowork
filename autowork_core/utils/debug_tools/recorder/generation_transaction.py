from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import zipfile
from datetime import datetime
from pathlib import Path

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
from autowork_core.utils.debug_tools.recorder.generation_job import (
    generation_job_lease_is_valid,
)
from autowork_core.utils.debug_tools.recorder.generation_job_result import (
    publish_static_job_outcome,
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
    build_implementation_packet,
    build_implementation_manifest,
    implementation_manifest_identity_is_valid,
    implementation_manifest_matches_transaction,
)
from autowork_core.utils.debug_tools.recorder.implementation_materializer import (
    materialize_implementation_scaffold,
    rollback_implementation_scaffold,
    system_materialization_matches,
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
    JOB_WORKFLOW_STATE_VERSION,
    load_workflow_state,
    transition_generation_job,
    transition_workflow,
    write_workflow_state,
)
from autowork_core.utils.debug_tools.recorder.workflow_service import inspect_workflow
from autowork_core.utils.debug_tools.recorder.transaction_integrity import (
    completed_report_fingerprint,
    runtime_code_snapshot_fingerprint,
    transaction_result_fingerprint,
)
from autowork_core.utils.debug_tools.recorder.writer import write_json_atomic


TRANSACTION_VERSION = "3.0"
IMPLEMENTATION_RECEIPT_VERSION = "2.0"
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
    job_bound = bool(generation_job_lease)
    if existing.get("current_job") and not job_bound:
        raise ValueError("当前Workflow必须使用Generation Job prepare入口")
    if job_bound:
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
                return _resume_generation_transaction(
                    session_dir,
                    request,
                    existing,
                    path,
                    report,
                    project_root=effective_project_root,
                )
    try:
        orphan = find_committed_generation_file_lease_report(
            effective_project_root,
            request.get("request_id"),
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
            return _resume_generation_transaction(
                session_dir,
                request,
                existing,
                path,
                report,
                project_root=effective_project_root,
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
    ) if job_bound else state.get("status") == "ready"
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
    if job_bound and (plan.get("source") or {}).get(
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
    if plan.get("plan_version") != PLAN_VERSION:
        state["status"] = "draft"
        state["next_action"] = "submit_window_owned_plan"
        state["errors"] = [
            f"历史 Plan 可审阅，但新事务必须提交 validated PlanV{PLAN_VERSION}"
        ]
        write_workflow_state(session_dir, state)
        return {
            "transaction_version": TRANSACTION_VERSION,
            "status": "draft",
            "request_id": request.get("request_id"),
            "request_path": str(request_path),
            "workflow_state": state,
            "errors": list(state["errors"]),
            "warnings": [],
        }
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
    generation_input_snapshot = _snapshot_generation_roots(project_root)
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
        "workflow_version": "3.0",
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
        "implementation_manifest": implementation_manifest,
        "implementation_packet": build_implementation_packet(
            implementation_manifest
        ),
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
        report["generation_baseline"] = _capture_generation_baseline(
            project_root,
            implementation_manifest,
            generation_input_snapshot,
            lease=report["generation_file_lease"],
            output_path=output.parent / "generation-baseline.json",
            transaction_id=transaction_id,
        )
        write_json_atomic(output, report)
        report["system_materialization"] = materialize_implementation_scaffold(
            project_root,
            implementation_manifest,
            generation_input_snapshot,
            lease=report["generation_file_lease"],
            journal_path=output.parent / "materialization-journal.json",
        )
        write_json_atomic(output, report)
    except GenerationFileConflict as error:
        output.unlink(missing_ok=True)
        return _block_stale(session_dir, request, state, str(error))
    except Exception:
        try:
            rollback_implementation_scaffold(
                project_root,
                report.get("system_materialization") or {},
                lease=report.get("generation_file_lease"),
                manifest=implementation_manifest,
                journal_path=output.parent / "materialization-journal.json",
            )
        except Exception:
            pass
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
        if job_bound:
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
        else:
            transition_workflow(
                session_dir,
                request["request_id"],
                status="running",
                transaction=pointer,
            )
    except Exception:
        try:
            rollback_implementation_scaffold(
                project_root,
                report.get("system_materialization") or {},
                lease=report.get("generation_file_lease"),
                manifest=implementation_manifest,
                journal_path=output.parent / "materialization-journal.json",
            )
        except Exception:
            pass
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
        try:
            _validate_report_identity(report_path, report)
        except ValueError as error:
            errors.append(str(error))
        if (
            not errors
            and _report_requires_generation_contract_lease(report)
            and not generation_contract_lease_matches(
                session_dir,
                report.get("generation_contract_lease"),
            )
        ):
            aborted = _abort_generation_transaction_locked(
                report_path,
                reason=(
                    "Generation Contract changed while the transaction was "
                    "running; archive the draft and re-submit Design."
                ),
                project_root=project_root,
                generation_job_claim_id=(
                    (state.get("job_execution") or {}).get("claim_id")
                    if report.get("generation_job_lease")
                    else None
                ),
                generation_job_expected_epoch=(
                    (state.get("job_execution") or {}).get("epoch")
                    if report.get("generation_job_lease")
                    else None
                ),
            )
            if report.get("generation_job_lease"):
                return {
                    **aborted,
                    "workflow_state": load_workflow_state(
                        session_dir,
                        request.get("request_id"),
                    ),
                }
            refreshed = load_workflow_state(
                session_dir,
                request.get("request_id"),
            )
            refreshed.update({
                "status": "draft",
                "next_action": "submit_generation_design",
                "plan": {},
                "active_transaction": None,
                "errors": [],
                "warnings": [
                    "生成能力已更新；草稿已归档并恢复基线，"
                    "业务Request和Decision保持有效，请重新提交Design"
                ],
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            })
            write_workflow_state(session_dir, refreshed)
            return {
                "transaction_version": TRANSACTION_VERSION,
                "status": "draft",
                "request_id": request.get("request_id"),
                "workflow_state": refreshed,
                "aborted_transaction": {
                    "transaction_id": aborted.get("transaction_id"),
                    "report_path": aborted.get("report_path"),
                    "draft_archive": (
                        aborted.get("abort") or {}
                    ).get("draft_archive"),
                },
                "errors": [],
                "warnings": list(refreshed["warnings"]),
            }
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
            report["system_materialization"] = (
                materialize_implementation_scaffold(
                    project_root,
                    report.get("implementation_manifest") or {},
                    report.get("generation_input_snapshot") or {},
                    lease=report.get("generation_file_lease"),
                    journal_path=report_path.parent
                    / "materialization-journal.json",
                )
            )
            write_json_atomic(report_path, report)
        except (OSError, TypeError, ValueError) as error:
            errors.append(
                "Implementation scaffold recovery failed: "
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
        if report.get("generation_job_lease"):
            if (state.get("active_transaction") or {}) != pointer:
                return _block_stale(
                    session_dir,
                    request,
                    state,
                    "Generation Job active Transaction pointer不一致",
                )
        else:
            transition_workflow(
                session_dir,
                request["request_id"],
                status="running",
                transaction=pointer,
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
        "workflow_version": state.get("workflow_state_version")
        == JOB_WORKFLOW_STATE_VERSION,
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
    if report.get("generation_job_lease"):
        return publish_static_job_outcome(
            session_dir,
            request_id,
            report_path,
            report,
        )
    status = str(report.get("status") or "")
    transition_workflow(
        session_dir,
        request_id,
        status=(
            "completed"
            if status in {"completed", "completed_no_changes"}
            else "failed"
        ),
        transaction=None,
        result={
            "transaction_id": report.get("transaction_id"),
            "report_path": str(report_path),
            "status": status,
            "completion_fingerprint": report.get("completion_fingerprint"),
            "result_fingerprint": report.get("result_fingerprint"),
        },
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
    if report.get("generation_job_lease"):
        job_errors = _generation_job_context_errors(
            state,
            request,
            report.get("generation_job_lease") or {},
            claim_id=generation_job_claim_id,
            expected_epoch=generation_job_expected_epoch,
            expected_phase="implementation",
        )
        if (
            generation_job_claim_id
            != report.get("generation_job_claim_id")
        ):
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
        _snapshot_generation_roots(root),
    ))
    allowed = set(
        (report.get("implementation_manifest") or {}).get(
            "allowed_changes"
        ) or ()
    )
    unexpected = sorted(changed - allowed)
    if unexpected:
        raise ValueError(
            f"Abort found generation changes outside transaction: {unexpected}"
        )
    if _snapshot_project_guard(root) != report.get("project_guard_snapshot"):
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
        _snapshot_generation_roots(project_root),
    ))
    allowed = set(
        (report.get("implementation_manifest") or {}).get(
            "allowed_changes"
        ) or ()
    )
    unexpected = sorted(changed - allowed)
    if unexpected:
        raise ValueError(
            f"Abort recovery found changes outside transaction: {unexpected}"
        )
    if _snapshot_project_guard(project_root) != report.get(
            "project_guard_snapshot"):
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
        rollback_implementation_scaffold(
            project_root,
            report.get("system_materialization") or {},
            lease=report.get("generation_file_lease"),
            manifest=report.get("implementation_manifest") or {},
            journal_path=report_path.parent / "materialization-journal.json",
        )
        report = _write_abort_progress(
            report_path,
            report,
            phase="system_materialization_rolled_back",
            system_materialization_rolled_back=True,
        )
        restored = _restore_generation_baseline(
            project_root,
            report,
            report_path,
        )
        report = _write_abort_progress(
            report_path,
            report,
            phase="generation_baseline_restored",
            restored_files=restored,
        )
    _validate_aborted_implementation_archive(
        report_path.parent / "aborted-implementation.zip",
        report,
    )
    if _snapshot_generation_roots(project_root) != report.get(
            "generation_input_snapshot"):
        raise ValueError("Abort recovery generation baseline mismatch")
    if _snapshot_project_guard(project_root) != report.get(
            "project_guard_snapshot"):
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
        }
        report["stage_timing_ledger"] = _complete_transaction_timing(
            report,
            completed_at.isoformat(timespec="milliseconds"),
        )
        report["result_fingerprint"] = transaction_result_fingerprint(report)
        write_json_atomic(report_path, report)
    if report.get("generation_job_lease"):
        publish_static_job_outcome(
            session_dir,
            request["request_id"],
            report_path,
            report,
        )
    else:
        transition_workflow(
            session_dir,
            request["request_id"],
            status="ready",
            transaction=None,
            result={
                "transaction_id": report.get("transaction_id"),
                "report_path": str(report_path),
                "status": "aborted",
                "result_fingerprint": report["result_fingerprint"],
            },
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
    if report.get("generation_job_lease"):
        job_errors = _generation_job_context_errors(
            state,
            request,
            report.get("generation_job_lease") or {},
            claim_id=generation_job_claim_id,
            expected_epoch=generation_job_expected_epoch,
            expected_phase="implementation",
        )
        if (
            generation_job_claim_id != report.get(
                "generation_job_claim_id"
            )
        ):
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
    system_materialization = report.get("system_materialization") or {}
    if derive_changed_files:
        if system_materialization.get("status") == "materialized":
            if not system_materialization_matches(
                    project_root,
                    system_materialization,
            ):
                materialization_errors.append(
                    "Implementation scaffold changed after system "
                    "materialization"
                )
        else:
            materialization_errors.append(
                "Implementation scaffold was not materialized during prepare"
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
    implementation_manifest = report.get("implementation_manifest") or {}
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
    change_audit["implementation_manifest"] = {
        "allowed_changes": sorted(manifest_allowed),
        "read_only_reuse": sorted(manifest_read_only),
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
        guard_changes = _changed_snapshot_paths(
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
    if (
        _report_requires_generation_contract_lease(report)
        and not generation_contract_lease_matches(
            session_dir,
            report.get("generation_contract_lease"),
        )
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
    if (
        system_materialization.get("status") == "materialized"
        and not system_materialization_matches(
            project_root,
            system_materialization,
        )
    ):
        artifact_errors.append(
            "Implementation scaffold changed after system materialization"
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
        ) in {"1.6", "1.7"}
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
        }
        report["stage_timing_ledger"] = _update_stage_timing(
            report,
            "implementation",
            source="implementation_validation_ledger",
            started_at=validation_started_at,
            finished_at=_now_millis(),
            duration_ms=_elapsed_ms(validation_started_monotonic),
        )
        report["system_materialization"] = system_materialization
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
    report.update({
        "status": status,
        "completed_at": completed_at.isoformat(timespec="seconds"),
        "execution_outcome": _execution_outcome(request, status),
        "changed_files": changed,
        "reported_changed_files": reported,
        "change_set_audit": change_audit,
        "system_materialization": system_materialization,
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
        "implementation_snapshot": snapshot_files(
            changed,
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
        report.get("changed_files") or (),
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
        "validation_ledger": dict(
            report.get("implementation_validation_ledger") or {}
        ),
        "summary": str(report.get("summary") or ""),
    }
    value["fingerprint"] = _receipt_fingerprint(value)
    return value


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
                _validate_terminal_report_identity(report_path, report)
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
    declared = (plan.get("plan") or {}).get("memory_trace") or {}
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
    elif applied or dismissed:
        status = "passed"
    elif available:
        status = "not_provided"
        warnings.append(
            "Brief 含相关经验，但 Plan 未声明采用或拒绝"
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


def _snapshot_generation_roots(project_root):
    return _snapshot_paths(
        project_root,
        ALLOWED_WRITE_ROOTS,
        exact_files=(),
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


def _restore_generation_baseline(project_root, report, report_path):
    project_root = Path(project_root).resolve()
    baseline = _load_generation_baseline(report_path, report)
    restored = []
    with generation_file_lease_write_guard(
            project_root,
            report.get("generation_file_lease"),
    ):
        for item in baseline["files"]:
            relative = item["path"]
            path = _generation_target_path(project_root, relative)
            if item.get("exists") is True:
                content = base64.b64decode(
                    item["content_base64"].encode("ascii"),
                    validate=True,
                )
                _atomic_write_bytes(path, content)
            else:
                if path.exists() and not path.is_file():
                    raise ValueError(
                        f"Abort target changed type: {relative}"
                    )
                path.unlink(missing_ok=True)
            restored.append(relative)
    return restored


def _atomic_write_bytes(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def snapshot_runtime_code(project_root):
    return _snapshot_generation_roots(project_root)


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
    current = _snapshot_generation_roots(project_root)
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


def _execution_outcome(request, transaction_status):
    execution = dict(request.get("execution") or {})
    completed = transaction_status in {"completed", "completed_no_changes"}
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


def _report_requires_generation_contract_lease(report):
    pointer = report.get("generation_plan") or {}
    if pointer.get("generation_contract_lease_fingerprint"):
        return True
    plan_path = Path(str(report.get("plan_path") or ""))
    try:
        plan = _read_json(plan_path)
    except (OSError, TypeError, ValueError):
        return False
    return plan.get("plan_version") == PLAN_VERSION


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
    if state.get("workflow_state_version") == JOB_WORKFLOW_STATE_VERSION:
        return _job_block_result(
            request,
            state,
            "missing_generation_plan",
            [f"Workflow implementation阶段缺少有效GenerationPlanV{PLAN_VERSION}"],
        )
    state = transition_workflow(
        session_dir,
        request["request_id"],
        status="blocked",
        result={"status": "missing_generation_plan"},
    )
    return {
        "transaction_version": TRANSACTION_VERSION,
        "status": "blocked",
        "request_id": request.get("request_id"),
        "workflow_state": state,
        "errors": [f"Workflow ready 但缺少有效 GenerationPlanV{PLAN_VERSION}"],
        "warnings": [],
    }


def _block_stale(session_dir, request, state, reason):
    if state.get("workflow_state_version") == JOB_WORKFLOW_STATE_VERSION:
        return _job_block_result(
            request,
            state,
            "stale_during_generation",
            [reason],
        )
    state = transition_workflow(
        session_dir,
        request["request_id"],
        status="stale",
        result={"status": "stale", "reason": reason},
    )
    return {
        "transaction_version": TRANSACTION_VERSION,
        "status": "stale",
        "request_id": request.get("request_id"),
        "workflow_state": state,
        "errors": [reason],
        "warnings": [],
    }


def _block_contract_changed(session_dir, request, state):
    if state.get("workflow_state_version") == JOB_WORKFLOW_STATE_VERSION:
        return _job_block_result(
            request,
            state,
            "generation_contract_changed",
            ["Generation Contract在Job期间已变化"],
        )
    state = dict(state)
    state.update({
        "status": "draft",
        "next_action": "submit_generation_design",
        "plan": {},
        "active_transaction": None,
        "errors": [],
        "warnings": [
            "生成能力已更新；业务Request和Decision保持有效，"
            "请基于当前Contract重新提交Design"
        ],
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    })
    write_workflow_state(session_dir, state)
    return {
        "transaction_version": TRANSACTION_VERSION,
        "status": "draft",
        "request_id": request.get("request_id"),
        "workflow_state": state,
        "errors": [],
        "warnings": list(state["warnings"]),
    }


def _block_pic_policy(session_dir, request, state, audit):
    if state.get("workflow_state_version") == JOB_WORKFLOW_STATE_VERSION:
        result = _job_block_result(
            request,
            state,
            "invalid_pic_authorization",
            audit.get("errors") or ["PIC authorization无效"],
        )
        result["pic_authorization_audit"] = audit
        return result
    state = transition_workflow(
        session_dir,
        request["request_id"],
        status="blocked",
        result={
            "status": "invalid_pic_authorization",
            "pic_authorization_audit": audit,
        },
    )
    return {
        "transaction_version": TRANSACTION_VERSION,
        "status": "blocked",
        "request_id": request.get("request_id"),
        "workflow_state": state,
        "pic_authorization_audit": audit,
        "errors": audit.get("errors") or ["PIC authorization 无效"],
        "warnings": [],
    }


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
