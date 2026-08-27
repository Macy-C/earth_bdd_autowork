from __future__ import annotations

import json
import hashlib
from datetime import datetime
from pathlib import Path

from autowork_core.utils.debug_tools.recorder.decision_pack import (
    answer_pointer,
    build_decision_pack,
    decision_pack_pointer,
    load_answer_record,
    load_decision_pack,
    persist_answers,
    persist_decision_pack,
)
from autowork_core.utils.debug_tools.recorder.generation_plan import (
    PLAN_VERSION,
    apply_decision_constraints,
    bind_generation_annotation_trace,
    compile_generation_intent,
    load_generation_plan,
    normalize_generation_plan,
    persist_generation_plan,
    plan_pointer,
    validate_generation_plan,
    validate_decision_conformance,
)
from autowork_core.utils.debug_tools.recorder.generation_design import (
    GENERATION_DESIGN_VERSION,
    compile_generation_design,
)
from autowork_core.utils.debug_tools.recorder.generation_contract import (
    generation_contract_lease,
    generation_contract_lease_matches,
)
from autowork_core.utils.debug_tools.recorder.generation_job import (
    generation_job_lease_is_valid,
    load_generation_job,
)
from autowork_core.utils.debug_tools.recorder.generation_profile import (
    profile_lease_is_recognized,
    resolve_generation_profile,
)
from autowork_core.utils.debug_tools.recorder.models import SCHEMA_VERSION
from autowork_core.utils.debug_tools.recorder.project_memory import (
    inspect_request_memory_freshness,
)
from autowork_core.utils.debug_tools.recorder.request_repository import (
    request_identity_is_valid,
    request_matches_current_projection,
    request_revision_matches,
    request_revision_snapshot,
    resolve_session_path,
    session_dir_for_request_path,
)
from autowork_core.utils.debug_tools.recorder.reconciliation_repository import (
    load_generation_brief,
)
from autowork_core.utils.debug_tools.recorder.semantic_reconciler import (
    build_generation_brief,
)
from autowork_core.utils.debug_tools.recorder.semantic_pack import (
    SUPPORTED_SEMANTIC_PACK_VERSIONS,
)
from autowork_core.utils.debug_tools.recorder.workflow_state import (
    JOB_WORKFLOW_STATE_VERSION,
    load_workflow_state,
    next_workflow_action,
    write_workflow_state,
)


_TERMINAL_TRANSACTION_STATUSES = {"running", "completed", "failed"}


