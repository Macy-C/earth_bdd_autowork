from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path

from autowork_core.utils.debug_tools.recorder.generation_job_service import (
    abort_generation_job,
    build_generation_job_baseline_design,
    discover_generation_job_typed_patch_issues,
    finish_generation_job,
    inspect_generation_job,
    mark_generation_job_design_required,
    materialize_prepared_system_candidate,
    prepare_generation_job,
    query_generation_job_implementation_candidate,
    record_generation_job_result_interaction,
    start_generation_job,
    submit_generation_job_design,
    submit_generation_job_ambiguity_choice_patch,
    submit_generation_job_assertion_choice_patch,
    business_review_patch_from_direct_arguments,
    submit_generation_job_business_review,
    submit_generation_job_method_choice_patch,
    submit_generation_job_naming_patch,
    submit_generation_job_operation_choice_patch,
    submit_generation_job_typed_patch_batch,
    submit_generation_job_value_source_choice_patch,
    validate_generation_job_implementation,
)
from autowork_core.utils.debug_tools.recorder.generation_design import (
    GenerationAmbiguityChoiceRequired,
    GenerationAssertionChoiceRequired,
    GenerationDesignValidationError,
    GenerationMethodChoiceRequired,
    GenerationOperationChoiceRequired,
    GenerationValueSourceChoiceRequired,
    _baseline_target_name,
)
from autowork_core.utils.debug_tools.recorder.identity import (
    operation_choice_key,
)
from autowork_core.utils.debug_tools.recorder.reconciliation_repository import (
    load_generation_brief,
)
from autowork_core.utils.debug_tools.recorder.value_authority import (
    qualify_value_sources,
)


GENERATION_ORCHESTRATOR_VERSION = "1.11"
TYPED_PATCH_REQUIREMENTS_VERSION = "1.0"
GENERATION_ENTRYPOINT_VERSION = "1.0"


def settle_generation_job(job_path):
    job_path = Path(job_path).resolve()
    inspected = inspect_generation_job(job_path)
    transition = inspected.get("job_transition") or {}
    phase = str(transition.get("phase") or "")
    questions = list(inspected.get("business_questions") or ())
    review = inspected.get("business_review") or {}
    if phase in {"completed", "failed"}:
        status = "terminal"
        next_action = inspected.get("next_action") or "review_generation_result"
    elif review.get("status") == "required":
        status = "business_answers_required"
        next_action = "submit_business_review_answers"
        questions = list(
            inspected.get("business_questions")
            or review.get("questions")
            or ()
        )
    elif questions:
        status = "business_answers_required"
        next_action = "submit_business_review_answers"
    else:
        status = "ready_to_generate"
        next_action = "generate_job"
    terminal_summary = (
        {
            "unresolved_issues": list(
                inspected.get("unresolved_issues") or ()
            ),
            "delivery_visibility": dict(
                inspected.get("delivery_visibility") or {}
            ),
            "delivery_summary": dict(
                inspected.get("delivery_summary") or {}
            ),
            "implementation_diff_summary": dict(
                inspected.get("implementation_diff_summary") or {}
            ),
            "acceptance_summary": dict(
                inspected.get("acceptance_summary") or {}
            ),
            "workspace_projection_summary": dict(
                inspected.get("workspace_projection_summary") or {}
            ),
            "service_level": dict(inspected.get("service_level") or {}),
            "orchestration_timing": dict(
                inspected.get("orchestration_timing") or {}
            ),
        }
        if status == "terminal"
        else {}
    )
    value = {
        "generation_entrypoint_version": GENERATION_ENTRYPOINT_VERSION,
        "entrypoint": "settle-job",
        "status": status,
        "next_action": next_action,
        "job_id": inspected.get("job_id"),
        "request_id": inspected.get("request_id"),
        "job_path": str(job_path),
        "job_transition": transition,
        "terminal_status": (
            inspected.get("status") if status == "terminal" else None
        ),
        "category": inspected.get("category"),
        "failure_summary": inspected.get("failure_summary"),
        "stages": inspected.get("stages") or {},
        "last_job_result": inspected.get("last_job_result"),
        "generation_workspace_projection": inspected.get(
            "generation_workspace_projection"
        ) or {},
        "workspace_projection_summary": inspected.get(
            "workspace_projection_summary"
        ) or {},
        "business_questions": questions,
        "errors": list(inspected.get("errors") or ()),
        "warnings": list(inspected.get("warnings") or ()),
        **terminal_summary,
    }
    if status == "business_answers_required" and review.get("status") == "required":
        value["business_review"] = {
            "status": review.get("status"),
            "business_review_workset_version": review.get(
                "business_review_workset_version"
            ),
            "workset_fingerprint": review.get("workset_fingerprint"),
            "unit_count": review.get("unit_count"),
            "question_count": len(questions),
        }
    if status != "terminal":
        contract = inspected.get("decision_answer_contract") or {}
        if contract:
            value["decision_answer_contract"] = contract
        value["settlement_package"] = _settlement_package(
            inspected,
            questions,
        )
    return value


def _settlement_package(inspected, questions):
    design_context = inspected.get("design_context") or {}
    target = design_context.get("target") or {}
    transition = inspected.get("job_transition") or {}
    first_issue = inspected.get("first_issue") or {}
    locked_facts = {
        "operation_order": "system_baseline_when_proven",
        "target_action_id": "frozen_action_reference",
        "value_source": "frozen_or_declared_value_source",
        "window_ownership": "frozen_owner_and_view_candidates",
        "locator_route": "plan_compiler_owned",
        "write_scope": "implementation_manifest_owned",
    }
    blanks = []
    if (
            isinstance(first_issue, dict)
            and first_issue.get("kind") == "system_baseline_needs_ai_naming"
    ):
        blanks.append({
            "kind": "stable_ascii_naming",
            "fields": ["target_name", "business_name"],
            "scope": "unresolved non-ASCII public names only",
        })
    value = {
        "settlement_package_version": "1.0",
        "status": inspected.get("status"),
        "phase": transition.get("phase"),
        "target": {
            "feature": ((target.get("feature") or {}).get("name")),
            "scenario": ((target.get("scenario") or {}).get("name")),
            "step_count": target.get("step_count"),
        },
        "business_questions": list(questions or []),
        "locked_facts": locked_facts,
        "ai_fillable_blanks": blanks,
        "placeholder_policy": {
            "allowed": "only when compile emits unresolved_issues",
            "required_fields": [
                "issue_domain",
                "severity",
                "reuse_policy",
            ],
            "success_semantics": "placeholder is not static success unless validations explicitly pass with unresolved issues",
        },
    }
    contract = inspected.get("decision_answer_contract") or {}
    if contract:
        value["decision_answer_contract"] = contract
    return value


def generate_generation_job(
        job_path,
        *,
        project_root=None,
        summary="",
        design=None,
        ambiguity_choice_patch=None,
        assertion_choice_patch=None,
        method_choice_patch=None,
        naming_patch=None,
        operation_choice_patch=None,
        value_source_choice_patch=None,
    ):
    job_path = Path(job_path).resolve()
    if design is not None:
        raise ValueError(
            "完整GenerationDesign产品入口已退役；"
            "请使用generate-job返回的typed patch或修复证据"
        )
    result = run_generation_job(
        job_path,
        project_root=project_root,
        summary=summary,
        auto_materialize_system_candidate=True,
    )
    patches = [
        ("assertion_choice", assertion_choice_patch, _requires_assertion_choice,
         submit_generation_job_assertion_choice_patch),
        ("method_choice", method_choice_patch, _requires_method_choice,
         submit_generation_job_method_choice_patch),
        ("operation_choice", operation_choice_patch, _requires_operation_choice,
         submit_generation_job_operation_choice_patch),
        ("value_source_choice", value_source_choice_patch,
         _requires_value_source_choice,
         submit_generation_job_value_source_choice_patch),
        ("ambiguity_choice", ambiguity_choice_patch, _requires_ambiguity_choice,
         submit_generation_job_ambiguity_choice_patch),
        ("naming", naming_patch, _requires_naming_patch,
         submit_generation_job_naming_patch),
    ]
    if any(value is not None for _name, value, _requires, _submit in patches):
        result = _submit_typed_patch_batch(
            job_path,
            result,
            patches,
            project_root=project_root,
            summary=summary,
        )
    else:
        automatic_submission = _system_verified_unique_typed_patch_submission(
            result,
        )
        if automatic_submission:
            result = _submit_typed_patch_batch(
                job_path,
                result,
                _typed_patch_submission_patches(automatic_submission),
                project_root=project_root,
                summary=summary,
            )
            result = {
                **result,
                "typed_patch_auto_submission": _typed_patch_auto_submission_summary(
                    automatic_submission,
                    result,
                ),
            }
    if result.get("status") == "design_required" and _requires_naming_patch(result):
        result = _naming_patch_required(
            result,
            result.get("job_transition") or {},
            issue=result.get("first_issue"),
        )
    elif result.get("status") == "design_required" and _requires_ambiguity_choice(result):
        result = _ambiguity_choice_required(
            result,
            result.get("job_transition") or {},
            issue=result.get("first_issue"),
        )
    elif result.get("status") == "design_required" and _requires_assertion_choice(result):
        result = _assertion_choice_required(
            result,
            result.get("job_transition") or {},
            issue=result.get("first_issue"),
        )
    elif result.get("status") == "design_required" and _requires_method_choice(result):
        result = _method_choice_required(
            result,
            result.get("job_transition") or {},
            issue=result.get("first_issue"),
        )
    elif result.get("status") == "design_required" and _requires_operation_choice(result):
        result = _operation_choice_required(
            result,
            result.get("job_transition") or {},
            issue=result.get("first_issue"),
        )
    elif result.get("status") == "design_required" and _requires_value_source_choice(result):
        result = _value_source_choice_required(
            result,
            result.get("job_transition") or {},
            issue=result.get("first_issue"),
        )
    elif result.get("status") == "design_required" and _complete_typed_patch_batch(result):
        result = _typed_patch_batch_required(
            result,
            result.get("job_transition") or {},
        )
    elif result.get("status") == "design_required":
        result = _untyped_design_retired(
            result,
            result.get("job_transition") or {},
            issue=result.get("first_issue"),
        )
    value = {
        "generation_entrypoint_version": GENERATION_ENTRYPOINT_VERSION,
        "entrypoint": "generate-job",
        "job_path": str(job_path),
        **result,
    }
    return value


def submit_business_review(job_path, patch=None, *, decisions=None, reasons=None):
    job_path = Path(job_path).resolve()
    if patch is None:
        patch = business_review_patch_from_direct_arguments(
            job_path,
            decisions or {},
            reasons or {},
        )
    result = submit_generation_job_business_review(job_path, patch)
    return {
        **result,
        "generation_entrypoint_version": GENERATION_ENTRYPOINT_VERSION,
        "entrypoint": "submit-business-review",
        "job_path": str(job_path),
    }


