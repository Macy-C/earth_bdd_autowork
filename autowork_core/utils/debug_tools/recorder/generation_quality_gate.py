from __future__ import annotations

import json
from pathlib import Path

from autowork_core.utils.debug_tools.recorder.code_manifest import (
    code_manifest_matches_transaction,
)
from autowork_core.utils.debug_tools.recorder.generation_plan import (
    PLAN_ORIGINS,
    plan_artifact_identity_is_valid,
)
from autowork_core.utils.debug_tools.recorder.generation_job import (
    generation_job_lease_is_valid,
)
from autowork_core.utils.debug_tools.recorder.request_repository import (
    request_identity_is_valid,
)
from autowork_core.utils.debug_tools.recorder.transaction_integrity import (
    completed_report_fingerprint,
)
from autowork_core.runtime.reporting.run_result_bridge import (
    CURRENT_RUN_RESULT_PROVENANCE_VERSION,
    generation_provenance_from_artifacts,
    generation_provenance_matches,
    run_result_identity_is_valid,
)
from autowork_core.runtime.reporting.oracle_registry import (
    load_persisted_matrix_run_result,
    validate_runtime_matrix_receipt,
)
from autowork_core.utils.debug_tools.recorder.runtime_risk_policy import (
    derive_runtime_risk_policy,
    runtime_risk_policy_identity_is_valid,
)


QUALITY_GATE_VERSION = "1.3"
SUPPORTED_QUALITY_GATE_VERSIONS = {QUALITY_GATE_VERSION}
def evaluate_generation_quality(
        request,
        plan_artifact,
        transaction_report,
        run_result=None,
        runtime_matrix=None,
    ):
    request = _load_value(request)
    plan_artifact = _load_value(plan_artifact)
    transaction_report = _load_value(transaction_report)
    run_result = _load_value(run_result) if run_result is not None else None
    runtime_matrix = (
        _load_value(runtime_matrix)
        if runtime_matrix is not None
        else None
    )
    reasons = []

    request_valid = request_identity_is_valid(request)
    if not request_valid:
        reasons.append("RequestV3 身份或完整性无效")

    plan_valid = plan_artifact_identity_is_valid(plan_artifact)
    if not plan_valid:
        reasons.append("GenerationPlan 身份或完整性无效")
    if plan_artifact.get("request_id") != request.get("request_id"):
        reasons.append("Plan request_id 与 Request 不一致")

    transaction_valid, transaction_reasons = _transaction_passed(
        transaction_report,
        request,
        plan_artifact,
    )
    reasons.extend(transaction_reasons)
    protocol_e2e_passed = bool(
        request_valid and plan_valid and transaction_valid
    )

    plan_origin = str(
        (plan_artifact.get("source") or {}).get("plan_origin") or ""
    )
    origin_valid = plan_origin in PLAN_ORIGINS
    if not origin_valid:
        reasons.append(f"未知 Plan 来源: {plan_origin}")
    external_ai = plan_origin == "external_ai"
    if not external_ai:
        reasons.append(
            "Plan 来源是 surrogate 或人工产物，不能声明 external AI 质量通过"
        )
    if (
        external_ai
        and (plan_artifact.get("source") or {}).get(
            "confirmation_source"
        ) != "ai_generated"
    ):
        reasons.append(
            "external_ai 来源与 Plan confirmation_source 不一致"
        )
        external_ai = False
    ai_plan_validated = bool(
        protocol_e2e_passed and origin_valid and external_ai
    )

    single_run_passed, runtime_reasons = _single_run_passed(
        request,
        plan_artifact,
        transaction_report,
        run_result,
    )
    reasons.extend(runtime_reasons)
    matrix_passed, matrix_reasons = _runtime_matrix_passed(
        request,
        plan_artifact,
        transaction_report,
        runtime_matrix,
    )
    reasons.extend(matrix_reasons)
    risk_policy = transaction_report.get("runtime_risk_policy") or {}
    risk_policy_present = bool(risk_policy)
    risk_policy_matches_source = True
    if not risk_policy_present:
        risk_policy_matches_source = False
        reasons.append(
            "Current GenerationTransaction requires a runtime risk policy"
        )
    if risk_policy_present:
        try:
            expected_risk_policy = derive_runtime_risk_policy(
                transaction_report.get("project_root"),
                transaction_report.get("implementation_manifest") or {},
            )
            risk_policy_matches_source = risk_policy == expected_risk_policy
        except (OSError, TypeError, ValueError):
            risk_policy_matches_source = False
        if not risk_policy_matches_source:
            reasons.append(
                "Transaction runtime risk policy does not match current "
                "source and Oracle registry"
            )
    risk_policy_valid = bool(
        runtime_risk_policy_identity_is_valid(risk_policy)
        and risk_policy_matches_source
    )
    high_risk = bool(
        risk_policy_valid
        and risk_policy_present
        and risk_policy.get("requires_runtime_matrix") is True
    )
    runtime_quality_passed = bool(
        single_run_passed
        and risk_policy_valid
        and matrix_passed
    )
    if (
        isinstance(run_result, dict)
        and (run_result.get("generation_provenance") or {}).get(
            "provenance_version"
        ) != CURRENT_RUN_RESULT_PROVENANCE_VERSION
    ):
        runtime_quality_passed = False
        reasons.append(
            "Current GenerationTransaction requires provenance 1.1"
        )

    if ai_plan_validated and runtime_quality_passed:
        quality_level = "ai_quality_passed"
        reasons = []
    elif protocol_e2e_passed:
        quality_level = "protocol_e2e_passed"
    else:
        quality_level = "failed"
    return {
        "quality_gate_version": QUALITY_GATE_VERSION,
        "quality_level": quality_level,
        "plan_origin": plan_origin,
        "protocol_e2e_passed": protocol_e2e_passed,
        "ai_plan_validated": ai_plan_validated,
        "quality_passed": runtime_quality_passed,
        "single_run_passed": single_run_passed,
        "runtime_matrix_required": high_risk,
        "runtime_matrix_passed": matrix_passed if high_risk else None,
        "oracle_passed": matrix_passed if high_risk else None,
        "request_id": request.get("request_id"),
        "plan_id": plan_artifact.get("plan_id"),
        "transaction_id": transaction_report.get("transaction_id"),
        "run_result_id": (
            run_result.get("run_result_id")
            if isinstance(run_result, dict)
            else None
        ),
        "reasons": list(dict.fromkeys(reasons)),
    }