def inspect_workflow(
        request_path,
        *,
        write=True,
        preserve_transaction=True,
        return_brief=False,
    ):
    request_path = Path(request_path).resolve()
    request = _read_json(request_path)
    session_dir = session_dir_for_request_path(request_path, request)
    existing = load_workflow_state(session_dir, request.get("request_id"))
    terminal_job_workflow = bool(
        existing.get("workflow_state_version") == JOB_WORKFLOW_STATE_VERSION
        and not existing.get("current_job")
        and existing.get("last_job_result")
    )
    if (
        existing.get("workflow_state_version") == JOB_WORKFLOW_STATE_VERSION
        and existing.get("current_job")
    ):
        return _inspect_job_workflow(
            session_dir,
            request,
            existing,
            write=write,
            return_brief=return_brief,
        )
    errors = []
    warnings = []
    decision = {}
    memory_freshness = {"status": "compatible"}
    request_integrity_valid = request_identity_is_valid(request)
    current_evidence_matches = (
        request_matches_current_projection(session_dir, request)
        if request_integrity_valid
        else False
    )

    if request.get("request_version") != "3.0":
        status = "blocked"
        errors.append(
            "旧版 Request 不再支持；请从原始录制证据重新物化当前 RequestV3，"
            "或通过 recording_portability 导入完整录制包"
        )
        brief = None
        current_revision = {}
    elif not request_integrity_valid:
        status = "stale"
        errors.append("RequestV3 完整性校验失败，请重新物化请求")
        brief = None
        current_revision = {}
    elif not current_evidence_matches:
        status = "stale"
        errors.append(
            "selected Take 或当前投影已变化，请重新物化请求"
        )
        brief = None
        current_revision = request_revision_snapshot(session_dir, request)
    else:
        expected_revision = request.get("revision_snapshot") or {}
        if not expected_revision:
            current_revision = request_revision_snapshot(session_dir, request)
            status = "stale"
            errors.append("RequestV3 缺少 revision snapshot，请重新物化请求")
            brief = None
        else:
            revision_matches, current_revision = request_revision_matches(
                session_dir,
                request,
                expected_revision,
            )
            if not revision_matches:
                status = "stale"
                errors.append("selected Take、timeline、Evidence Graph 或 contract 已变化")
                brief = None
            elif not (
                existing.get("status") == "running"
                or (
                    preserve_transaction
                    and existing.get("status") in {"completed", "failed"}
                )
            ) and (
                memory_freshness := inspect_request_memory_freshness(
                    session_dir,
                    request,
                    include_current_results=not bool(
                        existing.get("last_result")
                        or existing.get("last_job_result")
                    ),
                )
            )["status"] not in {"compatible", "matched"}:
                status = "stale"
                errors.append(
                    memory_freshness.get("message")
                    or "目标相关项目经验已变化，请重新物化 RequestV3"
                )
                brief = None
            elif not (request.get("readiness") or {}).get("bundle_valid", False):
                status = "blocked"
                errors.append("录制证据包无效")
                brief = None
            elif _hard_blockers(request):
                status = "blocked"
                errors.append("存在 hard recovery blocker")
                brief = None
            else:
                try:
                    if terminal_job_workflow:
                        brief = build_generation_brief(
                            session_dir,
                            request,
                            write=write,
                        )
                        plan_artifact = None
                    elif (existing.get("plan") or {}).get("path"):
                        brief = load_generation_brief(
                            _brief_path(session_dir, existing)
                        )
                        plan_artifact = load_generation_plan(
                            session_dir,
                            existing,
                            request,
                        )
                        if plan_artifact is None:
                            raise ValueError(
                                "已提交 Plan 或其冻结 Brief 无效"
                            )
                        if not generation_contract_lease_matches(
                            session_dir,
                            (plan_artifact.get("source") or {}).get(
                                "generation_contract_lease"
                            ),
                        ):
                            existing = dict(existing)
                            existing["plan"] = {}
                            plan_artifact = None
                            warnings.append(
                                "生成能力已更新；业务Request和Decision保持有效，"
                                "请基于当前Contract重新提交Design"
                            )
                    else:
                        brief = build_generation_brief(
                            session_dir,
                            request,
                            write=write,
                        )
                        plan_artifact = None
                    decision = _decision_context(
                        session_dir,
                        request,
                        existing,
                        brief,
                        write=write,
                    )
                    if plan_artifact is not None and not _decision_prevents_plan(
                        decision
                    ):
                        status = "ready"
                    else:
                        status = _status_without_plan(brief, decision)
                except Exception as error:
                    brief = None
                    status = "blocked"
                    errors.append(
                        f"Semantic Reconciler 失败: {type(error).__name__}: {error}"
                    )

    state = {
        "schema_version": SCHEMA_VERSION,
        "workflow_state_version": (
            JOB_WORKFLOW_STATE_VERSION
            if existing.get("workflow_state_version")
            == JOB_WORKFLOW_STATE_VERSION
            else "3.0"
        ),
        "request_id": request.get("request_id"),
        "request_path": str(request_path),
        "session_dir": str(session_dir),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "status": status,
        "next_action": _next_action(status, decision),
        "revision": current_revision,
        "brief": _brief_pointer(session_dir, brief),
        "risk": (brief or {}).get("risk") or {},
        "adjustment": (brief or {}).get("adjustment") or {},
        "required_forensic_evidence": (
            (brief or {}).get("required_forensic_evidence") or []
        ),
        "errors": errors,
        "warnings": warnings,
        "plan": existing.get("plan") or {},
        "decision": decision,
        "ambiguity": _ambiguity_projection(
            brief or {},
            decision,
            plan_artifact if brief is not None else None,
        ),
        "active_transaction": None,
    }
    if state["workflow_state_version"] == JOB_WORKFLOW_STATE_VERSION:
        state.update({
            "current_job": existing.get("current_job"),
            "job_execution": existing.get("job_execution"),
            "retired_jobs": list(existing.get("retired_jobs") or []),
            "last_job_result": existing.get("last_job_result"),
            "attempt_history": list(existing.get("attempt_history") or []),
        })
    if (
        status == "ready"
        and existing.get("status") == "ready"
        and existing.get("last_result")
    ):
        state["last_result"] = existing["last_result"]
    if (
        preserve_transaction
        and status == "ready"
        and brief is not None
        and plan_artifact is not None
        and request_integrity_valid
        and existing.get("status") in _TERMINAL_TRANSACTION_STATUSES
        and (existing.get("revision") or {}).get("seal")
        == (current_revision or {}).get("seal")
    ):
        state["status"] = existing["status"]
        state["next_action"] = existing.get("next_action")
        state["active_transaction"] = existing.get("active_transaction")
        state["last_result"] = existing.get("last_result")
    elif terminal_job_workflow and status == "ready":
        state["status"] = existing.get("status")
        state["next_action"] = existing.get("next_action")
        state["last_result"] = existing.get("last_result")
    if write:
        write_workflow_state(session_dir, state)
    return (state, brief) if return_brief else state