def advance_generation_job(
        job_path,
        *,
        project_root=None,
        summary="",
        design=None,
        ambiguity_choice_patch=None,
        assertion_choice_patch=None,
        method_choice_patch=None,
        naming_patch=None,
        operation_choice_patch=None,
        value_source_choice_patch=None,
    ):
    job_path = Path(job_path).resolve()
    settled = settle_generation_job(job_path)
    if settled.get("status") in {
        "terminal",
        "failed",
        "business_answers_required",
    }:
        if settled.get("status") == "business_answers_required":
            _record_generation_interaction(
                job_path,
                settled,
                event="business_answers_required",
                status="business_answers_required",
                next_action="submit_business_review_answers",
                details={
                    "question_count": len(settled.get("business_questions") or []),
                },
            )
        terminal_status = (
            settled.get("terminal_status")
            or (
                (settled.get("job_transition") or {}).get("phase")
                if settled.get("status") == "terminal"
                else None
            )
            or settled.get("status")
            or "failed"
        )
        return {
            **settled,
            "entrypoint": "advance-job",
            "status": terminal_status,
            "advance_summary": {
                "advance_summary_version": "1.0",
                "steps_executed": ["settle"],
                "blocked_on": (
                    "terminal" if settled.get("status") == "terminal"
                    else settled.get("status")
                ),
                "rule": "Return failed, terminal, or pre-generation answer boundary without re-entering generation.",
            },
        }
    result = generate_generation_job(
        job_path,
        project_root=project_root,
        summary=summary,
        design=design,
        ambiguity_choice_patch=ambiguity_choice_patch,
        assertion_choice_patch=assertion_choice_patch,
        method_choice_patch=method_choice_patch,
        naming_patch=naming_patch,
        operation_choice_patch=operation_choice_patch,
        value_source_choice_patch=value_source_choice_patch,
    )
    value = {
        **result,
        "entrypoint": "advance-job",
        "advance_summary": _advance_summary(result),
    }
    if value.get("status") == "typed_patch_required":
        batch = result.get("typed_patch_requirement_batch") or {}
        _record_generation_interaction(
            job_path,
            value,
            event="typed_patch_required",
            status="typed_patch_required",
            next_action="submit_all_complete_typed_patch_arguments",
            details={
                "issue_count": batch.get("issue_count"),
                "patch_types": list(batch.get("patch_types") or []),
            },
        )
    return value


def _record_generation_interaction(
        job_path,
        result,
        *,
        event,
        status,
        next_action,
        details=None,
    ):
    try:
        record_generation_job_result_interaction(
            job_path,
            result,
            event=event,
            status=status,
            next_action=next_action,
            details=details,
        )
    except (OSError, ValueError, KeyError):
        return


def _advance_summary(result):
    blocked_on = _advance_blocked_on(result)
    orchestration_stages = _orchestration_stage_names(result)
    return {
        "advance_summary_version": "1.0",
        "steps_executed": ["settle", "generate"],
        "blocked_on": blocked_on,
        "deterministic_progression": {
            "status": (
                "advanced_to_boundary"
                if orchestration_stages
                else "not_observed"
            ),
            "orchestration_stages": orchestration_stages,
            "boundary": blocked_on,
            "rule": (
                "advance-job runs system-owned deterministic stages until "
                "it reaches an AI-decision, explicit implementation "
                "boundary, terminal, or failure boundary."
            ),
        },
        "rule": (
            "System freezes deterministic candidates and writes normal "
            "system-owned candidates through system_materializer; candidate "
            "artifacts are diagnostics and Agent file edits are not a "
            "delivery path."
        ),
    }


def _orchestration_stage_names(result):
    timing = (result or {}).get("orchestration_timing") or {}
    return [
        str(item.get("name") or "")
        for item in timing.get("stages") or []
        if isinstance(item, dict) and item.get("name")
    ]


def _business_answer_review_summary(review, questions):
    review = review if isinstance(review, dict) else {}
    return {
        "status": review.get("status"),
        "business_review_workset_version": review.get(
            "business_review_workset_version"
        ),
        "workset_fingerprint": review.get("workset_fingerprint"),
        "unit_count": review.get("unit_count"),
        "question_count": len(list(questions or ())),
    }


def _advance_blocked_on(result):
    status = str((result or {}).get("status") or "")
    if status == "implementation_required":
        return str(
            (result or {}).get("implementation_boundary")
            or "implementation_required"
        )
    if status.endswith("_required"):
        return status
    if status in {"completed", "completed_no_changes"}:
        return "terminal"
    if status in {"failed", "aborted", "generation_blocked"}:
        return status
    return "unknown"


def _submit_typed_patch_batch(
        job_path,
        result,
        patches,
        *,
        project_root=None,
        summary="",
    ):
    submitted = [
        (name, value, requires, submit)
        for name, value, requires, submit in patches
        if value is not None
    ]
    allowed = _allowed_typed_patch_types(result)
    submitted_names = {name for name, _value, _requires, _submit in submitted}
    if allowed and not submitted_names.issubset(allowed):
        raise ValueError(
            "generate-job提交了当前Job未请求的typed patch: "
            f"{sorted(submitted_names - allowed)}"
        )
    transition = result.get("job_transition") or {}
    patch_values = {}
    for name, value, requires, _submit in submitted:
        if not allowed and not requires(result):
            raise ValueError(f"当前Job未请求{name} patch")
        patch_values[name] = value
    if len(submitted) == 1:
        name, value, _requires, submit = submitted[0]
        try:
            submit(
                job_path,
                value,
                claim_id=_required_claim(transition),
                expected_epoch=_required_epoch(transition),
            )
        except (OSError, ValueError) as error:
            return {
                **_typed_patch_required_result(result, result),
                "errors": [
                    f"{_typed_patch_error_label(name)}无效: "
                    f"{type(error).__name__}: {error}",
                ],
            }
        return run_generation_job(
            job_path,
            project_root=project_root,
            summary=summary,
            auto_materialize_system_candidate=True,
        )
    try:
        submit_generation_job_typed_patch_batch(
            job_path,
            claim_id=_required_claim(transition),
            expected_epoch=_required_epoch(transition),
            naming_patch=patch_values.get("naming"),
            ambiguity_choice_patch=patch_values.get("ambiguity_choice"),
            assertion_choice_patch=patch_values.get("assertion_choice"),
            method_choice_patch=patch_values.get("method_choice"),
            operation_choice_patch=patch_values.get("operation_choice"),
            value_source_choice_patch=patch_values.get("value_source_choice"),
        )
    except (OSError, ValueError) as error:
        labels = ", ".join(
            _typed_patch_error_label(name)
            for name in sorted(submitted_names)
        )
        return {
            **_typed_patch_required_result(result, result),
            "errors": [
                f"TypedPatchBatch无效({labels}): "
                f"{type(error).__name__}: {error}",
            ],
        }
    return run_generation_job(
        job_path,
        project_root=project_root,
        summary=summary,
        auto_materialize_system_candidate=True,
    )


def _typed_patch_error_label(name):
    return {
        "assertion_choice": "AssertionChoicePatch",
        "method_choice": "MethodChoicePatch",
        "operation_choice": "OperationChoicePatch",
        "value_source_choice": "ValueSourceChoicePatch",
        "ambiguity_choice": "AmbiguityChoicePatch",
        "naming": "NamingPatch",
    }.get(str(name or ""), "TypedPatch")


def _allowed_typed_patch_types(result):
    batch = result.get("typed_patch_requirement_batch") or {}
    requirements = batch.get("requirements") or []
    allowed = {
        str(item.get("patch_type") or "")
        for item in requirements
        if isinstance(item, dict) and item.get("patch_type")
    }
    if allowed:
        return allowed
    issue = result.get("first_issue") or {}
    patch_type = str(issue.get("patch_type") or "")
    return {patch_type} if patch_type else set()


def _typed_patch_required_result(current, fallback):
    current = current if isinstance(current, dict) else {}
    fallback = fallback if isinstance(fallback, dict) else {}
    value = current if current.get("status") == "design_required" else fallback
    transition = value.get("job_transition") or {}
    issue = value.get("first_issue")
    if _requires_naming_patch(value):
        return _naming_patch_required(value, transition, issue=issue)
    if _requires_ambiguity_choice(value):
        return _ambiguity_choice_required(value, transition, issue=issue)
    if _requires_assertion_choice(value):
        return _assertion_choice_required(value, transition, issue=issue)
    if _requires_method_choice(value):
        return _method_choice_required(value, transition, issue=issue)
    if _requires_operation_choice(value):
        return _operation_choice_required(value, transition, issue=issue)
    if _requires_value_source_choice(value):
        return _value_source_choice_required(value, transition, issue=issue)
    return value


def _typed_patch_auto_submission_summary(submission, result):
    summary = dict((submission or {}).get("summary") or {})
    failed = bool((result or {}).get("errors"))
    summary["status"] = "failed" if failed else "succeeded"
    summary["result_status"] = (result or {}).get("status")
    return summary


def _typed_patch_submission_patches(submission):
    patches = submission.get("patches") or {}
    return [
        ("assertion_choice", None, _requires_assertion_choice,
         submit_generation_job_assertion_choice_patch),
        ("method_choice", None, _requires_method_choice,
         submit_generation_job_method_choice_patch),
        ("operation_choice", None, _requires_operation_choice,
         submit_generation_job_operation_choice_patch),
        ("value_source_choice", None, _requires_value_source_choice,
         submit_generation_job_value_source_choice_patch),
        ("ambiguity_choice", patches.get("ambiguity_choice"),
         _requires_ambiguity_choice,
         submit_generation_job_ambiguity_choice_patch),
        ("naming", patches.get("naming"), _requires_naming_patch,
         submit_generation_job_naming_patch),
    ]


def _system_verified_unique_typed_patch_submission(result):
    if result.get("status") != "design_required":
        return None
    batch = result.get("typed_patch_requirement_batch") or {}
    requirements = [
        requirement for requirement in batch.get("requirements") or []
        if isinstance(requirement, dict)
    ]
    if any((
            batch.get("status") != "required",
            batch.get("complete") is not True,
            not requirements,
            batch.get("terminal_error"),
            batch.get("allowed_queries"),
            not any(_requirement_is_system_verified_unique(item) for item in requirements),
    )):
        return None
    patches = {
        "naming": {
            "naming_patch_version": "1.0",
            "patch_type": "naming",
            "target_names": {},
            "business_names": {},
        },
        "ambiguity_choice": {
            "ambiguity_choice_patch_version": "1.0",
            "patch_type": "ambiguity_choice",
            "choices": [],
        },
    }
    patch_types = []
    recommended_argument_count = 0
    submitted_requirement_count = 0
    for requirement in requirements:
        if not _requirement_is_system_verified_unique(requirement):
            continue
        submitted_requirement_count += 1
        patch_type = str(requirement.get("patch_type") or "")
        recommended = list(requirement.get("recommended_arguments") or [])
        patch_types.append(patch_type)
        recommended_argument_count += len(recommended)
        for argument in recommended:
            if not _add_recommended_typed_patch_argument(patches, argument):
                return None
    patches = {
        key: value for key, value in patches.items()
        if key == "naming" and (
            value["target_names"] or value["business_names"]
        )
        or key == "ambiguity_choice" and value["choices"]
    }
    if not patches:
        return None
    return {
        "patches": patches,
        "summary": {
            "status": "planned",
            "classification": "system_verified_unique",
            "requirement_count": submitted_requirement_count,
            "batch_requirement_count": len(requirements),
            "patch_types": patch_types,
            "recommended_argument_count": recommended_argument_count,
            "rule": (
                "Complete typed patch requirements with one system-verified "
                "unique recommendation are submitted before returning any "
                "remaining Agent decision boundary."
            ),
        },
    }


def _requirement_is_system_verified_unique(requirement):
    if requirement.get("complete") is not True:
        return False
    if any((
            requirement.get("allowed_query"),
            requirement.get("framework_defect"),
            requirement.get("missing_fields"),
    )):
        return False
    if (
            (requirement.get("recommendation") or {}).get("classification")
            != "system_verified_unique"
    ):
        return False
    patch_type = str(requirement.get("patch_type") or "")
    if patch_type not in {"naming", "ambiguity_choice"}:
        return False
    recommended = list(requirement.get("recommended_arguments") or [])
    return len(recommended) == 1 and all(
        "<" not in str(argument or "")
        and "generate_issue_placeholder" not in str(argument or "")
        for argument in recommended
    )


def _add_recommended_typed_patch_argument(patches, argument):
    option, value = _recommended_argument_option_value(argument)
    if not option or not value:
        return False
    assignment = _recommended_argument_assignment(value)
    if option == "--target-name" and assignment:
        patches["naming"]["target_names"][assignment[0]] = assignment[1]
        return True
    if option == "--business-name" and assignment:
        patches["naming"]["business_names"][assignment[0]] = assignment[1]
        return True
    if option == "--ambiguity-choice" and assignment:
        patches["ambiguity_choice"]["choices"].append({
            "ambiguity_id": assignment[0],
            "outcome": assignment[1],
        })
        return True
    return False