def _transaction_passed(report, request, plan_artifact):
    reasons = []
    plan_job_lease = (plan_artifact.get("source") or {}).get(
        "generation_job_lease"
    )
    report_job_lease = report.get("generation_job_lease")
    if any((
        not generation_job_lease_is_valid(plan_job_lease),
        report_job_lease != plan_job_lease,
        not report.get("generation_job_claim_id"),
    )):
        reasons.append("GenerationTransaction Job lease无效或绑定不一致")
    if report.get("status") not in {"completed", "completed_no_changes"}:
        reasons.append("GenerationTransaction 未完成")
    if report.get("unresolved_issues"):
        reasons.append("GenerationTransaction 包含未解决生成问题")
    expected_fingerprint = completed_report_fingerprint(report)
    if report.get("completion_fingerprint") != expected_fingerprint:
        reasons.append("GenerationTransaction completion fingerprint 无效")
    if report.get("request_id") != request.get("request_id"):
        reasons.append("Transaction request_id 与 Request 不一致")
    if (
        (report.get("generation_plan") or {}).get("plan_fingerprint")
        != plan_artifact.get("plan_fingerprint")
    ):
        reasons.append("Transaction 使用的 Plan fingerprint 不一致")
    if not code_manifest_matches_transaction(
            report.get("code_manifest"),
            request_id=request.get("request_id"),
            plan_fingerprint=plan_artifact.get("plan_fingerprint"),
            project_root=report.get("project_root"),
            plan_audit=report.get("plan_conformance_audit"),
    ):
        reasons.append("GenerationTransaction Code Manifest 无效或绑定不一致")
    for name in report.get("required_validations") or ():
        if (report.get("validations") or {}).get(name, {}).get(
            "status"
        ) != "passed":
            reasons.append(f"Transaction 必需验证未通过: {name}")
    for field in (
        "plan_conformance_audit",
        "evidence_audit",
        "generation_policy_audit",
        "lease_revision_audit",
        *(
            ("annotation_lease_audit",)
            if request.get("annotation_snapshot") is not None
            else ()
        ),
    ):
        if (report.get(field) or {}).get("status") != "passed":
            reasons.append(f"Transaction 审计未通过: {field}")
    if report.get("errors"):
        reasons.append("GenerationTransaction 包含错误")
    if (
        (report.get("implementation_manifest") or {}).get(
            "implementation_manifest_version"
        ) == "1.12"
        and (report.get("terminal_snapshot_audit") or {}).get("status")
        != "passed"
    ):
        reasons.append("GenerationTransaction terminal snapshot 未完成复核")
    return not reasons, reasons