def submit_decision_answers(request_path, answers):
    request_path = Path(request_path).resolve()
    request = _read_json(request_path)
    session_dir = session_dir_for_request_path(request_path, request)
    state = inspect_workflow(request_path, write=True)
    if state.get("status") in {"blocked", "stale", "running"}:
        raise ValueError(
            f"当前 workflow 不能提交回答: status={state.get('status')}"
        )
    decision_status = (state.get("decision") or {}).get("status")
    if decision_status != "awaiting_answers":
        raise ValueError(
            "当前 workflow 不接受 Decision Answers: "
            f"decision_status={decision_status}"
        )
    decision = state.get("decision") or {}
    pack = load_decision_pack(
        session_dir,
        decision.get("pack") or {},
        request,
        brief_fingerprint=(state.get("brief") or {}).get(
            "brief_fingerprint"
        ),
    )
    if pack is None:
        raise ValueError("当前 workflow 没有有效 Decision Pack")
    output, record = persist_answers(
        session_dir,
        request,
        pack,
        answers,
    )
    decision["answers"] = answer_pointer(session_dir, record, output)
    decision["status"] = (
        "forensic"
        if pack.get("forensic_blocking_count")
        else "answered"
    )
    state["decision"] = decision
    state["status"] = (
        "forensic"
        if decision["status"] == "forensic"
        else "draft"
    )
    state["next_action"] = _next_action(state["status"], decision)
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    write_workflow_state(session_dir, state)
    refreshed = inspect_workflow(
        request_path,
        write=True,
        preserve_transaction=False,
    )
    return {
        **record,
        "answers_path": str(output),
        "workflow_status": refreshed["status"],
        "next_action": refreshed["next_action"],
    }


def submit_generation_design(
        request_path,
        design,
        *,
        note="",
        confirmation_source="ai_generated",
        plan_origin="external_ai",
        generation_job_lease=None,
        generation_job_claim_id=None,
        generation_job_expected_epoch=None,
    ):
    return _submit_generation_input(
        request_path,
        design,
        input_kind="generation_design",
        input_version=GENERATION_DESIGN_VERSION,
        compiler=compile_generation_design,
        note=note,
        confirmation_source=confirmation_source,
        plan_origin=plan_origin,
        generation_job_lease=generation_job_lease,
        generation_job_claim_id=generation_job_claim_id,
        generation_job_expected_epoch=generation_job_expected_epoch,
    )


