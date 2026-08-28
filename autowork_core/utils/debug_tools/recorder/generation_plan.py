from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path, PurePosixPath

from autowork_core.utils.debug_tools.recorder.annotations import (
    annotation_snapshot_is_valid,
    build_annotation_snapshot,
)
from autowork_core.utils.debug_tools.recorder.models import SCHEMA_VERSION
from autowork_core.utils.debug_tools.recorder.semantic_pack import (
    explicit_assertion_expectation,
)
from autowork_core.utils.debug_tools.recorder.table_usage import (
    normalize_table_usage,
    validate_table_business_outcome,
    validate_table_usage,
)
from autowork_core.utils.debug_tools.recorder.ai_capability_registry import (
    capability_by_name,
    plan_operation_names,
)
from autowork_core.utils.debug_tools.recorder.action_knowledge import (
    operation_compatibility,
)
from autowork_core.utils.debug_tools.recorder.code_reuse_index import (
    candidate_step_pattern_contracts,
    step_pattern_contract_matches,
)
from autowork_core.utils.debug_tools.recorder.identity import (
    locator_candidate_id as expected_locator_candidate_id,
)
from autowork_core.utils.debug_tools.recorder.writer import write_json_atomic
from autowork_core.utils.debug_tools.recorder.value_authority import (
    declared_example_arguments,
    SEMANTIC_LITERAL_OPERATIONS,
    resolve_feature_literal,
    resolve_declared_feature_literal,
    resolve_recorded_action_value,
)
from autowork_core.utils.bus import normalize


PLAN_VERSION = "4.2"
SUPPORTED_PLAN_VERSIONS = {PLAN_VERSION}
PLAN_ORIGINS = {
    "external_ai",
    "deterministic_surrogate",
    "human_authored",
}
MAX_MEMORY_TRACE_ITEMS = 6
MAX_MEMORY_TRACE_REASON = 96
ALLOWED_OPERATIONS = plan_operation_names()
IMPLEMENTATION_LOCATIONS = {
    "page_method",
    "step_inline_base_api",
}
_RUNTIME_BINDING_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_RUNTIME_SOURCE_PREFIX = "runtime."
_RUNTIME_PRODUCER_OPERATIONS = frozenset({"save_text", "save_attr"})


def _is_qualified_implementation_method(value):
    owner, separator, method = str(value or "").rpartition(".")
    return bool(
        separator
        and owner.isidentifier()
        and method.isidentifier()
    )


def load_generation_plan(session_dir, state, request):
    pointer = (state or {}).get("plan") or {}
    if not pointer.get("path"):
        return None
    session_dir = Path(session_dir).resolve()
    path = (session_dir / pointer["path"]).resolve()
    expected = (
        session_dir / "ai" / "plans" / str(request.get("request_id"))
    ).resolve()
    try:
        path.relative_to(expected)
        artifact = _read_json(path)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    source = artifact.get("source") or {}
    decision = (state or {}).get("decision") or {}
    if decision.get("status") == "awaiting_answers":
        return None
    expected_answer_fingerprint = (
        ((decision.get("answers") or {}).get("answer_fingerprint"))
        if (decision.get("answers") or {}).get("path")
        else None
    )
    if any((
        not plan_artifact_identity_is_valid(artifact),
        artifact.get("request_id") != request.get("request_id"),
        artifact.get("plan_fingerprint") != pointer.get("plan_fingerprint"),
        source.get("revision_seal")
        != (request.get("revision_snapshot") or {}).get("seal"),
        source.get("decision_answer_fingerprint")
        != expected_answer_fingerprint,
        not _generation_job_lease_matches_state(
            source.get("generation_job_lease"),
            state,
        ),
        (
            source.get("intent_fingerprint")
            != pointer.get("intent_fingerprint")
            if pointer.get("intent_fingerprint")
            else False
        ),
    )):
        return None
    brief_pointer = (state or {}).get("brief") or {}
    brief_path_value = brief_pointer.get("path")
    if not brief_path_value:
        return None
    brief_path = (session_dir / brief_path_value).resolve()
    brief_root = (session_dir / "ai" / "generation-briefs").resolve()
    try:
        brief_path.relative_to(brief_root)
        from autowork_core.utils.debug_tools.recorder.reconciliation_repository import (
            load_generation_brief,
        )
        from autowork_core.utils.debug_tools.recorder.semantic_reconciler import (
            brief_matches_request,
        )

        brief = load_generation_brief(brief_path)
        user_confirmed_references = _trusted_decision_references(
            session_dir,
            state,
            request,
            brief,
        )
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if any((
        brief.get("brief_fingerprint")
        != brief_pointer.get("brief_fingerprint"),
        not brief_matches_request(brief, request),
        source.get("brief_basis_fingerprint")
        != _plan_brief_basis_fingerprint(brief),
        bool(validate_generation_plan(
            artifact.get("plan") or {},
            brief,
            require_window_ownership=True,
            require_scenario_model=True,
            user_confirmed_references=user_confirmed_references,
            require_action_roles=True,
        )),
    )):
        return None
    return artifact


def plan_pointer(session_dir, artifact, path=None):
    path = Path(path or artifact.get("plan_path") or "").resolve()
    return {
        "path": path.relative_to(Path(session_dir).resolve()).as_posix(),
        "plan_id": artifact.get("plan_id"),
        "plan_fingerprint": artifact.get("plan_fingerprint"),
        "revision_seal": (artifact.get("source") or {}).get("revision_seal"),
        "confirmation_source": (
            artifact.get("source") or {}
        ).get("confirmation_source"),
        "plan_origin": (
            artifact.get("source") or {}
        ).get("plan_origin"),
        "intent_fingerprint": (
            artifact.get("source") or {}
        ).get("intent_fingerprint"),
        "decision_answer_fingerprint": (
            artifact.get("source") or {}
        ).get("decision_answer_fingerprint"),
        "generation_contract_lease_fingerprint": (
            ((artifact.get("source") or {}).get(
                "generation_contract_lease"
            ) or {}).get("lease_fingerprint")
        ),
        "generation_job_lease_fingerprint": (
            ((artifact.get("source") or {}).get(
                "generation_job_lease"
            ) or {}).get("lease_fingerprint")
        ),
    }


def compile_generation_intent(intent, brief):
    compiled = json.loads(json.dumps(intent or {}, ensure_ascii=False))
    forbidden_proof_fields = {
        "annotation_trace",
        "annotation_references",
        "annotation_snapshot",
    }
    unexpected = forbidden_proof_fields & set(compiled)
    if unexpected:
        raise ValueError(
            "AI Intent不能提交系统Annotation proof字段: "
            f"{sorted(unexpected)}"
        )
    snapshot = _brief_annotation_snapshot(brief)
    required_by_step = snapshot.get(
        "required_annotation_ids_by_step"
    ) or {}
    actions_by_step = {}
    for action in (brief or {}).get("actions") or ():
        step_id = str(action.get("step_id") or "")
        action_id = str(action.get("id") or "")
        if step_id and action_id:
            actions_by_step.setdefault(step_id, {})[action_id] = action

    for step_id, step in (compiled.get("steps") or {}).items():
        step = step if isinstance(step, dict) else {}
        step_unexpected = forbidden_proof_fields & set(step)
        if step_unexpected:
            raise ValueError(
                f"Step {step_id}不能提交系统Annotation proof字段: "
                f"{sorted(step_unexpected)}"
            )
        expected_annotation_ids = sorted(
            str(item)
            for item in required_by_step.get(str(step_id)) or ()
        )
        if "annotation_ids" in step:
            provided_annotation_ids = _unique_strings(
                step.get("annotation_ids")
            )
            if sorted(provided_annotation_ids) != expected_annotation_ids:
                raise ValueError(
                    f"Step {step_id} annotation_ids与当前Annotation scope不一致: "
                    f"expected={expected_annotation_ids}, "
                    f"actual={sorted(provided_annotation_ids)}"
                )
        step["annotation_ids"] = expected_annotation_ids
        available = actions_by_step.get(str(step_id), {})
        for operation in (step or {}).get("operations") or ():
            if not isinstance(operation, dict):
                continue
            operation_proof_fields = {
                key
                for key in operation
                if key.startswith("annotation_")
            }
            if operation_proof_fields:
                raise ValueError(
                    f"Step {step_id} operation不能提交Annotation proof字段: "
                    f"{sorted(operation_proof_fields)}"
                )
            op = str(operation.get("op") or "")
            target_action_id = str(
                operation.get("target_action_id") or ""
            ).strip()
            if not target_action_id:
                raise ValueError(
                    f"Step {step_id} 操作 {op} 缺少 target_action_id"
                )
            target_action = available.get(target_action_id)
            if target_action is None:
                raise ValueError(
                    f"Step {step_id} 操作 {op} target_action_id "
                    f"引用未知 action: {target_action_id}"
                )
            value_action_ids = _unique_strings(
                operation.get("value_action_ids")
            )
            unknown_value_actions = [
                action_id
                for action_id in value_action_ids
                if action_id not in available
            ]
            if unknown_value_actions:
                raise ValueError(
                    f"Step {step_id} 操作 {op} value_action_ids "
                    f"引用未知 action: {unknown_value_actions}"
                )

            action_ids = _unique_strings([
                target_action_id,
                *value_action_ids,
            ])
            provided_action_ids = _unique_strings(
                operation.get("action_ids")
            )
            if provided_action_ids and provided_action_ids != action_ids:
                raise ValueError(
                    f"Step {step_id} 操作 {op} action_ids 与 "
                    "target/value Action 选择不一致"
                )

            target_fingerprint = str(
                ((target_action.get("target") or {}).get(
                    "target_fingerprint"
                ))
                or ""
            ).strip()
            if not target_fingerprint:
                raise ValueError(
                    f"Step {step_id} 操作 {op} 的 target Action "
                    "缺少冻结 target_fingerprint"
                )
            locator_candidate_id = str(
                operation.get("locator_candidate_id") or ""
            ).strip()
            target = target_action.get("target") or {}
            target_locator = target.get("locator") or {}
            if (
                "locator_candidates" in target
                and target_locator.get("by", "child") in {"child", "xpath"}
                and not locator_candidate_id
            ):
                raise ValueError(
                    f"Step {step_id} 操作 {op}缺少冻结locator candidate ID"
                )
            if locator_candidate_id:
                matches = [
                    item
                    for item in target.get("locator_candidates") or ()
                    if isinstance(item, dict)
                    and str(item.get("candidate_id") or "")
                    == locator_candidate_id
                ]
                if len(matches) != 1:
                    raise ValueError(
                        f"Step {step_id} 操作 {op}引用未知或冲突"
                        f"locator candidate: {locator_candidate_id}"
                    )
                candidate = matches[0]
                validation = candidate.get("validation") or {}
                locator = candidate.get("locator") or {}
                if any((
                    locator_candidate_id != expected_locator_candidate_id(
                        locator,
                        candidate.get("reason"),
                    ),
                    validation.get("status") != "unique",
                    validation.get("target_matches") is not True,
                    str(locator.get("root") or "")
                    != str(target.get("root_name") or ""),
                    locator.get("by", "child") not in {"child", "xpath"},
                )):
                    raise ValueError(
                        f"Step {step_id} 操作 {op}的locator candidate"
                        "未通过冻结唯一目标验证"
                    )
                operation["locator_candidate_id"] = locator_candidate_id
            provided_fingerprint = str(
                operation.get("target_fingerprint") or ""
            ).strip()
            if (
                provided_fingerprint
                and provided_fingerprint != target_fingerprint
            ):
                raise ValueError(
                    f"Step {step_id} 操作 {op} target_fingerprint "
                    "与 target_action_id 不一致"
                )

            derived_evidence = _unique_strings(
                evidence_id
                for action_id in action_ids
                for evidence_id in (
                    (available.get(action_id) or {}).get("evidence") or ()
                )
            )
            provided_evidence = _unique_strings(
                operation.get("evidence_ids")
            )
            if provided_evidence and not set(derived_evidence) <= set(
                provided_evidence
            ):
                raise ValueError(
                    f"Step {step_id} 操作 {op} evidence_ids 未覆盖 "
                    "target/value Action 的冻结证据"
                )

            operation["action_ids"] = action_ids
            operation["target_action_id"] = target_action_id
            operation["value_action_ids"] = value_action_ids
            operation["target_fingerprint"] = target_fingerprint
            operation["evidence_ids"] = _unique_strings([
                *derived_evidence,
                *provided_evidence,
            ])
    missing_steps = sorted(
        str(step_id)
        for step_id, annotation_ids in required_by_step.items()
        if annotation_ids and str(step_id) not in (compiled.get("steps") or {})
    )
    if missing_steps:
        raise ValueError(
            "AI Intent缺少带Annotation约束的Step: "
            f"{missing_steps}"
        )
    return compiled


def bind_generation_annotation_trace(plan, brief):
    plan = json.loads(json.dumps(plan or {}, ensure_ascii=False))
    snapshot = _brief_annotation_snapshot(brief)
    references = {
        str(item.get("annotation_id") or ""): dict(item)
        for item in snapshot.get("references") or ()
        if item.get("annotation_id")
    }
    selected_ids = sorted({
        str(annotation_id)
        for step in (plan.get("steps") or {}).values()
        if isinstance(step, dict)
        for annotation_id in step.get("annotation_ids") or ()
        if annotation_id
    })
    unknown = [
        annotation_id
        for annotation_id in selected_ids
        if annotation_id not in references
    ]
    if unknown:
        raise ValueError(
            f"Plan annotation_ids引用未知Annotation: {unknown}"
        )
    plan["annotation_trace"] = {
        "annotation_snapshot_version": snapshot.get(
            "annotation_snapshot_version"
        ),
        "snapshot_fingerprint": snapshot.get("snapshot_fingerprint"),
        "required_annotation_ids_by_step": snapshot.get(
            "required_annotation_ids_by_step"
        ) or {},
        "references": [references[item] for item in selected_ids],
    }
    return plan


def _generation_input_content(value, *, input_kind):
    if input_kind != "generation_design":
        raise ValueError(f"未知generation input kind: {input_kind}")
    content = json.loads(json.dumps(value or {}, ensure_ascii=False))
    for key in (
        "annotation_trace",
        "annotation_references",
        "annotation_snapshot",
        "action_ids",
        "evidence_ids",
        "target_fingerprint",
    ):
        content.pop(key, None)
    return content


def _generation_intent_fingerprint(
        request_id,
        revision_seal,
        brief_basis_fingerprint,
        content,
        *,
        input_kind,
        input_version,
):
    value = {
        "intent_version": "1.0",
        "request_id": request_id,
        "revision_seal": revision_seal,
        "brief_basis_fingerprint": brief_basis_fingerprint,
        "content": content,
    }
    value["input_kind"] = input_kind
    value["input_version"] = input_version
    return _stable_hash(value)


def _generation_intent_identity_is_valid(artifact):
    source = artifact.get("source") or {}
    expected = source.get("intent_fingerprint")
    intent = artifact.get("intent")
    if not expected or not isinstance(intent, dict):
        return False
    if intent.get("intent_version") != "1.0":
        return False
    content = intent.get("content")
    if not isinstance(content, dict):
        return False
    input_kind = str(intent.get("input_kind") or "")
    input_version = str(intent.get("input_version") or "")
    if input_kind != "generation_design" or not input_version:
        return False
    actual = _generation_intent_fingerprint(
        artifact.get("request_id"),
        source.get("revision_seal"),
        source.get("brief_basis_fingerprint"),
        content,
        input_kind=input_kind,
        input_version=input_version,
    )
    return all((
        intent.get("intent_fingerprint") == expected,
        actual == expected,
    ))