def _recommended_argument_option_value(argument):
    text = str(argument or "").strip()
    if not text:
        return None, None
    option, separator, value = text.partition(" ")
    if not separator or not option.startswith("--") or not value.strip():
        return None, None
    return option, value.strip()


def _recommended_argument_assignment(value):
    if "=" not in str(value or ""):
        return None
    scope, assigned = str(value).split("=", 1)
    scope = scope.strip()
    assigned = assigned.strip()
    if not scope or not assigned:
        return None
    return scope, assigned


def run_generation_job(
        job_path,
        *,
        project_root=None,
        summary="",
    auto_materialize_system_candidate=False,
    ):
    timing = _new_orchestration_timing()
    job_path = Path(job_path).resolve()
    inspected = _timed_orchestration_call(
        timing,
        "inspect_job",
        inspect_generation_job,
        job_path,
    )
    transition = inspected.get("job_transition") or {}
    phase = transition.get("phase")
    if phase == "completed":
        return _with_orchestration_timing(
            _completed_result(inspected, inspected),
            timing,
        )
    if (inspected.get("business_review") or {}).get("status") == "required":
        questions = list(
            inspected.get("business_questions")
            or (inspected.get("business_review") or {}).get("questions")
            or ()
        )
        return _with_orchestration_timing(
            {
                **inspected,
                "business_review": _business_answer_review_summary(
                    inspected.get("business_review"),
                    questions,
                ),
                "business_questions": questions,
                "generation_entrypoint_version": GENERATION_ENTRYPOINT_VERSION,
                "entrypoint": "generate-job",
                "status": "business_answers_required",
                "next_action": "submit_business_review_answers",
                "job_path": str(job_path),
            },
            timing,
        )
    if phase == "ready":
        _timed_orchestration_call(
            timing,
            "start_job",
            start_generation_job,
            job_path,
            expected_epoch=_required_epoch(transition),
        )
        inspected = _timed_orchestration_call(
            timing,
            "inspect_after_start",
            inspect_generation_job,
            job_path,
        )
        transition = inspected.get("job_transition") or {}
        phase = transition.get("phase")
        if (inspected.get("business_review") or {}).get("status") == "required":
            questions = list(
                inspected.get("business_questions")
                or (inspected.get("business_review") or {}).get("questions")
                or ()
            )
            return _with_orchestration_timing(
                {
                    **inspected,
                    "business_review": _business_answer_review_summary(
                        inspected.get("business_review"),
                        questions,
                    ),
                    "business_questions": questions,
                    "generation_entrypoint_version": GENERATION_ENTRYPOINT_VERSION,
                    "entrypoint": "generate-job",
                    "status": "business_answers_required",
                    "next_action": "submit_business_review_answers",
                    "job_path": str(job_path),
                },
                timing,
            )
    resume_mode = _implementation_transaction_resume_mode(job_path, inspected)
    resumed_transaction = bool(
        phase == "implementation"
        and resume_mode in {"direct", "legacy_prepare"}
    )
    if phase == "design" and _system_baseline_can_run(inspected):
        try:
            design = _timed_orchestration_call(
                timing,
                "build_system_baseline_design",
                build_generation_job_baseline_design,
                job_path,
            )
        except GenerationAmbiguityChoiceRequired as error:
            marked = _timed_orchestration_call(
                timing,
                "mark_design_required",
                mark_generation_job_design_required,
                job_path,
                reason="system_baseline_needs_ai_ambiguity_choice",
                message=str(error),
                details={"ambiguity_id": error.ambiguity_id},
            )
            return _with_orchestration_timing(
                _design_required(
                    {**inspected, **marked},
                    marked.get("job_transition") or transition,
                    issue={
                        "kind": "system_baseline_needs_ai_ambiguity_choice",
                        "message": str(error),
                        "patch_type": "ambiguity_choice",
                        "ambiguity_id": error.ambiguity_id,
                        "outcomes": list(error.outcomes),
                    },
                    job_path=job_path,
                ),
                timing,
            )
        except GenerationAssertionChoiceRequired as error:
            marked = _timed_orchestration_call(
                timing,
                "mark_design_required",
                mark_generation_job_design_required,
                job_path,
                reason="system_baseline_needs_ai_assertion_choice",
                message=str(error),
                details={"ambiguity_id": error.ambiguity_id},
            )
            return _with_orchestration_timing(
                _design_required(
                    {**inspected, **marked},
                    marked.get("job_transition") or transition,
                    issue={
                        "kind": "system_baseline_needs_ai_assertion_choice",
                        "message": str(error),
                        "patch_type": "assertion_choice",
                        "ambiguity_id": error.ambiguity_id,
                        "candidates": list(error.candidates),
                    },
                    job_path=job_path,
                ),
                timing,
            )
        except GenerationMethodChoiceRequired as error:
            marked = _timed_orchestration_call(
                timing,
                "mark_design_required",
                mark_generation_job_design_required,
                job_path,
                reason="system_baseline_needs_ai_method_choice",
                message=str(error),
                details={"step_id": error.step_id},
            )
            return _with_orchestration_timing(
                _design_required(
                    {**inspected, **marked},
                    marked.get("job_transition") or transition,
                    issue={
                        "kind": "system_baseline_needs_ai_method_choice",
                        "message": str(error),
                        "patch_type": "method_choice",
                        "step_id": error.step_id,
                        "candidates": list(error.candidates),
                    },
                    job_path=job_path,
                ),
                timing,
            )
        except GenerationOperationChoiceRequired as error:
            marked = _timed_orchestration_call(
                timing,
                "mark_design_required",
                mark_generation_job_design_required,
                job_path,
                reason="system_baseline_needs_ai_operation_choice",
                message=str(error),
                details={
                    "step_id": error.step_id,
                    "action_id": error.action_id,
                },
            )
            return _with_orchestration_timing(
                _design_required(
                    {**inspected, **marked},
                    marked.get("job_transition") or transition,
                    issue={
                        "kind": "system_baseline_needs_ai_operation_choice",
                        "message": str(error),
                        "patch_type": "operation_choice",
                        "step_id": error.step_id,
                        "action_id": error.action_id,
                        "candidates": list(error.candidates),
                    },
                    job_path=job_path,
                ),
                timing,
            )
        except GenerationValueSourceChoiceRequired as error:
            marked = _timed_orchestration_call(
                timing,
                "mark_design_required",
                mark_generation_job_design_required,
                job_path,
                reason="system_baseline_needs_ai_value_source_choice",
                message=str(error),
                details={
                    "step_id": error.step_id,
                    "action_id": error.action_id,
                    "operation": error.operation,
                },
            )
            return _with_orchestration_timing(
                _design_required(
                    {**inspected, **marked},
                    marked.get("job_transition") or transition,
                    issue={
                        "kind": "system_baseline_needs_ai_value_source_choice",
                        "message": str(error),
                        "patch_type": "value_source_choice",
                        "step_id": error.step_id,
                        "action_id": error.action_id,
                        "operation": error.operation,
                    },
                ),
                timing,
            )
        except (KeyError, OSError, ValueError) as error:
            marked = _timed_orchestration_call(
                timing,
                "mark_design_required",
                mark_generation_job_design_required,
                job_path,
                reason="system_baseline_unavailable",
                message=f"{type(error).__name__}: {error}",
            )
            return _with_orchestration_timing(
                _design_required(
                    {**inspected, **marked},
                    marked.get("job_transition") or transition,
                    issue={
                        "kind": "system_baseline_unavailable",
                        "message": f"{type(error).__name__}: {error}",
                    },
                    job_path=job_path,
                ),
                timing,
            )
        try:
            designed = _timed_orchestration_call(
                timing,
                "submit_system_baseline_design",
                submit_generation_job_design,
                job_path,
                design,
                claim_id=_required_claim(transition),
                expected_epoch=_required_epoch(transition),
                confirmation_source="system_generated",
                plan_origin="system_baseline",
            )
        except GenerationDesignValidationError as error:
            marked = _timed_orchestration_call(
                timing,
                "mark_design_required",
                mark_generation_job_design_required,
                job_path,
                reason="system_baseline_needs_ai_naming",
                message=str(error),
            )
            return _with_orchestration_timing(
                _design_required(
                    {**inspected, **marked},
                    marked.get("job_transition") or transition,
                    issue={
                        "kind": "system_baseline_needs_ai_naming",
                        "message": str(error),
                        "patch_type": "naming",
                    },
                    job_path=job_path,
                ),
                timing,
            )
        inspected = {**inspected, **designed}
        transition = designed.get("job_transition") or {}
        phase = transition.get("phase")
    if phase in {"ready", "design"}:
        return _with_orchestration_timing(
            _design_required(
                inspected,
                transition,
                issue=_design_required_issue(inspected),
                job_path=job_path,
            ),
            timing,
        )
    if phase != "implementation":
        return _with_orchestration_timing(
            _result(
                inspected,
                status="stopped",
                next_action="inspect_current_job_phase",
                reason=(
                    "Expected implementation phase, "
                    f"found {phase}"
                ),
            ),
            timing,
        )
    if resume_mode == "direct":
        prepared = _prepared_from_resumed_transaction(job_path, inspected)
    else:
        prepared = _timed_orchestration_call(
            timing,
            "prepare_transaction",
            prepare_generation_job,
            job_path,
            claim_id=_required_claim(transition),
            expected_epoch=_required_epoch(transition),
            project_root=project_root,
        )
        if prepared.get("status") in {"aborted", "failed"}:
            return _with_orchestration_timing(
                _terminal_failure(inspected, prepared),
                timing,
            )
        if prepared.get("status") in {"completed", "completed_no_changes"}:
            return _with_orchestration_timing(
                _completed_result(inspected, prepared),
                timing,
            )
        prepared_transition = prepared.get("job_transition") or {}
        prepared_phase = str(prepared_transition.get("phase") or "")
        if prepared_phase in {"ready", "design"}:
            return _with_orchestration_timing(
                _ready_to_generate_after_refresh(prepared),
                timing,
            )
        if not _prepared_transaction_boundary_is_valid(prepared):
            return _with_orchestration_timing(
                _implementation_candidate_protocol_defect(
                    inspected,
                    prepared,
                ),
                timing,
            )
        transition = prepared_transition
    system_candidate_ready = _system_candidate_can_run(prepared)
    if not resumed_transaction and (
            not system_candidate_ready or not auto_materialize_system_candidate
    ):
        return _with_orchestration_timing(
            _implementation_required(inspected, prepared, transition),
            timing,
        )

    materialized = None
    if not resumed_transaction and system_candidate_ready:
        try:
            materialized = _timed_orchestration_call(
                timing,
                "materialize_system_candidate",
                materialize_prepared_system_candidate,
                prepared["report_path"],
                project_root=project_root,
            )
        except (OSError, ValueError) as error:
            return _with_orchestration_timing(
                _implementation_required(
                    inspected,
                    prepared,
                    transition,
                    validation={
                        "implementation_validation_version": "1.0",
                        "status": "invalid",
                        "projected_transaction_status": (
                            "system_materialization_failed"
                        ),
                        "attempt": {
                            "status": "invalid",
                            "issues": [{
                                "code": "system_materialization_failed",
                                "message": f"{type(error).__name__}: {error}",
                            }],
                        },
                        "workspace_candidate_audit": {
                            "status": "not_applied",
                            "files": [],
                        },
                        "issues": [{
                            "code": "system_materialization_failed",
                            "message": f"{type(error).__name__}: {error}",
                        }],
                        "errors": [
                            "System candidate materialization failed: "
                            f"{type(error).__name__}: {error}"
                        ],
                    },
                ),
                timing,
            )
        prepared = {**prepared, "system_materialization_commit": materialized}

    validation = _timed_orchestration_call(
        timing,
        "validate_implementation",
        validate_generation_job_implementation,
        prepared["report_path"],
        claim_id=_required_claim(transition),
        expected_epoch=_required_epoch(transition),
        project_root=project_root,
    )
    if validation.get("status") in {"aborted", "failed"}:
        return _with_orchestration_timing(
            _terminal_failure(inspected, validation),
            timing,
        )
    if validation.get("status") != "valid":
        validation_transition = validation.get("job_transition") or transition
        if _validation_scope_guard_failure(validation):
            aborted = _timed_orchestration_call(
                timing,
                "abort_scope_guard_transaction",
                abort_generation_job,
                prepared["report_path"],
                reason=_scope_guard_abort_reason(validation),
                claim_id=_required_claim(validation_transition),
                expected_epoch=_required_epoch(validation_transition),
                project_root=project_root,
                allow_project_guard_drift=True,
            )
            return _with_orchestration_timing(
                _terminal_failure(inspected, aborted),
                timing,
            )
        return _with_orchestration_timing(
            _implementation_required(
                inspected,
                prepared,
                validation_transition,
                validation=validation,
            ),
            timing,
        )
    transition = validation.get("job_transition") or transition
    completed = _timed_orchestration_call(
        timing,
        "finish_transaction",
        finish_generation_job,
        prepared["report_path"],
        claim_id=_required_claim(transition),
        expected_epoch=_required_epoch(transition),
        project_root=project_root,
        summary=summary,
    )
    if completed.get("status") in {"aborted", "failed"}:
        return _with_orchestration_timing(
            _terminal_failure(inspected, completed),
            timing,
        )
    return _with_orchestration_timing(
        _completed_result(inspected, completed),
        timing,
    )