def _submit_generation_input(
        request_path,
        submitted_input,
        *,
        input_kind,
        input_version,
        compiler,
        note,
        confirmation_source,
        plan_origin,
        generation_job_lease=None,
        generation_job_claim_id=None,
        generation_job_expected_epoch=None,
    ):
    prepared = _prepare_generation_input(
        request_path,
        submitted_input,
        input_kind=input_kind,
        input_version=input_version,
        compiler=compiler,
        confirmation_source=confirmation_source,
        plan_origin=plan_origin,
        write=True,
        generation_job_lease=generation_job_lease,
        generation_job_claim_id=generation_job_claim_id,
        generation_job_expected_epoch=generation_job_expected_epoch,
    )
    request_path = prepared["request_path"]
    request = prepared["request"]
    session_dir = prepared["session_dir"]
    state = prepared["state"]
    brief = prepared["brief"]
    normalized = prepared["normalized"]
    plan_origin = prepared["plan_origin"]

    artifact, output = persist_generation_plan(
        session_dir,
        request_path,
        request,
        state,
        brief,
        normalized,
        intent=submitted_input,
        input_kind=input_kind,
        input_version=input_version,
        confirmation_source=confirmation_source,
        plan_origin=plan_origin,
        note=note,
        generation_contract_lease=prepared[
            "generation_contract_lease"
        ],
        generation_job_lease=prepared["generation_job_lease"],
    )
    if prepared["generation_job_lease"]:
        artifact["plan_path"] = str(output)
        artifact["workflow_status"] = state["status"]
        artifact["brief"] = state.get("brief") or {}
        artifact["learning"] = {
            "status": "deferred_until_accepted_feedback",
            "memory_ids": [],
            "capability_paths": [],
            "warnings": [],
        }
        return artifact
    state["plan"] = plan_pointer(session_dir, artifact, output)
    state["status"] = "ready"
    state["next_action"] = next_workflow_action("ready")
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    write_workflow_state(session_dir, state)
    artifact["plan_path"] = str(output)
    artifact["workflow_status"] = state["status"]
    artifact["brief"] = state.get("brief") or {}
    artifact["learning"] = {
        "status": "deferred_until_accepted_feedback",
        "memory_ids": [],
        "capability_paths": [],
        "warnings": [],
    }
    return artifact