def normalize_generation_plan(request, plan):
    plan = dict(plan or {})
    target_step_ids = [
        str(step.get("id"))
        for step in (request.get("target") or {}).get("steps") or []
        if step.get("id")
    ]
    steps = plan.get("steps") or {}
    normalized_steps = {}
    for step_id, value in dict(steps).items():
        value = dict(value or {})
        operations = []
        for operation in value.get("operations") or []:
            if not isinstance(operation, dict):
                continue
            parameters = (
                json.loads(json.dumps(
                    operation.get("parameters") or {},
                    ensure_ascii=False,
                ))
                if isinstance(operation.get("parameters"), dict)
                else {}
            )
            value_source = str(
                operation.get("source") or ""
            ).strip() or None
            operations.append({
                "op": str(operation.get("op") or "").strip(),
                "target": str(operation.get("target") or "").strip() or None,
                "value": operation.get("value"),
                "source": value_source,
                "value_provenance": _normalize_value_provenance(
                    operation.get("value_provenance"),
                    source=value_source,
                    value_action_ids=operation.get("value_action_ids"),
                ),
                "result_binding": str(
                    operation.get("result_binding") or ""
                ).strip() or None,
                "window_owner": str(
                    operation.get("window_owner") or ""
                ).strip() or None,
                "view_owner": str(
                    operation.get("view_owner") or ""
                ).strip() or None,
                "implementation_location": str(
                    operation.get("implementation_location")
                    or "page_method"
                ).strip(),
                "implementation_method": str(
                    operation.get("implementation_method") or ""
                ).strip() or None,
                "implementation_resolution": (
                    _normalize_implementation_resolution(
                        operation.get("implementation_resolution")
                    )
                    if isinstance(
                        operation.get("implementation_resolution"),
                        dict,
                    )
                    else None
                ),
                "parameters": parameters,
                "action_ids": _unique_strings(operation.get("action_ids")),
                "target_action_id": str(
                    operation.get("target_action_id") or ""
                ).strip() or None,
                "value_action_ids": _unique_strings(
                    operation.get("value_action_ids")
                ),
                "target_fingerprint": str(
                    operation.get("target_fingerprint") or ""
                ).strip() or None,
                "evidence_ids": _unique_strings(operation.get("evidence_ids")),
                "effect_ids": _unique_strings(operation.get("effect_ids")),
                "decision_ids": _unique_strings(operation.get("decision_ids")),
                "reason": str(operation.get("reason") or "").strip() or None,
                "rejected_alternatives": _unique_strings(
                    operation.get("rejected_alternatives")
                ),
                "confidence": _confidence(operation.get("confidence")),
                "uncertainty": operation.get("uncertainty"),
                "reuse_reference": operation.get("reuse_reference"),
            })
        normalized_steps[str(step_id)] = {
            "behavior_owner": str(
                value.get("behavior_owner") or "existing_step_definition"
            ),
            "behavior_file": value.get("behavior_file"),
            "behavior_resolution": _normalize_implementation_resolution(
                value.get("behavior_resolution")
            ),
            "covered_action_ids": _unique_strings(
                value.get("covered_action_ids")
            ),
            "action_relationships": _normalize_action_relationships(
                value.get("action_relationships")
            ),
            "annotation_ids": _unique_strings(
                value.get("annotation_ids")
            ),
            "page_object": value.get("page_object"),
            "locator_file": value.get("locator_file"),
            "data_file": value.get("data_file"),
            "operations": operations,
            "locators": [
                dict(item)
                for item in value.get("locators") or []
                if isinstance(item, dict)
            ],
            "ignored_action_ids": _unique_strings(
                value.get("ignored_action_ids")
            ),
            "table_usage": normalize_table_usage(
                value.get("table_usage"),
            ),
            "unresolved_issues": [
                dict(item)
                for item in value.get("unresolved_issues") or ()
                if isinstance(item, dict)
            ],
        }
    return {
        "summary": str(plan.get("summary") or "").strip(),
        **(
            {
                "annotation_trace": _normalize_annotation_trace(
                    plan.get("annotation_trace")
                )
            }
            if "annotation_trace" in plan
            else {}
        ),
        "scenario_model": _normalize_scenario_model(
            plan.get("scenario_model")
        ),
        "window_owners": _normalize_window_owners(
            plan.get("window_owners")
        ),
        "steps": normalized_steps,
        "global_changes": [
            dict(item)
            for item in plan.get("global_changes") or []
            if isinstance(item, dict)
        ],
        "decision_trace": [
            dict(item)
            for item in plan.get("decision_trace") or []
            if isinstance(item, dict)
        ],
        "pic_authorizations": [
            dict(item)
            for item in plan.get("pic_authorizations") or []
            if isinstance(item, dict)
        ],
        "memory_trace": _normalize_memory_trace(
            plan.get("memory_trace"),
        ),
        "ambiguity_resolutions": [
            _normalize_ambiguity_resolution(item)
            for item in plan.get("ambiguity_resolutions") or []
            if isinstance(item, dict)
        ],
        "uncertainties": [
            item
            for item in plan.get("uncertainties") or []
            if isinstance(item, (str, dict))
        ],
    }


def _normalize_action_relationships(value):
    if value is None:
        return []
    if not isinstance(value, list):
        return [{
            "kind": "",
            "source_action_id": "",
            "consumer_action_id": "",
        }]
    result = []
    for item in value:
        if not isinstance(item, dict):
            result.append({
                "kind": "",
                "source_action_id": "",
                "consumer_action_id": "",
            })
            continue
        relationship = {
            "kind": str(item.get("kind") or "").strip(),
            "source_action_id": str(
                item.get("source_action_id") or ""
            ).strip(),
            "consumer_action_id": str(
                item.get("consumer_action_id") or ""
            ).strip(),
        }
        reason = str(item.get("reason") or "").strip()
        if reason:
            relationship["reason"] = reason
        result.append(relationship)
    return result


def apply_decision_constraints(plan, compiled_patch):
    plan = json.loads(json.dumps(plan or {}, ensure_ascii=False))
    compiled_patch = dict(compiled_patch or {})
    steps = plan.setdefault("steps", {})
    for step_id, constraints in (
            compiled_patch.get("steps") or {}
    ).items():
        step = steps.setdefault(str(step_id), {})
        step["ignored_action_ids"] = _unique_strings([
            *(step.get("ignored_action_ids") or []),
            *(constraints.get("ignored_action_ids") or []),
        ])
    plan["decision_trace"] = _merge_by_key(
        plan.get("decision_trace") or [],
        compiled_patch.get("decision_trace") or [],
        "question_id",
    )
    plan["ambiguity_resolutions"] = _merge_by_key(
        plan.get("ambiguity_resolutions") or [],
        compiled_patch.get("ambiguity_resolutions") or [],
        "ambiguity_id",
    )
    plan["pic_authorizations"] = [
        dict(item)
        for item in compiled_patch.get("pic_authorizations") or []
        if isinstance(item, dict)
    ]
    plan["uncertainties"] = list(dict.fromkeys([
        *(
            json.dumps(item, ensure_ascii=False, sort_keys=True)
            for item in plan.get("uncertainties") or []
        ),
        *(
            json.dumps(item, ensure_ascii=False, sort_keys=True)
            for item in compiled_patch.get("uncertainties") or []
        ),
    ]))
    plan["uncertainties"] = [
        json.loads(item)
        for item in plan["uncertainties"]
    ]
    return plan


def validate_decision_conformance(plan, compiled_patch):
    errors = []
    compiled_patch = dict(compiled_patch or {})
    for step_id, constraints in (
            compiled_patch.get("steps") or {}
    ).items():
        step = (plan.get("steps") or {}).get(str(step_id)) or {}
        expected_business_outcome = constraints.get(
            "table_business_outcome"
        )
        if expected_business_outcome:
            errors.extend(validate_table_business_outcome(
                step_id,
                expected_business_outcome,
                step.get("table_usage"),
            ))
    expected_authorizations = {
        item.get("authorization_id"): item
        for item in compiled_patch.get("pic_authorizations") or []
    }
    actual_authorizations = {
        item.get("authorization_id"): item
        for item in plan.get("pic_authorizations") or []
    }
    for authorization_id, expected in expected_authorizations.items():
        if actual_authorizations.get(authorization_id) != expected:
            errors.append(
                f"Plan 未保留 PIC Decision: {authorization_id}"
            )
    unexpected_authorizations = sorted(
        str(item)
        for item in set(actual_authorizations) - set(expected_authorizations)
        if item
    )
    if unexpected_authorizations:
        errors.append(
            "Plan 包含未由 Decision Answers 生成的 PIC authorization: "
            f"{unexpected_authorizations}"
        )
    expected_decisions = {
        item.get("question_id")
        for item in compiled_patch.get("decision_trace") or []
    }
    actual_decisions = {
        item.get("question_id")
        for item in plan.get("decision_trace") or []
    }
    missing = sorted(expected_decisions - actual_decisions)
    if missing:
        errors.append(f"Plan 缺少 Decision trace: {missing}")
    expected_resolutions = {
        item.get("ambiguity_id"): item
        for item in compiled_patch.get("ambiguity_resolutions") or []
        if item.get("ambiguity_id")
    }
    actual_resolutions = {
        item.get("ambiguity_id"): item
        for item in plan.get("ambiguity_resolutions") or []
        if item.get("ambiguity_id")
    }
    for ambiguity_id, expected in expected_resolutions.items():
        if actual_resolutions.get(ambiguity_id) != expected:
            errors.append(
                f"Plan 未保留 ambiguity Decision: {ambiguity_id}"
            )
    for step_id, constraints in (compiled_patch.get("steps") or {}).items():
        actual_ignored = set(
            ((plan.get("steps") or {}).get(str(step_id)) or {}).get(
                "ignored_action_ids"
            ) or []
        )
        expected_ignored = set(constraints.get("ignored_action_ids") or [])
        if not expected_ignored <= actual_ignored:
            errors.append(
                f"Plan 未保留 ignored action Decision: step={step_id}"
            )
    return errors


def validate_generation_plan(
    plan,
    brief,
    *,
    require_window_ownership=False,
    require_scenario_model=None,
    require_action_roles=False,
    user_confirmed_references=(),
):
    errors = []
    target_ids = {
        str(step.get("id"))
        for step in (brief.get("target") or {}).get("steps") or []
        if step.get("id")
    }
    step_ids = set((plan.get("steps") or {}).keys())
    if target_ids != step_ids:
        errors.append(
            f"Step 范围不匹配: expected={sorted(target_ids)}, "
            f"actual={sorted(step_ids)}"
        )
    actions_by_step = {}
    action_values_by_step = {}
    action_roots_by_step = {}
    action_locators_by_step = {}
    action_target_fingerprints_by_step = {}
    action_parameters_by_step = {}
    action_details_by_step = {}
    evidence_roots_by_step = {}
    evidence_by_step = {}
    for action in brief.get("actions") or []:
        step_id = str(action.get("step_id") or "")
        target = action.get("target") or {}
        if action.get("id") and action.get("role") != "noise":
            action_details_by_step.setdefault(step_id, {})[
                str(action["id"])
            ] = action
            actions_by_step.setdefault(step_id, set()).add(str(action["id"]))
            action_values_by_step.setdefault(step_id, []).append(action)
            action_roots_by_step.setdefault(step_id, {})[
                str(action["id"])
            ] = normalize(str(
                (target.get("root_name") or "")
            ))
            locator_name = normalize(str(target.get("locator_name") or ""))
            if locator_name:
                action_locators_by_step.setdefault(step_id, {})[
                    str(action["id"])
                ] = locator_name
            target_fingerprint = str(
                target.get("target_fingerprint") or ""
            ).strip()
            if target_fingerprint:
                action_target_fingerprints_by_step.setdefault(
                    step_id,
                    {},
                )[str(action["id"])] = target_fingerprint
            action_parameters_by_step.setdefault(step_id, {})[
                str(action["id"])
            ] = dict(action.get("parameters") or {})
            root_name = normalize(str(target.get("root_name") or ""))
            if root_name:
                evidence_roots_by_step.setdefault(step_id, set()).add(
                    root_name
                )
        evidence_by_step.setdefault(step_id, set()).update(
            str(item) for item in action.get("evidence") or []
        )
    global_forensic = set(brief.get("required_forensic_evidence") or [])
    if not plan.get("summary"):
        errors.append("计划缺少 summary")
    errors.extend(_validate_runtime_bindings(plan, brief))
    window_owners = plan.get("window_owners") or {}
    has_operations = any(
        (step or {}).get("operations")
        for step in (plan.get("steps") or {}).values()
    )
    if require_window_ownership and has_operations:
        errors.extend(_validate_window_owners(window_owners, brief))
        errors.extend(_validate_child_view_candidates(window_owners, brief))
    method_resolutions = {}
    action_handling = {}
    for step_id, step in (plan.get("steps") or {}).items():
        step_id = str(step_id)
        action_ids = actions_by_step.get(step_id, set())
        evidence_ids = evidence_by_step.get(step_id, set()) | global_forensic
        referenced_actions = set()
        ignored_actions = set(step.get("ignored_action_ids") or [])
        covered_actions = set(step.get("covered_action_ids") or [])
        issue_actions = {
            str(action_id)
            for issue in step.get("unresolved_issues") or ()
            for action_id in (issue or {}).get("action_ids") or ()
            if action_id
        }
        behavior_resolution = step.get("behavior_resolution") or {}
        behavior_reuse = behavior_resolution.get("strategy") == "reuse"
        operations = step.get("operations") or []
        operation_window_owners = set()
        locator_evidence_aliases, locator_mapping_errors = (
            _locator_evidence_aliases(
                step_id,
                step.get("locators") or [],
                evidence_locators=set(
                    action_locators_by_step.get(step_id, {}).values()
                ),
                evidence_roots=evidence_roots_by_step.get(
                    step_id,
                    set(),
                ),
            )
        )
        errors.extend(locator_mapping_errors)
        target_step = next(
            (
                item
                for item in (brief.get("target") or {}).get("steps") or ()
                if str(item.get("id")) == step_id
            ),
            {},
        )
        errors.extend(validate_table_usage(
            step_id,
            step.get("table_usage"),
            target_step.get("table"),
            required=bool(target_step.get("table")),
        ))
        table_usage = step.get("table_usage") or {}
        issue_errors, valid_issue_actions, has_valid_issues = (
            _validate_unresolved_issues(
            step_id,
            step,
            plan,
            brief,
            action_ids,
            )
        )
        errors.extend(issue_errors)
        if (
            not operations
            and table_usage.get("consumption") != "scenario_state"
            and not behavior_reuse
            and not has_valid_issues
        ):
            errors.append(f"Step {step_id} 缺少 operations")
        step_semantic_type = str(
            target_step.get("semantic_type")
            or target_step.get("keyword")
            or ""
        ).strip().casefold()
        expected_assertion = (
            _explicit_assertion_operation(target_step.get("text"))
            if step_semantic_type in {"", "then"}
            else None
        )
        if expected_assertion and not behavior_reuse and expected_assertion not in {
            operation.get("op") for operation in operations
        }:
            errors.append(
                f"Step {step_id} 文案要求 {expected_assertion}，"
                "Plan assertion 不一致"
            )
        for operation in operations:
            op = operation.get("op")
            errors.extend(_validate_operation_target_compatibility(
                step_id,
                operation,
                action_details_by_step.get(step_id, {}),
            ))
            if require_action_roles:
                errors.extend(_validate_operation_action_roles(
                    step_id,
                    operation,
                    action_ids=action_ids,
                    action_target_fingerprints=(
                        action_target_fingerprints_by_step.get(
                            step_id,
                            {},
                        )
                    ),
                        action_parameters=(
                            action_parameters_by_step.get(step_id, {})
                        ),
                ))
            errors.extend(_validate_operation_evidence_alias(
                step_id,
                operation,
                step.get("locators") or [],
                action_locators=action_locators_by_step.get(step_id, {}),
                action_roots=action_roots_by_step.get(step_id, {}),
            ))
            if op not in ALLOWED_OPERATIONS:
                errors.append(f"不支持的操作: {op}")
            capability = capability_by_name(op)
            if require_window_ownership:
                window_owner = operation.get("window_owner")
                view_owner = operation.get("view_owner")
                implementation_location = str(
                    operation.get("implementation_location")
                    or "page_method"
                )
                implementation_method = operation.get(
                    "implementation_method"
                )
                implementation_resolution = operation.get(
                    "implementation_resolution"
                ) or {}
                if implementation_location not in IMPLEMENTATION_LOCATIONS:
                    errors.append(
                        f"Step {step_id} 操作 {op} 的 "
                        "implementation_location 无效: "
                        f"{implementation_location}"
                    )
                elif implementation_location == "page_method":
                    if not implementation_method:
                        errors.append(
                            f"Step {step_id} 操作 {op} 缺少 "
                            "implementation_method"
                        )
                    elif not _is_qualified_implementation_method(
                        implementation_method
                    ):
                        errors.append(
                            f"Step {step_id} 操作 {op} 的 "
                            "implementation_method 必须为 Class.method: "
                            f"{implementation_method}"
                        )
                else:
                    if implementation_method:
                        errors.append(
                            f"Step {step_id} 操作 {op} 的 "
                            "step_inline_base_api 不接受 "
                            "implementation_method"
                        )
                    if implementation_resolution:
                        errors.append(
                            f"Step {step_id} 操作 {op} 的 "
                            "step_inline_base_api 不接受 "
                            "implementation_resolution"
                        )
                if not window_owner:
                    errors.append(
                        f"Step {step_id} 操作 {op} 缺少 window_owner"
                    )
                elif window_owner not in window_owners:
                    errors.append(
                        f"Step {step_id} 操作 {op} 引用未知 window_owner: "
                        f"{window_owner}"
                    )
                else:
                    operation_window_owners.add(window_owner)
                    owner = window_owners[window_owner]
                    if view_owner and view_owner not in (
                        owner.get("views") or {}
                    ):
                        errors.append(
                            f"Step {step_id} 操作 {op} 引用未知 view_owner: "
                            f"{window_owner}.{view_owner}"
                        )
                    action_roots = {
                        action_roots_by_step.get(step_id, {}).get(action_id)
                        for action_id in operation.get("action_ids") or []
                    } - {None, ""}
                    evidence_roots = _owner_evidence_roots(
                        owner,
                        brief,
                        locator_evidence_aliases=locator_evidence_aliases,
                    )
                    if action_roots and not action_roots <= evidence_roots:
                        errors.append(
                            f"Step {step_id} 操作 {op} 的 window_owner "
                            f"与 evidence root 不一致: owner="
                            f"{owner.get('root_locator')} expected="
                            f"{sorted(evidence_roots)} evidence="
                            f"{sorted(action_roots)}"
                        )
                    if implementation_location == "page_method":
                        errors.extend(_validate_implementation_resolution(
                            step_id,
                            implementation_method,
                            implementation_resolution,
                            owner,
                            view_owner,
                            brief,
                        ))
                    if (
                        implementation_location == "page_method"
                        and implementation_method
                    ):
                        previous = method_resolutions.setdefault(
                            implementation_method,
                            _implementation_resolution_identity(
                                implementation_resolution
                            ),
                        )
                        if previous != _implementation_resolution_identity(
                                implementation_resolution
                        ):
                            errors.append(
                                f"实现方法 {implementation_method} 的 "
                                "implementation_resolution 不一致"
                            )
            errors.extend(_validate_capability_plan_profile(
                step_id,
                operation,
                capability.plan_validation_profile if capability else None,
                brief=brief,
                locator_evidence_aliases=locator_evidence_aliases,
            ))
            errors.extend(_validate_operation_value_provenance(
                step_id,
                operation,
                brief,
            ))
            operation_actions = set(operation.get("action_ids") or [])
            referenced_actions.update(operation_actions)
            unknown_actions = operation_actions - action_ids
            if unknown_actions:
                errors.append(
                    f"Step {step_id} 引用未知 action: {sorted(unknown_actions)}"
                )
            unknown_evidence = set(operation.get("evidence_ids") or []) - evidence_ids
            if unknown_evidence:
                errors.append(
                    f"Step {step_id} 引用未知 evidence: "
                    f"{sorted(unknown_evidence)}"
                )
            if op not in {"wait_exists", "wait_not_exists"} and not (
                operation.get("action_ids") or operation.get("evidence_ids")
            ):
                errors.append(f"操作 {op} 缺少 provenance")
            source = str(operation.get("source") or "")
            if source.startswith("table."):
                column = source.split(".", 1)[1]
                if column not in (
                    (step.get("table_usage") or {}).get("columns") or {}
                ):
                    errors.append(
                        f"Step {step_id} operation 引用未声明表格列: {column}"
                    )
        unknown_ignored = ignored_actions - action_ids
        if unknown_ignored:
            errors.append(
                f"Step {step_id} 忽略列表引用未知 action: "
                f"{sorted(unknown_ignored)}"
            )
        unsupported_ignored = {
            action_id
            for action_id in ignored_actions - unknown_ignored
            if not _ignored_action_is_authorized(
                action_id,
                actions=action_values_by_step.get(step_id, ()),
                operations=operations,
                plan=plan,
                brief=brief,
            )
        }
        if unsupported_ignored:
            errors.append(
                f"Step {step_id} ignored_action_ids 缺少冻结的 "
                "transport 或 Decision 依据: "
                f"{sorted(unsupported_ignored)}"
            )
        relationship_errors, absorbed_actions = _validate_action_relationships(
            step_id,
            step.get("action_relationships") or [],
            actions=action_values_by_step.get(step_id, ()),
            operations=operations,
            referenced_actions=referenced_actions,
            ignored_actions=ignored_actions,
            covered_actions=covered_actions,
            brief=brief,
        )
        errors.extend(relationship_errors)
        errors.extend(_validate_behavior_resolution(
            step_id,
            step,
            brief,
            target_step,
            actions=action_details_by_step.get(step_id, {}),
        ))
        unknown_covered = covered_actions - action_ids
        if unknown_covered:
            errors.append(
                f"Step {step_id} 覆盖列表引用未知 action: "
                f"{sorted(unknown_covered)}"
            )
        uncovered = (
            action_ids
            - referenced_actions
            - ignored_actions
            - covered_actions
            - absorbed_actions
            - issue_actions
        )
        if uncovered:
            errors.append(
                f"Step {step_id} 有效 action 未被生成或覆盖: "
                f"{sorted(uncovered)}"
            )
        overlap = referenced_actions & ignored_actions
        if overlap:
            errors.append(
                f"Step {step_id} action 同时被生成和忽略: "
                f"{sorted(overlap)}"
            )
        covered_overlap = covered_actions & (
            referenced_actions | ignored_actions
        )
        if covered_overlap:
            errors.append(
                f"Step {step_id} action 同时被现有行为覆盖并生成或忽略: "
                f"{sorted(covered_overlap)}"
            )
        action_handling[step_id] = {
            "operations": referenced_actions,
            "ignored": ignored_actions,
            "behavior": covered_actions,
            "absorbed": absorbed_actions,
            "issues": issue_actions,
            "operation_values": operations,
            "locator_evidence_aliases": locator_evidence_aliases,
        }
        if (
            require_window_ownership
            and len(operation_window_owners) > 1
            and step.get("behavior_owner") not in {
                "step_orchestration",
                "workflow",
            }
        ):
            errors.append(
                f"Step {step_id} 跨窗口操作必须由 step_orchestration "
                "或 workflow 编排"
            )
    errors.extend(_validate_ambiguity_resolutions(
        plan,
        brief,
        action_handling,
    ))
    errors.extend(_validate_scenario_model(
        plan,
        brief,
        required=require_scenario_model,
        user_confirmed_references=user_confirmed_references,
    ))
    errors.extend(_validate_memory_trace(plan, brief))
    errors.extend(_validate_annotation_trace(plan, brief))
    errors.extend(_validate_pic_region_roots(plan))
    return errors