def _single_run_passed(request, plan_artifact, transaction_report, run_result):
    if not isinstance(run_result, dict):
        return False, ["缺少匹配的真实运行结果"]
    if not run_result_identity_is_valid(run_result):
        return False, ["Run Result 身份或内容指纹无效"]
    expected_provenance = generation_provenance_from_artifacts(
        request,
        plan_artifact,
        transaction_report,
    )
    if not generation_provenance_matches(
        run_result,
        expected_provenance,
    ):
        return False, [
            "Run Result 未绑定当前 Request、Plan、Transaction 和代码快照"
        ]
    if run_result.get("status") != "passed":
        return False, ["Run Result 整体运行未通过"]
    feature_path = _normalized_path(
        ((request.get("target") or {}).get("feature") or {}).get(
            "source_relpath"
        )
    )
    scenario = (request.get("target") or {}).get("scenario") or {}
    template_name = str(
        (((scenario.get("specification") or {}).get("template") or {}).get(
            "name"
        )
        or "")
    )
    scenario_names = {
        str(scenario.get("name") or ""),
        template_name,
    } - {""}
    example_id = _normalized_example_id(scenario.get("example_id"))
    match = next((
        candidate
        for feature in run_result.get("features") or ()
        if _normalized_path(feature.get("source_relpath")) == feature_path
        for candidate in feature.get("scenarios") or ()
        if {
            str(candidate.get("name") or ""),
            str(candidate.get("outline_name") or ""),
        } & scenario_names
        and _normalized_example_id(candidate.get("example_id"))
        == example_id
    ), None)
    if match is None:
        return False, ["未找到与 Request Feature/Scenario/Examples 匹配的运行结果"]
    if match.get("status") != "passed":
        return False, ["匹配 Scenario 的真实运行未通过"]
    matched_steps, missing = _ordered_target_step_statuses(request, match)
    failed = [
        label for label, status in matched_steps
        if status != "passed"
    ]
    reasons = []
    if missing:
        reasons.append(f"运行结果缺少目标 Step: {missing}")
    if failed:
        reasons.append(f"目标 Step 运行未通过: {failed}")
    return not reasons, reasons


def _runtime_matrix_passed(
        request,
        plan_artifact,
        transaction_report,
        runtime_matrix,
    ):
    policy = transaction_report.get("runtime_risk_policy") or {}
    if not policy:
        return True, []
    if not runtime_risk_policy_identity_is_valid(policy):
        return False, ["Transaction runtime risk policy invalid"]
    if policy.get("requires_runtime_matrix") is not True:
        return True, []
    expected_provenance = generation_provenance_from_artifacts(
        request,
        plan_artifact,
        transaction_report,
    )
    errors = validate_runtime_matrix_receipt(
        runtime_matrix,
        policy,
        expected_provenance,
        project_root=transaction_report.get("project_root"),
    )
    if errors:
        return False, [
            "高风险运行矩阵或独立Oracle未通过: " + error
            for error in errors
        ]
    variants = runtime_matrix.get("variants") or []
    run_result_ids = set()
    for variant in variants:
        role = str(variant.get("role") or "")
        try:
            result = load_persisted_matrix_run_result(
                transaction_report.get("project_root"),
                variant,
            )
        except (OSError, TypeError, ValueError) as error:
            errors.append(
                f"runtime matrix persisted Run Result invalid: {role}: {error}"
            )
            continue
        if not run_result_identity_is_valid(result):
            errors.append(f"runtime matrix Run Result invalid: {role}")
            continue
        if not generation_provenance_matches(result, expected_provenance):
            errors.append(f"runtime matrix Run Result provenance mismatch: {role}")
        if variant.get("run_result_id") != result.get("run_result_id"):
            errors.append(f"runtime matrix Run Result id mismatch: {role}")
        if variant.get("run_result_fingerprint") != result.get("fingerprint"):
            errors.append(f"runtime matrix Run Result fingerprint mismatch: {role}")
        run_result_id = str(result.get("run_result_id") or "")
        if not run_result_id or run_result_id in run_result_ids:
            errors.append(f"runtime matrix Run Result reused: {role}")
        run_result_ids.add(run_result_id)
        outcome = str(variant.get("outcome") or "")
        errors.extend(
            f"runtime matrix target mismatch: {role}: {reason}"
            for reason in _matrix_target_reasons(
                request,
                result,
                outcome,
            )
        )
        if outcome == "business_passed" and result.get("status") != "passed":
            errors.append(f"runtime matrix business variant failed: {role}")
        if outcome == "fail_closed" and result.get("status") != "failed":
            errors.append(f"runtime matrix fail-closed variant did not fail: {role}")
    return (not errors), [
        "高风险运行矩阵或独立Oracle未通过: " + error
        for error in errors
    ]