def _prepare_generation_input(
        request_path,
        submitted_input,
        *,
        input_kind,
        input_version,
        compiler,
        confirmation_source,
        plan_origin,
        write,
        generation_job_lease=None,
        generation_job_claim_id=None,
        generation_job_expected_epoch=None,
    ):
    request_path = Path(request_path).resolve()
    request = _read_json(request_path)
    if request.get("request_version") != "3.0":
        raise ValueError(f"GenerationPlanV{PLAN_VERSION} 只接受 RequestV3")
    if confirmation_source not in {
        "ai_generated",
        "user_adjustment",
    }:
        raise ValueError(f"无效 Plan 确认来源: {confirmation_source}")
    if plan_origin is None:
        plan_origin = {
            "user_adjustment": "human_authored",
        }.get(confirmation_source)
    if confirmation_source == "ai_generated" and plan_origin is None:
        raise ValueError(
            "AI Plan 必须声明 plan_origin=external_ai 或 "
            "deterministic_surrogate"
        )
    from autowork_core.utils.debug_tools.recorder.generation_plan import (
        _plan_origin_is_valid,
    )
    if not _plan_origin_is_valid(
        PLAN_VERSION,
        confirmation_source,
        plan_origin,
    ):
        raise ValueError(
            "Plan 来源与 confirmation_source 不一致: "
            f"{confirmation_source}/{plan_origin}"
        )
    session_dir = session_dir_for_request_path(request_path, request)
    if write:
        state = inspect_workflow(request_path, write=True)
        transient_brief = None
    else:
        state, transient_brief = inspect_workflow(
            request_path,
            write=False,
            return_brief=True,
        )
    if generation_job_lease:
        if not generation_job_lease_is_valid(generation_job_lease):
            raise ValueError("Generation Job lease无效")
        pointer = state.get("current_job") or {}
        execution = state.get("job_execution") or {}
        if any((
            state.get("workflow_state_version") != JOB_WORKFLOW_STATE_VERSION,
            state.get("status") != "running",
            execution.get("phase") != "design",
            execution.get("claim_id") != generation_job_claim_id,
            int(execution.get("epoch") or 0)
            != int(generation_job_expected_epoch or -1),
            pointer.get("job_id") != generation_job_lease.get("job_id"),
            pointer.get("job_fingerprint")
            != generation_job_lease.get("job_fingerprint"),
            pointer.get("nonce") != generation_job_lease.get("job_nonce"),
            pointer.get("profile_lease_fingerprint")
            != generation_job_lease.get("profile_fingerprint"),
        )):
            raise ValueError("Generation Job claim、epoch或lease与Workflow不一致")
    elif state.get("current_job"):
        raise ValueError("当前Workflow必须使用Generation Job Design入口")
    if state.get("status") in {"completed", "failed"}:
        memory_freshness = inspect_request_memory_freshness(
            session_dir,
            request,
        )
        if memory_freshness["status"] not in {"compatible", "matched"}:
            raise ValueError(
                "当前 workflow 不能提交计划: status=stale; "
                + (
                    memory_freshness.get("message")
                    or "目标相关项目经验已变化，请重新物化 RequestV3"
                )
            )
    if state.get("status") in {"blocked", "stale"} or (
        state.get("status") == "running" and not generation_job_lease
    ):
        raise ValueError(
            f"当前 workflow 不能提交计划: status={state.get('status')}"
        )
    decision_status = (state.get("decision") or {}).get("status")
    if decision_status == "awaiting_answers":
        raise ValueError(
            "当前 Plan 必须先完成 Decision Pack: "
            f"decision_status={decision_status}"
        )
    brief = (
        load_generation_brief(_brief_path(session_dir, state))
        if write
        else transient_brief
    )
    if not isinstance(brief, dict):
        raise ValueError("当前 workflow 缺少可用 Generation Brief")
    compiled_patch = _compiled_decision_patch(
        session_dir,
        request,
        state,
    )
    plan = compiler(submitted_input, brief)
    if input_kind == "generation_design":
        plan = compile_generation_intent(plan, brief)
    if compiled_patch:
        plan = apply_decision_constraints(plan, compiled_patch)
        plan = compile_generation_intent(plan, brief)
    plan = bind_generation_annotation_trace(plan, brief)
    normalized = normalize_generation_plan(request, plan)
    errors = validate_generation_plan(
        normalized,
        brief,
        require_window_ownership=True,
        require_scenario_model=True,
        require_action_roles=True,
        user_confirmed_references={
            str(item.get("question_id"))
            for item in compiled_patch.get("decision_trace") or ()
            if item.get("question_id")
        },
    )
    errors.extend(validate_decision_conformance(normalized, compiled_patch))
    if errors:
        raise ValueError(f"GenerationPlanV{PLAN_VERSION} 无效: {errors}")
    return {
        "request_path": request_path,
        "request": request,
        "session_dir": session_dir,
        "state": state,
        "brief": brief,
        "compiled_patch": compiled_patch,
        "normalized": normalized,
        "plan_origin": plan_origin,
        "generation_contract_lease": generation_contract_lease(
            session_dir,
            write=write,
        ),
        "generation_job_lease": generation_job_lease,
    }


def _validation_issue_category(message):
    text = str(message)
    if any(marker in text for marker in (
        "必须先完成 Decision Pack",
        "用户确认",
        "user_confirmed",
    )):
        return "business_authority"
    if any(marker in text for marker in (
        "证据",
        "Evidence",
        "evidence_required",
        "status=stale",
    )):
        return "evidence"
    return "technical"


def _validation_issue_code(message):
    text = str(message)
    if "Decision Pack" in text:
        return "decision_required"
    if "Step范围或顺序" in text:
        return "step_scope_mismatch"
    if "target_action_id" in text:
        return "target_action_binding"
    if "value_source" in text or "值权威" in text:
        return "value_authority"
    if "未知字段" in text:
        return "unknown_field"
    if "status=stale" in text:
        return "stale_request"
    return "invalid_design"