def _validate_unresolved_issues(step_id, step, plan, brief, action_ids):
    issues = list(step.get("unresolved_issues") or ())
    if not issues:
        return [], set(), False
    ambiguities = {
        str(item.get("ambiguity_id") or ""): item
        for item in brief.get("ambiguities") or ()
        if isinstance(item, dict)
        and str(item.get("step_id") or "") == str(step_id)
    }
    resolutions = {
        str(item.get("ambiguity_id") or ""): item
        for item in plan.get("ambiguity_resolutions") or ()
        if isinstance(item, dict)
        and item.get("outcome") == "generate_issue_placeholder"
    }
    errors = []
    covered = set()
    valid_issue_count = 0
    for issue in issues:
        if not isinstance(issue, dict):
            errors.append(f"Step {step_id} unresolved_issue必须是object")
            continue
        ambiguity_id = str(issue.get("ambiguity_id") or "")
        ambiguity = ambiguities.get(ambiguity_id)
        resolution = resolutions.get(ambiguity_id)
        if ambiguity is None or resolution is None:
            errors.append(
                f"Step {step_id} unresolved_issue缺少冻结placeholder ambiguity"
            )
            continue
        allowed = next((
            item
            for item in ambiguity.get("allowed_outcomes") or ()
            if item.get("outcome") == "generate_issue_placeholder"
            and item.get("authority") == "ai"
            and item.get("effect") == "issue_placeholder"
        ), None)
        if allowed is None:
            errors.append(
                f"Step {step_id} ambiguity不允许issue placeholder: "
                f"{ambiguity_id}"
            )
            continue
        expected_id = "generation-issue-" + hashlib.sha256(
            f"{brief.get('request_id')}:{step_id}:{ambiguity_id}".encode(
                "utf-8"
            )
        ).hexdigest()[:16]
        issue_actions = set(issue.get("action_ids") or ())
        if any((
            issue.get("issue_id") != expected_id,
            str(issue.get("step_id") or "") != str(step_id),
            issue.get("issue_type") != ambiguity.get("code"),
            issue_actions != set(action_ids),
        )):
            errors.append(
                f"Step {step_id} unresolved_issue与冻结ambiguity不一致: "
                f"{ambiguity_id}"
            )
            continue
        covered.update(issue_actions)
        valid_issue_count += 1
    return errors, covered, bool(valid_issue_count)


def _validate_action_relationships(
        step_id,
        relationships,
        *,
        actions,
        operations,
        referenced_actions,
        ignored_actions,
        covered_actions,
        brief,
):
    actions_by_id = {
        str(action.get("id") or ""): {
            **action,
            "_order": index,
        }
        for index, action in enumerate(actions, start=1)
        if action.get("id")
    }
    operation_positions = {
        id(operation): index
        for index, operation in enumerate(operations)
    }
    operation_consumers = {}
    for operation in operations:
        for action_id in set(operation.get("action_ids") or ()):
            operation_consumers.setdefault(str(action_id), []).append(operation)
    errors = []
    absorbed_actions = set()
    relationship_sources = set()
    relationship_ids = set()
    for relationship in relationships:
        if not isinstance(relationship, dict):
            errors.append(f"Step {step_id} action_relationship必须是object")
            continue
        kind = str(relationship.get("kind") or "")
        source_action_id = str(relationship.get("source_action_id") or "")
        consumer_action_id = str(
            relationship.get("consumer_action_id") or ""
        )
        identity = (kind, source_action_id, consumer_action_id)
        if kind not in {
                "activation_for", "transport_for", "absorbed_by"
        }:
            errors.append(
                f"Step {step_id} action_relationship kind无效: {kind}"
            )
            continue
        if not source_action_id or not consumer_action_id:
            errors.append(f"Step {step_id} action_relationship缺少Action引用")
            continue
        if source_action_id == consumer_action_id:
            errors.append(
                f"Step {step_id} action_relationship不能引用同一Action"
            )
            continue
        source = actions_by_id.get(source_action_id)
        consumer = actions_by_id.get(consumer_action_id)
        if source is None or consumer is None:
            errors.append(
                f"Step {step_id} action_relationship引用未知Action"
            )
            continue
        source_ordinal = _action_relationship_ordinal(source)
        consumer_ordinal = _action_relationship_ordinal(consumer)
        if source_ordinal is None or consumer_ordinal is None:
            errors.append(
                f"Step {step_id} action_relationship缺少冻结Action ordinal"
            )
            continue
        if source_ordinal >= consumer_ordinal:
            errors.append(
                f"Step {step_id} action_relationship source必须早于consumer"
            )
            continue
        if source_action_id in relationship_sources:
            errors.append(
                f"Step {step_id} 一个Action只能声明一个action_relationship"
            )
            continue
        if identity in relationship_ids:
            errors.append(f"Step {step_id} action_relationship重复")
            continue
        consumer_operations = operation_consumers.get(consumer_action_id) or []
        if len(consumer_operations) != 1:
            errors.append(
                f"Step {step_id} action_relationship consumer必须恰好被一个operation引用"
            )
            continue
        if kind in {"activation_for", "transport_for"}:
            source_operations = operation_consumers.get(source_action_id) or []
            if len(source_operations) != 1:
                errors.append(
                    f"Step {step_id} {kind} source必须保留独立operation覆盖"
                )
                continue
            if operation_positions[id(source_operations[0])] >= (
                    operation_positions[id(consumer_operations[0])]
            ):
                errors.append(
                    f"Step {step_id} {kind} source operation顺序必须早于consumer"
                )
                continue
        if kind == "transport_for" and not _is_auxiliary_relationship_action(
                source
        ):
            errors.append(
                f"Step {step_id} transport_for source必须是无语义辅助Action"
            )
            continue
        if kind in {"transport_for", "absorbed_by"} and not (
                _same_relationship_target(source, consumer)
        ):
            errors.append(
                f"Step {step_id} {kind}必须引用同一冻结target"
            )
            continue
        if kind == "absorbed_by":
            if source_action_id in referenced_actions:
                errors.append(
                    f"Step {step_id} absorbed_by source不能同时被operation直接引用"
                )
                continue
            if source_action_id in ignored_actions | covered_actions:
                errors.append(
                    f"Step {step_id} absorbed_by source不能同时被忽略或复用覆盖"
                )
                continue
            if not _is_auxiliary_relationship_action(source):
                errors.append(
                    f"Step {step_id} absorbed_by source必须是无语义辅助Action"
                )
                continue
            if str(consumer_operations[0].get("op") or "").startswith(
                    "assert_"
            ):
                errors.append(
                    f"Step {step_id} absorbed_by不能覆盖assertion关系"
                )
                continue
            if _relationship_action_has_decision_constraint(
                    source_action_id,
                    brief,
            ):
                errors.append(
                    f"Step {step_id} absorbed_by不能覆盖Decision约束Action"
                )
                continue
            absorbed_actions.add(source_action_id)
        relationship_sources.add(source_action_id)
        relationship_ids.add(identity)
    return errors, absorbed_actions


def _action_relationship_ordinal(action):
    value = action.get("ordinal")
    if not isinstance(value, bool) and isinstance(value, int) and value > 0:
        return value
    value = action.get("_order")
    if not isinstance(value, bool) and isinstance(value, int) and value > 0:
        return value
    return None


def _is_auxiliary_relationship_action(action):
    action_type = str(action.get("type") or "").casefold()
    control_type = str(
        ((action.get("target") or {}).get("control_type") or "")
    ).casefold()
    if action_type in {"click", "focus"}:
        if control_type not in {"document", "edit"}:
            return False
    elif action_type == "scroll":
        if str(action.get("role") or "").casefold() != "transport":
            return False
    else:
        return False
    if str(action.get("role") or "").casefold() == "assertion":
        return False
    if ((action.get("semantics") or {}).get("effect") or {}):
        return False
    expectation = (
        (action.get("canonical_action") or {}).get(
            "business_expectation"
        ) or {}
    )
    return expectation.get("status") in {None, "", "not_declared"}


def _same_relationship_target(source, consumer):
    source_target = source.get("target") or {}
    consumer_target = consumer.get("target") or {}
    return bool(
        source_target.get("target_fingerprint")
        and source_target.get("target_fingerprint") == consumer_target.get(
            "target_fingerprint"
        )
        and source_target.get("root_name") == consumer_target.get("root_name")
    )


def _relationship_action_has_decision_constraint(action_id, brief):
    return any(
        str(action_id) in {
            str(item) for item in ambiguity.get("action_ids") or []
        }
        for ambiguity in brief.get("ambiguities") or []
    )


def _validate_runtime_bindings(plan, brief):
    step_order = [
        str(step.get("id"))
        for step in (brief.get("target") or {}).get("steps") or ()
        if step.get("id")
    ]
    step_positions = {
        step_id: index
        for index, step_id in enumerate(step_order)
    }
    producers = {}
    consumers = []
    errors = []
    for step_id, step in (plan.get("steps") or {}).items():
        step_id = str(step_id)
        step_position = step_positions.get(step_id, len(step_positions))
        for operation_index, operation in enumerate(
                (step or {}).get("operations") or ()
        ):
            if not isinstance(operation, dict):
                continue
            op = str(operation.get("op") or "")
            binding = str(operation.get("result_binding") or "").strip()
            if op in _RUNTIME_PRODUCER_OPERATIONS and not binding:
                errors.append(
                    f"Step {step_id} 操作 {op} 缺少 result_binding"
                )
            if binding:
                if _RUNTIME_BINDING_NAME.fullmatch(binding) is None:
                    errors.append(
                        f"Step {step_id} result_binding 无效: {binding!r}"
                    )
                if op not in _RUNTIME_PRODUCER_OPERATIONS:
                    errors.append(
                        f"Step {step_id} 操作 {op} 不能生产 runtime binding"
                    )
                if binding in producers:
                    errors.append(
                        f"重复生产 runtime binding: {binding}"
                    )
                else:
                    producers[binding] = (
                        step_position,
                        operation_index,
                        step_id,
                    )
            source = str(operation.get("source") or "")
            if not source.startswith(_RUNTIME_SOURCE_PREFIX):
                continue
            source_binding = source[len(_RUNTIME_SOURCE_PREFIX):]
            if _RUNTIME_BINDING_NAME.fullmatch(source_binding) is None:
                errors.append(
                    f"Step {step_id} runtime source 无效: {source!r}"
                )
                continue
            capability = capability_by_name(op)
            if capability is None or capability.value_argument is None:
                errors.append(
                    f"Step {step_id} 操作 {op} 没有可绑定的值参数"
                )
            parameters = operation.get("parameters") or {}
            declared_arguments = _declared_example_arguments(
                brief,
                step_id,
            )
            if declared_arguments and "argument" not in parameters:
                errors.append(
                    f"Step {step_id} runtime source所在Step包含Feature参数，"
                    "必须显式声明 parameters.argument（参数名或null）"
                )
            elif declared_arguments:
                argument_value = parameters.get("argument")
                argument = str(argument_value or "").strip()
                if argument and argument not in declared_arguments:
                    errors.append(
                        f"Step {step_id} runtime source声明未知Feature参数: "
                        f"{argument}"
                    )
                elif argument:
                    errors.append(
                        f"Step {step_id} runtime source与Feature声明来源冲突: "
                        f"examples.{argument}"
                    )
            consumers.append((
                source_binding,
                step_position,
                operation_index,
                step_id,
            ))
    for binding, step_position, operation_index, step_id in consumers:
        producer = producers.get(binding)
        if producer is None:
            errors.append(f"未知 runtime binding: {binding}")
            continue
        if producer[:2] >= (step_position, operation_index):
            errors.append(
                f"Step {step_id} 消费 runtime binding {binding} "
                "早于生产者"
            )
    consumed_bindings = {
        binding
        for binding, _step_position, _operation_index, _step_id in consumers
    }
    for binding in sorted(set(producers) - consumed_bindings):
        errors.append(f"runtime binding {binding} 没有 consumer")
    return errors