def _new_orchestration_timing():
    return {
        "orchestration_timing_version": "1.0",
        "scope": "generate_job_orchestrator",
        "started_at": datetime.now().isoformat(timespec="milliseconds"),
        "_started_monotonic": time.perf_counter(),
        "stages": [],
    }


def _timed_orchestration_call(timing, stage_name, function, *args, **kwargs):
    started_at = datetime.now().isoformat(timespec="milliseconds")
    started_monotonic = time.perf_counter()
    entry = {
        "name": stage_name,
        "started_at": started_at,
    }
    try:
        result = function(*args, **kwargs)
    except Exception as error:
        entry["status"] = "raised"
        entry["error"] = f"{type(error).__name__}: {error}"
        raise
    else:
        entry["status"] = "completed"
        return result
    finally:
        entry["finished_at"] = datetime.now().isoformat(timespec="milliseconds")
        entry["duration_ms"] = max(
            0,
            int((time.perf_counter() - started_monotonic) * 1000),
        )
        timing.setdefault("stages", []).append(entry)


def _with_orchestration_timing(result, timing):
    value = dict(result or {})
    value["orchestration_timing"] = _finalize_orchestration_timing(timing)
    return value


def _finalize_orchestration_timing(timing):
    stages = [dict(item) for item in timing.get("stages") or []]
    total_duration_ms = max(
        0,
        int((time.perf_counter() - timing.get("_started_monotonic")) * 1000),
    )
    return {
        "orchestration_timing_version": timing.get(
            "orchestration_timing_version"
        ),
        "scope": timing.get("scope"),
        "started_at": timing.get("started_at"),
        "finished_at": datetime.now().isoformat(timespec="milliseconds"),
        "total_duration_ms": total_duration_ms,
        "stage_count": len(stages),
        "stages": stages,
    }


def _design_required(inspected, transition, *, issue=None, job_path=None):
    task_bundle = inspected.get("generation_task_bundle") or {}
    typed = _is_typed_patch_issue(issue)
    batch = _typed_patch_requirement_batch(job_path, inspected, issue)
    return {
        "generation_orchestrator_version": GENERATION_ORCHESTRATOR_VERSION,
        "status": "design_required",
        "next_action": (
            "start_generation_job"
            if transition.get("phase") == "ready"
            else "submit_minimal_generation_patch"
            if typed
            else "classification_required"
        ),
        "job_id": inspected.get("job_id"),
        "request_id": inspected.get("request_id"),
        "job_transition": transition,
        "generation_task_bundle": task_bundle,
        "first_issue": issue,
        **({"typed_patch_requirement_batch": batch} if batch else {}),
        "errors": [],
        "warnings": [],
    }


def _typed_patch_requirement_batch(job_path, inspected, issue):
    if job_path is None and not _is_typed_patch_issue(issue):
        return None
    brief = _typed_patch_brief(inspected, job_path=job_path)
    discovery = None
    if job_path is not None:
        try:
            discovery = discover_generation_job_typed_patch_issues(job_path)
        except (OSError, ValueError) as error:
            discovery = {
                "typed_patch_issue_discovery_version": "1.0",
                "status": "failed",
                "complete": False,
                "issue_count": 0,
                "issues": [],
                "terminal_error": f"{type(error).__name__}: {error}",
            }
    issues = list((discovery or {}).get("issues") or [])
    if not issues and _is_typed_patch_issue(issue):
        issues = [issue]
    if not issues:
        return None
    issues = _merge_typed_patch_issues(
        issues,
        _predictive_naming_patch_issues(brief),
    )
    requirements = []
    for index, item in enumerate(issues, start=1):
        patch_type = str(item.get("patch_type") or "")
        requirement = _typed_patch_requirements(
            patch_type,
            inspected,
            item,
            brief=brief,
        )
        requirement["requirement_id"] = (
            f"typed-{index:03d}-{patch_type or 'unknown'}"
        )
        requirements.append(requirement)
    requirements = _prune_placeholdered_naming_requirements(requirements, brief)
    return {
        "typed_patch_requirement_batch_version": "1.0",
        "status": "required",
        "complete": all(item.get("complete") is True for item in requirements)
        and (discovery or {}).get("complete", True) is True,
        "issue_count": len(requirements),
        "requirements": requirements,
        "allowed_queries": _batch_allowed_queries(requirements),
        "terminal_error": (discovery or {}).get("terminal_error"),
        "rule": (
            "Submit all complete requirements in one advance-job command; "
            "do not query job-design-context unless an allowed_query is listed."
        ),
    }


def _prune_placeholdered_naming_requirements(requirements, brief=None):
    placeholdered_actions = set()
    for requirement in requirements:
        if not isinstance(requirement, dict):
            continue
        if requirement.get("patch_type") != "ambiguity_choice":
            continue
        if not any(
                str(argument or "").endswith("=generate_issue_placeholder")
                for argument in requirement.get("recommended_arguments") or ()
        ):
            continue
        ambiguity = ((requirement.get("facts") or {}).get("ambiguity") or {})
        step_id = str(ambiguity.get("step_id") or "")
        for action_id in ambiguity.get("action_ids") or ():
            if step_id and action_id:
                placeholdered_actions.add((step_id, str(action_id)))
    if not placeholdered_actions:
        return requirements
    remaining_roots = _remaining_roots_after_placeholders(
        brief,
        placeholdered_actions,
    )
    pruned = []
    for requirement in requirements:
        naming = ((requirement.get("facts") or {}).get("naming") or {})
        scope = (str(naming.get("step_id") or ""), str(naming.get("action_id") or ""))
        if requirement.get("patch_type") == "naming" and scope in placeholdered_actions:
            continue
        root_name = str(naming.get("root_name") or "")
        if (
                requirement.get("patch_type") == "naming"
                and naming.get("field") == "business_name"
                and remaining_roots is not None
                and root_name not in remaining_roots
        ):
            continue
        pruned.append(requirement)
    return pruned


def _remaining_roots_after_placeholders(brief, placeholdered_actions):
    if not isinstance(brief, dict):
        return None
    roots = set()
    for action in brief.get("actions") or ():
        if not isinstance(action, dict):
            continue
        scope = (str(action.get("step_id") or ""), str(action.get("id") or ""))
        if scope in placeholdered_actions:
            continue
        root_name = str((action.get("target") or {}).get("root_name") or "")
        if root_name:
            roots.add(root_name)
    return roots


def _merge_typed_patch_issues(*issue_groups):
    merged = []
    seen = set()
    for group in issue_groups:
        for issue in group or ():
            if not isinstance(issue, dict):
                continue
            key = _typed_patch_issue_scope_key(issue)
            if key in seen:
                continue
            seen.add(key)
            merged.append(issue)
    return merged


def _typed_patch_issue_scope_key(issue):
    patch_type = str(issue.get("patch_type") or "")
    arguments = _typed_patch_submit_arguments(patch_type, issue)
    if arguments:
        option, value = _recommended_argument_option_value(arguments[0])
        assignment = _recommended_argument_assignment(value)
        if option and assignment:
            return (option, assignment[0])
    return (
        patch_type,
        str(issue.get("step_id") or ""),
        str(issue.get("action_id") or ""),
        str(issue.get("ambiguity_id") or ""),
        str(issue.get("business_name") or ""),
        str(issue.get("root_name") or ""),
    )


def _predictive_naming_patch_issues(brief):
    brief = brief if isinstance(brief, dict) else {}
    actions = {
        (str(action.get("step_id") or ""), str(action.get("id") or "")): action
        for action in brief.get("actions") or ()
        if isinstance(action, dict)
    }
    candidates = []
    seen = set()
    for ambiguity in brief.get("ambiguities") or ():
        if not isinstance(ambiguity, dict):
            continue
        if not any(
                isinstance(outcome, dict)
                and outcome.get("authority") == "ai"
                and outcome.get("effect") == "plan_coverage"
                for outcome in ambiguity.get("allowed_outcomes") or ()
        ):
            continue
        step_id = str(ambiguity.get("step_id") or "")
        for action_id in ambiguity.get("action_ids") or ():
            action_key = (step_id, str(action_id or ""))
            if action_key in seen:
                continue
            action = actions.get(action_key) or {}
            if str(action.get("type") or "").casefold() == "scroll":
                continue
            target = action.get("target") or {}
            if not _target_needs_predictive_public_name(action, target):
                continue
            seen.add(action_key)
            candidates.append({
                "kind": "system_baseline_needs_ai_naming",
                "patch_type": "naming",
                "step_id": step_id,
                "action_id": str(action_id or ""),
                "reason": "predictive_new_locator_public_name_required",
                "source_ambiguity_id": ambiguity.get("ambiguity_id"),
            })
    return candidates


def _target_needs_predictive_public_name(action, target):
    target = target if isinstance(target, dict) else {}
    if not target.get("root_name"):
        return False
    return not _baseline_target_name(action)


def _batch_allowed_queries(requirements):
    queries = []
    seen = set()
    for item in requirements:
        query = item.get("allowed_query")
        if not isinstance(query, dict):
            continue
        key = (
            query.get("command"),
            query.get("step_id"),
            query.get("reason"),
        )
        if key in seen:
            continue
        seen.add(key)
        queries.append(dict(query))
    return queries


def _untyped_design_retired(inspected, transition, *, issue=None):
    return {
        "generation_orchestrator_version": GENERATION_ORCHESTRATOR_VERSION,
        "status": "generation_blocked",
        "next_action": "repair_evidence_or_classify_generation_gap",
        "job_id": inspected.get("job_id"),
        "request_id": inspected.get("request_id"),
        "job_transition": transition,
        "first_issue": issue or {
            "kind": "complete_generation_design_retired",
            "message": (
                "系统baseline缺口未分类为typed patch；"
                "完整GenerationDesign产品入口已退役"
            ),
        },
        "errors": [],
        "warnings": list(inspected.get("warnings") or ()),
    }


def _is_typed_patch_issue(issue):
    return bool(
        isinstance(issue, dict)
        and issue.get("patch_type") in {
            "naming",
            "ambiguity_choice",
            "assertion_choice",
            "method_choice",
            "operation_choice",
            "value_source_choice",
        }
    )


def _typed_patch_batch_fields(inspected):
    batch = (inspected or {}).get("typed_patch_requirement_batch")
    return {"typed_patch_requirement_batch": batch} if batch else {}


def _complete_typed_patch_batch(result):
    batch = (result or {}).get("typed_patch_requirement_batch") or {}
    return bool(
        batch.get("status") == "required"
        and batch.get("complete") is True
        and batch.get("requirements")
    )