def _status_without_plan(brief, decision):
    ambiguities = brief.get("ambiguities") or []
    if any(
        item.get("routing") == "evidence_required"
        for item in ambiguities
    ):
        return "blocked"
    decision_status = (decision or {}).get("status")
    if decision_status == "awaiting_answers":
        return "needs_adjustment"
    if decision_status == "forensic":
        return "forensic"
    resolved = {
        str(item)
        for item in (decision or {}).get("resolved_ambiguity_ids") or ()
        if item
    }
    selected_outcomes = {
        str(item.get("ambiguity_id") or ""): str(item.get("outcome") or "")
        for item in (decision or {}).get("ambiguity_resolutions") or ()
        if item.get("ambiguity_id")
    }
    if any(
        item.get("routing") == "user_decision_required"
        and str(item.get("ambiguity_id") or "") not in resolved
        for item in ambiguities
    ):
        return "needs_adjustment"
    for ambiguity in ambiguities:
        outcome = selected_outcomes.get(
            str(ambiguity.get("ambiguity_id") or "")
        )
        selected = next(
            (
                item
                for item in ambiguity.get("allowed_outcomes") or ()
                if item.get("outcome") == outcome
            ),
            None,
        )
        if (selected or {}).get("effect") == "evidence_required":
            return "blocked"
    if any(
        item.get("routing") in {"ai_plan_required", "mixed"}
        for item in ambiguities
    ):
        return "forensic"
    return "draft"


def _ambiguity_projection(brief, decision, plan_artifact):
    ambiguities = brief.get("ambiguities") or []
    decision_resolutions = {
        str(item.get("ambiguity_id") or ""): item
        for item in (decision or {}).get("ambiguity_resolutions") or ()
        if item.get("ambiguity_id")
    }
    plan_resolutions = {
        str(item.get("ambiguity_id") or ""): item
        for item in (
            ((plan_artifact or {}).get("plan") or {}).get(
                "ambiguity_resolutions"
            ) or []
        )
        if item.get("ambiguity_id")
    }
    known_review_ids = set()
    visible_review_ids = set()
    pending = {
        "ai": 0,
        "user": 0,
        "evidence": 0,
    }
    resolved_ids = set(plan_resolutions) | set(decision_resolutions)
    for ambiguity in ambiguities:
        ambiguity_id = str(ambiguity.get("ambiguity_id") or "")
        source_review_ids = _ambiguity_source_review_ids(ambiguity)
        known_review_ids.update(source_review_ids)
        selected = (
            plan_resolutions.get(ambiguity_id)
            or decision_resolutions.get(ambiguity_id)
        )
        if selected is not None:
            outcome = next(
                (
                    item
                    for item in ambiguity.get("allowed_outcomes") or ()
                    if item.get("outcome") == selected.get("outcome")
                ),
                {},
            )
            if outcome.get("effect") == "evidence_required":
                pending["evidence"] += 1
                visible_review_ids.update(source_review_ids)
            continue
        routing = str(ambiguity.get("routing") or "")
        if routing == "evidence_required":
            pending["evidence"] += 1
            visible_review_ids.update(source_review_ids)
        elif routing == "user_decision_required":
            pending["user"] += 1
            visible_review_ids.update(source_review_ids)
        elif routing in {"ai_plan_required", "mixed"}:
            pending["ai"] += 1
        else:
            pending["evidence"] += 1
            visible_review_ids.update(source_review_ids)
    return {
        "total_count": len(ambiguities),
        "resolved_count": len(resolved_ids & {
            str(item.get("ambiguity_id") or "")
            for item in ambiguities
        }),
        "pending_ai_count": pending["ai"],
        "pending_user_count": pending["user"],
        "pending_evidence_count": pending["evidence"],
        "known_review_ids": sorted(known_review_ids),
        "visible_review_ids": sorted(visible_review_ids),
    }


def _ambiguity_source_review_ids(ambiguity):
    source = ambiguity.get("source") or {}
    result = {
        str(item)
        for item in source.get("source_review_ids") or ()
        if item
    }
    source_review_id = str(source.get("source_review_id") or "")
    if source_review_id:
        result.add(source_review_id)
    return result