def _declared_example_arguments(brief, step_id):
    return declared_example_arguments(brief, step_id)


def _validate_pic_region_roots(plan):
    errors = []
    steps = plan.get("steps") or {}
    owners = plan.get("window_owners") or {}
    for authorization in plan.get("pic_authorizations") or ():
        if not authorization.get("authorized"):
            continue
        step_id = str(authorization.get("step_id") or "")
        action_id = str(authorization.get("action_id") or "")
        operations = [
            operation
            for operation in (steps.get(step_id) or {}).get("operations") or ()
            if str(operation.get("target_action_id") or "") == action_id
        ]
        if len(operations) != 1:
            errors.append(
                f"PIC authorization {authorization.get('authorization_id')} "
                "必须唯一映射到 Plan operation"
            )
            continue
        owner_id = str(operations[0].get("window_owner") or "")
        owner_root = normalize(str(
            (owners.get(owner_id) or {}).get("root_locator") or ""
        ))
        region_root = normalize(str(
            (authorization.get("region_locator") or {}).get("root") or ""
        ))
        if not region_root or owner_root != region_root:
            errors.append(
                f"PIC authorization {authorization.get('authorization_id')} "
                "Region root 与 window_owner root 不一致: "
                f"region={region_root or None} owner={owner_root or None}"
            )
    return errors


def _brief_annotation_snapshot(brief):
    snapshot = (brief or {}).get("annotation_snapshot")
    if snapshot is None:
        return build_annotation_snapshot([])
    if not annotation_snapshot_is_valid(snapshot):
        raise ValueError("Generation Brief Annotation snapshot无效")
    return snapshot


def _normalize_annotation_trace(value):
    value = value if isinstance(value, dict) else {}
    return {
        "annotation_snapshot_version": value.get(
            "annotation_snapshot_version"
        ),
        "snapshot_fingerprint": value.get("snapshot_fingerprint"),
        "required_annotation_ids_by_step": {
            str(step_id): sorted(_unique_strings(annotation_ids))
            for step_id, annotation_ids in dict(
                value.get("required_annotation_ids_by_step") or {}
            ).items()
        },
        "references": [
            dict(item)
            for item in value.get("references") or ()
            if isinstance(item, dict)
        ],
    }


def _validate_annotation_trace(plan, brief):
    try:
        snapshot = _brief_annotation_snapshot(brief)
    except ValueError as error:
        return [str(error)]
    required_by_step = snapshot.get(
        "required_annotation_ids_by_step"
    ) or {}
    provided_by_step = {
        str(step_id): sorted(_unique_strings(
            (step or {}).get("annotation_ids")
        ))
        for step_id, step in (plan.get("steps") or {}).items()
        if isinstance(step, dict)
    }
    expected_by_step = {
        str(step_id): sorted(_unique_strings(annotation_ids))
        for step_id, annotation_ids in required_by_step.items()
    }
    for step_id in set(provided_by_step) | set(expected_by_step):
        if provided_by_step.get(step_id, []) != expected_by_step.get(
                step_id,
                [],
        ):
            return [
                f"Step {step_id} annotation_ids与Brief snapshot不一致"
            ]
    trace = plan.get("annotation_trace")
    if not snapshot.get("references") and not trace:
        return []
    expected = bind_generation_annotation_trace(
        {
            "steps": {
                step_id: {"annotation_ids": annotation_ids}
                for step_id, annotation_ids in provided_by_step.items()
            },
        },
        brief,
    )["annotation_trace"]
    if _normalize_annotation_trace(trace) != _normalize_annotation_trace(
            expected
    ):
        return ["Plan annotation_trace与Brief snapshot不一致"]
    return []


def _normalize_scenario_model(value):
    value = value if isinstance(value, dict) else {}
    result = {
        "model_version": str(value.get("model_version") or "").strip(),
        "summary": str(value.get("summary") or "").strip(),
        "states": [
            {
                "state_id": str(item.get("state_id") or "").strip(),
                "name": str(item.get("name") or "").strip(),
                "kind": str(item.get("kind") or "").strip(),
                "support": _normalize_model_support(item.get("support")),
            }
            for item in value.get("states") or ()
            if isinstance(item, dict)
        ],
        "steps": [
            {
                "step_id": str(item.get("step_id") or "").strip(),
                "role": str(item.get("role") or "").strip(),
                "consumes": _unique_strings(item.get("consumes")),
                "produces": _unique_strings(item.get("produces")),
                "observes": _unique_strings(item.get("observes")),
                "reason": str(item.get("reason") or "").strip(),
                "support": _normalize_model_support(item.get("support")),
            }
            for item in value.get("steps") or ()
            if isinstance(item, dict)
        ],
        "transitions": [
            {
                "from_step_id": str(
                    item.get("from_step_id") or ""
                ).strip(),
                "to_step_id": str(item.get("to_step_id") or "").strip(),
                "state_ids": _unique_strings(item.get("state_ids")),
                "reason": str(item.get("reason") or "").strip(),
                "support": _normalize_model_support(item.get("support")),
            }
            for item in value.get("transitions") or ()
            if isinstance(item, dict)
        ],
    }
    mode = str(value.get("mode") or "").strip()
    if mode:
        result["mode"] = mode
    return result


def _normalize_model_support(values):
    return [
        {
            "authority": str(item.get("authority") or "").strip(),
            "references": _unique_strings(item.get("references")),
        }
        for item in values or ()
        if isinstance(item, dict)
    ]


def _validate_scenario_model(
    plan,
    brief,
    *,
    required=None,
    user_confirmed_references=(),
):
    model = plan.get("scenario_model") or {}
    if required is None:
        required = bool(brief.get("scenario_intelligence"))
    if not model.get("model_version"):
        return ["Plan 缺少 scenario_model"] if required else []
    errors = []
    if model.get("model_version") != "1.0":
        errors.append("scenario_model.model_version 必须为 1.0")
    if not model.get("summary"):
        errors.append("scenario_model 缺少 summary")
    mode = str(model.get("mode") or "state_model")
    if mode not in {"state_model", "single_step_intent"}:
        errors.append(f"scenario_model mode 无效: {mode}")
    target_steps = [
        item
        for item in (brief.get("target") or {}).get("steps") or ()
        if isinstance(item, dict) and item.get("id")
    ]
    target_ids = [str(item["id"]) for item in target_steps]
    if mode == "single_step_intent":
        errors.extend(_validate_single_step_intent_model(
            model,
            brief,
            target_steps,
        ))
    model_steps = model.get("steps") or []
    model_ids = [str(item.get("step_id") or "") for item in model_steps]
    if model_ids != target_ids:
        errors.append(
            "scenario_model Step 顺序或范围不匹配: "
            f"expected={target_ids} actual={model_ids}"
        )
    state_ids = [
        str(item.get("state_id") or "")
        for item in model.get("states") or ()
    ]
    if any(not item for item in state_ids) or len(state_ids) != len(set(state_ids)):
        errors.append("scenario_model state_id 必须非空且唯一")
    state_id_set = set(state_ids)
    for state in model.get("states") or ():
        state_id = str(state.get("state_id") or "")
        if not state.get("name") or not state.get("kind"):
            errors.append(f"scenario_model state {state_id} 缺少 name/kind")
        errors.extend(_validate_model_support(
            state.get("support"),
            brief,
            label=f"state {state_id}",
            user_confirmed_references=user_confirmed_references,
        ))
    expected_roles = {
        str(item.get("id")): _scenario_step_role(item)
        for item in target_steps
    }
    for item in model_steps:
        step_id = str(item.get("step_id") or "")
        expected_role = expected_roles.get(step_id)
        if expected_role and item.get("role") != expected_role:
            errors.append(
                f"scenario_model Step {step_id} role 应为 {expected_role}"
            )
        unknown_states = (
            set(item.get("consumes") or ())
            | set(item.get("produces") or ())
            | set(item.get("observes") or ())
        ) - state_id_set
        if unknown_states:
            errors.append(
                f"scenario_model Step {step_id} 引用未知 state: "
                f"{sorted(unknown_states)}"
            )
        if not item.get("reason"):
            errors.append(f"scenario_model Step {step_id} 缺少 reason")
        errors.extend(_validate_model_support(
            item.get("support"),
            brief,
            label=f"Step {step_id}",
            user_confirmed_references=user_confirmed_references,
        ))
    positions = {step_id: index for index, step_id in enumerate(target_ids)}
    for transition in model.get("transitions") or ():
        source = str(transition.get("from_step_id") or "")
        target = str(transition.get("to_step_id") or "")
        if source not in positions or target not in positions:
            errors.append(
                "scenario_model transition 引用未知 Step: "
                f"{source}->{target}"
            )
        elif positions[source] >= positions[target]:
            errors.append(
                f"scenario_model transition 因果顺序无效: {source}->{target}"
            )
        unknown_states = set(
            transition.get("state_ids") or ()
        ) - state_id_set
        if unknown_states:
            errors.append(
                "scenario_model transition 引用未知 state: "
                f"{sorted(unknown_states)}"
            )
        if not transition.get("reason"):
            errors.append(
                f"scenario_model transition {source}->{target} 缺少 reason"
            )
        errors.extend(_validate_model_support(
            transition.get("support"),
            brief,
            label=f"transition {source}->{target}",
            user_confirmed_references=user_confirmed_references,
        ))
    return errors


def _validate_single_step_intent_model(model, brief, target_steps):
    errors = []
    scope = (
        ((brief.get("target") or {}).get("scenario") or {}).get(
            "generation_scope"
        )
        or {}
    )
    if scope.get("complete") is not True:
        errors.append(
            "scenario_model single_step_intent 仅允许完整 Scenario 范围"
        )
    if len(target_steps) != 1:
        errors.append(
            "scenario_model single_step_intent 仅允许一个目标 Step"
        )
    elif target_steps[0].get("table"):
        errors.append(
            "scenario_model single_step_intent 不允许 Data Table Step"
        )
    if model.get("states"):
        errors.append(
            "scenario_model single_step_intent 不应声明 states"
        )
    if model.get("transitions"):
        errors.append(
            "scenario_model single_step_intent 不应声明 transitions"
        )
    for item in model.get("steps") or ():
        if any(
            item.get(key)
            for key in ("consumes", "produces", "observes")
        ):
            errors.append(
                "scenario_model single_step_intent 不应声明状态消费或产出"
            )
            break
    return errors


def _scenario_step_role(step):
    semantic_type = str(
        step.get("semantic_type") or step.get("keyword") or ""
    ).strip().casefold()
    return {
        "given": "precondition",
        "when": "business_action",
        "then": "business_assertion",
    }.get(semantic_type)


def _validate_model_support(
    values,
    brief,
    *,
    label,
    user_confirmed_references=(),
):
    values = list(values or ())
    if not values:
        return [f"scenario_model {label} 缺少 support"]
    allowed = {
        "feature_declared",
        "runtime_observed",
        "code_verified",
        "user_confirmed",
        "ai_hypothesis",
    }
    references_by_authority = _scenario_model_references(
        brief,
        user_confirmed_references=user_confirmed_references,
    )
    known = set().union(*references_by_authority.values())
    errors = []
    for support in values:
        authority = str(support.get("authority") or "")
        references = set(support.get("references") or ())
        if authority not in allowed:
            errors.append(
                f"scenario_model {label} authority 无效: {authority}"
            )
        if not references:
            errors.append(f"scenario_model {label} support 缺少 references")
            continue
        unknown = references - known
        if unknown:
            errors.append(
                f"scenario_model {label} 引用未知 support: "
                f"{sorted(unknown)}"
            )
            continue
        mismatched = references - references_by_authority.get(
            authority,
            set(),
        )
        if mismatched:
            errors.append(
                f"scenario_model {label} authority 与 support 不匹配: "
                f"{authority} cannot cite {sorted(mismatched)}"
            )
    return errors


def _scenario_model_references(brief, *, user_confirmed_references=()):
    target = brief.get("target") or {}
    feature_declared = {
        f"step:{item.get('id')}"
        for item in target.get("steps") or ()
        if isinstance(item, dict) and item.get("id")
    }
    feature = target.get("feature") or {}
    scenario = target.get("scenario") or {}
    if feature.get("id"):
        feature_declared.add(f"feature:{feature['id']}")
    if scenario.get("id"):
        feature_declared.add(f"scenario:{scenario['id']}")
    runtime_observed = set()
    for action in brief.get("actions") or ():
        if action.get("id"):
            runtime_observed.add(str(action["id"]))
        runtime_observed.update(
            str(item) for item in action.get("evidence") or ()
        )
    code_verified = set()
    semantics = brief.get("semantics") or {}
    for candidate in semantics.get("reuse_candidates") or ():
        if candidate.get("candidate_id"):
            code_verified.add(str(candidate["candidate_id"]))
    for dependency in semantics.get("environment_dependencies") or ():
        if dependency.get("dependency_id"):
            code_verified.add(str(dependency["dependency_id"]))
    hypothesis = set()
    for ambiguity in brief.get("ambiguities") or ():
        if ambiguity.get("ambiguity_id"):
            hypothesis.add(str(ambiguity["ambiguity_id"]))
        runtime_observed.update(
            str(item) for item in ambiguity.get("evidence_ids") or ()
        )
    user_confirmed = {
        str(item) for item in user_confirmed_references if item
    }
    known = (
        feature_declared
        | runtime_observed
        | code_verified
        | user_confirmed
        | hypothesis
    )
    return {
        "feature_declared": feature_declared,
        "runtime_observed": runtime_observed,
        "code_verified": code_verified,
        "user_confirmed": user_confirmed,
        "ai_hypothesis": known,
    }


def _trusted_decision_references(session_dir, state, request, brief):
    decision = (state or {}).get("decision") or {}
    answers = decision.get("answers") or {}
    if not answers.get("path"):
        return set()
    from autowork_core.utils.debug_tools.recorder.decision_pack import (
        load_answer_record,
        load_decision_pack,
    )

    pack = load_decision_pack(
        session_dir,
        decision.get("pack") or {},
        request,
        brief_fingerprint=brief.get("brief_fingerprint"),
    )
    if pack is None:
        raise ValueError("Decision Pack 已失效")
    record = load_answer_record(
        session_dir,
        answers,
        request,
        pack,
    )
    if record is None:
        raise ValueError("Decision Answers 已失效")
    return {
        str(item.get("question_id"))
        for item in (
            (record.get("compiled_patch") or {}).get("decision_trace") or ()
        )
        if item.get("question_id")
    }


def _normalize_ambiguity_resolution(value):
    return {
        "ambiguity_id": str(value.get("ambiguity_id") or "").strip(),
        "outcome": str(value.get("outcome") or "").strip(),
        "action_ids": _unique_strings(value.get("action_ids")),
        "evidence_ids": _unique_strings(value.get("evidence_ids")),
        "candidate_id": str(value.get("candidate_id") or "").strip() or None,
        "decision_ids": _unique_strings(value.get("decision_ids")),
        "reason": str(value.get("reason") or "").strip(),
    }