def _matrix_target_reasons(request, run_result, outcome):
    feature_path = _normalized_path(
        ((request.get("target") or {}).get("feature") or {}).get(
            "source_relpath"
        )
    )
    scenario = (request.get("target") or {}).get("scenario") or {}
    template_name = str(
        (((scenario.get("specification") or {}).get("template") or {}).get(
            "name"
        ) or "")
    )
    scenario_names = {
        str(scenario.get("name") or ""),
        template_name,
    } - {""}
    example_id = _normalized_example_id(scenario.get("example_id"))
    match = next((
        candidate
        for feature in run_result.get("features") or ()
        if _normalized_path(feature.get("source_relpath")) == feature_path
        for candidate in feature.get("scenarios") or ()
        if {
            str(candidate.get("name") or ""),
            str(candidate.get("outline_name") or ""),
        } & scenario_names
        and _normalized_example_id(candidate.get("example_id")) == example_id
    ), None)
    if match is None:
        return ["target Feature/Scenario/Examples missing"]
    matched_steps, missing = _ordered_target_step_statuses(request, match)
    reasons = [f"target Steps missing: {missing}"] if missing else []
    if outcome == "business_passed":
        if match.get("status") != "passed":
            reasons.append("target Scenario did not pass")
        failed = [
            label for label, status in matched_steps
            if status != "passed"
        ]
        if failed:
            reasons.append(f"target Steps did not pass: {failed}")
    elif outcome == "fail_closed":
        if match.get("status") != "failed":
            reasons.append("target Scenario did not fail closed")
        if not any(status == "failed" for _label, status in matched_steps):
            reasons.append("no target Step failed closed")
    return reasons


def _ordered_target_step_statuses(request, scenario_result):
    targets = [
        (
            str(step.get("text") or ""),
            str(step.get("id") or ""),
        )
        for step in (request.get("target") or {}).get("steps") or ()
    ]
    observed = [
        (
            str(step.get("name") or ""),
            str(step.get("status") or "unknown"),
        )
        for step in (scenario_result or {}).get("steps") or ()
    ]
    matched = []
    missing = []
    cursor = 0
    occurrences = {}
    for text, step_id in targets:
        occurrences[text] = occurrences.get(text, 0) + 1
        label = (
            f"{text}#{occurrences[text]}"
            if text
            else step_id
        )
        index = next((
            position
            for position in range(cursor, len(observed))
            if observed[position][0] == text
        ), None)
        if index is None:
            missing.append(label)
            continue
        matched.append((label, observed[index][1]))
        cursor = index + 1
    return matched, missing


def _load_value(value):
    if isinstance(value, dict):
        return dict(value)
    path = Path(value).resolve()
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"JSON 必须是 object: {path}")
    return loaded


def _normalized_path(value):
    return str(value or "").replace("\\", "/").casefold()


def _normalized_example_id(value):
    return str(value or "").strip().removeprefix("@").strip()


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        description="Evaluate Recorder protocol and external-AI quality"
    )
    parser.add_argument("request_path")
    parser.add_argument("plan_path")
    parser.add_argument("transaction_report_path")
    parser.add_argument("run_result_path")
    parser.add_argument("--runtime-matrix")
    args = parser.parse_args(argv)
    result = evaluate_generation_quality(
        args.request_path,
        args.plan_path,
        args.transaction_report_path,
        args.run_result_path,
        runtime_matrix=args.runtime_matrix,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["quality_level"] != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