def _typed_patch_batch_required(inspected, transition):
    return {
        "generation_orchestrator_version": GENERATION_ORCHESTRATOR_VERSION,
        "status": "typed_patch_required",
        "next_action": "submit_all_complete_typed_patch_arguments",
        "job_id": inspected.get("job_id"),
        "request_id": inspected.get("request_id"),
        "job_transition": transition,
        "first_issue": inspected.get("first_issue"),
        **_typed_patch_batch_fields(inspected),
        **(
            {"typed_patch_auto_submission": inspected["typed_patch_auto_submission"]}
            if inspected.get("typed_patch_auto_submission")
            else {}
        ),
        "errors": [],
        "warnings": list(inspected.get("warnings") or []),
    }


def _naming_patch_required(inspected, transition, *, issue=None):
    return {
        "generation_orchestrator_version": GENERATION_ORCHESTRATOR_VERSION,
        "status": "naming_patch_required",
        "next_action": "submit_naming_patch",
        "job_id": inspected.get("job_id"),
        "request_id": inspected.get("request_id"),
        "job_transition": transition,
        "first_issue": issue,
        "naming_patch_contract": {
            "version": "1.0",
            "patch_type": "naming",
            "submit_command": (
                "generate-job <job> --target-name "
                "<step-id/action-id=ascii_snake_case>"
            ),
            "fields": ["target_name", "business_name"],
            "scope": "only unresolved public ASCII names; no draft file",
        },
        "typed_patch_requirements": _typed_patch_requirements(
            "naming",
            inspected,
            issue,
        ),
        "naming_requirements": _naming_requirements(inspected, issue),
        **_typed_patch_batch_fields(inspected),
        "errors": [],
        "warnings": [],
    }


def _ambiguity_choice_required(inspected, transition, *, issue=None):
    batch_fields = _typed_patch_batch_fields(inspected)
    requirements = _typed_patch_requirements(
        "ambiguity_choice",
        inspected,
        issue,
    )
    batch_requirement = _batch_requirement_for_issue(
        batch_fields.get("typed_patch_requirement_batch"),
        "ambiguity_choice",
        issue,
    )
    if batch_requirement is not None:
        requirements = batch_requirement
    return {
        "generation_orchestrator_version": GENERATION_ORCHESTRATOR_VERSION,
        "status": "ambiguity_choice_required",
        "next_action": "submit_ambiguity_choice_patch",
        "job_id": inspected.get("job_id"),
        "request_id": inspected.get("request_id"),
        "job_transition": transition,
        "first_issue": issue,
        "ambiguity_choice_patch_contract": {
            "version": "1.0",
            "patch_type": "ambiguity_choice",
            "fields": ["ambiguity_id", "outcome"],
            "scope": (
                "only AI-authority frozen plan_coverage outcomes without "
                "candidate, value, locator, or method changes"
            ),
        },
        "typed_patch_requirements": requirements,
        **batch_fields,
        "errors": [],
        "warnings": [],
    }


def _batch_requirement_for_issue(batch, patch_type, issue):
    if not isinstance(batch, dict):
        return None
    issue_scope = _typed_patch_scope(issue)
    for requirement in batch.get("requirements") or ():
        if not isinstance(requirement, dict):
            continue
        if requirement.get("patch_type") != patch_type:
            continue
        scope = requirement.get("scope") or {}
        if all(scope.get(key) == value for key, value in issue_scope.items()):
            return dict(requirement)
    return None


def _assertion_choice_required(inspected, transition, *, issue=None):
    return {
        "generation_orchestrator_version": GENERATION_ORCHESTRATOR_VERSION,
        "status": "assertion_choice_required",
        "next_action": "submit_assertion_choice_patch",
        "job_id": inspected.get("job_id"),
        "request_id": inspected.get("request_id"),
        "job_transition": transition,
        "first_issue": issue,
        "assertion_choice_patch_contract": {
            "version": "1.0",
            "patch_type": "assertion_choice",
            "fields": ["ambiguity_id", "candidate_key"],
            "scope": (
                "only frozen assertion implementation candidate keys from "
                "assertion_implementation ambiguity facts; never operation, "
                "parameters, values, windows, locators, methods, or proof"
            ),
        },
        "typed_patch_requirements": _typed_patch_requirements(
            "assertion_choice",
            inspected,
            issue,
        ),
        **_typed_patch_batch_fields(inspected),
        "errors": [],
        "warnings": [],
    }


def _method_choice_required(inspected, transition, *, issue=None):
    return {
        "generation_orchestrator_version": GENERATION_ORCHESTRATOR_VERSION,
        "status": "method_choice_required",
        "next_action": "submit_method_choice_patch",
        "job_id": inspected.get("job_id"),
        "request_id": inspected.get("request_id"),
        "job_transition": transition,
        "first_issue": issue,
        "method_choice_patch_contract": {
            "version": "1.0",
            "patch_type": "method_choice",
            "fields": ["step_id", "candidate_id"],
            "scope": (
                "only frozen Page method candidate_id for a Step whose "
                "baseline operations exactly match the candidate call_sequence; "
                "never method body, operation, value, window, locator, or proof"
            ),
        },
        "typed_patch_requirements": _typed_patch_requirements(
            "method_choice",
            inspected,
            issue,
        ),
        **_typed_patch_batch_fields(inspected),
        "errors": [],
        "warnings": [],
    }


def _operation_choice_required(inspected, transition, *, issue=None):
    return {
        "generation_orchestrator_version": GENERATION_ORCHESTRATOR_VERSION,
        "status": "operation_choice_required",
        "next_action": "submit_operation_choice_patch",
        "job_id": inspected.get("job_id"),
        "request_id": inspected.get("request_id"),
        "job_transition": transition,
        "first_issue": issue,
        "operation_choice_patch_contract": {
            "version": "1.0",
            "patch_type": "operation_choice",
            "fields": ["step_id", "action_id", "choice_key"],
            "scope": (
                "only choice_key values from the frozen OperationChoiceSet "
                "for a Step/Action; never operation strings, values, "
                "windows, locators, methods, or proof"
            ),
        },
        "typed_patch_requirements": _typed_patch_requirements(
            "operation_choice",
            inspected,
            issue,
        ),
        **_typed_patch_batch_fields(inspected),
        "errors": [],
        "warnings": [],
    }


def _value_source_choice_required(inspected, transition, *, issue=None):
    return {
        "generation_orchestrator_version": GENERATION_ORCHESTRATOR_VERSION,
        "status": "value_source_choice_required",
        "next_action": "submit_value_source_choice_patch",
        "job_id": inspected.get("job_id"),
        "request_id": inspected.get("request_id"),
        "job_transition": transition,
        "first_issue": issue,
        "value_source_choice_patch_contract": {
            "version": "1.0",
            "patch_type": "value_source_choice",
            "fields": [
                "step_id",
                "action_id",
                "operation",
                "source.kind",
                "source.reference/action_id",
            ],
            "scope": (
                "only status=available value_source qualification shapes "
                "for the named frozen Step/Action/operation; never values, "
                "provenance, windows, locators, methods, or operation changes"
            ),
        },
        "typed_patch_requirements": _typed_patch_requirements(
            "value_source_choice",
            inspected,
            issue,
        ),
        **_typed_patch_batch_fields(inspected),
        "errors": [],
        "warnings": [],
    }


def _requires_naming_patch(result):
    issue = result.get("first_issue") or {}
    return bool(
        isinstance(issue, dict)
        and issue.get("kind") == "system_baseline_needs_ai_naming"
    )


def _requires_ambiguity_choice(result):
    issue = result.get("first_issue") or {}
    return bool(
        isinstance(issue, dict)
        and issue.get("kind")
        == "system_baseline_needs_ai_ambiguity_choice"
    )


def _requires_assertion_choice(result):
    issue = result.get("first_issue") or {}
    return bool(
        isinstance(issue, dict)
        and issue.get("kind")
        == "system_baseline_needs_ai_assertion_choice"
    )


def _requires_method_choice(result):
    issue = result.get("first_issue") or {}
    return bool(
        isinstance(issue, dict)
        and issue.get("kind") == "system_baseline_needs_ai_method_choice"
    )


def _requires_operation_choice(result):
    issue = result.get("first_issue") or {}
    return bool(
        isinstance(issue, dict)
        and issue.get("kind") == "system_baseline_needs_ai_operation_choice"
    )


def _requires_value_source_choice(result):
    issue = result.get("first_issue") or {}
    return bool(
        isinstance(issue, dict)
        and issue.get("kind")
        == "system_baseline_needs_ai_value_source_choice"
    )


def _design_required_issue(result):
    marker = _design_required_marker(result)
    if not marker:
        return None
    issue = {
        "kind": marker.get("reason"),
        "message": marker.get("message") or "",
    }
    if issue["kind"] == "system_baseline_needs_ai_naming":
        issue["patch_type"] = "naming"
    elif issue["kind"] == "system_baseline_needs_ai_ambiguity_choice":
        issue["patch_type"] = "ambiguity_choice"
        details = marker.get("details") or {}
        if details.get("ambiguity_id"):
            issue["ambiguity_id"] = details.get("ambiguity_id")
    elif issue["kind"] == "system_baseline_needs_ai_assertion_choice":
        issue["patch_type"] = "assertion_choice"
        details = marker.get("details") or {}
        if details.get("ambiguity_id"):
            issue["ambiguity_id"] = details.get("ambiguity_id")
    elif issue["kind"] == "system_baseline_needs_ai_method_choice":
        issue["patch_type"] = "method_choice"
        details = marker.get("details") or {}
        if details.get("step_id"):
            issue["step_id"] = details.get("step_id")
    elif issue["kind"] == "system_baseline_needs_ai_operation_choice":
        issue["patch_type"] = "operation_choice"
        details = marker.get("details") or {}
        for field in ("step_id", "action_id"):
            if details.get(field):
                issue[field] = details.get(field)
    elif issue["kind"] == "system_baseline_needs_ai_value_source_choice":
        issue["patch_type"] = "value_source_choice"
        details = marker.get("details") or {}
        for field in ("step_id", "action_id", "operation"):
            if details.get(field):
                issue[field] = details.get(field)
    return issue


def _design_required_marker(result):
    marker = ((result.get("job_execution") or {}).get("design_required") or {})
    if marker.get("status") != "required":
        return None
    return marker


def _system_baseline_can_run(inspected):
    if _design_required_marker(inspected):
        return False
    transition = inspected.get("job_transition") or {}
    return (
        inspected.get("next_action") == "submit_generation_design"
        or transition.get("next_action") == "submit_generation_design"
    )


def _system_candidate_can_run(prepared):
    return (
        (prepared.get("system_materialization") or {}).get("status")
        == "candidate_prepared"
        and (prepared.get("candidate_preflight") or {}).get("status")
        == "passed"
    )


def _implementation_transaction_resume_mode(job_path, inspected):
    transaction = (inspected.get("job_execution") or {}).get("transaction")
    if not transaction:
        return None
    if not transaction.get("path"):
        return "legacy_prepare"
    try:
        report_path = _resumed_transaction_report_path(job_path, transaction)
        report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if report.get("status") == "running":
        return "direct"
    return None


def _prepared_from_resumed_transaction(job_path, inspected):
    transaction = ((inspected.get("job_execution") or {}).get(
        "transaction"
    ) or {})
    report_path = _resumed_transaction_report_path(job_path, transaction)
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    materialization = report.get("system_materialization") or {}
    return {
        "status": report.get("status"),
        "transaction_id": report.get("transaction_id"),
        "report_path": str(report_path),
        "system_materialization": materialization,
        "system_owned_changes": list(
            materialization.get("system_owned_files") or []
        ),
        "implementation_manifest": report.get("implementation_manifest") or {},
        "implementation_packet": report.get("implementation_packet") or {},
        "ai_editable_changes": report.get("ai_editable_changes") or [],
    }