def _validate_ambiguity_resolutions(plan, brief, action_handling):
    ambiguities = {
        str(item.get("ambiguity_id") or ""): item
        for item in brief.get("ambiguities") or ()
        if isinstance(item, dict) and item.get("ambiguity_id")
    }
    resolutions = plan.get("ambiguity_resolutions") or []
    errors = []
    counts = {}
    for resolution in resolutions:
        ambiguity_id = str(resolution.get("ambiguity_id") or "")
        counts[ambiguity_id] = counts.get(ambiguity_id, 0) + 1
    duplicates = sorted(
        ambiguity_id
        for ambiguity_id, count in counts.items()
        if ambiguity_id and count > 1
    )
    if duplicates:
        errors.append(f"Plan ambiguity resolution 重复: {duplicates}")
    unknown = sorted(set(counts) - set(ambiguities) - {""})
    if unknown:
        errors.append(f"Plan 引用未知 ambiguity: {unknown}")
    resolution_map = {
        str(item.get("ambiguity_id") or ""): item
        for item in resolutions
        if item.get("ambiguity_id")
    }
    decision_trace_ids = {
        str(item.get("question_id") or "")
        for item in plan.get("decision_trace") or ()
        if isinstance(item, dict) and item.get("question_id")
    }
    for ambiguity_id, ambiguity in ambiguities.items():
        resolution = resolution_map.get(ambiguity_id)
        routing = str(ambiguity.get("routing") or "")
        if resolution is None:
            if routing == "evidence_required":
                errors.append(
                    f"{ambiguity_id} 必须补录或修复证据，Plan 不能放行"
                )
            elif routing == "user_decision_required":
                errors.append(
                    f"{ambiguity_id} 缺少用户 Decision resolution"
                )
            else:
                errors.append(f"Plan 缺少 ambiguity resolution: {ambiguity_id}")
            continue
        if routing == "evidence_required":
            errors.append(
                f"{ambiguity_id} 必须补录或修复证据，Plan 不能放行"
            )
            continue
        if not resolution.get("reason"):
            errors.append(f"{ambiguity_id} resolution 缺少 reason")
        allowed = {
            str(item.get("outcome") or ""): item
            for item in ambiguity.get("allowed_outcomes") or ()
            if isinstance(item, dict) and item.get("outcome")
        }
        outcome = str(resolution.get("outcome") or "")
        selected = allowed.get(outcome)
        if selected is None:
            errors.append(
                f"{ambiguity_id} outcome 不在冻结候选中: {outcome}"
            )
            continue
        authority = str(selected.get("authority") or "")
        effect = str(selected.get("effect") or "")
        decision_ids = set(resolution.get("decision_ids") or ())
        if effect == "evidence_required":
            errors.append(
                f"{ambiguity_id} 必须补录或修复证据，Plan 不能放行"
            )
            continue
        if authority == "evidence":
            errors.append(
                f"{ambiguity_id} 必须补录或修复证据，Plan 不能放行"
            )
            continue
        if authority == "user" and (
            not decision_ids
            or not decision_ids <= decision_trace_ids
        ):
            errors.append(
                f"{ambiguity_id} 必须引用用户 Decision"
            )
        expected_actions = {
            str(item) for item in ambiguity.get("action_ids") or () if item
        }
        actual_actions = set(resolution.get("action_ids") or ())
        if actual_actions != expected_actions:
            errors.append(
                f"{ambiguity_id} 动作覆盖不匹配: "
                f"expected={sorted(expected_actions)} "
                f"actual={sorted(actual_actions)}"
            )
        expected_evidence = {
            str(item) for item in ambiguity.get("evidence_ids") or () if item
        }
        if not expected_evidence <= set(resolution.get("evidence_ids") or ()):
            errors.append(f"{ambiguity_id} 未引用全部冻结 evidence")
        step_id = str(ambiguity.get("step_id") or "")
        handling = action_handling.get(step_id) or {
            "operations": set(),
            "ignored": set(),
            "behavior": set(),
            "operation_values": [],
        }
        if effect == "plan_coverage" and not expected_actions <= (
            handling["operations"] | handling["behavior"]
        ):
            errors.append(f"{ambiguity_id} 动作覆盖未进入 Plan 实现")
        elif effect == "plan_coverage":
            errors.extend(_validate_action_ambiguity_coverage(
                ambiguity_id,
                ambiguity,
                handling.get("operation_values") or [],
                locator_evidence_aliases=(
                    handling.get("locator_evidence_aliases") or {}
                ),
            ))
            errors.extend(_validate_assertion_ambiguity_coverage(
                ambiguity_id,
                ambiguity,
                handling.get("operation_values") or [],
                locator_evidence_aliases=(
                    handling.get("locator_evidence_aliases") or {}
                ),
            ))
        elif effect == "behavior_coverage" and not expected_actions <= (
            handling["behavior"]
        ):
            errors.append(f"{ambiguity_id} 动作覆盖未由现有行为承担")
        elif effect == "behavior_coverage":
            step = (plan.get("steps") or {}).get(step_id) or {}
            behavior = step.get("behavior_resolution") or {}
            if any((
                behavior.get("strategy") != "reuse",
                not resolution.get("candidate_id"),
                resolution.get("candidate_id")
                != behavior.get("candidate_id"),
            )):
                errors.append(
                    f"{ambiguity_id} 未绑定同一冻结 behavior candidate"
                )
        elif effect == "issue_placeholder" and not expected_actions <= (
            handling.get("issues") or set()
        ):
            errors.append(f"{ambiguity_id} 动作未由typed issue占位覆盖")
        elif effect == "ignored_action" and not expected_actions <= (
            handling["ignored"]
        ):
            errors.append(f"{ambiguity_id} Decision 未落实 ignored action")
    return errors


def _validate_action_ambiguity_coverage(
        ambiguity_id,
        ambiguity,
        operations,
        *,
        locator_evidence_aliases=None,
):
    if str(ambiguity.get("code") or "") != "action_implementation":
        return []
    locator_evidence_aliases = locator_evidence_aliases or {}
    facts = ambiguity.get("facts") or {}
    action_type = str(facts.get("action_type") or "")
    action = {
        "type": action_type,
        "target": facts.get("target") or {},
    }
    target_name = normalize(str(
        (facts.get("target") or {}).get("locator_name") or ""
    ))
    expected_parameters = facts.get("parameters") or {}
    action_ids = set(ambiguity.get("action_ids") or ())
    for operation in operations:
        if not action_ids & set(operation.get("action_ids") or ()):
            continue
        compatibility = operation_compatibility(
            operation.get("op"),
            action,
        )
        if compatibility["status"] == "incompatible":
            continue
        if target_name and _evidence_locator_name(
            operation.get("target"),
            locator_evidence_aliases,
        ) != target_name:
            continue
        if action_type in {"scroll", "drag"} and any(
            (operation.get("parameters") or {}).get(name) != value
            for name, value in expected_parameters.items()
        ):
            continue
        return []
    return [
        f"{ambiguity_id} action 实现与冻结动作类型或目标不一致"
    ]


def _validate_assertion_ambiguity_coverage(
        ambiguity_id,
        ambiguity,
        operations,
        *,
        locator_evidence_aliases=None,
    ):
    locator_evidence_aliases = locator_evidence_aliases or {}
    code = str(ambiguity.get("code") or "")
    if code not in {
        "assertion_implementation",
        "assertion_value_unobserved",
    }:
        return []
    action_ids = set(ambiguity.get("action_ids") or ())
    covered = [
        operation
        for operation in operations
        if action_ids & set(operation.get("action_ids") or ())
    ]
    facts = ambiguity.get("facts") or {}
    target_name = str(
        (facts.get("target") or {}).get("locator_name") or ""
    )
    if code == "assertion_implementation":
        candidates = facts.get("assertion_candidates") or []
        valid = any(
            operation.get("op") == candidate.get("operation")
            and (
                not candidate.get("target")
                or _evidence_locator_name(
                    operation.get("target"),
                    locator_evidence_aliases,
                ) == normalize(str(candidate.get("target") or ""))
            )
            and (
                not (
                    capability_by_name(candidate.get("operation"))
                    and capability_by_name(candidate.get("operation")).ambiguity_parameters_exact
                )
                or (operation.get("parameters") or {})
                == (candidate.get("parameters") or {})
            )
            for operation in covered
            for candidate in candidates
        )
    else:
        expected_values = {
            str(item.get("value"))
            for item in facts.get("declared_expectations") or ()
            if item.get("value") is not None
        }
        valid = any(
            str(operation.get("op") or "").startswith("assert_")
            and operation.get("op") not in {
                "assert_exists",
                "assert_not_exists",
                "assert_visible",
                "assert_not_visible",
                "assert_enabled",
                "assert_disabled",
            }
            and (
                not target_name
                or _evidence_locator_name(
                    operation.get("target"),
                    locator_evidence_aliases,
                ) == normalize(target_name)
            )
            and str(
                (operation.get("parameters") or {}).get(
                    "expected",
                    operation.get("value"),
                )
            ) in expected_values
            for operation in covered
        )
    if valid:
        return []
    return [
        f"{ambiguity_id} assertion 覆盖与冻结事实不一致"
    ]


def _explicit_assertion_operation(text):
    expectation = explicit_assertion_expectation(text)
    return expectation.get("operation") if expectation else None


def _validate_behavior_resolution(
        step_id,
        step,
        brief,
        target_step,
    *,
    actions,
):
    resolution = step.get("behavior_resolution") or {}
    strategy = resolution.get("strategy")
    covered = step.get("covered_action_ids") or []
    if not strategy:
        return (
            [f"Step {step_id} covered_action_ids 缺少 behavior_resolution"]
            if covered
            else []
        )
    errors = []
    if strategy not in {"reuse", "modify", "create"}:
        return [f"Step {step_id} behavior_resolution.strategy 无效"]
    if not resolution.get("reason"):
        errors.append(f"Step {step_id} behavior_resolution 缺少 reason")
    if strategy != "reuse":
        if covered:
            errors.append(
                f"Step {step_id} 只有 behavior reuse 可以声明 covered_action_ids"
            )
        if strategy == "modify":
            candidate_id = str(resolution.get("candidate_id") or "")
            candidate = _brief_implementation_candidates(brief).get(
                candidate_id
            ) or {}
            step_pattern = str(resolution.get("step_pattern") or "")
            step_decorator = str(
                resolution.get("step_decorator") or ""
            ).casefold()
            matched_contract = _matched_step_pattern_contract(
                candidate,
                target_step,
            )
            if any((
                    candidate.get("kind") != "step_definition",
                    resolution.get("symbol") != candidate.get("symbol"),
                    matched_contract is None,
                    step_pattern != str(
                        (matched_contract or {}).get("pattern") or ""
                    ),
                    step_decorator != str(
                        (matched_contract or {}).get("decorator") or ""
                    ).casefold(),
            )):
                errors.append(
                    f"Step {step_id} modify必须冻结当前匹配的Step candidate"
                )
        return errors
    candidate_id = str(resolution.get("candidate_id") or "")
    candidates = _brief_implementation_candidates(brief)
    candidate = candidates.get(candidate_id) or {}
    if candidate.get("kind") != "step_definition":
        errors.append(
            f"Step {step_id} behavior reuse candidate 必须为 step_definition"
        )
        return errors
    if "exact_step_pattern" not in (candidate.get("reasons") or []):
        errors.append(
            f"Step {step_id} behavior reuse candidate 缺少 exact_step_pattern"
        )
    if str(step.get("behavior_file") or "") != str(candidate.get("path") or ""):
        errors.append(f"Step {step_id} behavior_file 与 reuse candidate 不一致")
    matched_contract = _matched_step_pattern_contract(candidate, target_step)
    if matched_contract is None:
        errors.append(f"Step {step_id} behavior reuse 未绑定目标 Gherkin Step")
    elif any((
            str(resolution.get("step_pattern") or "")
            != str(matched_contract.get("pattern") or ""),
            str(resolution.get("step_decorator") or "").casefold()
            != str(matched_contract.get("decorator") or "").casefold(),
    )):
        errors.append(
            f"Step {step_id} behavior reuse必须冻结匹配的Step decorator"
        )
    mappings = list(resolution.get("action_mappings") or ())
    sequence = list(candidate.get("call_sequence") or ())
    if not sequence:
        errors.append(
            f"Step {step_id} behavior reuse candidate缺少冻结call_sequence"
        )
        return errors
    if len(mappings) != len(sequence):
        errors.append(
            f"Step {step_id} behavior reuse action_mappings长度不匹配"
        )
        return errors
    mapping_action_ids = [
        str(item.get("action_id") or "") for item in mappings
        if isinstance(item, dict)
    ]
    if set(mapping_action_ids) != set(covered) or len(
            mapping_action_ids
    ) != len(set(mapping_action_ids)):
        errors.append(
            f"Step {step_id} behavior reuse action_mappings覆盖不匹配"
        )
        return errors
    for index, (mapping, call) in enumerate(zip(mappings, sequence)):
        if not isinstance(mapping, dict):
            errors.append(
                f"Step {step_id} behavior reuse action_mapping必须是object"
            )
            continue
        if any((
            mapping.get("call_index") != index,
            str(mapping.get("operation") or "")
            != str(call.get("operation") or ""),
            str(mapping.get("target") or "")
            != str(call.get("target") or ""),
        )):
            errors.append(
                f"Step {step_id} behavior reuse action_mapping与冻结调用不一致"
            )
            continue
        action = actions.get(str(mapping.get("action_id") or "")) or {}
        action_target = action.get("target") or {}
        if str(mapping.get("target") or "") != str(
                action_target.get("locator_name") or ""
        ):
            errors.append(
                f"Step {step_id} behavior reuse action_mapping与冻结Action目标不一致"
            )
        value_provenance = mapping.get("value_provenance") or {}
        if call.get("value") is not None:
            if any((
                value_provenance.get("kind") is None,
                value_provenance.get("literal") != call.get("value"),
            )):
                errors.append(
                    f"Step {step_id} behavior reuse action_mapping值证明不一致"
                )
        elif call.get("value_parameter"):
            if value_provenance.get("kind") is None:
                errors.append(
                    f"Step {step_id} behavior reuse action_mapping缺少值证明"
                )
            elif not _candidate_binds_value_parameter(
                    candidate,
                    target_step,
                    str(call.get("value_parameter") or ""),
            ):
                errors.append(
                    f"Step {step_id} behavior reuse action_mapping的"
                    "value_parameter未由匹配的 decorator 参数绑定"
                )
    return errors


def _candidate_binds_value_parameter(candidate, target_step, parameter):
    for contract in candidate.get("step_parameter_contracts") or ():
        if not isinstance(contract, dict):
            continue
        if str(contract.get("matcher") or "") not in {"parse", "cfparse"}:
            continue
        if step_pattern_contract_matches(contract, target_step):
            return any(
                str(binding.get("parameter") or "") == parameter
                and str(binding.get("capture_kind") or "") == "named"
                for binding in contract.get("parameter_bindings") or ()
                if isinstance(binding, dict)
            )
    return False


def _normalize_window_owners(value):
    owners = {}
    for owner_id, raw_owner in dict(value or {}).items():
        owner = dict(raw_owner or {})
        views = {}
        for view_id, raw_view in dict(owner.get("views") or {}).items():
            view = dict(raw_view or {})
            views[str(view_id)] = {
                "evidence_root": str(
                    view.get("evidence_root") or ""
                ).strip() or None,
                "ownership_candidate_id": str(
                    view.get("ownership_candidate_id") or ""
                ).strip() or None,
                "locator_file": str(
                    view.get("locator_file") or ""
                ).strip() or None,
                "view_object": str(
                    view.get("view_object") or ""
                ).strip() or None,
                "active_locator": str(
                    view.get("active_locator") or ""
                ).strip() or None,
                "root_locator": (
                    normalize(str(view.get("root_locator") or ""))
                    or None
                ),
            }
        owners[str(owner_id)] = {
            "evidence_root": (
                normalize(str(
                    owner.get("evidence_root")
                    or owner.get("root_locator")
                    or ""
                ))
                or None
            ),
            "public_name": str(
                owner.get("public_name") or ""
            ).strip() or None,
            "root_locator": (
                normalize(str(owner.get("root_locator") or ""))
                or None
            ),
            "page_object": str(
                owner.get("page_object") or ""
            ).strip() or None,
            "root_locator_file": str(
                owner.get("root_locator_file") or ""
            ).strip() or None,
            "resolution": _normalize_owner_resolution(
                owner.get("resolution")
            ),
            "ownership_decision": _normalize_ownership_decision(
                owner.get("ownership_decision")
            ),
            "views": views,
        }
    return owners


def _normalize_owner_resolution(value):
    value = value if isinstance(value, dict) else {}
    return {
        "strategy": str(value.get("strategy") or "").strip() or None,
        "candidate_id": (
            str(value.get("candidate_id") or "").strip() or None
        ),
        "reason": str(value.get("reason") or "").strip(),
    }