def _decision_context(session_dir, request, existing, brief, *, write):
    existing_decision = existing.get("decision") or {}
    pack = load_decision_pack(
        session_dir,
        existing_decision.get("pack") or {},
        request,
        brief_fingerprint=brief.get("brief_fingerprint"),
    )
    pointer = existing_decision.get("pack") or {}
    if pack is None:
        semantic_packs = _load_semantic_packs(session_dir, request)
        pack = build_decision_pack(
            request,
            semantic_packs,
            brief=brief,
        )
        if write:
            path, pack = persist_decision_pack(session_dir, pack)
            pointer = decision_pack_pointer(session_dir, pack, path)
        else:
            pointer = {
                "path": None,
                "pack_id": pack.get("pack_id"),
                "pack_fingerprint": pack.get("pack_fingerprint"),
                "revision_seal": pack.get("revision_seal"),
                "question_count": len(pack.get("questions") or ()),
                "blocking_count": pack.get("blocking_count", 0),
                "forensic_blocking_count": pack.get(
                    "forensic_blocking_count",
                    0,
                ),
            }
    answers_pointer = existing_decision.get("answers") or {}
    answer_record = load_answer_record(
        session_dir,
        answers_pointer,
        request,
        pack,
    )
    if pack.get("forensic_blocking_count"):
        status = "forensic"
    elif pack.get("blocking_count") and answer_record is None:
        status = "awaiting_answers"
    elif answer_record is not None:
        status = "answered"
    else:
        status = "not_required"
    return {
        "status": status,
        "pack": pointer,
        "answers": answers_pointer if answer_record is not None else {},
        "resolved_ambiguity_ids": sorted({
            str(item.get("ambiguity_id"))
            for item in (
                ((answer_record or {}).get("compiled_patch") or {}).get(
                    "ambiguity_resolutions"
                ) or []
            )
            if item.get("ambiguity_id")
        }),
        "ambiguity_resolutions": list(
            (((answer_record or {}).get("compiled_patch") or {}).get(
                "ambiguity_resolutions"
            ) or [])
        ),
    }


def _compiled_decision_patch(session_dir, request, state):
    decision = state.get("decision") or {}
    if not (decision.get("answers") or {}).get("path"):
        return {}
    pack = load_decision_pack(
        session_dir,
        decision.get("pack") or {},
        request,
        brief_fingerprint=(state.get("brief") or {}).get(
            "brief_fingerprint"
        ),
    )
    if pack is None:
        raise ValueError("Decision Pack 已失效")
    record = load_answer_record(
        session_dir,
        decision.get("answers") or {},
        request,
        pack,
    )
    if record is None:
        raise ValueError("Decision Answers 已失效")
    return record.get("compiled_patch") or {}


def _load_semantic_packs(session_dir, request):
    packs = []
    for evidence in request.get("evidence") or ():
        relative = (evidence.get("artifacts") or {}).get("semantic_pack")
        if not relative:
            continue
        path = resolve_session_path(session_dir, relative)
        pack = _read_json(path)
        artifacts = evidence.get("artifacts") or {}
        hashes = evidence.get("artifact_hashes") or {}
        step_id = str((evidence.get("step") or {}).get("id") or "")
        if pack.get("semantic_pack_version") not in (
            SUPPORTED_SEMANTIC_PACK_VERSIONS
        ):
            raise ValueError(f"Semantic Pack 版本无效: {relative}")
        if str((pack.get("step") or {}).get("id") or "") != step_id:
            raise ValueError(f"Semantic Pack Step 不匹配: {relative}")
        for fallback in pack.get("locator_fallback_candidates") or ():
            candidate = fallback.get("pic_candidate")
            if not candidate:
                continue
            artifact_key = f"pic_template:{candidate.get('candidate_id')}"
            template_path = artifacts.get(artifact_key)
            template_hash = hashes.get(artifact_key)
            if template_path and template_hash:
                resolved = resolve_session_path(session_dir, template_path)
                actual_hash = hashlib.sha256(resolved.read_bytes()).hexdigest()
                if actual_hash != template_hash:
                    raise ValueError(
                        f"PIC template hash 与 Request 不一致: {artifact_key}"
                    )
            candidate["template_request_path"] = template_path
            candidate["template_request_sha256"] = template_hash
            candidate["audit_request_path"] = artifacts.get(
                "pic_template_audit"
            )
            candidate["audit_request_sha256"] = hashes.get(
                "pic_template_audit"
            )
        packs.append(pack)
    return packs