def _resumed_transaction_report_path(job_path, transaction):
    relative = str((transaction or {}).get("path") or "")
    if not relative:
        raise ValueError("Generation Job implementation阶段缺少Transaction报告路径")
    session_dir = Path(job_path).resolve().parents[3]
    report_path = Path(relative)
    if not report_path.is_absolute():
        report_path = session_dir / report_path
    report_path = report_path.resolve()
    try:
        report_path.relative_to(session_dir.resolve())
    except ValueError as error:
        raise ValueError("Generation Job Transaction报告越出Session目录") from error
    return report_path


def _ai_editable_changes(prepared):
    packet = prepared.get("implementation_packet") or {}
    manifest = prepared.get("implementation_manifest") or {}
    return list(
        prepared.get("ai_editable_changes")
        or packet.get("ai_editable_changes")
        or manifest.get("ai_editable_changes")
        or []
    )


def _implementation_required(inspected, prepared, transition, validation=None):
    validation = validation or {}
    issues = list(validation.get("issues") or ())
    candidate_mismatches = _candidate_mismatches(validation)
    boundary = _implementation_required_boundary(
        prepared,
        validation,
        candidate_mismatches,
    )
    native_edit_allowed = boundary in {
        "explicit_native_edit",
        "candidate_mismatch",
    }
    ai_editable = _ai_editable_changes(prepared)
    system_owned = list(
        prepared.get("system_owned_changes")
        or (prepared.get("implementation_packet") or {}).get(
            "system_owned_changes"
        )
        or (prepared.get("implementation_manifest") or {}).get(
            "system_owned_changes"
        )
        or []
    )
    native_edit_candidate_files = (
        _native_edit_candidate_files(prepared, ai_editable, system_owned)
        if native_edit_allowed
        else []
    )
    candidate_delivery = (
        _candidate_delivery_projection(prepared)
        if native_edit_allowed
        else {}
    )
    candidate_manifest = candidate_delivery.get("candidate_manifest")
    candidate_index = candidate_delivery.get("candidate_index")
    projected_status = validation.get("projected_transaction_status")
    return {
        "generation_orchestrator_version": GENERATION_ORCHESTRATOR_VERSION,
        **(
            {
                "implementation_validation_version": validation[
                    "implementation_validation_version"
                ],
                "projected_transaction_status": validation.get(
                    "projected_transaction_status"
                ),
                "attempt": validation.get("attempt") or {},
                "workspace_candidate_audit": validation.get(
                    "workspace_candidate_audit"
                ) or {},
            }
            if validation.get("implementation_validation_version")
            else {}
        ),
        "status": "implementation_required",
        "job_id": inspected.get("job_id"),
        "request_id": inspected.get("request_id"),
        "transaction_id": prepared.get("transaction_id"),
        "report_path": prepared.get("report_path"),
        "job_transition": transition,
        "implementation_boundary": boundary,
        "implementation_boundary_reason": _implementation_boundary_reason(
            prepared,
            validation,
            boundary,
        ),
        "next_action": _implementation_boundary_next_action(boundary),
        "ai_editable_changes": ai_editable,
        "native_edit_candidate_files": native_edit_candidate_files,
        "candidate_files": native_edit_candidate_files,
        "system_owned_files": system_owned,
        "generation_workspace_projection": inspected.get(
            "generation_workspace_projection"
        ) or {},
        "workspace_projection_summary": prepared.get(
            "workspace_projection_summary"
        ) or inspected.get("workspace_projection_summary") or {},
        "system_materialization": prepared.get("system_materialization"),
        **(
            {"candidate_manifest": candidate_manifest}
            if candidate_manifest
            else {}
        ),
        **(
            {"candidate_index": candidate_index}
            if candidate_index
            else {}
        ),
        "implementation_packet_ref": prepared.get(
            "implementation_packet_ref"
        ),
        "first_issue": issues[0] if issues else None,
        "issue_count": len(issues),
        "workspace_candidate_status": (
            validation.get("workspace_candidate_audit") or {}
        ).get("status"),
        "candidate_file_count": len(
            (validation.get("workspace_candidate_audit") or {}).get("files")
            or []
        ),
        "candidate_mismatches": candidate_mismatches,
        "errors": list(validation.get("errors") or ()),
        "warnings": list(
            validation.get("warnings") or prepared.get("warnings") or ()
        ),
    }


def _implementation_required_boundary(prepared, validation, candidate_mismatches):
    projected_status = str(validation.get("projected_transaction_status") or "")
    if projected_status == "system_materialization_failed":
        return "system_materialization_failed"
    if _validation_scope_guard_failure(validation):
        return "transaction_scope_guard"
    if (
            (validation.get("workspace_candidate_audit") or {}).get("status")
            == "mismatch"
            or candidate_mismatches
    ):
        return "candidate_mismatch"
    preflight = prepared.get("candidate_preflight") or {}
    if preflight.get("reason") == "candidate_bundle_missing_ai_content":
        return "ai_candidate_content_required"
    if (
            preflight.get("status") == "passed"
            and (prepared.get("system_materialization") or {}).get("status")
            == "candidate_prepared"
    ):
        return "system_materialization_disabled"
    if preflight.get("status") == "failed":
        return "system_candidate_invalid"
    if prepared.get("explicit_native_edit_required"):
        return "explicit_native_edit"
    return "implementation_required"


def _implementation_boundary_reason(prepared, validation, boundary):
    if boundary == "system_materialization_failed":
        issue = next(iter(validation.get("issues") or ()), {})
        return issue.get("message") or next(iter(validation.get("errors") or ()), None)
    if boundary == "ai_candidate_content_required":
        return (prepared.get("candidate_preflight") or {}).get("reason")
    if boundary == "system_materialization_disabled":
        return "system materialization was not run for this debug/internal call"
    if boundary == "transaction_scope_guard":
        return "transaction scope guard requires review before continuing"
    if boundary == "candidate_mismatch":
        return "candidate bytes do not match the frozen transaction candidate"
    return None


def _implementation_boundary_next_action(boundary):
    return {
        "explicit_native_edit": "apply_workspace_candidate_with_native_edit",
        "candidate_mismatch": "review_candidate_mismatch",
        "ai_candidate_content_required": "provide_ai_candidate_file_content",
        "system_materialization_failed": "review_system_materialization_failure",
        "transaction_scope_guard": "review_transaction_scope_guard",
        "system_materialization_disabled": "run_with_system_materialization_enabled",
        "system_candidate_invalid": "review_generation_failure",
    }.get(boundary, "inspect_system_candidate_materialization")