def _normalize_ownership_decision(value):
    if not isinstance(value, dict) or not value:
        return None
    return {
        "selected_kind": str(value.get("selected_kind") or "").strip(),
        "dismissed_candidate_ids": [
            str(item)
            for item in value.get("dismissed_candidate_ids") or ()
            if item
        ],
        "reason": str(value.get("reason") or "").strip(),
    }


def _normalize_implementation_resolution(value):
    value = value if isinstance(value, dict) else {}
    return {
        "strategy": str(value.get("strategy") or "").strip() or None,
        "candidate_id": (
            str(value.get("candidate_id") or "").strip() or None
        ),
        "reason": str(value.get("reason") or "").strip(),
        "symbol": str(value.get("symbol") or "").strip() or None,
        "step_pattern": str(
            value.get("step_pattern") or ""
        ).strip() or None,
        "step_decorator": str(
            value.get("step_decorator") or ""
        ).strip().casefold() or None,
        "action_mappings": [
            {
                "action_id": str(item.get("action_id") or "").strip(),
                "call_index": item.get("call_index"),
                "operation": str(item.get("operation") or "").strip(),
                "target": str(item.get("target") or "").strip(),
                "value_provenance": _normalize_value_provenance(
                    item.get("value_provenance"),
                    source=None,
                    value_action_ids=(),
                ),
            }
            for item in value.get("action_mappings") or ()
            if isinstance(item, dict)
        ],
    }


def _matched_step_pattern_contract(candidate, target_step):
    contracts = [
        {
            "decorator": str(contract.get("decorator") or "").casefold(),
            "pattern": str(contract.get("pattern") or ""),
        }
        for contract in candidate_step_pattern_contracts(candidate)
        if step_pattern_contract_matches(contract, target_step)
    ]
    return contracts[0] if len(contracts) == 1 else None


def _implementation_resolution_identity(value):
    value = value if isinstance(value, dict) else {}
    return {
        "strategy": value.get("strategy"),
        "candidate_id": value.get("candidate_id"),
    }


def _validate_implementation_resolution(
        step_id,
        implementation_method,
        resolution,
        owner,
        view_owner,
        brief,
):
    strategy = resolution.get("strategy")
    candidate_id = resolution.get("candidate_id")
    errors = []
    if strategy not in {"reuse", "modify", "create"}:
        errors.append(
            f"Step {step_id} 方法 {implementation_method} 的 "
            "implementation_resolution.strategy 无效"
        )
    if not resolution.get("reason"):
        errors.append(
            f"Step {step_id} 方法 {implementation_method} 缺少 "
            "implementation_resolution.reason"
        )
    if strategy == "create":
        if candidate_id:
            errors.append(
                f"Step {step_id} 方法 {implementation_method} create "
                "不应声明 candidate_id"
            )
        return errors
    if strategy not in {"reuse", "modify"}:
        return errors
    candidates = _brief_implementation_candidates(brief)
    candidate = candidates.get(str(candidate_id or ""))
    if candidate is None:
        errors.append(
            f"Step {step_id} 方法 {implementation_method} 引用未知 "
            f"implementation candidate: {candidate_id}"
        )
        return errors
    if candidate.get("kind") != "page_object_method":
        errors.append(
            f"Step {step_id} 方法 {implementation_method} 不能复用 "
            f"candidate kind={candidate.get('kind')}"
        )
    expected_path = owner.get("page_object")
    if view_owner:
        expected_path = (
            (owner.get("views") or {}).get(view_owner) or {}
        ).get("view_object")
    if candidate.get("path") != expected_path:
        errors.append(
            f"Step {step_id} 方法 {implementation_method} 与 candidate "
            "path 不一致"
        )
    if candidate.get("symbol") != implementation_method:
        errors.append(
            f"Step {step_id} 方法 {implementation_method} 与 candidate "
            "symbol 不一致"
        )
    return errors


def _validate_window_owners(owners, brief):
    errors = []
    if not owners:
        return ["新 Generation Plan 缺少 window_owners"]
    for owner_id, owner in owners.items():
        for field in (
            "root_locator",
            "page_object",
            "root_locator_file",
        ):
            if not owner.get(field):
                errors.append(
                    f"window_owner {owner_id} 缺少 {field}"
                )
        resolution = owner.get("resolution") or {}
        strategy = resolution.get("strategy")
        candidate_id = resolution.get("candidate_id")
        if strategy not in {"reuse_existing", "create_new"}:
            errors.append(
                f"window_owner {owner_id} resolution.strategy 无效"
            )
        if not resolution.get("reason"):
            errors.append(
                f"window_owner {owner_id} 缺少 resolution.reason"
            )
        candidates = {
            str(item.get("candidate_id")): item
            for _window, item in _brief_candidate_matches(brief)
            if item.get("candidate_id")
        }
        if strategy == "reuse_existing":
            if not candidate_id:
                errors.append(
                    f"window_owner {owner_id} reuse_existing 缺少 candidate_id"
                )
            elif candidate_id not in candidates:
                errors.append(
                    f"window_owner {owner_id} 引用未知 candidate: "
                    f"{candidate_id}"
                )
            else:
                candidate = candidates[candidate_id]
                if candidate.get("kind") != "canonical_window":
                    errors.append(
                        f"window_owner {owner_id} 不能复用 legacy candidate"
                    )
                for field, candidate_field in (
                    ("page_object", "page_object"),
                    ("root_locator_file", "root_locator_file"),
                    ("root_locator", "root_locator"),
                ):
                    if owner.get(field) != candidate.get(candidate_field):
                        errors.append(
                            f"window_owner {owner_id} 与 candidate "
                            f"{candidate_id} 的 {field} 不一致"
                        )
        elif strategy == "create_new" and candidate_id:
            candidate = candidates.get(candidate_id)
            if candidate is None:
                errors.append(
                    f"window_owner {owner_id} 引用未知 candidate: "
                    f"{candidate_id}"
                )
            elif candidate.get("kind") != "legacy_root":
                errors.append(
                    f"window_owner {owner_id} create_new 只能接管 "
                    "legacy_root candidate"
                )
            else:
                for field, candidate_field in (
                    ("root_locator_file", "root_locator_file"),
                    ("root_locator", "root_locator"),
                ):
                    if owner.get(field) != candidate.get(candidate_field):
                        errors.append(
                            f"window_owner {owner_id} 与 legacy candidate "
                            f"{candidate_id} 的 {field} 不一致"
                        )
        root_file = _project_path(owner.get("root_locator_file"))
        page_object = _project_path(owner.get("page_object"))
        public_name = str(owner.get("public_name") or "")
        if public_name:
            if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", public_name):
                errors.append(
                    f"window_owner {owner_id} public_name无效"
                )
        public_root_is_separate = bool(
            owner.get("evidence_root")
            and owner.get("root_locator") != owner.get("evidence_root")
        )
        if public_name and strategy == "create_new" and public_root_is_separate:
            expected_root = f"{public_name}_window"
            expected_page = PurePosixPath(
                f"Bdd/page_obj/{public_name}/page.py"
            )
            expected_locators = PurePosixPath(
                f"Bdd/locators/{public_name}/window.yaml"
            )
            if owner.get("root_locator") != expected_root:
                errors.append(
                    f"window_owner {owner_id} root_locator未由public_name派生"
                )
            if page_object != expected_page:
                errors.append(
                    f"window_owner {owner_id} page_object未由public_name派生"
                )
            if root_file != expected_locators:
                errors.append(
                    f"window_owner {owner_id} root_locator_file未由public_name派生"
                )
        if root_file is None or not _is_bdd_subpath(root_file, "locators"):
            errors.append(
                f"window_owner {owner_id} root_locator_file 非法"
            )
        if page_object is None or not _is_bdd_subpath(
            page_object,
            "page_obj",
        ):
            errors.append(
                f"window_owner {owner_id} page_object 非法"
            )
        root_package = root_file.parent if root_file is not None else None
        for view_id, view in (owner.get("views") or {}).items():
            view_file = _project_path(view.get("locator_file"))
            view_object = _project_path(view.get("view_object"))
            if view_file is None:
                errors.append(
                    f"view_owner {owner_id}.{view_id} 缺少 locator_file"
                )
                continue
            if view_object is None:
                errors.append(
                    f"view_owner {owner_id}.{view_id} 缺少 view_object"
                )
            if not view.get("active_locator"):
                errors.append(
                    f"view_owner {owner_id}.{view_id} 缺少 active_locator"
                )
            view_root = normalize(str(view.get("root_locator") or ""))
            active_locator = normalize(str(
                view.get("active_locator") or ""
            ).lstrip("$").removeprefix("loc:"))
            if view_root and view_root != active_locator:
                errors.append(
                    f"view_owner {owner_id}.{view_id} root_locator "
                    "必须等于active_locator"
                )
            if (
                root_package is None
                or (
                    view_file.parent != root_package
                    and root_package not in view_file.parents
                )
            ):
                errors.append(
                    f"view_owner {owner_id}.{view_id} locator_file "
                    "不在窗口 locator 包内"
                )
            page_package = _project_path(owner.get("page_object"))
            if (
                view_object is not None
                and page_package is not None
                and view_object.parent != page_package.parent
            ):
                errors.append(
                    f"view_owner {owner_id}.{view_id} view_object "
                    "不在 WindowPage 包内"
                )
    return errors


def _validate_child_view_candidates(owners, brief):
    errors = []
    candidates = [
        item
        for item in (brief.get("window_ownership") or {}).get(
            "ownership_candidates"
        ) or ()
        if (item or {}).get("kind") == "child_view"
    ]
    candidates_by_id = {
        str(item.get("candidate_id") or ""): item
        for item in candidates
        if item.get("candidate_id")
    }
    for owner_id, owner in (owners or {}).items():
        for view_id, view in (owner.get("views") or {}).items():
            if not (view or {}).get("root_locator"):
                continue
            candidate_id = str(
                (view or {}).get("ownership_candidate_id") or ""
            )
            candidate = candidates_by_id.get(candidate_id)
            if (
                candidate is None
                or normalize(str(candidate.get("child_root") or ""))
                != normalize(str((view or {}).get("evidence_root") or ""))
            ):
                errors.append(
                    "semantic_shape_error: 独立 root WindowView 必须引用"
                    "匹配的 child_view ownership candidate: "
                    f"view={owner_id}.{view_id} candidate={candidate_id}"
                )
    owner_roots = {
        normalize(str(owner.get("evidence_root") or "")): owner_id
        for owner_id, owner in (owners or {}).items()
        if owner.get("evidence_root")
    }
    view_roots = {
        normalize(str((view or {}).get("evidence_root") or "")): (
            owner_id,
            view_id,
            view,
        )
        for owner_id, owner in (owners or {}).items()
        for view_id, view in (owner.get("views") or {}).items()
        if (view or {}).get("evidence_root")
    }
    candidates_by_child = {}
    for candidate in candidates:
        child_root = normalize(str(candidate.get("child_root") or ""))
        if child_root:
            candidates_by_child.setdefault(child_root, []).append(candidate)
    for child_root, child_candidates in candidates_by_child.items():
        if child_root in view_roots:
            owner_id, _view_id, view = view_roots[child_root]
            if not view.get("root_locator"):
                errors.append(
                    "semantic_shape_error: child_view WindowView 缺少 "
                    f"root_locator: child={child_root}"
                )
            selected_id = str(view.get("ownership_candidate_id") or "")
            selected = [
                candidate
                for candidate in child_candidates
                if str(candidate.get("candidate_id") or "") == selected_id
            ]
            if len(selected) != 1:
                errors.append(
                    "semantic_shape_error: WindowView 缺少或引用未知 "
                    "ownership candidate: "
                    f"child={child_root} candidate={selected_id}"
                )
                continue
            expected_parent = normalize(str(
                selected[0].get("parent_root") or ""
            ))
            actual_parent = normalize(str(
                (owners.get(owner_id) or {}).get("evidence_root") or ""
            ))
            if expected_parent != actual_parent:
                errors.append(
                    "semantic_shape_error: WindowView ownership candidate "
                    "与父 owner 不一致: "
                    f"candidate={selected_id} expected={expected_parent} "
                    f"actual={actual_parent}"
                )
            continue
        if child_root in owner_roots:
            owner = owners[owner_roots[child_root]]
            decision = owner.get("ownership_decision") or {}
            dismissed = set(decision.get("dismissed_candidate_ids") or ())
            required = {
                str(candidate.get("candidate_id") or "")
                for candidate in child_candidates
                if candidate.get("candidate_id")
            }
            if (
                required <= dismissed
                and decision.get("selected_kind") == "window_page"
                and str(decision.get("reason") or "").strip()
            ):
                continue
            errors.append(
                "semantic_shape_error: child_view ownership candidate "
                "未采用为 WindowView: "
                f"candidates={sorted(required)} child={child_root}"
            )
    return errors


def _brief_candidate_matches(brief, candidate_id=None):
    candidate_id = str(candidate_id or "")
    return [
        (window, candidate)
        for window in (
            ((brief or {}).get("window_ownership") or {}).get("windows")
            or []
        )
        for candidate in (
            ((window.get("owner_match") or {}).get("candidates")) or []
        )
        if (
            candidate.get("candidate_id")
            and (
                not candidate_id
                or str(candidate.get("candidate_id")) == candidate_id
            )
        )
    ]


def _brief_implementation_candidates(brief):
    candidates = {}
    for item in (
        ((brief or {}).get("semantics") or {}).get("reuse_candidates")
        or []
    ):
        if item.get("candidate_id"):
            candidates[str(item["candidate_id"])] = item
    for _window, owner in _brief_candidate_matches(brief):
        for item in owner.get("method_candidates") or ():
            if item.get("candidate_id"):
                candidates[str(item["candidate_id"])] = item
    return candidates


def _owner_evidence_roots(
        owner,
        brief,
        *,
        locator_evidence_aliases=None,
    ):
    resolution = owner.get("resolution") or {}
    roots = set()
    if resolution.get("candidate_id"):
        roots.update({
            normalize(str(window.get("root_name") or ""))
            for window, _candidate in _brief_candidate_matches(
                brief,
                resolution.get("candidate_id"),
            )
        } - {""})
    aliases = locator_evidence_aliases or {}
    if not roots:
        root_name = normalize(str(
            owner.get("evidence_root") or owner.get("root_locator") or ""
        ))
        roots.add(aliases.get(root_name, root_name))
    for view in (owner.get("views") or {}).values():
        evidence_root = normalize(str(view.get("evidence_root") or ""))
        active_locator = normalize(str(view.get("active_locator") or ""))
        if evidence_root:
            roots.add(evidence_root)
        if active_locator:
            roots.add(aliases.get(active_locator, active_locator))
    return roots - {""}


def _locator_evidence_aliases(
        step_id,
        locators,
        *,
        evidence_locators,
        evidence_roots,
    ):
    aliases = {}
    errors = []
    for locator in locators or ():
        if not isinstance(locator, dict):
            continue
        name = normalize(str(locator.get("name") or ""))
        if not name:
            continue
        evidence_name = normalize(str(
            locator.get("evidence_name") or name
        ))
        previous = aliases.get(name)
        if previous is not None and previous != evidence_name:
            errors.append(
                f"Step {step_id} locator {name} evidence_name 冲突"
            )
            continue
        aliases[name] = evidence_name
        if not locator.get("evidence_name"):
            continue
        kind = str(locator.get("kind") or "").casefold()
        allowed = evidence_roots if kind == "top_level" else evidence_locators
        if evidence_name not in allowed:
            errors.append(
                f"Step {step_id} locator {name} 引用未知冻结 evidence_name: "
                f"{evidence_name}"
            )
    return aliases, errors


def _evidence_locator_name(value, aliases):
    name = normalize(str(value or ""))
    return aliases.get(name, name)


def _validate_ocr_candidate_binding(
        step_id,
        operation,
        brief,
        locator_evidence_aliases,
    ):
    action_ids = {
        str(item)
        for item in operation.get("action_ids") or ()
        if item
    }
    operation_target = _evidence_locator_name(
        operation.get("target"),
        locator_evidence_aliases,
    )
    parameters = operation.get("parameters") or {}
    candidates = [
        candidate
        for action in brief.get("actions") or ()
        if str(action.get("step_id") or "") == str(step_id)
        and str(action.get("id") or "") in action_ids
        for candidate in (
            ((action.get("semantics") or {}).get("assertion_candidates") or ())
        )
        if isinstance(candidate, dict)
    ]
    candidates.extend(
        candidate
        for action in brief.get("actions") or ()
        if str(action.get("step_id") or "") == str(step_id)
        and str(action.get("id") or "") in action_ids
        for candidate in (
            ((action.get("semantics") or {}).get(
                "implementation_constraints"
            ) or ())
        )
        if isinstance(candidate, dict)
    )
    candidates.extend(
        candidate
        for ambiguity in brief.get("ambiguities") or ()
        if isinstance(ambiguity, dict)
        and str(ambiguity.get("step_id") or "") == str(step_id)
        and action_ids & {
            str(item) for item in ambiguity.get("action_ids") or ()
        }
        for candidate in (
            ((ambiguity.get("facts") or {}).get(
                "assertion_candidates"
            ) or ())
        )
        if isinstance(candidate, dict)
    )
    if any(
        candidate.get("operation") == operation.get("op")
        and normalize(str(candidate.get("target") or "")) == operation_target
        and (candidate.get("parameters") or {}) == parameters
        for candidate in candidates
    ):
        return []
    return [
        f"Step {step_id} 操作 {operation.get('op')} 未由冻结Canvas "
        "assertion candidate支持"
    ]