def _decision_prevents_plan(decision):
    return (decision or {}).get("status") == "awaiting_answers"


def _next_action(status, decision):
    decision_status = (decision or {}).get("status")
    if decision_status == "awaiting_answers":
        return "answer_decision_pack"
    if decision_status == "answered" and status == "draft":
        return "submit_decision_constrained_plan"
    if decision_status == "forensic":
        return "inspect_named_evidence_and_submit_plan"
    if status == "draft":
        return "submit_window_owned_plan"
    return next_workflow_action(status)


def _hard_blockers(request):
    return [
        item
        for item in (request.get("readiness") or {}).get(
            "target_review_required", []
        )
        if (item.get("recovery") or {}).get("hard_blocker")
    ]


def _inspect_job_workflow(
        session_dir,
        request,
        state,
        *,
        write,
        return_brief,
    ):
    job = load_generation_job(
        session_dir,
        state.get("current_job") or {},
    )
    errors = []
    if job is None:
        errors.append("job_integrity_failed")
    else:
        job_request = job.get("request") or {}
        profile = job.get("profile_lease") or {}
        if any((
            not request_identity_is_valid(request),
            job_request.get("request_id") != request.get("request_id"),
            job_request.get("request_fingerprint")
            != request.get("request_fingerprint"),
            job_request.get("revision_seal")
            != (request.get("revision_snapshot") or {}).get("seal"),
            not request_matches_current_projection(session_dir, request),
        )):
            errors.append("job_request_stale")
        revision_matches, _current_revision = request_revision_matches(
            session_dir,
            request,
            request.get("revision_snapshot") or {},
        )
        if not revision_matches:
            errors.append("job_revision_stale")
        if not generation_contract_lease_matches(
            session_dir,
            job.get("generation_contract_lease") or {},
        ):
            errors.append("job_contract_stale")
        if not profile_lease_is_recognized(profile):
            errors.append("job_profile_stale")
    if errors:
        state = dict(state)
        execution = dict(state.get("job_execution") or {})
        state.update({
            "status": "failed",
            "next_action": "review_generation_job_failure",
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "errors": errors,
            "job_execution": {
                **execution,
                "phase": "failed",
                "epoch": int(execution.get("epoch") or 0) + 1,
                "last_issue_fingerprint": hashlib.sha256(
                    json.dumps(errors, separators=(",", ":")).encode("utf-8")
                ).hexdigest(),
            },
        })
    brief = None
    try:
        brief = load_generation_brief(_brief_path(session_dir, state))
    except (OSError, ValueError):
        if not errors:
            state = dict(state)
            state["status"] = "failed"
            state["next_action"] = "review_generation_job_failure"
            state["errors"] = ["job_brief_invalid"]
    return (state, brief) if return_brief else state


def _brief_pointer(session_dir, brief):
    if not brief:
        return {}
    path_value = brief.get("brief_path")
    path = Path(path_value) if path_value else None
    if path is None:
        relative = None
    else:
        try:
            relative = path.resolve().relative_to(
                Path(session_dir).resolve()
            ).as_posix()
        except (ValueError, OSError):
            relative = str(path)
    return {
        "path": relative,
        "brief_version": brief.get("brief_version"),
        "brief_fingerprint": brief.get("brief_fingerprint"),
        "size_bytes": (
            path.stat().st_size
            if path is not None and path.is_file()
            else None
        ),
    }


def _brief_path(session_dir, state):
    value = (state.get("brief") or {}).get("path")
    if not value:
        raise ValueError("workflow state 缺少 Generation Brief")
    path = Path(value)
    return path.resolve() if path.is_absolute() else (Path(session_dir) / path).resolve()


def _read_json(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 必须是 object: {path}")
    return value