def _candidate_delivery_projection(prepared):
    report_path = prepared.get("report_path")
    if not report_path:
        return {}
    try:
        candidate = query_generation_job_implementation_candidate(report_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return {
        "candidate_manifest": candidate.get("candidate_manifest"),
        "candidate_index": candidate.get("candidate_index"),
    }

def _validation_scope_guard_failure(validation):
    if str(validation.get("projected_transaction_status") or "") == "scope_violation":
        return True
    for issue in validation.get("issues") or ():
        if isinstance(issue, dict) and issue.get("code") == "scope_violation":
            return True
    attempt = validation.get("attempt") or {}
    for issue in attempt.get("issues") or ():
        if isinstance(issue, dict) and issue.get("code") == "scope_violation":
            return True
    return False


def _scope_guard_abort_reason(validation):
    messages = []
    issues = [
        *(validation.get("issues") or ()),
        *((validation.get("attempt") or {}).get("issues") or ()),
    ]
    for issue in issues:
        if isinstance(issue, dict) and issue.get("code") == "scope_violation":
            message = str(issue.get("message") or "").strip()
            if message:
                messages.append(message)
    suffix = "; ".join(dict.fromkeys(messages))
    reason = "transaction scope guard failed during native edit validation"
    if suffix:
        reason = f"{reason}: {suffix}"
    return reason[:500]


def _naming_requirements(inspected, issue):
    business_name = _business_name_scope(issue)
    if business_name:
        brief = _typed_patch_brief(inspected)
        requirement = _business_name_requirement(brief, business_name)
        return [requirement] if requirement else []
    step_id, action_id = _naming_scope(issue)
    if not step_id or not action_id:
        return []
    brief = _typed_patch_brief(inspected)
    actions = {
        (str(action.get("step_id") or ""), str(action.get("id") or "")): action
        for action in brief.get("actions") or ()
        if isinstance(action, dict)
    }
    steps = {
        str(step.get("id") or ""): step
        for step in (brief.get("target") or {}).get("steps") or ()
        if isinstance(step, dict)
    }
    action = actions.get((step_id, action_id)) or {}
    target = action.get("target") or {}
    step = steps.get(step_id) or {}
    target_display_name = _first_text(
        target.get("name"),
        target.get("text"),
        target.get("evidence_name"),
        target.get("locator_name"),
        target.get("auto_id"),
    )
    requirement = {
        "field": "target_name",
        "scope": f"{step_id}/{action_id}",
        "step_id": step_id,
        "action_id": action_id,
        "step_keyword": step.get("keyword"),
        "step_text": step.get("text"),
        "step_semantic_type": step.get("semantic_type"),
        "action_type": action.get("type"),
        "root_name": target.get("root_name"),
        "target_display_name": target_display_name,
        "control_type": target.get("control_type"),
        "evidence_name": target.get("name"),
        "auto_id": target.get("auto_id"),
        "locator_name": target.get("locator_name"),
        "locator_strategy": target.get("locator_strategy"),
        "target_fingerprint": target.get("target_fingerprint"),
        "name_hints": _naming_name_hints(step, action, target),
        "submit_argument": f"--target-name {step_id}/{action_id}=<ascii_snake_case>",
    }
    candidates = _verified_target_name_candidates(
        brief,
        step_id,
        action_id,
        target.get("target_fingerprint"),
    )
    if candidates:
        requirement["verified_name_candidates"] = candidates
    return [requirement]


def _typed_patch_requirements(patch_type, inspected, issue, *, brief=None):
    patch_type = str(patch_type or "")
    brief = brief if isinstance(brief, dict) else _typed_patch_brief(inspected)
    facts = _typed_patch_facts(patch_type, brief, issue)
    choices = _typed_patch_choices(patch_type, brief, issue)
    missing = _typed_patch_missing_fields(patch_type, facts, choices, issue)
    value = {
        "typed_patch_requirements_version": TYPED_PATCH_REQUIREMENTS_VERSION,
        "patch_type": patch_type,
        "complete": not missing,
        "scope": _typed_patch_scope(issue),
        "facts": facts,
        "choices": choices,
        "submit_arguments": _typed_patch_submit_arguments(
            patch_type,
            issue,
            facts=facts,
        ),
        "recommended_arguments": _typed_patch_recommended_arguments(
            patch_type,
            issue,
            facts=facts,
            choices=choices,
            brief=brief,
        ),
        "forbidden_fields": _typed_patch_forbidden_fields(patch_type),
        "missing_fields": missing,
    }
    recommendation = _typed_patch_recommendation(
        patch_type,
        value.get("recommended_arguments") or [],
    )
    if recommendation:
        value["recommendation"] = recommendation
    if missing:
        allowed_query = _typed_patch_allowed_query(patch_type, issue)
        if allowed_query is not None:
            value["allowed_query"] = allowed_query
        else:
            value["framework_defect"] = (
                "typed_patch_requirements_incomplete_without_allowed_query"
            )
    return value


def _typed_patch_recommendation(patch_type, recommended_arguments):
    recommended_arguments = list(recommended_arguments or [])
    if len(recommended_arguments) != 1:
        return None
    if patch_type not in {"naming", "ambiguity_choice"}:
        return None
    argument = str(recommended_arguments[0] or "")
    if "<" in argument or "generate_issue_placeholder" in argument:
        return None
    return {
        "classification": "system_verified_unique",
        "source": (
            "verified_name_candidates"
            if patch_type == "naming"
            else "verified_ambiguity_choices"
        ),
    }


def _typed_patch_facts(patch_type, brief, issue):
    step_id = str((issue or {}).get("step_id") or "")
    action_id = str((issue or {}).get("action_id") or "")
    ambiguity_id = str((issue or {}).get("ambiguity_id") or "")
    operation = str((issue or {}).get("operation") or "")
    facts = {}
    step = _brief_step(brief, step_id)
    action = _brief_action(brief, step_id, action_id)
    ambiguity = _brief_ambiguity(brief, ambiguity_id)
    if step:
        facts["step"] = _project_step_fact(step)
    if action:
        facts["action"] = _project_action_fact(action)
    if ambiguity:
        facts["ambiguity"] = _project_ambiguity_fact(ambiguity)
    if operation:
        facts["operation"] = operation
    if patch_type == "naming":
        requirements = _naming_requirements({"ai_context_envelope": {"brief": brief}}, issue)
        if requirements:
            facts["naming"] = requirements[0]
    return facts


def _typed_patch_choices(patch_type, brief, issue):
    issue = issue or {}
    if patch_type == "naming":
        return []
    if patch_type == "ambiguity_choice":
        outcomes = list(issue.get("outcomes") or [])
        if not outcomes:
            ambiguity = _brief_ambiguity(brief, issue.get("ambiguity_id"))
            outcomes = [
                item for item in (ambiguity.get("allowed_outcomes") or [])
                if isinstance(item, dict) and item.get("authority") == "ai"
            ] if ambiguity else []
        return [_project_ambiguity_outcome(item) for item in outcomes]
    if patch_type == "assertion_choice":
        candidates = list(issue.get("candidates") or [])
        if not candidates:
            ambiguity = _brief_ambiguity(brief, issue.get("ambiguity_id"))
            candidates = list(
                ((ambiguity.get("facts") or {}).get("assertion_candidates") or [])
                if ambiguity else []
            )
        return [_project_assertion_candidate(item) for item in candidates]
    if patch_type == "method_choice":
        return [_project_method_candidate(item) for item in issue.get("candidates") or []]
    if patch_type == "operation_choice":
        return [_project_operation_choice(item) for item in issue.get("candidates") or []]
    if patch_type == "value_source_choice":
        sources = list(issue.get("sources") or [])
        if not sources and issue.get("step_id") and issue.get("operation"):
            qualification = qualify_value_sources(
                brief,
                issue.get("step_id"),
                issue.get("operation"),
            )
            sources = [
                dict(item.get("shape") or {})
                for item in qualification.get("sources") or []
                if item.get("status") == "available"
                and isinstance(item.get("shape"), dict)
            ]
        return [_project_value_source_choice(item) for item in sources]
    return []


def _typed_patch_missing_fields(patch_type, facts, choices, issue):
    missing = []
    if patch_type == "naming":
        naming = facts.get("naming") or {}
        if naming.get("field") == "business_name":
            if not naming.get("root_name"):
                missing.append("facts.naming.root_name")
        else:
            for field in ("step_text", "target_display_name", "control_type"):
                if not naming.get(field):
                    missing.append(f"facts.naming.{field}")
    elif patch_type == "method_choice":
        if not facts.get("step"):
            missing.append("facts.step")
        if not choices:
            missing.append("choices")
    elif patch_type in {"operation_choice", "value_source_choice"}:
        if not facts.get("step"):
            missing.append("facts.step")
        if not facts.get("action"):
            missing.append("facts.action")
        if not choices:
            missing.append("choices")
    elif patch_type in {"ambiguity_choice", "assertion_choice"}:
        if not facts.get("ambiguity"):
            missing.append("facts.ambiguity")
        if not choices:
            missing.append("choices")
    if not _typed_patch_submit_arguments(patch_type, issue, facts=facts):
        missing.append("submit_arguments")
    return missing


def _typed_patch_submit_arguments(patch_type, issue, *, facts=None):
    issue = issue or {}
    if patch_type == "naming":
        naming = (facts or {}).get("naming") or {}
        if naming.get("submit_argument"):
            return [naming["submit_argument"]]
        business_name = _business_name_scope(issue)
        if business_name:
            root_name = str(issue.get("root_name") or business_name)
            return [f"--business-name {root_name}=<ascii_snake_case>"]
        step_id, action_id = _naming_scope(issue)
        if step_id and action_id:
            return [f"--target-name {step_id}/{action_id}=<ascii_snake_case>"]
    if patch_type == "ambiguity_choice" and issue.get("ambiguity_id"):
        return [f"--ambiguity-choice {issue['ambiguity_id']}=<outcome>"]
    if patch_type == "assertion_choice" and issue.get("ambiguity_id"):
        return [f"--assertion-choice {issue['ambiguity_id']}=<candidate_key>"]
    if patch_type == "method_choice" and issue.get("step_id"):
        return [f"--method-choice {issue['step_id']}=<candidate_id>"]
    if patch_type == "operation_choice" and issue.get("step_id") and issue.get("action_id"):
        return [f"--operation-choice {issue['step_id']}/{issue['action_id']}=<choice_key>"]
    if all(issue.get(field) for field in ("step_id", "action_id", "operation")):
        return [
            "--value-source-choice "
            f"{issue['step_id']}/{issue['action_id']}/{issue['operation']}=<kind:reference>"
        ]
    return []


def _typed_patch_recommended_arguments(
        patch_type,
        issue,
        *,
        facts,
        choices,
        brief,
    ):
    issue = issue or {}
    if patch_type == "naming":
        naming = (facts or {}).get("naming") or {}
        candidates = list(naming.get("verified_name_candidates") or [])
        if len(candidates) == 1:
            name = str(candidates[0].get("name") or "").strip()
        else:
            return []
        if not _is_public_patch_name(name):
            return []
        if naming.get("field") == "business_name":
            root_name = str(naming.get("root_name") or "").strip()
            return [f"--business-name {root_name}={name}"] if root_name else []
        scope = str(naming.get("scope") or "").strip()
        return [f"--target-name {scope}={name}"] if scope else []
    if patch_type == "ambiguity_choice" and issue.get("ambiguity_id"):
        ambiguity_id = str(issue.get("ambiguity_id") or "")
        candidates = [
            item
            for item in ((brief.get("semantics") or {}).get(
                "verified_ambiguity_choices"
            ) or [])
            if isinstance(item, dict)
            and str(item.get("ambiguity_id") or "") == ambiguity_id
        ]
        allowed = {
            str(choice.get("outcome") or "")
            for choice in choices or []
            if isinstance(choice, dict)
            and choice.get("effect") == "plan_coverage"
        }
        if candidates and len(candidates) == 1:
            outcome = str(candidates[0].get("outcome") or "").strip()
            if outcome not in allowed:
                return []
            return [f"--ambiguity-choice {ambiguity_id}={outcome}"]
        if candidates:
            return []
    return []


def _is_public_patch_name(value):
    return bool(re.fullmatch(r"[a-z][a-z0-9_]{1,63}", str(value or "")))


def _typed_patch_forbidden_fields(patch_type):
    common = ["plan_ast", "paths", "proof", "user_decision", "business_answer"]
    return {
        "naming": ["operation", "target_action_id", "value_source", "locator_body", "view_fact"],
        "ambiguity_choice": ["candidate_id", "operation", "value", "locator", "method"],
        "assertion_choice": ["operation", "parameters", "expected_value", "provenance", "locator"],
        "method_choice": ["method_body", "operation", "value", "locator", "window"],
        "operation_choice": ["operation_string", "value", "locator", "method", "proof"],
        "value_source_choice": ["actual_value", "provenance", "operation_change", "locator", "method"],
    }.get(patch_type, []) + common


def _typed_patch_scope(issue):
    issue = issue or {}
    scope = {
        key: issue.get(key)
        for key in ("step_id", "action_id", "operation")
        if issue.get(key)
    }
    if issue.get("ambiguity_id"):
        scope["ambiguity_id"] = issue.get("ambiguity_id")
    if scope:
        return scope
    step_id, action_id = _naming_scope(issue)
    if step_id and action_id:
        return {"step_id": step_id, "action_id": action_id}
    business_name = _business_name_scope(issue)
    return {"business_name": business_name} if business_name else {}


def _typed_patch_allowed_query(patch_type, issue):
    scope = _typed_patch_scope(issue)
    if patch_type in {"method_choice", "operation_choice", "value_source_choice", "naming"} and scope.get("step_id"):
        return {
            "command": "job-design-context",
            "step_id": scope.get("step_id"),
            "reason": "typed_patch_requirements_missing_bounded_step_facts",
        }
    if patch_type in {"ambiguity_choice", "assertion_choice"} and scope.get("step_id"):
        return {
            "command": "job-design-context",
            "step_id": scope.get("step_id"),
            "reason": "typed_patch_requirements_missing_ambiguity_facts",
        }
    return None


def _business_name_scope(issue):
    if not isinstance(issue, dict):
        return None
    if issue.get("business_name"):
        return str(issue["business_name"])
    match = re.search(
        r"window business_name[^:]*:\s*(?P<name>[A-Za-z0-9_\-]+)",
        str(issue.get("message") or ""),
    )
    return match.group("name") if match else None


def _business_name_requirement(brief, business_name):
    for window in ((brief.get("window_ownership") or {}).get("windows") or ()):
        root_name = str(window.get("root_name") or "")
        if not root_name:
            continue
        generated = _generated_business_name_candidates(root_name)
        if str(business_name) not in generated:
            continue
        requirement = {
            "field": "business_name",
            "scope": root_name,
            "root_name": root_name,
            "current_name": business_name,
            "window_title": window.get("title"),
            "class_name": window.get("class_name"),
            "constraints": [
                "2-64 lowercase ASCII snake_case",
                "must not end with _window or _page",
                "must not include internal identity suffixes",
            ],
            "submit_argument": (
                f"--business-name {root_name}="
                "<business_snake_case_without_window_or_page_suffix>"
            ),
        }
        candidates = _verified_business_name_candidates(brief, root_name)
        if candidates:
            requirement["verified_name_candidates"] = candidates
        return requirement
    return None


def _generated_business_name_candidates(root_name):
    full = str(root_name).replace("_window_", "_")
    full = re.sub(r"[^0-9A-Za-z_]+", "_", full).strip("_").lower()
    full = re.sub(r"_+", "_", full) or "generated"
    prefix = str(root_name).split("_window_", 1)[0]
    prefix = re.sub(r"[^0-9A-Za-z_]+", "_", prefix).strip("_").lower()
    prefix = re.sub(r"_+", "_", prefix)
    return {value for value in (full, prefix) if value}


def _brief_step(brief, step_id):
    step_id = str(step_id or "")
    return next((
        item for item in (brief.get("target") or {}).get("steps") or []
        if isinstance(item, dict) and str(item.get("id") or "") == step_id
    ), {})


def _brief_action(brief, step_id, action_id):
    step_id = str(step_id or "")
    action_id = str(action_id or "")
    return next((
        item for item in brief.get("actions") or []
        if isinstance(item, dict)
        and str(item.get("step_id") or "") == step_id
        and str(item.get("id") or "") == action_id
    ), {})


def _brief_ambiguity(brief, ambiguity_id):
    ambiguity_id = str(ambiguity_id or "")
    return next((
        item for item in brief.get("ambiguities") or []
        if isinstance(item, dict)
        and str(item.get("ambiguity_id") or "") == ambiguity_id
    ), {})


def _project_step_fact(step):
    return {
        key: step.get(key)
        for key in ("id", "keyword", "semantic_type", "text")
        if step.get(key) not in (None, "", [], {})
    }


def _project_action_fact(action):
    target = action.get("target") or {}
    return {
        "id": action.get("id"),
        "step_id": action.get("step_id"),
        "type": action.get("type"),
        "role": action.get("role"),
        "target": {
            key: target.get(key)
            for key in (
                "root_name",
                "control_type",
                "name",
                "auto_id",
                "locator_name",
                "target_fingerprint",
            )
            if target.get(key) not in (None, "", [], {})
        },
    }


def _project_ambiguity_fact(ambiguity):
    return {
        key: ambiguity.get(key)
        for key in ("ambiguity_id", "code", "step_id", "action_ids", "message")
        if ambiguity.get(key) not in (None, "", [], {})
    }


def _project_ambiguity_outcome(outcome):
    return {
        key: outcome.get(key)
        for key in ("outcome", "effect", "authority", "candidate_id", "label")
        if outcome.get(key) not in (None, "", [], {})
    }


def _project_assertion_candidate(candidate):
    return {
        key: candidate.get(key)
        for key in ("candidate_key", "operation", "target", "parameters", "reason")
        if candidate.get(key) not in (None, "", [], {})
    }


def _project_method_candidate(candidate):
    return {
        key: candidate.get(key)
        for key in ("candidate_id", "symbol", "path", "file_sha256", "call_sequence", "reason")
        if candidate.get(key) not in (None, "", [], {})
    }


def _project_operation_choice(candidate):
    value = dict(candidate or {})
    if value and not value.get("choice_key"):
        value["choice_key"] = operation_choice_key(value)
    return {
        key: value.get(key)
        for key in (
            "choice_key",
            "operation",
            "category",
            "status",
            "basis",
            "requires_value_action",
            "value_source_status",
            "action_type",
            "control_type",
            "target_fingerprint",
        )
        if value.get(key) not in (None, "", [], {})
    }


def _project_value_source_choice(source):
    return {
        key: source.get(key)
        for key in ("kind", "action_id", "reference")
        if source.get(key) not in (None, "", [], {})
    }


def _typed_patch_brief(inspected, *, job_path=None):
    envelope_brief = (inspected.get("ai_context_envelope") or {}).get("brief") or {}
    if envelope_brief.get("actions"):
        return envelope_brief
    brief_path = str(inspected.get("brief_path") or "")
    if not brief_path:
        return envelope_brief
    path = Path(brief_path)
    if not path.is_absolute():
        source_job_path = Path(str(job_path or inspected.get("job_path") or ""))
        if not source_job_path.is_absolute():
            try:
                source_job_path = source_job_path.resolve()
            except OSError:
                return envelope_brief
        if len(source_job_path.parents) < 4:
            return envelope_brief
        path = source_job_path.parents[3] / path
    try:
        return load_generation_brief(path)
    except (OSError, ValueError):
        return envelope_brief


def _naming_name_hints(step, action, target):
    hints = []
    for source, value in (
            ("target.name", target.get("name")),
            ("target.locator_name", target.get("locator_name")),
            ("target.auto_id", target.get("auto_id")),
            ("step.text", step.get("text")),
            ("action.type", action.get("type")),
            ("target.control_type", target.get("control_type")),
    ):
        text = str(value or "").strip()
        if text:
            hints.append({"source": source, "value": text})
    return hints


def _verified_target_name_candidates(
        brief,
        step_id,
        action_id,
        target_fingerprint,
    ):
    return _verified_name_candidates(
        brief,
        kind="target_name",
        step_id=step_id,
        action_id=action_id,
        target_fingerprint=target_fingerprint,
    )


def _verified_business_name_candidates(brief, root_name):
    return _verified_name_candidates(
        brief,
        kind="business_name",
        root_name=root_name,
    )


def _verified_name_candidates(brief, *, kind, **scope):
    candidates = []
    seen = set()
    for candidate in ((brief.get("semantics") or {}).get(
            "verified_naming_candidates"
    ) or ()):
        if not isinstance(candidate, dict) or candidate.get("kind") != kind:
            continue
        if any(
                str(candidate.get(key) or "") != str(value or "")
                for key, value in scope.items()
        ):
            continue
        name = str(candidate.get("name") or "")
        source = candidate.get("source") or {}
        identity = (
            name,
            str(source.get("job_id") or ""),
            str(source.get("plan_id") or ""),
        )
        if not name or identity in seen:
            continue
        seen.add(identity)
        candidates.append({
            "name": name,
            "source": {
                key: source.get(key)
                for key in ("kind", "job_id", "plan_id", "revision_seal")
                if source.get(key) not in (None, "", [], {})
            },
        })
    return candidates[:3]


def _first_text(*values):
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None


def _naming_scope(issue):
    if not isinstance(issue, dict):
        return None, None
    if issue.get("step_id") and issue.get("action_id"):
        return str(issue["step_id"]), str(issue["action_id"])
    match = re.search(
        r"step=(?P<step>[A-Za-z0-9_-]+)\s+action=(?P<action>[A-Za-z0-9_-]+)",
        str(issue.get("message") or ""),
    )
    if not match:
        return None, None
    return match.group("step"), match.group("action")


def _candidate_mismatches(validation):
    audit = (validation or {}).get("workspace_candidate_audit") or {}
    return [
        {
            "path": item.get("path"),
            "status": item.get("status"),
            "comparison": item.get("comparison"),
            "expected_sha256": item.get("expected_sha256"),
            "actual_sha256": item.get("actual_sha256"),
        }
        for item in audit.get("files") or []
        if isinstance(item, dict) and item.get("status") != "matches"
    ]


def _completed_result(inspected, completed):
    warnings = list(completed.get("warnings") or ())
    if completed is inspected:
        warnings = []
    return {
        "generation_orchestrator_version": GENERATION_ORCHESTRATOR_VERSION,
        "status": completed.get("status"),
        "next_action": (
            completed.get("job_transition") or {}
        ).get("next_action"),
        "category": completed.get("category"),
        "stages": completed.get("stages") or {},
        "failure_summary": completed.get("failure_summary"),
        "job_id": inspected.get("job_id"),
        "request_id": inspected.get("request_id"),
        "transaction_id": completed.get("transaction_id"),
        "report_path": completed.get("report_path"),
        "job_transition": completed.get("job_transition") or {},
        "last_job_result": completed.get("last_job_result"),
        "generation_workspace_projection": inspected.get(
            "generation_workspace_projection"
        ) or {},
        "workspace_projection_summary": completed.get(
            "workspace_projection_summary"
        ) or inspected.get("workspace_projection_summary") or {},
        "service_level": completed.get("service_level"),
        "health_issues": list(completed.get("health_issues") or ()),
        "unresolved_issues": list(completed.get("unresolved_issues") or ()),
        "execution_outcome": completed.get("execution_outcome") or {},
        "terminal_snapshot_audit": completed.get("terminal_snapshot_audit") or {},
        "implementation_diff": completed.get("implementation_diff"),
        "implementation_diff_summary": completed.get(
            "implementation_diff_summary"
        ) or {},
        "changed_files": list(completed.get("changed_files") or ()),
        "delivery_visibility": completed.get("delivery_visibility") or {},
        "delivery_summary": completed.get("delivery_summary") or {},
        "acceptance_summary": completed.get("acceptance_summary") or {},
        "system_materialization": completed.get("system_materialization") or {},
        "system_materialization_commit": completed.get(
            "system_materialization_commit"
        ) or {},
        "implementation_receipt": completed.get("implementation_receipt") or {},
        "errors": list(completed.get("errors") or ()),
        "warnings": warnings,
    }


def _ready_to_generate_after_refresh(prepared):
    return {
        "generation_orchestrator_version": GENERATION_ORCHESTRATOR_VERSION,
        "status": "ready_to_generate",
        "next_action": "generate_job",
        "job_id": prepared.get("job_id"),
        "request_id": prepared.get("request_id"),
        "job_path": prepared.get("job_path"),
        "job_transition": prepared.get("job_transition") or {},
        "generation_profile": prepared.get("generation_profile") or {},
        "workload": prepared.get("workload") or {},
        "execution_boundary": prepared.get("execution_boundary") or {},
        "reason": "generation_job_refreshed_before_candidate_delivery",
        "errors": list(prepared.get("errors") or ()),
        "warnings": list(prepared.get("warnings") or ()),
    }


def _prepared_transaction_boundary_is_valid(prepared):
    transition = prepared.get("job_transition") or {}
    return bool(
        str(transition.get("phase") or "") == "implementation"
        and prepared.get("transaction_id")
        and prepared.get("report_path")
    )


def _implementation_candidate_protocol_defect(inspected, prepared):
    missing = [
        field for field in ("transaction_id", "report_path")
        if not prepared.get(field)
    ]
    transition = prepared.get("job_transition") or {}
    reason = (
        "implementation phase lacks a transaction/report boundary; "
        "host-native candidate delivery cannot be projected"
    )
    return {
        "generation_orchestrator_version": GENERATION_ORCHESTRATOR_VERSION,
        "status": "failed",
        "next_action": "review_generation_failure",
        "job_id": inspected.get("job_id"),
        "request_id": inspected.get("request_id"),
        "transaction_id": prepared.get("transaction_id"),
        "report_path": prepared.get("report_path"),
        "job_transition": transition,
        "category": "internal_candidate_protocol_defect",
        "failure_summary": {
            "category": "internal_candidate_protocol_defect",
            "owner": "generation_orchestrator",
            "reason": reason,
            "prepared_status": prepared.get("status"),
            "prepared_phase": transition.get("phase"),
            "missing_fields": missing,
        },
        "errors": [reason],
        "warnings": list(prepared.get("warnings") or ()),
    }


def _required_epoch(transition):
    value = (transition or {}).get("epoch")
    if not isinstance(value, int):
        raise ValueError("Generation orchestrator transition lacks epoch")
    return value


def _required_claim(transition):
    value = str((transition or {}).get("claim_id") or "")
    if not value:
        raise ValueError("Generation orchestrator transition lacks claim_id")
    return value


def _result(inspected, *, status, next_action, reason):
    return {
        "generation_orchestrator_version": GENERATION_ORCHESTRATOR_VERSION,
        "status": status,
        "next_action": next_action,
        "job_id": inspected.get("job_id"),
        "request_id": inspected.get("request_id"),
        "job_transition": inspected.get("job_transition") or {},
        "reason": reason,
        "errors": [],
        "warnings": [],
    }


def _native_edit_candidate_files(prepared, ai_editable, system_owned):
    if ai_editable:
        return list(dict.fromkeys([*ai_editable, *system_owned]))
    if (prepared.get("system_materialization") or {}).get(
            "status"
    ) == "candidate_prepared":
        return list(dict.fromkeys(system_owned))
    return []


def _terminal_failure(inspected, result):
    return {
        "generation_orchestrator_version": GENERATION_ORCHESTRATOR_VERSION,
        "status": "failed",
        "next_action": (
            (result.get("job_transition") or {}).get("next_action")
            or result.get("next_action")
        ),
        "job_id": inspected.get("job_id"),
        "request_id": inspected.get("request_id"),
        "transaction_id": result.get("transaction_id"),
        "report_path": result.get("report_path"),
        "job_transition": result.get("job_transition") or {},
        "last_job_result": result.get("last_job_result"),
        "generation_workspace_projection": inspected.get(
            "generation_workspace_projection"
        ) or {},
        "workspace_projection_summary": result.get(
            "workspace_projection_summary"
        ) or inspected.get("workspace_projection_summary") or {},
        "category": result.get("category"),
        "stages": result.get("stages") or {},
        "failure_summary": result.get("failure_summary"),
        "service_level": result.get("service_level"),
        "health_issues": list(result.get("health_issues") or ()),
        "delivery_visibility": result.get("delivery_visibility") or {},
        "delivery_summary": result.get("delivery_summary") or {},
        "acceptance_summary": result.get("acceptance_summary") or {},
        "execution_outcome": result.get("execution_outcome") or {},
        "implementation_diff": result.get("implementation_diff"),
        "implementation_diff_summary": result.get(
            "implementation_diff_summary"
        ) or {},
        "changed_files": list(result.get("changed_files") or ()),
        "system_materialization": result.get("system_materialization") or {},
        "system_materialization_commit": result.get(
            "system_materialization_commit"
        ) or {},
        "implementation_receipt": result.get("implementation_receipt") or {},
        "workspace_candidate_audit": result.get("workspace_candidate_audit") or {},
        "reason": result.get("reason") or (result.get("abort") or {}).get("reason"),
        "errors": list(result.get("errors") or ()),
        "warnings": list(result.get("warnings") or ()),
    }


__all__ = [
    "GENERATION_ENTRYPOINT_VERSION",
    "GENERATION_ORCHESTRATOR_VERSION",
    "generate_generation_job",
    "run_generation_job",
    "settle_generation_job",
]