def _validate_operation_evidence_alias(
        step_id,
        operation,
        locators,
        *,
        action_locators,
        action_roots,
    ):
    target = normalize(str(operation.get("target") or ""))
    declaration = next((
        item
        for item in locators or ()
        if isinstance(item, dict)
        and normalize(str(item.get("name") or "")) == target
    ), None)
    if not declaration or not declaration.get("evidence_name"):
        return []
    referenced_actions = {
        str(item)
        for item in operation.get("action_ids") or ()
        if item
    }
    target_action_id = str(
        operation.get("target_action_id") or ""
    ).strip()
    if target_action_id:
        referenced_actions = {target_action_id}
    target_prefix = f"target:{step_id}:"
    if not target_action_id:
        referenced_actions.update(
            str(item)[len(target_prefix):]
            for item in operation.get("evidence_ids") or ()
            if str(item).startswith(target_prefix)
        )
    kind = str(declaration.get("kind") or "").casefold()
    source = action_roots if kind == "top_level" else action_locators
    allowed = {
        normalize(str(source.get(action_id) or ""))
        for action_id in referenced_actions
    } - {""}
    evidence_name = normalize(str(declaration.get("evidence_name") or ""))
    if evidence_name in allowed:
        return []
    ownership = (
        "target_action_id"
        if target_action_id
        else "该操作"
    )
    return [
        f"Step {step_id} 操作 {operation.get('op')} target={target} "
        f"evidence_name 未由{ownership}的冻结 action 支持: "
        f"{evidence_name}"
    ]


def _validate_capability_plan_profile(
        step_id,
        operation,
        profile,
        *,
        brief,
        locator_evidence_aliases,
    ):
    op = str(operation.get("op") or "")
    parameters = operation.get("parameters") or {}
    errors = []
    if profile == "frozen_click_offset":
        if parameters:
            offset_x = parameters.get("offset_x")
            offset_y = parameters.get("offset_y")
            if any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                for value in (offset_x, offset_y)
            ):
                errors.append("click offset_x/offset_y 必须为非负整数")
    elif profile == "frozen_scroll":
        direction = parameters.get("direction")
        steps = parameters.get("steps")
        if direction not in {"up", "down", "left", "right"}:
            errors.append("scroll_to direction 必须为 up、down、left 或 right")
        if (
            isinstance(steps, bool)
            or not isinstance(steps, int)
            or steps <= 0
        ):
            errors.append("scroll_to steps 必须为正整数")
    elif profile == "frozen_drag":
        delta_x = parameters.get("delta_x")
        delta_y = parameters.get("delta_y")
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in (delta_x, delta_y)
        ):
            errors.append("drag_by_offset delta_x/delta_y 必须为整数")
        elif delta_x == 0 and delta_y == 0:
            errors.append("drag_by_offset 位移不能同时为 0")
    elif profile == "collection_assertion":
        expected = parameters.get("expected")
        expected_source = str(parameters.get("expected_source") or "")
        if (
            not isinstance(expected, list)
            or any(not isinstance(item, str) for item in expected)
        ):
            errors.append(
                "assert_collection_equal expected 必须为字符串列表"
            )
        if not expected_source.startswith("structured_observation."):
            errors.append(
                "assert_collection_equal 必须引用冻结结构化Observation"
            )
        if parameters.get("max_items") != 200:
            errors.append("assert_collection_equal max_items 必须为 200")
    elif profile == "ocr_assertion":
        expected = parameters.get("expected")
        expected_source = str(parameters.get("expected_source") or "")
        region_source = str(parameters.get("region_source") or "")
        timeout = parameters.get("timeout")
        if not isinstance(expected, str) or not expected:
            errors.append(f"{op} expected 必须为非空字符串")
        if not (
            expected_source in {"step_text", "text_block"}
            or expected_source.startswith("examples.")
            or expected_source.startswith("table.")
        ):
            errors.append(f"{op} expected_source 必须引用冻结业务规格")
        if not (
            region_source.startswith("structured_observation.")
            and region_source.endswith(".region")
        ):
            errors.append(f"{op} region_source 必须引用冻结Canvas Region")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or timeout <= 0
        ):
            errors.append(f"{op} timeout 必须为正数")
        errors.extend(_validate_ocr_candidate_binding(
            step_id,
            operation,
            brief,
            locator_evidence_aliases,
        ))
    elif profile == "dropdown_selection":
        source = str(operation.get("source") or "")
        if all((
            operation.get("value") is None,
            parameters.get("option") is None,
            not source,
        )):
            errors.append("select_dropdown_option 缺少选项值或来源")
        if (
            source.startswith("step_argument.")
            and not parameters.get("argument")
        ):
            errors.append("select_dropdown_option 缺少 Step 参数名")
    elif profile == "semantic_control_value":
        source = str(operation.get("source") or "")
        value = _semantic_operation_value(operation)
        if value is None and not source:
            errors.append(f"{op} 缺少最终状态值或来源")
        elif not source or source == "literal":
            if op in {"set_checked", "set_tree_expanded"} and not isinstance(
                value,
                bool,
            ):
                errors.append(f"{op} value 必须为 bool")
            if op == "set_slider_value" and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
            ):
                errors.append("set_slider_value value 必须为数字")
        if op == "set_slider_value":
            minimum = parameters.get("expected_minimum")
            maximum = parameters.get("expected_maximum")
            if any(
                isinstance(item, bool)
                or not isinstance(item, (int, float))
                for item in (minimum, maximum)
            ):
                errors.append(
                    "set_slider_value expected_minimum/expected_maximum "
                    "必须为数字"
                )
            elif minimum > maximum:
                errors.append("set_slider_value 冻结范围无效")
            elif isinstance(value, (int, float)) and not (
                minimum <= value <= maximum
            ):
                errors.append("set_slider_value value 超出冻结范围")
    elif profile == "runtime_value_producer":
        target_action_id = str(
            operation.get("target_action_id") or ""
        )
        target_action = next((
            action
            for action in brief.get("actions") or ()
            if str(action.get("step_id") or "") == str(step_id)
            and str(action.get("id") or "") == target_action_id
        ), None)
        if (target_action or {}).get("type") != "observe":
            errors.append(
                f"{op} producer必须引用F9 observe Action"
            )
        target_step = next((
            step
            for step in (brief.get("target") or {}).get("steps") or ()
            if str(step.get("id") or "") == str(step_id)
        ), {})
        typed_intent = next((
            intent
            for intent in target_step.get("observation_intents") or ()
            if str(intent.get("action_id") or "") == target_action_id
            and intent.get("annotation_id")
        ), None)
        if typed_intent is None:
            errors.append(
                f"{op} producer缺少绑定F9 Action的ObservationIntent"
            )
        runtime_sources = (
            (target_action or {}).get("semantics") or {}
        ).get("runtime_value_sources") or {}
        if op == "save_text" and runtime_sources.get("text") is not True:
            errors.append("save_text producer缺少冻结的可读文本证据")
        if op == "save_attr":
            attr_name = parameters.get("attr_name")
            if not isinstance(attr_name, str) or not attr_name.strip():
                errors.append("save_attr attr_name 必须为非空字符串")
            elif attr_name.strip().casefold() not in set(
                    runtime_sources.get("attributes") or ()
            ):
                errors.append(
                    "save_attr attr_name缺少冻结属性证据: "
                    f"{attr_name}"
                )
    return errors


def _validate_operation_target_compatibility(
        step_id,
        operation,
        actions,
    ):
    target_action_id = str(
        operation.get("target_action_id") or ""
    )
    op = str(operation.get("op") or "")
    if not target_action_id:
        return []
    action = actions.get(target_action_id)
    if action is None:
        return []
    compatibility = operation_compatibility(op, action)
    if compatibility["status"] != "incompatible":
        return []
    return [
        f"Step {step_id} Action {target_action_id} 与操作 {op} 技术不兼容: "
        f"{compatibility['reason']}"
    ]


def _semantic_operation_value(operation):
    parameters = operation.get("parameters") or {}
    if "value" in operation:
        return operation.get("value")
    if "value" in parameters:
        return parameters.get("value")
    if "checked" in parameters:
        return parameters.get("checked")
    return None


def _validate_operation_action_roles(
        step_id,
        operation,
        *,
        action_ids,
        action_target_fingerprints,
    action_parameters,
):
    op = str(operation.get("op") or "")
    capability = capability_by_name(op)
    profile = capability.plan_validation_profile if capability else None
    operation_actions = set(operation.get("action_ids") or ())
    target_action_id = str(
        operation.get("target_action_id") or ""
    ).strip()
    value_action_ids = set(operation.get("value_action_ids") or ())
    target_fingerprint = str(
        operation.get("target_fingerprint") or ""
    ).strip()
    errors = []
    if not target_action_id:
        errors.append(
            f"Step {step_id} 操作 {op} 缺少 target_action_id"
        )
    elif target_action_id not in operation_actions:
        errors.append(
            f"Step {step_id} 操作 {op} target_action_id "
            "不属于 action_ids"
        )
    elif target_action_id not in action_ids:
        errors.append(
            f"Step {step_id} 操作 {op} target_action_id 引用未知 action"
        )
    expected_fingerprint = action_target_fingerprints.get(
        target_action_id
    )
    if not target_fingerprint:
        errors.append(
            f"Step {step_id} 操作 {op} 缺少 target_fingerprint"
        )
    elif not expected_fingerprint:
        errors.append(
            f"Step {step_id} 操作 {op} 的 target action 缺少冻结指纹"
        )
    elif target_fingerprint != expected_fingerprint:
        errors.append(
            f"Step {step_id} 操作 {op} target_fingerprint 与冻结目标不一致"
        )
    if profile == "frozen_click_offset" and target_action_id:
        expected_parameters = action_parameters.get(target_action_id) or {}
        actual_parameters = operation.get("parameters") or {}
        expected_offset = {
            key: expected_parameters.get(key)
            for key in ("offset_x", "offset_y")
            if key in expected_parameters
        }
        actual_offset = {
            key: actual_parameters.get(key)
            for key in ("offset_x", "offset_y")
            if key in actual_parameters
        }
        if actual_offset != expected_offset:
            errors.append(
                f"Step {step_id} 操作 {op} 点击偏移与冻结 action 不一致"
            )
    if profile == "frozen_scroll" and target_action_id:
        expected_parameters = action_parameters.get(target_action_id) or {}
        actual_parameters = operation.get("parameters") or {}
        expected_scroll = {
            "direction": expected_parameters.get("direction"),
            "steps": expected_parameters.get("steps"),
        }
        actual_scroll = {
            "direction": actual_parameters.get("direction"),
            "steps": actual_parameters.get("steps"),
        }
        if actual_scroll != expected_scroll:
            errors.append(
                f"Step {step_id} 操作 {op} 参数与冻结 action 不一致"
            )
    if profile == "frozen_drag" and target_action_id:
        expected_parameters = action_parameters.get(target_action_id) or {}
        actual_parameters = operation.get("parameters") or {}
        expected_offset = {
            "delta_x": expected_parameters.get("delta_x"),
            "delta_y": expected_parameters.get("delta_y"),
        }
        actual_offset = {
            "delta_x": actual_parameters.get("delta_x"),
            "delta_y": actual_parameters.get("delta_y"),
        }
        if actual_offset != expected_offset:
            errors.append(
                f"Step {step_id} 操作 {op} 位移与冻结 action 不一致"
            )
    unknown_value_actions = value_action_ids - operation_actions
    if unknown_value_actions:
        errors.append(
            f"Step {step_id} 操作 {op} value_action_ids "
            f"不属于 action_ids: {sorted(unknown_value_actions)}"
        )
    unknown_value_actions = value_action_ids - action_ids
    if unknown_value_actions:
        errors.append(
            f"Step {step_id} 操作 {op} value_action_ids 引用未知 action: "
            f"{sorted(unknown_value_actions)}"
        )
    classified_actions = value_action_ids | (
        {target_action_id} if target_action_id else set()
    )
    if operation_actions != classified_actions:
        errors.append(
            f"Step {step_id} 操作 {op} action_ids 未由 "
            "target_action_id/value_action_ids 完整分类"
        )
    capability = capability_by_name(op)
    requires_value_action = bool(
        capability and capability.requires_value_action
    )
    value_provenance = _normalize_value_provenance(
        operation.get("value_provenance"),
        source=operation.get("source"),
        value_action_ids=operation.get("value_action_ids"),
    )
    value_kind = str(value_provenance.get("kind") or "")
    has_value_authority = bool(value_kind)
    if requires_value_action and not has_value_authority:
        errors.append(
            f"Step {step_id} 值操作 {op} 缺少 value_provenance"
        )
    recorded_value_kinds = {"recorded_action"}
    if value_kind in recorded_value_kinds and not value_action_ids:
        errors.append(
            f"Step {step_id} 录制值操作 {op} 缺少 value_action_ids"
        )
    if value_kind not in recorded_value_kinds and value_action_ids:
        errors.append(
            f"Step {step_id} 非录制值来源 {op} 不接受 value_action_ids"
        )
    if not requires_value_action and (value_action_ids or has_value_authority):
        errors.append(
            f"Step {step_id} 非值操作 {op} 不接受 value provenance"
        )
    return errors


def _normalize_value_provenance(value, *, source, value_action_ids):
    if isinstance(value, dict) and value.get("kind"):
        return {
            key: item
            for key, item in value.items()
            if key in {
                "kind",
                "action_id",
                "step_id",
                "reference",
                "literal",
                "decision_id",
                "binding",
            }
            and item not in (None, "")
        }
    return {}


def _validate_operation_value_provenance(step_id, operation, brief):
    provenance = _normalize_value_provenance(
        operation.get("value_provenance"),
        source=operation.get("source"),
        value_action_ids=operation.get("value_action_ids"),
    )
    if not provenance:
        return []
    kind = str(provenance.get("kind") or "")
    allowed = {
        "recorded_action",
        "feature_literal",
        "semantic_literal",
        "examples",
        "data_table",
        "decision",
        "runtime",
    }
    if kind not in allowed:
        return [f"Step {step_id} value_provenance kind无效: {kind}"]
    errors = []
    declared_step = str(provenance.get("step_id") or step_id)
    if declared_step != str(step_id):
        errors.append(
            f"Step {step_id} value_provenance跨Step: {declared_step}"
        )
    value_actions = set(operation.get("value_action_ids") or ())
    source = str(operation.get("source") or "")
    if kind == "recorded_action":
        action_id = str(provenance.get("action_id") or "")
        if not action_id or action_id not in value_actions:
            errors.append(
                f"Step {step_id} recorded value provenance与value Action不一致"
            )
        frozen_value = resolve_recorded_action_value(
            brief,
            step_id,
            action_id,
            operation.get("op"),
        )
        if frozen_value is None:
            errors.append(
                f"Step {step_id} recorded Action缺少冻结值: {action_id}"
            )
        elif operation.get("value") != frozen_value:
            errors.append(
                f"Step {step_id} recorded value与冻结Action不一致: {action_id}"
            )
    elif value_actions:
        errors.append(
            f"Step {step_id} {kind} value provenance不能声明value Action"
        )
    if kind in {
        "semantic_literal",
        "feature_literal",
    } and source != "literal":
        errors.append(f"Step {step_id} {kind}必须使用literal source")
    if (
        kind == "semantic_literal"
        and str(operation.get("op") or "") not in SEMANTIC_LITERAL_OPERATIONS
    ):
        errors.append(
            f"Step {step_id} semantic_literal不适用于operation: "
            f"{operation.get('op')}"
        )
    if kind == "feature_literal":
        reference = str(provenance.get("reference") or "")
        try:
            frozen_value = (
                resolve_declared_feature_literal(
                    brief,
                    step_id,
                    reference,
                    provenance["literal"],
                )
                if "literal" in provenance
                else resolve_feature_literal(brief, step_id, reference)
            )
        except ValueError as error:
            errors.append(str(error))
        else:
            if operation.get("value") != frozen_value:
                errors.append(
                    f"Step {step_id} feature literal与冻结Brief不一致: {reference}"
                )
    if kind == "examples":
        reference = str(provenance.get("reference") or "")
        values = ((brief.get("target") or {}).get("scenario") or {}).get(
            "example_values",
            {},
        )
        if reference not in values:
            errors.append(
                f"Step {step_id} examples value provenance引用未知列: {reference}"
            )
        elif operation.get("value") != values[reference]:
            errors.append(
                f"Step {step_id} examples value与冻结值不一致: {reference}"
            )
        if source != f"examples.{reference}":
            errors.append(f"Step {step_id} examples source不一致")
    if kind == "data_table":
        reference = str(provenance.get("reference") or "")
        target_step = next((
            item
            for item in (brief.get("target") or {}).get("steps") or ()
            if str(item.get("id") or "") == str(step_id)
        ), {})
        headings = list((target_step.get("table") or {}).get("headings") or ())
        if reference not in headings:
            errors.append(
                f"Step {step_id} table value provenance引用未知列: {reference}"
            )
        if source != f"table.{reference}":
            errors.append(f"Step {step_id} table source不一致")
    if kind == "runtime":
        binding = str(provenance.get("binding") or "")
        if not binding or source != f"runtime.{binding}":
            errors.append(f"Step {step_id} runtime value provenance不一致")
    if kind == "decision":
        decision_id = str(provenance.get("decision_id") or "")
        if not decision_id or decision_id not in set(
                operation.get("decision_ids") or ()
            ):
            errors.append(f"Step {step_id} decision value provenance未获授权")
    capability = capability_by_name(str(operation.get("op") or ""))
    if (
        capability is not None
        and capability.requires_value_action
        and kind not in {"data_table", "runtime"}
        and operation.get("value") is None
    ):
        errors.append(f"Step {step_id} 值操作 {operation.get('op')} 的冻结值为空")
    return errors


def _ignored_action_is_authorized(
        action_id,
        *,
        actions,
        operations,
        plan,
        brief,
):
    action_id = str(action_id or "")
    actions = list(actions or ())
    action = next(
        (
            item for item in actions
            if str(item.get("id") or "") == action_id
        ),
        None,
    )
    if action is None:
        return False
    if str(action.get("role") or "") == "transport":
        return True
    if _ignored_action_has_resolution(action_id, plan, brief):
        return True

    action_type = str(action.get("type") or "").casefold()
    control_type = str(
        ((action.get("target") or {}).get("control_type") or "")
    ).casefold()
    if action_type not in {"click", "focus"} or control_type not in {
        "document",
        "edit",
    }:
        return False
    if ((action.get("semantics") or {}).get("effect") or {}):
        return False

    index = next(
        (
            offset for offset, item in enumerate(actions)
            if str(item.get("id") or "") == action_id
        ),
        -1,
    )
    if index < 0 or index + 1 >= len(actions):
        return False
    following = actions[index + 1]
    if str(following.get("type") or "").casefold() not in {
        "input_text",
        "keyboard",
    }:
        return False
    target_fingerprint = str(
        ((action.get("target") or {}).get("target_fingerprint") or "")
    )
    following_fingerprint = str(
        ((following.get("target") or {}).get("target_fingerprint") or "")
    )
    if not target_fingerprint or target_fingerprint != following_fingerprint:
        return False

    following_id = str(following.get("id") or "")
    return any(
        str(operation.get("op") or "") in {
            "clear_text",
            "input_text",
            "send_text_keys",
        }
        and following_id in set(operation.get("action_ids") or ())
        for operation in operations or ()
    )


def _ignored_action_has_resolution(action_id, plan, brief):
    ambiguities = {
        str(item.get("ambiguity_id") or ""): item
        for item in (brief.get("ambiguities") or ())
        if item.get("ambiguity_id")
    }
    for resolution in plan.get("ambiguity_resolutions") or ():
        if action_id not in {
            str(item) for item in resolution.get("action_ids") or ()
        }:
            continue
        ambiguity = ambiguities.get(str(
            resolution.get("ambiguity_id") or ""
        )) or {}
        selected = str(resolution.get("outcome") or "")
        if any(
            str(outcome.get("outcome") or "") == selected
            and outcome.get("effect") == "ignored_action"
            for outcome in ambiguity.get("allowed_outcomes") or ()
        ):
            return True
    return False


def _project_path(value):
    text = str(value or "").replace("\\", "/").strip()
    if not text:
        return None
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts:
        return None
    return path


def _is_bdd_subpath(path, directory):
    parts = [part.casefold() for part in path.parts]
    return len(parts) >= 3 and parts[:2] == ["bdd", directory.casefold()]


def plan_artifact_identity_is_valid(artifact):
    if not isinstance(artifact, dict):
        return False
    version = artifact.get("plan_version")
    source = artifact.get("source") or {}
    if any((
        version not in SUPPORTED_PLAN_VERSIONS,
        artifact.get("status") != "validated",
        not artifact.get("request_id"),
        not source.get("revision_seal"),
    )):
        return False
    actual = _plan_fingerprint(
        artifact.get("request_id"),
        source.get("revision_seal"),
        source.get("confirmation_source"),
        artifact.get("plan") or {},
        decision_answer_fingerprint=source.get(
            "decision_answer_fingerprint"
        ),
        brief_basis_fingerprint=source.get(
            "brief_basis_fingerprint"
        ),
        plan_origin=source.get("plan_origin"),
        intent_fingerprint=source.get("intent_fingerprint"),
        generation_contract_lease_fingerprint=(
            (source.get("generation_contract_lease") or {}).get(
                "lease_fingerprint"
            )
        ),
        generation_job_lease_fingerprint=(
            (source.get("generation_job_lease") or {}).get(
                "lease_fingerprint"
            )
        ),
    )
    return all((
        _plan_origin_is_valid(
            source.get("confirmation_source"),
            source.get("plan_origin"),
        ),
        _generation_intent_identity_is_valid(artifact),
        _generation_job_lease_is_valid(
            source.get("generation_job_lease")
        ),
        _generation_contract_lease_is_valid(
            source.get("generation_contract_lease")
        ),
        artifact.get("plan_fingerprint") == actual,
        artifact.get("plan_id") == f"plan-{actual[:16]}",
    ))


def _generation_contract_lease_is_valid(value):
    from autowork_core.utils.debug_tools.recorder.generation_contract import (
        generation_contract_lease_is_valid,
    )

    return generation_contract_lease_is_valid(value)


def persist_generation_plan(
        session_dir,
        request_path,
        request,
        state,
        brief,
        normalized,
        *,
        intent,
        input_kind,
        input_version,
        confirmation_source,
        plan_origin,
        note,
        generation_contract_lease,
        generation_job_lease,
):
    from autowork_core.utils.debug_tools.recorder.generation_design import (
        GENERATION_DESIGN_VERSION,
    )

    if any((
        input_kind != "generation_design",
        input_version != GENERATION_DESIGN_VERSION,
        not _generation_contract_lease_is_valid(generation_contract_lease),
        not _generation_job_lease_is_valid(generation_job_lease),
    )):
        raise ValueError("Generation Plan输入身份无效")
    revision_seal = (state.get("revision") or {}).get("seal")
    decision_answer_fingerprint = (
        ((state.get("decision") or {}).get("answers") or {}).get(
            "answer_fingerprint"
        )
    )
    brief_basis_fingerprint = _plan_brief_basis_fingerprint(brief)
    intent_content = _generation_input_content(
        intent,
        input_kind=input_kind,
    )
    intent_fingerprint = _generation_intent_fingerprint(
        request.get("request_id"),
        revision_seal,
        brief_basis_fingerprint,
        intent_content,
        input_kind=input_kind,
        input_version=input_version,
    )
    fingerprint = _plan_fingerprint(
        request.get("request_id"),
        revision_seal,
        confirmation_source,
        normalized,
        decision_answer_fingerprint=decision_answer_fingerprint,
        brief_basis_fingerprint=brief_basis_fingerprint,
        plan_origin=plan_origin,
        intent_fingerprint=intent_fingerprint,
        generation_contract_lease_fingerprint=(
            generation_contract_lease or {}
        ).get("lease_fingerprint"),
        generation_job_lease_fingerprint=(
            generation_job_lease or {}
        ).get("lease_fingerprint"),
    )
    plan_id = f"plan-{fingerprint[:16]}"
    output = (
        Path(session_dir)
        / "ai"
        / "plans"
        / request["request_id"]
        / f"{plan_id}.json"
    )
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "plan_version": PLAN_VERSION,
        "plan_id": plan_id,
        "status": "validated",
        "confirmed_at": datetime.now().isoformat(timespec="seconds"),
        "request_id": request.get("request_id"),
        "request_path": str(request_path),
        "source": {
            "confirmation_source": confirmation_source,
            "plan_origin": plan_origin,
            "brief_path": str(brief.get("brief_path") or ""),
            "brief_fingerprint": brief.get("brief_fingerprint"),
            "brief_basis_fingerprint": brief_basis_fingerprint,
            "revision_seal": revision_seal,
            "decision_answer_fingerprint": decision_answer_fingerprint,
            "intent_fingerprint": intent_fingerprint,
            "generation_contract_lease": generation_contract_lease,
            "generation_job_lease": generation_job_lease,
        },
        "intent": {
            "intent_version": "1.0",
            "input_kind": input_kind,
            "input_version": input_version,
            "intent_fingerprint": intent_fingerprint,
            "content": intent_content,
        },
        "plan": normalized,
        "note": str(note or ""),
        "plan_fingerprint": fingerprint,
    }
    if output.exists():
        existing = _read_json(output)
        if existing.get("plan_fingerprint") != fingerprint:
            raise ValueError(f"计划 ID 冲突: {output}")
        artifact = existing
    else:
        write_json_atomic(output, artifact)
    return artifact, output


def _unique_strings(values):
    return list(dict.fromkeys(str(item) for item in values or [] if item))


def _normalize_memory_trace(value):
    value = value if isinstance(value, dict) else {}
    return {
        key: _normalize_memory_entries(value.get(key))
        for key in ("applied", "dismissed")
    }


def _normalize_memory_entries(values):
    result = []
    seen = set()
    for value in values or []:
        if isinstance(value, dict):
            memory_id = str(value.get("memory_id") or "").strip()
            reason = str(value.get("reason") or "").strip()
        else:
            memory_id = str(value or "").strip()
            reason = ""
        if not memory_id or memory_id in seen:
            continue
        item = {"memory_id": memory_id}
        if reason:
            item["reason"] = reason[:MAX_MEMORY_TRACE_REASON]
        result.append(item)
        seen.add(memory_id)
    return result


def _validate_memory_trace(plan, brief):
    trace = plan.get("memory_trace") or {}
    applied = trace.get("applied") or []
    dismissed = trace.get("dismissed") or []
    errors = []
    if len(applied) > MAX_MEMORY_TRACE_ITEMS:
        errors.append(
            "Plan memory_trace.applied 超出上限: "
            f"{len(applied)} > {MAX_MEMORY_TRACE_ITEMS}"
        )
    if len(dismissed) > MAX_MEMORY_TRACE_ITEMS:
        errors.append(
            "Plan memory_trace.dismissed 超出上限: "
            f"{len(dismissed)} > {MAX_MEMORY_TRACE_ITEMS}"
        )
    available = {
        str(item.get("memory_id"))
        for item in (brief.get("memory_digest") or {}).get("items") or []
        if item.get("memory_id")
    }
    applied_ids = {
        str(item.get("memory_id"))
        for item in applied
        if item.get("memory_id")
    }
    dismissed_ids = {
        str(item.get("memory_id"))
        for item in dismissed
        if item.get("memory_id")
    }
    unknown = sorted((applied_ids | dismissed_ids) - available)
    if unknown:
        errors.append(
            f"Plan memory_trace 引用冻结 Brief 之外的 memory: {unknown}"
        )
    overlap = sorted(applied_ids & dismissed_ids)
    if overlap:
        errors.append(
            f"Plan memory_trace 同时采用和拒绝同一 memory: {overlap}"
        )
    return errors


def _confidence(value):
    if value is None:
        return None
    try:
        return round(max(0.0, min(1.0, float(value))), 4)
    except (TypeError, ValueError):
        return None


def _merge_by_key(existing, required, key):
    values = {
        item.get(key): dict(item)
        for item in existing
        if isinstance(item, dict) and item.get(key)
    }
    for item in required:
        if isinstance(item, dict) and item.get(key):
            values[item[key]] = dict(item)
    return list(values.values())


def _operations_for_actions(operations, action_ids):
    expected = set(action_ids or [])
    return [
        operation
        for operation in operations
        if expected & set(operation.get("action_ids") or [])
    ]


def _operation_constraint_matches(actual, expected):
    return all((
        actual.get("op") == expected.get("op"),
        actual.get("target") == expected.get("target"),
        actual.get("parameters") == (expected.get("parameters") or {}),
        set(expected.get("evidence_ids") or [])
        <= set(actual.get("evidence_ids") or []),
        set(expected.get("decision_ids") or [])
        <= set(actual.get("decision_ids") or []),
    ))


def _read_json(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 必须是 object: {path}")
    return value


def _stable_hash(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _plan_fingerprint(
        request_id,
        revision_seal,
        confirmation_source,
        plan,
        *,
        decision_answer_fingerprint,
        brief_basis_fingerprint=None,
        plan_origin=None,
        intent_fingerprint=None,
        generation_contract_lease_fingerprint=None,
        generation_job_lease_fingerprint=None,
    ):
    value = {
        "request_id": request_id,
        "revision_seal": revision_seal,
        "confirmation_source": confirmation_source,
        "plan": plan,
        "decision_answer_fingerprint": decision_answer_fingerprint,
        "brief_basis_fingerprint": brief_basis_fingerprint,
        "plan_origin": plan_origin,
        "intent_fingerprint": intent_fingerprint,
        "generation_contract_lease_fingerprint": (
            generation_contract_lease_fingerprint
        ),
        "generation_job_lease_fingerprint": (
            generation_job_lease_fingerprint
        ),
    }
    return _stable_hash(value)


def _generation_job_lease_is_valid(value):
    from autowork_core.utils.debug_tools.recorder.generation_job import (
        generation_job_lease_is_valid,
    )

    return generation_job_lease_is_valid(value)


def _generation_job_lease_matches_state(lease, state):
    pointer = (state or {}).get("current_job") or {}
    return bool(
        lease.get("job_id") == pointer.get("job_id")
        and lease.get("job_fingerprint") == pointer.get("job_fingerprint")
        and lease.get("job_nonce") == pointer.get("nonce")
        and lease.get("request_id") == (state or {}).get("request_id")
        and lease.get("profile_fingerprint")
        == pointer.get("profile_lease_fingerprint")
    )


def _plan_origin_is_valid(confirmation_source, plan_origin):
    if plan_origin not in PLAN_ORIGINS:
        return False
    allowed = {
        "ai_generated": {"external_ai", "deterministic_surrogate"},
        "user_adjustment": {"human_authored"},
    }
    return plan_origin in allowed.get(str(confirmation_source or ""), set())


def _plan_brief_basis_fingerprint(brief):
    revision = brief.get("revision") or {}
    stable_revision = {
        key: revision.get(key)
        for key in (
            "evidence_fingerprint",
            "context_fingerprint",
            "contract_hash",
            "api_signature_hash",
            "memory_revision",
            "takes",
            "policy_version",
        )
    }
    conflicts = [
        {
            key: item.get(key)
            for key in ("code", "step_id", "action_id")
        }
        for item in brief.get("conflicts") or []
        if isinstance(item, dict)
    ]
    semantics = brief.get("semantics") or {}
    stable_semantics = {
        "available": semantics.get("available", False),
        "packs": semantics.get("packs") or [],
        "window_causality": semantics.get("window_causality") or [],
        "step_continuity": semantics.get("step_continuity") or [],
    }
    basis = {
        "brief_version": brief.get("brief_version"),
        "request_id": brief.get("request_id"),
        "revision": stable_revision,
        "target": brief.get("target") or {},
        "story": brief.get("story") or [],
        "actions": brief.get("actions") or [],
        "ambiguities": brief.get("ambiguities") or [],
        "conflicts": conflicts,
        "coverage": brief.get("coverage") or {},
        "memory_digest": brief.get("memory_digest") or {},
        "semantics": stable_semantics,
        "generation": brief.get("generation") or {},
    }
    if "annotation_snapshot" in brief:
        basis["annotation_snapshot"] = brief.get("annotation_snapshot") or {}
    basis["scenario_intelligence"] = (
        brief.get("scenario_intelligence") or {}
    )
    basis["window_ownership"] = brief.get("window_ownership") or {}
    return _stable_hash(basis)
