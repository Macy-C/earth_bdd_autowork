from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from pathlib import Path

from autowork_core.utils.debug_tools.recorder.ai_capability_registry import (
    capability_by_name,
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
from autowork_core.utils.debug_tools.recorder.table_usage import (
    validate_table_usage,
)
from autowork_core.utils.debug_tools.recorder.value_authority import (
    declared_example_arguments,
    FEATURE_LITERAL_REFERENCES,
    SEMANTIC_LITERAL_OPERATIONS,
    resolve_declared_feature_literal,
    resolve_feature_literal,
    resolve_implementation_parameters,
    resolve_recorded_action_value,
)


GENERATION_DESIGN_VERSION = "1.1"
VALUE_SOURCE_KINDS = frozenset({
    "recorded_action",
    "feature_literal",
    "examples",
    "data_table",
    "runtime",
    "semantic_literal",
})
ACTION_RELATIONSHIP_KINDS = frozenset({
    "activation_for",
    "transport_for",
    "absorbed_by",
})
MAX_DESIGN_MEMORY_ITEMS = 6
MAX_DESIGN_MEMORY_REASON = 96


def compact_generation_design_contract():
    return {
        "design_version": GENERATION_DESIGN_VERSION,
        "purpose": (
            "AI submits semantic and implementation choices; the system "
            "compiles the complete GenerationPlan and proof."
        ),
        "top_level": {
            "required": [
                "design_version",
                "summary",
                "window_ownership",
                "steps",
                "ambiguity_choices",
            ],
            "optional": [
                "scenario_intent",
                "memory_trace",
            ],
            "field_types": {
                "steps": "array of Step objects in exact target order",
                "window_ownership": (
                    "array of window ownership objects; child roots may "
                    "declare parent_root to compile as an existing WindowView"
                ),
                "ambiguity_choices": "array of ambiguity choice objects",
                "memory_trace": "object",
            },
            "rule": "Fields outside required/optional are rejected.",
        },
        "step": {
            "container": "array item under top-level steps",
            "required": [
                "step_id",
                "intent",
                "implementation_strategy",
            ],
            "conditional_required": {
                "operations": "required unless step_behavior.strategy is reuse",
            },
            "optional": [
                "consumes",
                "produces",
                "observes",
                "action_relationships",
                "step_behavior",
                "table_use",
            ],
            "implementation_strategies": [
                "step_inline",
                "page_method",
            ],
            "concept_rule": (
                "consumes/produces/observes contain AI business concept "
                "names, not state IDs or support references"
            ),
            "action_relationships": {
                "container": "array of same-Step physical Action relationships",
                "required": [
                    "kind",
                    "source_action_id",
                    "consumer_action_id",
                ],
                "kinds": sorted(ACTION_RELATIONSHIP_KINDS),
                "rule": (
                    "Relations preserve physical Action order and proof. "
                    "Only absorbed_by may cover a source Action without "
                    "a separate runtime operation."
                ),
            },
        },
        "operation": {
            "container": "array item under Step operations",
            "required": [
                "operation",
                "window_root",
                "target_action_id",
            ],
            "optional": [
                "target_name",
                "locator_candidate_id",
                "value_source",
                "method_name",
                "method_resolution",
                "runtime_value",
                "reason",
                "rejected_alternatives",
                "uncertainty",
            ],
            "rule": (
                "Choose semantic operations and frozen target Actions. "
                "locator_candidate_id may select only a unique, target-"
                "matching candidate frozen on that Action; omit it to use "
                "the system-selected candidate. Do not submit raw locator "
                "proof, YAML, paths, owners, Scenario support, or "
                "value_action_ids."
            ),
            "field_types": {
                "value_source": "object",
                "method_resolution": "object",
                "runtime_value": "object",
                "rejected_alternatives": "array of operation strings",
            },
        },
        "step_behavior": {
            "required": ["strategy"],
            "optional": ["candidate_id", "reason", "action_mappings"],
            "strategies": ["create", "reuse", "modify"],
            "rule": (
                "reuse/modify require a frozen step_definition candidate. "
                "create forbids candidate_id. Exact reuse forbids operations "
                "and requires action_mappings to prove every current Action "
                "against one ordered frozen call_sequence entry; modify still "
                "requires operations."
            ),
        },
        "method_resolution": {
            "required": ["strategy"],
            "optional": ["candidate_id", "reason"],
            "strategies": ["create", "reuse", "modify"],
            "rule": (
                "Only page_method operations use this field. reuse/modify "
                "require a frozen page_object_method candidate belonging "
                "to the selected window owner and forbid method_name; create "
                "forbids candidate_id and may declare method_name; otherwise "
                "the compiler derives a target-specific deterministic name. "
                "When a frozen candidate exposes call_sequence, reuse requires "
                "the Design operations and target_name values to match that "
                "ordered sequence; target and value Actions remain separate. "
                "Every page_method operation declares method_resolution, "
                "while step_inline forbids it and method_name."
            ),
        },
        "runtime_value": {
            "required": ["produces"],
            "optional": ["attribute"],
            "rule": (
                "Only save_text/save_attr can produce a runtime business "
                "value. save_attr also requires attribute. The compiler "
                "derives the technical binding; save_text forbids attribute."
            ),
        },
        "table_use": {
            "required": [
                "relationship",
                "data_shape",
                "execution_owner",
                "order_matters",
                "column_meanings",
                "reason",
            ],
            "optional": ["state_name"],
            "relationships": [
                "independent_rows",
                "continuous_rows",
                "whole_table",
                "scenario_state",
            ],
            "data_shapes": [
                "action_sequence",
                "list",
                "mapping",
                "object",
                "records",
            ],
            "execution_owners": ["page", "scenario", "step"],
            "column_meanings": [
                "action",
                "expected",
                "field",
                "input",
                "key",
                "metadata",
                "option",
                "target",
                "value",
            ],
            "rule": (
                "Declare table_use for every Step with a Data Table. "
                "scenario_state requires execution_owner=scenario and "
                "state_name; other relationships use step or page."
            ),
        },
        "value_source": {
            "kinds": sorted(VALUE_SOURCE_KINDS),
            "shapes": {
                "recorded_action": ["kind", "action_id"],
                "feature_literal": ["kind", "reference", "value?"],
                "semantic_literal": ["kind", "value"],
                "examples": ["kind", "reference"],
                "data_table": ["kind", "reference"],
                "runtime": ["kind", "producer", "argument"],
            },
            "runtime_rule": (
                "producer names a prior runtime_value.produces value; "
                "argument is required and must be null because runtime "
                "cannot replace a Feature/Examples argument"
            ),
            "feature_literal_references": sorted(FEATURE_LITERAL_REFERENCES),
            "semantic_literal_operations": sorted(
                SEMANTIC_LITERAL_OPERATIONS
            ),
            "operation_rule": (
                "value_source is required exactly when the selected "
                "capability requires value authority; it is forbidden for "
                "non-value operations. feature_literal may use a unique "
                "compiler-resolved value or name an exact non-empty literal "
                "that exists in the frozen Feature declaration. "
                "Global shapes do not prove Step availability; after naming "
                "an operation, query Action Knowledge "
                "value_source_qualification for that exact Step/Action/operation."
            ),
        },
        "window_ownership": {
            "container": "array of objects",
            "required": ["root_name", "strategy"],
            "optional": [
                "candidate_id",
                "business_name",
                "parent_root",
                "ownership_candidate_id",
                "dismissed_ownership_candidate_ids",
                "reason",
            ],
            "strategies": ["reuse_existing", "create_new"],
            "rule": (
                "AI chooses the business owner shape. Declare exactly one "
                "owner for every distinct operation "
                "window_root and no others. reuse_existing requires a frozen "
                "candidate_id and forbids business_name. create_new forbids "
                "candidate_id and requires an ASCII snake_case business_name "
                "when the recorded Root contains a machine identity suffix. "
                "To create a WindowView, declare parent_root and the exact "
                "frozen ownership_candidate_id. To keep a candidate child "
                "root as an independent WindowPage, list every rejected "
                "candidate in dismissed_ownership_candidate_ids and provide "
                "a non-empty business reason. The system verifies candidate "
                "roots, scoped Actions, and order."
            ),
        },
        "ambiguity_choice": {
            "container": "array of objects",
            "required": ["ambiguity_id", "outcome"],
            "optional": ["candidate_id", "reason"],
            "rule": (
                "Declare every AI-only ambiguity exactly once. Omit user-"
                "authority and evidence-required ambiguities; mixed entries "
                "may select only a frozen AI-authority outcome. The frozen "
                "generate_issue_placeholder outcome creates a typed failing "
                "Step draft; AI never supplies its issue ID or code template."
            ),
        },
        "memory_trace": {
            "required": ["applied", "dismissed"],
            "entry": {
                "required": ["memory_id"],
                "optional": ["reason"],
            },
            "rule": (
                "Both fields are arrays of at most 6 unique entries. IDs "
                "must exist in Brief memory_digest, cannot overlap, and "
                "reason is at most 96 characters. Omit memory_trace when no "
                "memory assessment is supplied."
            ),
        },
        "system_compiles": [
            "Gherkin roles and Scenario Model support/transitions",
            "window owner objects and workspace paths",
            "Step files, Page/locator paths, and locator declarations",
            "Action/Evidence closure and target fingerprints",
            "Annotation trace and Decision constraints",
            "ambiguity Action/Evidence coverage",
            "frozen scroll/drag, range, collection, and OCR parameters",
        ],
    }


def compile_generation_design(design, brief):
    design = _validate_design_shape(design, brief)
    target_steps = list((brief.get("target") or {}).get("steps") or ())
    steps_by_id = {
        str(step.get("id") or ""): step
        for step in target_steps
        if step.get("id")
    }
    actions_by_step = {}
    action_orders_by_step = {}
    for action in brief.get("actions") or ():
        step_id = str(action.get("step_id") or "")
        action_id = str(action.get("id") or "")
        if step_id and action_id:
            order = action_orders_by_step.get(step_id, 0) + 1
            action_orders_by_step[step_id] = order
            actions_by_step.setdefault(step_id, {})[action_id] = {
                **action,
                "_order": order,
            }

    runtime_bindings = _compile_runtime_bindings(design)
    owners = _compile_window_owners(design, brief)
    design_steps = {
        str(item["step_id"]): item
        for item in design.get("steps") or ()
    }
    plan_steps = {}
    unresolved_by_step = _compile_unresolved_issues(
        design,
        brief,
    )
    for target_step in target_steps:
        step_id = str(target_step["id"])
        selected = design_steps[step_id]
        step_file = _step_file_from_brief(brief, step_id=step_id)
        behavior = _compile_step_behavior(
            selected,
            brief,
            step_id,
            step_file,
            actions_by_step.get(step_id, {}),
            target_step,
        )
        table_usage = _compile_table_use(
            step_id,
            selected.get("table_use"),
            target_step.get("table"),
        )
        unresolved = unresolved_by_step.get(step_id) or []
        if unresolved:
            plan_steps[step_id] = {
                "behavior_owner": "step_orchestration",
                "behavior_file": behavior.get("behavior_file") or step_file,
                "behavior_resolution": behavior["resolution"],
                "page_object": None,
                "locator_file": None,
                "data_file": None,
                "operations": [],
                "action_relationships": [],
                "locators": [],
                "ignored_action_ids": [],
                "table_usage": table_usage,
                "unresolved_issues": unresolved,
            }
            continue
        if behavior["strategy"] == "reuse":
            behavior["step"]["table_usage"] = table_usage
            plan_steps[step_id] = behavior["step"]
            continue
        operations = [
            _compile_operation(
                step_id,
                operation,
                actions_by_step.get(step_id, {}),
                owners,
                brief,
                target_step,
                selected["implementation_strategy"],
                runtime_bindings,
            )
            for operation in selected.get("operations") or ()
        ]
        action_relationships = _compile_action_relationships(
            step_id,
            selected.get("action_relationships"),
            actions_by_step.get(step_id, {}),
            operations,
            brief,
        )
        owner_ids = list(dict.fromkeys(
            operation["window_owner"] for operation in operations
        ))
        owner = owners[owner_ids[0]] if owner_ids else None
        plan_steps[step_id] = {
            "behavior_owner": (
                "step_orchestration"
                if len(owner_ids) > 1
                else "window_page"
                if selected.get("implementation_strategy") == "page_method"
                else "step_orchestration"
            ),
            "behavior_file": behavior.get("behavior_file") or step_file,
            "behavior_resolution": behavior["resolution"],
            "page_object": owner.get("page_object") if owner else None,
            "locator_file": owner.get("root_locator_file") if owner else None,
            "data_file": None,
            "operations": operations,
            "action_relationships": action_relationships,
            "locators": _compile_step_locators(
                operations,
                actions_by_step.get(step_id, {}),
                owners,
            ),
            "ignored_action_ids": [],
            "table_usage": table_usage,
        }

    ambiguity_resolutions = _compile_ambiguities(design, brief)
    _validate_confirmed_input_recovery_choices(
        ambiguity_resolutions,
        plan_steps,
        brief,
    )
    plan = {
        "summary": str(design.get("summary") or "").strip(),
        "scenario_model": _compile_scenario_model(
            design,
            brief,
            target_steps,
            actions_by_step,
        ),
        "window_owners": owners,
        "steps": plan_steps,
        "ambiguity_resolutions": ambiguity_resolutions,
        "memory_trace": deepcopy(design.get("memory_trace") or {}),
    }
    _validate_reused_method_sequences(plan_steps, brief)
    return plan


def _validate_confirmed_input_recovery_choices(
        resolutions,
        plan_steps,
        brief,
    ):
    ambiguities = {
        str(item.get("ambiguity_id") or ""): item
        for item in brief.get("ambiguities") or ()
        if isinstance(item, dict) and item.get("ambiguity_id")
    }
    for resolution in resolutions or ():
        if resolution.get("outcome") != "implement_feature_literal_input":
            continue
        ambiguity = ambiguities.get(
            str(resolution.get("ambiguity_id") or "")
        ) or {}
        if ambiguity.get("code") != "confirmed_keyboard_input_excluded":
            continue
        candidate = (ambiguity.get("facts") or {}).get("candidate") or {}
        candidate_id = str(candidate.get("candidate_id") or "")
        if not candidate_id or resolution.get("candidate_id") != candidate_id:
            raise ValueError("确认排除输入的恢复候选不一致")
        step_id = str(ambiguity.get("step_id") or "")
        operations = (plan_steps.get(step_id) or {}).get("operations") or []
        matching = [
            operation for operation in operations
            if all((
                operation.get("op") == "input_text",
                str(operation.get("target_action_id") or "")
                == str(candidate.get("target_action_id") or ""),
                str((operation.get("value_provenance") or {}).get("kind") or "")
                == "feature_literal",
                str((operation.get("value_provenance") or {}).get(
                    "reference"
                ) or "") == str(candidate.get("value_reference") or ""),
                operation.get("value") == candidate.get("literal"),
            ))
        ]
        if len(matching) != 1:
            raise ValueError(
                "确认排除输入的恢复候选必须由同一冻结目标和"
                "Feature literal 的唯一input_text操作实现"
            )


def _validate_reused_method_sequences(plan_steps, brief):
    for step_id, step in plan_steps.items():
        by_method = {}
        for operation in step.get("operations") or ():
            resolution = operation.get("implementation_resolution") or {}
            if resolution.get("strategy") != "reuse":
                continue
            method = str(operation.get("implementation_method") or "")
            by_method.setdefault(method, []).append(operation)
        for method, operations in by_method.items():
            candidate_id = str(
                (operations[0].get("implementation_resolution") or {}).get(
                    "candidate_id"
                )
                or ""
            )
            candidate = _implementation_candidate(brief, candidate_id) or {}
            sequence = list(candidate.get("call_sequence") or ())
            if not sequence:
                continue
            expected = [
                {
                    "operation": str(item.get("operation") or ""),
                    "target": str(item.get("target") or ""),
                }
                for item in sequence
            ]
            actual = [
                {
                    "operation": str(item.get("op") or ""),
                    "target": str(item.get("target") or ""),
                }
                for item in operations
            ]
            if actual != expected:
                raise ValueError(
                    f"Design Step {step_id}方法 {method} 的reuse调用序列"
                    f"与冻结候选不一致: expected={expected} actual={actual}"
                )


def _validate_design_shape(design, brief):
    if not isinstance(design, dict):
        raise ValueError("GenerationDesign必须是object")
    _reject_unknown_fields(
        design,
        {
            "design_version",
            "summary",
            "scenario_intent",
            "window_ownership",
            "steps",
            "ambiguity_choices",
            "memory_trace",
        },
        "GenerationDesign",
    )
    if design.get("design_version") != GENERATION_DESIGN_VERSION:
        raise ValueError(
            f"GenerationDesign version必须为{GENERATION_DESIGN_VERSION}"
        )
    if not str(design.get("summary") or "").strip():
        raise ValueError("GenerationDesign缺少summary")
    if "scenario_intent" in design and not isinstance(
            design.get("scenario_intent"), str
        ):
        raise ValueError("GenerationDesign scenario_intent必须是string")
    steps = _design_object_list(design, "steps", required=True)
    owners = _design_object_list(design, "window_ownership", required=True)
    ambiguities = _design_object_list(
        design,
        "ambiguity_choices",
        required=True,
    )
    unresolved_step_ids = set(_compile_unresolved_issues(
        design,
        brief,
    ))
    _validate_design_memory_trace(design.get("memory_trace"), brief)
    _reject_duplicate_design_keys(
        owners,
        "root_name",
        "window ownership",
    )
    _reject_duplicate_design_keys(
        ambiguities,
        "ambiguity_id",
        "ambiguity choice",
    )
    target_ids = [
        str(step.get("id"))
        for step in (brief.get("target") or {}).get("steps") or ()
        if step.get("id")
    ]
    design_ids = [
        str(step.get("step_id") or "")
        for step in steps
    ]
    if design_ids != target_ids:
        raise ValueError(
            "GenerationDesign Step范围或顺序不一致: "
            f"expected={target_ids} actual={design_ids}"
        )
    concept_slugs = {}
    for step in steps:
        if step.get("ignored_action_ids"):
            raise ValueError(
                "GenerationDesign不能忽略Action；用户权威选择必须来自Decision"
            )
        _reject_unknown_fields(
            step,
            {
                "step_id",
                "intent",
                "implementation_strategy",
                "operations",
                "consumes",
                "produces",
                "observes",
                "action_relationships",
                "step_behavior",
                "table_use",
            },
            f"Design Step {step.get('step_id')}",
        )
        if not str(step.get("intent") or "").strip():
            raise ValueError(
                f"Design Step {step.get('step_id')}缺少intent"
            )
        if step.get("implementation_strategy") not in {
            "step_inline",
            "page_method",
        }:
            raise ValueError("implementation_strategy无效")
        behavior_strategy = str(
            (step.get("step_behavior") or {}).get("strategy") or "create"
        )
        _validate_step_behavior_choice(
            step.get("step_behavior"),
            label="step_behavior",
            default="create",
        )
        if (
            behavior_strategy != "reuse"
            and str(step.get("step_id") or "") not in unresolved_step_ids
            and (
                not isinstance(step.get("operations"), list)
                or not step["operations"]
            )
        ):
            raise ValueError(f"Design Step {step.get('step_id')}缺少operations")
        if (
            str(step.get("step_id") or "") in unresolved_step_ids
            and step.get("operations")
        ):
            raise ValueError("issue placeholder Step不能声明operations")
        if behavior_strategy == "reuse" and step.get("operations"):
            raise ValueError("step_behavior reuse不能声明operations")
        if behavior_strategy == "reuse" and step.get("action_relationships"):
            raise ValueError("step_behavior reuse不能声明action_relationships")
        for relationship in ("consumes", "produces", "observes"):
            if relationship in step and not isinstance(
                    step.get(relationship), list
                ):
                raise ValueError(
                    f"Design Step {step.get('step_id')} {relationship}必须是array"
                )
            values = step.get(relationship) or []
            if not all(isinstance(item, str) and item.strip() for item in values):
                raise ValueError(
                    f"Design Step {step.get('step_id')} {relationship}必须是非空string array"
                )
            if len(values) != len(set(values)):
                raise ValueError(
                    f"Design Step {step.get('step_id')} {relationship}包含重复concept"
                )
            for concept in values:
                slug = _safe_name(concept)
                previous = concept_slugs.setdefault(slug, concept)
                if previous != concept:
                    raise ValueError(
                        "GenerationDesign concept规范化后冲突: "
                        f"{previous!r}/{concept!r}"
                    )
        if "step_behavior" in step and not isinstance(
                step.get("step_behavior"), dict
            ):
            raise ValueError("step_behavior必须是object")
        if "table_use" in step and not isinstance(step.get("table_use"), dict):
            raise ValueError("table_use必须是object")
        if "action_relationships" in step and not isinstance(
                step.get("action_relationships"), list
        ):
            raise ValueError("action_relationships必须是array")
        for relationship in step.get("action_relationships") or []:
            if not isinstance(relationship, dict):
                raise ValueError("action_relationships必须是object array")
            _reject_unknown_fields(
                relationship,
                {
                    "kind",
                    "source_action_id",
                    "consumer_action_id",
                    "reason",
                },
                "action_relationship",
            )
            for field in (
                    "kind",
                    "source_action_id",
                    "consumer_action_id",
            ):
                if not isinstance(relationship.get(field), str) or not (
                        relationship[field].strip()
                ):
                    raise ValueError(
                        f"action_relationship缺少{field}"
                    )
            if relationship["kind"] not in ACTION_RELATIONSHIP_KINDS:
                raise ValueError(
                    f"action_relationship kind无效: {relationship['kind']}"
                )
            if (
                "reason" in relationship
                and not isinstance(relationship.get("reason"), str)
            ):
                raise ValueError("action_relationship reason必须是string")
        if isinstance(step.get("table_use"), dict):
            _reject_unknown_fields(
                step["table_use"],
                {
                    "relationship",
                    "data_shape",
                    "execution_owner",
                    "order_matters",
                    "column_meanings",
                    "reason",
                    "state_name",
                },
                "table_use",
            )
            if not isinstance(step["table_use"].get("column_meanings"), dict):
                raise ValueError("table_use column_meanings必须是object")
        operations = step.get("operations") or []
        if not isinstance(operations, list) or not all(
                isinstance(item, dict) for item in operations
            ):
            raise ValueError(
                f"Design Step {step.get('step_id')} operations必须是object array"
            )
        for operation in operations:
            _reject_unknown_fields(
                operation,
                {
                    "operation",
                    "window_root",
                    "target_action_id",
                    "target_name",
                    "locator_candidate_id",
                    "value_source",
                    "method_name",
                    "method_resolution",
                    "runtime_value",
                    "reason",
                    "rejected_alternatives",
                    "uncertainty",
                },
                "Design operation",
            )
            for field in ("operation", "window_root", "target_action_id"):
                if not isinstance(operation.get(field), str) or not operation[field].strip():
                    raise ValueError(f"Design operation缺少{field}")
            if (
                "locator_candidate_id" in operation
                and (
                    not isinstance(operation.get("locator_candidate_id"), str)
                    or not operation["locator_candidate_id"].strip()
                )
            ):
                raise ValueError(
                    "Design operation locator_candidate_id必须是非空string"
                )
            capability = capability_by_name(operation["operation"])
            source = operation.get("value_source")
            if capability is not None and capability.plan_enabled:
                if capability.requires_value_action and not isinstance(
                        source, dict
                    ):
                    raise ValueError(
                        f"Design value operation {operation['operation']}缺少value_source"
                    )
                if not capability.requires_value_action and source is not None:
                    raise ValueError(
                        f"Design non-value operation {operation['operation']}不能声明value_source"
                    )
            for field in ("value_source", "method_resolution", "runtime_value"):
                if field in operation and not isinstance(
                        operation.get(field), dict
                    ):
                    raise ValueError(f"Design operation {field}必须是object")
            if step.get("implementation_strategy") == "step_inline":
                if "method_resolution" in operation or "method_name" in operation:
                    raise ValueError(
                        "step_inline operation不能声明method_resolution或method_name"
                    )
            else:
                if "method_resolution" not in operation:
                    raise ValueError(
                        "page_method operation缺少method_resolution"
                    )
                method_strategy = _validate_resolution_choice(
                    operation.get("method_resolution"),
                    label="method_resolution",
                    default=None,
                )
                if method_strategy != "create" and operation.get("method_name"):
                    raise ValueError(
                        "reuse/modify method_resolution不能声明method_name"
                    )
            if isinstance(operation.get("runtime_value"), dict):
                _reject_unknown_fields(
                    operation["runtime_value"],
                    {"produces", "attribute"},
                    "runtime_value",
                )
            alternatives = operation.get("rejected_alternatives")
            if alternatives is not None and (
                not isinstance(alternatives, list)
                or not all(isinstance(item, str) for item in alternatives)
            ):
                raise ValueError(
                    "Design operation rejected_alternatives必须是string array"
                )
            if isinstance(source, dict):
                _validate_value_source_shape(source)
                if (
                    source.get("kind") == "semantic_literal"
                    and operation["operation"] not in SEMANTIC_LITERAL_OPERATIONS
                ):
                    raise ValueError(
                        "semantic_literal不适用于operation: "
                        f"{operation['operation']}"
                    )
            if not isinstance(source, dict) or source.get("kind") != "runtime":
                continue
            if not str(source.get("producer") or "").strip():
                raise ValueError("runtime value source缺少producer")
            if "argument" not in source:
                raise ValueError(
                    "runtime value source必须显式声明argument:null"
                )
            if source.get("argument") is not None:
                raise ValueError(
                    "runtime value source不能替换Feature/Examples参数"
                )
    _validate_window_ownership_shape(owners, steps)
    for ambiguity in ambiguities:
        _reject_unknown_fields(
            ambiguity,
            {"ambiguity_id", "outcome", "candidate_id", "reason"},
            "ambiguity_choice",
        )
        for field in ("ambiguity_id", "outcome"):
            if not isinstance(ambiguity.get(field), str) or not ambiguity[field].strip():
                raise ValueError(f"ambiguity_choice缺少{field}")
    return deepcopy(design)


def _design_object_list(value, field, *, required=False):
    if field not in value:
        if required:
            raise ValueError(f"GenerationDesign缺少{field}")
        return []
    items = value.get(field)
    if not isinstance(items, list) or not all(
            isinstance(item, dict) for item in items
        ):
        raise ValueError(f"GenerationDesign {field}必须是object array")
    return items


def _reject_duplicate_design_keys(items, field, label):
    values = [
        str(item.get(field) or "")
        for item in items
        if item.get(field)
    ]
    duplicates = sorted({item for item in values if values.count(item) > 1})
    if duplicates:
        raise ValueError(f"GenerationDesign重复{label}: {duplicates}")


def _reject_unknown_fields(value, allowed, label):
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise ValueError(f"{label}包含未知字段: {unknown}")


def _validate_resolution_choice(value, *, label, default):
    if value is None:
        return default
    if not isinstance(value, dict):
        raise ValueError(f"{label}必须是object")
    _reject_unknown_fields(
        value,
        {"strategy", "candidate_id", "reason"},
        label,
    )
    strategy = str(value.get("strategy") or "")
    if strategy not in {"create", "reuse", "modify"}:
        raise ValueError(f"{label} strategy无效: {strategy}")
    candidate_id = str(value.get("candidate_id") or "").strip()
    if strategy in {"reuse", "modify"} and not candidate_id:
        raise ValueError(f"{label} {strategy}缺少candidate_id")
    if strategy == "create" and candidate_id:
        raise ValueError(f"{label} create不能声明candidate_id")
    return strategy


def _validate_step_behavior_choice(value, *, label, default):
    if value is None:
        return default
    if not isinstance(value, dict):
        raise ValueError(f"{label}必须是object")
    _reject_unknown_fields(
        value,
        {"strategy", "candidate_id", "reason", "action_mappings"},
        label,
    )
    strategy = str(value.get("strategy") or "")
    if strategy not in {"create", "reuse", "modify"}:
        raise ValueError(f"{label} strategy无效: {strategy}")
    candidate_id = str(value.get("candidate_id") or "").strip()
    if strategy in {"reuse", "modify"} and not candidate_id:
        raise ValueError(f"{label} {strategy}缺少candidate_id")
    if strategy == "create" and candidate_id:
        raise ValueError(f"{label} create不能声明candidate_id")
    mappings = value.get("action_mappings")
    if strategy == "reuse":
        if not isinstance(mappings, list) or not mappings:
            raise ValueError("step_behavior reuse缺少action_mappings")
    elif mappings is not None:
        raise ValueError(
            "只有step_behavior reuse可以声明action_mappings"
        )
    for mapping in mappings or ():
        if not isinstance(mapping, dict):
            raise ValueError("action_mappings必须是object array")
        _reject_unknown_fields(
            mapping,
            {"action_id", "call_index", "operation", "target", "value_source"},
            "step_behavior action_mapping",
        )
        if not isinstance(mapping.get("action_id"), str) or not (
                mapping["action_id"].strip()
        ):
            raise ValueError("action_mapping缺少action_id")
        if not isinstance(mapping.get("call_index"), int) or isinstance(
                mapping.get("call_index"), bool
        ) or mapping["call_index"] < 0:
            raise ValueError("action_mapping.call_index必须为非负整数")
        for field in ("operation", "target"):
            if not isinstance(mapping.get(field), str) or not (
                    mapping[field].strip()
            ):
                raise ValueError(f"action_mapping缺少{field}")
        if "value_source" in mapping and not isinstance(
                mapping.get("value_source"), dict
        ):
            raise ValueError("action_mapping.value_source必须是object")
    return strategy


def _validate_window_ownership_shape(owners, steps):
    referenced_roots = {
        str(operation.get("window_root") or "")
        for step in steps
        for operation in step.get("operations") or ()
    }
    selected_roots = set()
    public_names = {}
    for owner in owners:
        _reject_unknown_fields(
            owner,
            {
                "root_name",
                "parent_root",
                "ownership_candidate_id",
                "dismissed_ownership_candidate_ids",
                "strategy",
                "candidate_id",
                "business_name",
                "reason",
            },
            "window_ownership",
        )
        root_name = str(owner.get("root_name") or "").strip()
        strategy = str(owner.get("strategy") or "")
        candidate_id = str(owner.get("candidate_id") or "").strip()
        ownership_candidate_id = str(
            owner.get("ownership_candidate_id") or ""
        ).strip()
        dismissed_candidate_ids = [
            str(item).strip()
            for item in owner.get("dismissed_ownership_candidate_ids") or ()
            if str(item).strip()
        ]
        business_name = str(owner.get("business_name") or "").strip()
        parent_root = str(owner.get("parent_root") or "").strip()
        if not root_name:
            raise ValueError("window_ownership缺少root_name")
        if parent_root == root_name:
            raise ValueError("window view parent_root不能引用自身")
        if strategy not in {"reuse_existing", "create_new"}:
            raise ValueError(f"window ownership strategy无效: {strategy}")
        if strategy == "reuse_existing" and not candidate_id:
            raise ValueError("reuse_existing window ownership缺少candidate_id")
        if parent_root and strategy == "reuse_existing":
            raise ValueError("window view ownership不能声明reuse_existing")
        if parent_root and candidate_id:
            raise ValueError("window view ownership不能声明candidate_id")
        if parent_root and not ownership_candidate_id:
            raise ValueError("window view ownership缺少ownership_candidate_id")
        if not parent_root and ownership_candidate_id:
            raise ValueError("WindowPage ownership不能声明ownership_candidate_id")
        if parent_root and dismissed_candidate_ids:
            raise ValueError(
                "window view ownership不能同时拒绝ownership candidate"
            )
        if len(dismissed_candidate_ids) != len(set(dismissed_candidate_ids)):
            raise ValueError("window ownership包含重复dismissed candidate")
        if strategy == "reuse_existing" and business_name:
            raise ValueError(
                "reuse_existing window ownership不能声明business_name"
            )
        if strategy == "create_new" and candidate_id:
            raise ValueError("create_new window ownership不能声明candidate_id")
        if (
            strategy == "create_new"
            and _has_machine_identity_suffix(root_name)
            and not business_name
        ):
            raise ValueError(
                "带内部身份后缀的Root必须声明稳定business_name: "
                f"{root_name}"
            )
        if parent_root and not business_name:
            raise ValueError("window view ownership必须声明business_name")
        _validate_dismissed_ownership_candidates(
            owner,
            dismissed_candidate_ids,
            root_name,
        )
        public_name = _public_owner_name(root_name, business_name)
        previous_root = public_names.setdefault(public_name, root_name)
        if previous_root != root_name:
            raise ValueError(
                "不同window root不能共享business_name: "
                f"{previous_root}/{root_name} -> {public_name}"
            )
        selected_roots.add(root_name)
    parent_roots = {
        str(owner.get("parent_root") or "").strip()
        for owner in owners
        if str(owner.get("parent_root") or "").strip()
    }
    missing_parents = sorted(parent_roots - selected_roots)
    if missing_parents:
        raise ValueError(
            "window view parent_root必须引用已选择Root: "
            f"{missing_parents}"
        )
    if selected_roots != referenced_roots:
        raise ValueError(
            "GenerationDesign window ownership范围不一致: "
            f"expected={sorted(referenced_roots)} "
            f"actual={sorted(selected_roots)}"
        )


def _validate_value_source_shape(source):
    kind = str(source.get("kind") or "")
    fields = {
        "recorded_action": {"kind", "action_id"},
        "feature_literal": {"kind", "reference", "value"},
        "semantic_literal": {"kind", "value"},
        "examples": {"kind", "reference"},
        "data_table": {"kind", "reference"},
        "runtime": {"kind", "producer", "argument"},
    }
    if kind not in fields:
        raise ValueError(f"未知Design value source: {kind}")
    _reject_unknown_fields(source, fields[kind], f"value_source {kind}")
    required = fields[kind] - {"kind"}
    if kind == "feature_literal":
        required.discard("value")
    missing = sorted(field for field in required if field not in source)
    if missing:
        if kind == "runtime" and "argument" in missing:
            raise ValueError(
                "runtime value source必须显式声明argument:null"
            )
        raise ValueError(f"value_source {kind}缺少字段: {missing}")
    for field in required - {"value", "argument"}:
        if not isinstance(source.get(field), str) or not source[field].strip():
            raise ValueError(f"value_source {kind}.{field}必须是非空string")
    if (
        kind == "feature_literal"
        and source.get("reference") not in FEATURE_LITERAL_REFERENCES
    ):
        raise ValueError(
            f"未知feature literal reference: {source.get('reference')}"
        )
    if (
        kind == "feature_literal"
        and "value" in source
        and (
            not isinstance(source.get("value"), str)
            or not source["value"]
        )
    ):
        raise ValueError("feature_literal.value必须是非空string")


def _validate_design_memory_trace(value, brief):
    if value is None:
        return
    if not isinstance(value, dict):
        raise ValueError("GenerationDesign memory_trace必须是object")
    _reject_unknown_fields(value, {"applied", "dismissed"}, "memory_trace")
    if set(value) != {"applied", "dismissed"}:
        raise ValueError("memory_trace必须声明applied和dismissed")
    available = {
        str(item.get("memory_id"))
        for item in (brief.get("memory_digest") or {}).get("items") or ()
        if item.get("memory_id")
    }
    ids = {}
    for field in ("applied", "dismissed"):
        entries = value.get(field)
        if not isinstance(entries, list) or not all(
                isinstance(item, dict) for item in entries
            ):
            raise ValueError(f"memory_trace.{field}必须是object array")
        if len(entries) > MAX_DESIGN_MEMORY_ITEMS:
            raise ValueError(
                f"memory_trace.{field}超出上限: {MAX_DESIGN_MEMORY_ITEMS}"
            )
        field_ids = []
        for entry in entries:
            _reject_unknown_fields(
                entry,
                {"memory_id", "reason"},
                f"memory_trace.{field} entry",
            )
            memory_id = str(entry.get("memory_id") or "").strip()
            if not memory_id:
                raise ValueError(f"memory_trace.{field}缺少memory_id")
            reason = entry.get("reason")
            if reason is not None and (
                not isinstance(reason, str)
                or len(reason) > MAX_DESIGN_MEMORY_REASON
            ):
                raise ValueError(
                    f"memory_trace.{field} reason必须是不超过96字符的string"
                )
            field_ids.append(memory_id)
        if len(field_ids) != len(set(field_ids)):
            raise ValueError(f"memory_trace.{field}包含重复memory_id")
        unknown = sorted(set(field_ids) - available)
        if unknown:
            raise ValueError(
                f"memory_trace.{field}引用冻结Brief之外的memory: {unknown}"
            )
        ids[field] = set(field_ids)
    overlap = sorted(ids["applied"] & ids["dismissed"])
    if overlap:
        raise ValueError(f"memory_trace同时采用和拒绝memory: {overlap}")


def _compile_window_owners(design, brief):
    windows = {
        str(item.get("root_name") or ""): item
        for item in (brief.get("window_ownership") or {}).get("windows") or ()
        if item.get("root_name")
    }
    selections = {
        str(item.get("root_name") or ""): item
        for item in design.get("window_ownership") or ()
        if isinstance(item, dict) and item.get("root_name")
    }
    referenced_roots = {
        str(operation.get("window_root") or "")
        for step in design.get("steps") or ()
        for operation in step.get("operations") or ()
        if operation.get("window_root")
    }
    owners = {}
    view_roots = {
        root_name
        for root_name in referenced_roots
        if str((selections.get(root_name) or {}).get("parent_root") or "").strip()
    }
    for root_name in sorted(referenced_roots - view_roots):
        window = windows.get(root_name)
        if window is None:
            raise ValueError(f"Design引用未知window root: {root_name}")
        selection = selections.get(root_name) or {
            "root_name": root_name,
            "strategy": "create_new",
        }
        ownership_decision = _compiled_window_page_ownership_decision(
            selection,
            brief,
        )
        owner_id = _owner_id(root_name)
        if selection.get("strategy") == "reuse_existing":
            candidate_id = str(selection.get("candidate_id") or "")
            candidate = next((
                item
                for item in ((window.get("owner_match") or {}).get("candidates") or ())
                if str(item.get("candidate_id") or "") == candidate_id
            ), None)
            if candidate is None:
                raise ValueError(f"Design引用未知window candidate: {candidate_id}")
            if _candidate_has_machine_identity(candidate):
                raise ValueError(
                    "复用窗口候选的公开命名包含内部身份后缀: "
                    f"{candidate_id}"
                )
            owners[owner_id] = {
                "evidence_root": root_name,
                "public_name": _page_package_name(candidate.get("page_object")),
                "root_locator": candidate.get("root_locator"),
                "page_object": candidate.get("page_object"),
                "root_locator_file": candidate.get("root_locator_file"),
                "resolution": {
                    "strategy": "reuse_existing",
                    "candidate_id": candidate_id,
                    "reason": str(selection.get("reason") or "Reuse selected owner."),
                },
                "ownership_decision": ownership_decision,
                "views": {},
            }
        elif selection.get("strategy") == "create_new":
            public_name = _public_owner_name(
                root_name,
                selection.get("business_name"),
            )
            package = (
                public_name
                if selection.get("business_name")
                else _safe_name(root_name.replace("_window_", "_"))
            )
            owners[owner_id] = {
                "evidence_root": root_name,
                "public_name": public_name,
                "root_locator": (
                    f"{public_name}_window"
                    if selection.get("business_name")
                    else root_name
                ),
                "page_object": f"Bdd/page_obj/{package}/page.py",
                "root_locator_file": f"Bdd/locators/{package}/window.yaml",
                "resolution": {
                    "strategy": "create_new",
                    "candidate_id": None,
                    "reason": str(selection.get("reason") or "Create a new owner for the recorded Root."),
                },
                "ownership_decision": ownership_decision,
                "views": {},
            }
        else:
            raise ValueError(f"window ownership strategy无效: {selection.get('strategy')}")
    for root_name in sorted(view_roots):
        if root_name not in windows:
            raise ValueError(f"Design引用未知window root: {root_name}")
        selection = selections[root_name]
        parent_root = str(selection.get("parent_root") or "").strip()
        ownership_candidate = _validated_view_ownership_candidate(
            selection,
            brief,
        )
        owner_id = _owner_id(parent_root)
        owner = owners.get(owner_id)
        if owner is None:
            raise ValueError(f"window view引用未知parent_root: {parent_root}")
        view_id = _public_owner_name(root_name, selection.get("business_name"))
        if view_id in owner.get("views", {}):
            raise ValueError(f"window owner重复view: {owner_id}.{view_id}")
        package = _page_package_name(owner.get("page_object"))
        owner.setdefault("views", {})[view_id] = {
            "evidence_root": root_name,
            "ownership_candidate_id": ownership_candidate["candidate_id"],
            "locator_file": f"Bdd/locators/{package}/{view_id}.yaml",
            "view_object": f"Bdd/page_obj/{package}/{view_id}.py",
            "active_locator": f"{view_id}_window",
            "root_locator": f"{view_id}_window",
        }
    return owners


def _validate_dismissed_ownership_candidates(
        selection,
        candidate_ids,
        root_name,
    ):
    if not candidate_ids:
        return
    if not str(selection.get("reason") or "").strip():
        raise ValueError(
            "拒绝ownership candidate并选择独立WindowPage时必须说明reason"
        )


def _compiled_window_page_ownership_decision(selection, brief):
    candidate_ids = [
        str(item).strip()
        for item in selection.get("dismissed_ownership_candidate_ids") or ()
        if str(item).strip()
    ]
    if not candidate_ids:
        return None
    candidates = {
        str(item.get("candidate_id") or ""): item
        for item in (brief.get("window_ownership") or {}).get(
            "ownership_candidates"
        ) or ()
        if item.get("candidate_id")
    }
    root_name = str(selection.get("root_name") or "")
    for candidate_id in candidate_ids:
        candidate = candidates.get(candidate_id)
        if candidate is None:
            raise ValueError(
                f"dismissed ownership candidate不存在: {candidate_id}"
            )
        if any((
            candidate.get("kind") != "child_view",
            str(candidate.get("child_root") or "") != root_name,
        )):
            raise ValueError(
                "dismissed ownership candidate与独立WindowPage root不一致: "
                f"{candidate_id}"
            )
    return {
        "selected_kind": "window_page",
        "dismissed_candidate_ids": candidate_ids,
        "reason": str(selection.get("reason") or "").strip(),
    }


def _validated_view_ownership_candidate(selection, brief):
    candidate_id = str(selection.get("ownership_candidate_id") or "").strip()
    parent_root = str(selection.get("parent_root") or "").strip()
    child_root = str(selection.get("root_name") or "").strip()
    candidates = [
        item
        for item in (brief.get("window_ownership") or {}).get(
            "ownership_candidates"
        ) or ()
        if str(item.get("candidate_id") or "") == candidate_id
    ]
    if len(candidates) != 1:
        raise ValueError(
            f"window view ownership_candidate_id无效: {candidate_id}"
        )
    candidate = candidates[0]
    if any((
        candidate.get("kind") != "child_view",
        str(candidate.get("parent_root") or "") != parent_root,
        str(candidate.get("child_root") or "") != child_root,
    )):
        raise ValueError(
            "window view ownership_candidate与parent/child root不一致"
        )
    _validate_view_candidate_actions(candidate, brief)
    return candidate


def _validate_view_candidate_actions(candidate, brief):
    step_id = str(candidate.get("step_id") or "")
    actions = {}
    action_order = {}
    for index, action in enumerate(brief.get("actions") or (), start=1):
        action_id = str(action.get("id") or action.get("action_id") or "")
        action_step_id = str(action.get("step_id") or "")
        if not action_id or not action_step_id:
            continue
        key = (action_step_id, action_id)
        if key in actions:
            raise ValueError(
                "window view ownership_candidate引用的scoped Action冲突: "
                f"{action_step_id}/{action_id}"
            )
        actions[key] = action
        action_order[key] = index
    opener_id = str(candidate.get("opener_action_id") or "")
    opener_key = (step_id, opener_id)
    opener = actions.get(opener_key)
    parent_root = str(candidate.get("parent_root") or "")
    child_root = str(candidate.get("child_root") or "")
    if opener is None:
        raise ValueError("window view ownership_candidate缺少opener Action")
    if str((opener.get("target") or {}).get("root_name") or "") != parent_root:
        raise ValueError("window view ownership_candidate opener root不一致")
    opener_order = action_order[opener_key]
    child_action_ids = [
        str(item) for item in candidate.get("child_action_ids") or () if item
    ]
    if not child_action_ids:
        raise ValueError("window view ownership_candidate缺少child Action")
    for action_id in child_action_ids:
        action_key = (step_id, action_id)
        action = actions.get(action_key)
        if action is None:
            raise ValueError("window view ownership_candidate引用未知child Action")
        if str((action.get("target") or {}).get("root_name") or "") != child_root:
            raise ValueError("window view ownership_candidate child root不一致")
        if action_order[action_key] <= opener_order:
            raise ValueError("window view ownership_candidate child顺序无效")


def _compile_operation(
        step_id,
        selection,
        actions,
        owners,
        brief,
        target_step,
        implementation_strategy,
        runtime_bindings,
    ):
    op = str(selection.get("operation") or "")
    capability = capability_by_name(op)
    if capability is None or not capability.plan_enabled:
        raise ValueError(f"Design operation不可用于Plan: {op}")
    target_action_id = str(selection.get("target_action_id") or "")
    target_action = actions.get(target_action_id)
    if target_action is None:
        raise ValueError(
            f"Design Step {step_id}引用未知target Action: {target_action_id}"
        )
    root_name = str(selection.get("window_root") or "")
    owner_id = _owner_id(root_name)
    view_owner = None
    if owner_id not in owners:
        owner_id, view_owner = _view_owner_for_root(root_name, owners)
    if owner_id not in owners:
        raise ValueError(f"Design operation引用未编译owner: {root_name}")
    capability_parameters = _compile_capability_parameters(
        step_id,
        op,
        target_action,
        brief,
    )
    (
        value,
        source,
        value_action_ids,
        value_provenance,
        parameters,
    ) = _compile_value_source(
        step_id,
        selection.get("value_source"),
        target_action_id,
        actions,
        brief,
        target_step,
        runtime_bindings,
        op,
    )
    parameters = _merge_compiled_parameters(
        step_id,
        op,
        parameters,
        capability_parameters,
    )
    if (
        capability.requires_value_action
        and value_provenance.get("kind") not in {"data_table", "runtime"}
        and value is None
    ):
        raise ValueError(f"Design operation {op}的冻结值不能为空")
    target = target_action.get("target") or {}
    locator_candidate = _compile_locator_candidate(
        target,
        (
            selection.get("locator_candidate_id")
            or target.get("locator_candidate_id")
        ),
        root_name,
    )
    operation = {
        "op": op,
        "target": str(
            selection.get("target_name")
            or target.get("locator_name")
            or ""
        ),
        "value": value,
        "source": source,
        "value_provenance": value_provenance,
        "parameters": parameters,
        "window_owner": owner_id,
        "implementation_location": (
            "page_method"
            if implementation_strategy == "page_method"
            else "step_inline_base_api"
        ),
        "target_action_id": target_action_id,
        "locator_candidate_id": (
            locator_candidate.get("candidate_id")
            if locator_candidate is not None
            else target.get("locator_candidate_id")
        ),
        "value_action_ids": value_action_ids,
        "reason": str(selection.get("reason") or "AI selected this operation."),
        "rejected_alternatives": list(
            selection.get("rejected_alternatives") or ()
        ),
        "uncertainty": selection.get("uncertainty"),
    }
    if view_owner:
        operation["view_owner"] = view_owner
    runtime_value = selection.get("runtime_value")
    if op in {"save_text", "save_attr"}:
        runtime_value = dict(runtime_value or {})
        producer = _runtime_value_key(runtime_value.get("produces"))
        if producer not in runtime_bindings:
            raise ValueError(f"runtime producer未注册: {producer}")
        operation["result_binding"] = runtime_bindings[producer]
        attribute = str(runtime_value.get("attribute") or "").strip()
        if op == "save_attr":
            if not attribute:
                raise ValueError("save_attr runtime_value缺少attribute")
            operation["parameters"]["attr_name"] = attribute
        elif attribute:
            raise ValueError("save_text runtime_value不能声明attribute")
    elif runtime_value:
        raise ValueError(
            f"只有save_text/save_attr可以声明runtime_value: {op}"
        )
    if operation["implementation_location"] == "page_method":
        resolution = dict(selection.get("method_resolution") or {})
        strategy = str(resolution.get("strategy") or "create")
        candidate_id = str(resolution.get("candidate_id") or "") or None
        if strategy in {"reuse", "modify"}:
            candidate = _implementation_candidate(brief, candidate_id)
            if candidate is None or candidate.get("kind") != "page_object_method":
                raise ValueError(
                    f"Design引用未知Page method candidate: {candidate_id}"
                )
            if candidate.get("path") != _implementation_owner_path(
                    owners[owner_id],
                    view_owner,
                ):
                raise ValueError("Design Page method candidate与window owner不一致")
            method = str(candidate.get("symbol") or "")
        elif strategy == "create":
            method_name = str(
                selection.get("method_name")
                or _method_name(op, operation.get("target"))
            )
            class_name = _implementation_class_name(
                owners[owner_id],
                view_owner,
            )
            method = f"{class_name}.{method_name}"
            candidate_id = None
        else:
            raise ValueError(f"method resolution strategy无效: {strategy}")
        operation["implementation_method"] = method
        operation["implementation_resolution"] = {
            "strategy": strategy,
            "candidate_id": candidate_id,
            "reason": str(
                resolution.get("reason")
                or "Compiled from the selected Page method strategy."
            ),
        }
    return operation


def _view_owner_for_root(root_name, owners):
    matches = []
    for owner_id, owner in owners.items():
        for view_id, view in (owner.get("views") or {}).items():
            if str(view.get("evidence_root") or "") == str(root_name):
                matches.append((owner_id, view_id))
    if len(matches) > 1:
        raise ValueError(f"window root绑定多个view_owner: {root_name}")
    return matches[0] if matches else ("", None)


def _implementation_owner_path(owner, view_owner):
    if view_owner:
        return ((owner.get("views") or {}).get(view_owner) or {}).get(
            "view_object"
        )
    return owner.get("page_object")


def _implementation_class_name(owner, view_owner):
    path = _implementation_owner_path(owner, view_owner)
    if view_owner:
        name = Path(str(path or "")).stem
        return "".join(part.capitalize() for part in name.split("_")) + "View"
    return _page_class_name(path)


def _compile_action_relationships(
        step_id,
        selections,
        actions,
        operations,
    brief,
):
    if selections is None:
        return []
    operation_positions = {
        id(operation): index
        for index, operation in enumerate(operations)
    }
    operation_actions = {}
    for operation in operations:
        action_ids = dict.fromkeys((
            operation.get("target_action_id"),
            *(operation.get("value_action_ids") or ()),
        ))
        for action_id in action_ids:
            if not action_id:
                continue
            operation_actions.setdefault(str(action_id), []).append(operation)
    result = []
    source_action_ids = set()
    relationship_ids = set()
    for selection in selections:
        kind = str(selection.get("kind") or "")
        source_action_id = str(selection.get("source_action_id") or "")
        consumer_action_id = str(selection.get("consumer_action_id") or "")
        source = actions.get(source_action_id)
        consumer = actions.get(consumer_action_id)
        if source is None or consumer is None:
            raise ValueError(
                f"Design Step {step_id} action_relationship引用未知Action"
            )
        if source_action_id == consumer_action_id:
            raise ValueError("action_relationship不能引用同一source/consumer")
        if _action_ordinal(source) >= _action_ordinal(consumer):
            raise ValueError("action_relationship source必须早于consumer")
        if source_action_id in source_action_ids:
            raise ValueError("一个Action只能声明一个action_relationship")
        relationship_id = (kind, source_action_id, consumer_action_id)
        if relationship_id in relationship_ids:
            raise ValueError("action_relationship重复")
        consumer_operations = operation_actions.get(consumer_action_id) or []
        if len(consumer_operations) != 1:
            raise ValueError(
                "action_relationship consumer必须恰好被一个operation引用"
            )
        source_operations = operation_actions.get(source_action_id) or []
        if kind in {"activation_for", "transport_for"} and len(
                source_operations
        ) != 1:
            raise ValueError(
                f"{kind} source必须保留独立operation覆盖"
            )
        if kind in {"activation_for", "transport_for"} and (
                operation_positions[id(source_operations[0])]
                >= operation_positions[id(consumer_operations[0])]
        ):
            raise ValueError(
                f"{kind} source operation顺序必须早于consumer"
            )
        if kind == "absorbed_by":
            if source_operations:
                raise ValueError(
                    "absorbed_by source不能同时被operation直接引用"
                )
            _validate_absorbed_action(
                source_action_id,
                source,
                consumer,
                consumer_operations[0],
                brief,
            )
        elif kind == "transport_for":
            _validate_transport_action(source, consumer)
        result.append({
            "kind": kind,
            "source_action_id": source_action_id,
            "consumer_action_id": consumer_action_id,
            **(
                {"reason": str(selection["reason"])}
                if str(selection.get("reason") or "").strip()
                else {}
            ),
        })
        source_action_ids.add(source_action_id)
        relationship_ids.add(relationship_id)
    return result


def _action_ordinal(action):
    value = action.get("ordinal")
    if not isinstance(value, bool) and isinstance(value, int) and value > 0:
        return value
    value = action.get("_order")
    if not isinstance(value, bool) and isinstance(value, int) and value > 0:
        return value
    raise ValueError("action_relationship缺少冻结Action顺序")


def _validate_transport_action(source, consumer):
    if not _is_auxiliary_action(source):
        raise ValueError("transport_for source必须是无语义辅助Action")
    if not _same_frozen_target(source, consumer):
        raise ValueError("transport_for必须引用同一冻结target")


def _validate_absorbed_action(
    source_action_id,
    source,
    consumer,
    operation,
    brief,
):
    if not _is_auxiliary_action(source):
        raise ValueError("absorbed_by source必须是无语义辅助Action")
    if not _same_frozen_target(source, consumer):
        raise ValueError("absorbed_by必须引用同一冻结target")
    if str(operation.get("op") or "").startswith("assert_"):
        raise ValueError("absorbed_by不能覆盖assertion关系")
    if _action_has_decision_constraint(source_action_id, brief):
        raise ValueError("absorbed_by不能覆盖Decision约束Action")


def _action_has_decision_constraint(action_id, brief):
    return any(
        str(action_id) in {
            str(item) for item in ambiguity.get("action_ids") or []
        }
        for ambiguity in brief.get("ambiguities") or []
    )


def _is_auxiliary_action(action):
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


def _same_frozen_target(source, consumer):
    source_target = source.get("target") or {}
    consumer_target = consumer.get("target") or {}
    return bool(
        source_target.get("target_fingerprint")
        and source_target.get("target_fingerprint") == consumer_target.get(
            "target_fingerprint"
        )
        and source_target.get("root_name") == consumer_target.get("root_name")
    )


def _compile_locator_candidate(target, candidate_id, root_name):
    candidate_id = str(candidate_id or "").strip()
    locator = target.get("locator") or {}
    current_candidate_protocol = "locator_candidates" in target
    structural_target = locator.get("by", "child") in {"child", "xpath"}
    if not candidate_id:
        if current_candidate_protocol and structural_target:
            raise ValueError(
                "当前结构locator缺少冻结locator candidate ID"
            )
        return None
    matches = [
        item
        for item in target.get("locator_candidates") or ()
        if isinstance(item, dict)
        and str(item.get("candidate_id") or "") == candidate_id
    ]
    if len(matches) != 1:
        raise ValueError(
            f"Design引用未知或冲突locator candidate: {candidate_id}"
        )
    candidate = matches[0]
    validation = candidate.get("validation") or {}
    locator = candidate.get("locator") or {}
    if any((
        candidate_id != expected_locator_candidate_id(
            locator,
            candidate.get("reason"),
        ),
        validation.get("status") != "unique",
        validation.get("target_matches") is not True,
        str(locator.get("root") or "") != str(root_name or ""),
        locator.get("by", "child") not in {"child", "xpath"},
    )):
        raise ValueError(
            f"locator candidate未通过唯一目标或Root验证: {candidate_id}"
        )
    return candidate


def _compile_value_source(
        step_id,
        source,
        target_action_id,
        actions,
        brief,
        target_step,
        runtime_bindings,
        operation,
    ):
    source = dict(source or {})
    kind = str(source.get("kind") or "")
    if not kind:
        return None, None, [], {}, {}
    if kind not in VALUE_SOURCE_KINDS:
        raise ValueError(f"未知Design value source: {kind}")
    if kind == "recorded_action":
        action_id = str(source.get("action_id") or target_action_id)
        if action_id not in actions:
            raise ValueError(
                f"Design Step {step_id}值来源引用未知Action: {action_id}"
            )
        value = resolve_recorded_action_value(
            brief,
            step_id,
            action_id,
            operation,
        )
        if value is None:
            raise ValueError(
                f"Design Step {step_id} recorded Action缺少冻结值: {action_id}"
            )
        return value, "recorded_action", [action_id], {
            "kind": "recorded_action",
            "action_id": action_id,
            "step_id": step_id,
        }, {
            "value": value
        }
    if kind == "semantic_literal":
        value = source.get("value")
        return value, "literal", [], {
            "kind": kind,
            "step_id": step_id,
        }, {"value": value}
    if kind == "feature_literal":
        reference = str(source.get("reference") or "")
        value = (
            resolve_declared_feature_literal(
                brief,
                step_id,
                reference,
                source["value"],
            )
            if "value" in source
            else resolve_feature_literal(brief, step_id, reference)
        )
        return value, "literal", [], {
            "kind": kind,
            "reference": reference,
            "literal": value,
            "step_id": step_id,
        }, {
            "expected": value,
            "expected_source": reference,
        }
    if kind == "examples":
        reference = str(source.get("reference") or "")
        values = ((brief.get("target") or {}).get("scenario") or {}).get(
            "example_values",
            {},
        )
        if (
            reference not in values
            or reference not in declared_example_arguments(brief, step_id)
        ):
            raise ValueError(
                f"Design Step {step_id}值来源不可解析: examples.{reference}"
            )
        value = values[reference]
        return value, f"examples.{reference}", [], {
            "kind": "examples",
            "reference": reference,
            "step_id": step_id,
        }, {
            "expected": value,
            "expected_source": f"examples.{reference}",
        }
    if kind == "data_table":
        reference = str(source.get("reference") or "")
        headings = list((target_step.get("table") or {}).get("headings") or ())
        if reference not in headings:
            raise ValueError(
                f"Design Step {step_id}值来源不可解析: table.{reference}"
            )
        return None, f"table.{reference}", [], {
            "kind": "data_table",
            "reference": reference,
            "step_id": step_id,
        }, {
            "expected_source": f"table.{reference}",
        }
    if kind == "runtime":
        producer = _runtime_value_key(source.get("producer"))
        if not producer:
            raise ValueError("runtime value source缺少producer")
        binding = runtime_bindings.get(producer)
        if binding is None:
            raise ValueError(f"runtime value source引用未知producer: {producer}")
        return None, f"runtime.{binding}", [], {
            "kind": "runtime",
            "binding": binding,
            "step_id": step_id,
        }, {
            "argument": source.get("argument")
        }
    raise ValueError(f"暂不支持的Design value source: {kind}")


def _compile_capability_parameters(step_id, operation, action, brief):
    capability = capability_by_name(operation)
    profile = capability.plan_validation_profile if capability else None
    action_parameters = dict(action.get("parameters") or {})
    if profile == "frozen_click_offset":
        if not action_parameters:
            return {}
        parameters = {
            "offset_x": action_parameters.get("offset_x"),
            "offset_y": action_parameters.get("offset_y"),
        }
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in parameters.values()
        ):
            raise ValueError(f"Step {step_id} 缺少冻结click偏移")
        return parameters
    if profile == "frozen_scroll":
        parameters = {
            "direction": action_parameters.get("direction"),
            "steps": action_parameters.get("steps"),
        }
        if any((
            parameters["direction"] not in {"up", "down", "left", "right"},
            isinstance(parameters["steps"], bool),
            not isinstance(parameters["steps"], int),
            (parameters["steps"] or 0) <= 0,
        )):
            raise ValueError(f"Step {step_id} 缺少冻结scroll参数")
        return parameters
    if profile == "frozen_drag":
        parameters = {
            "delta_x": action_parameters.get("delta_x"),
            "delta_y": action_parameters.get("delta_y"),
        }
        if (
            any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in parameters.values()
            )
            or not any(parameters.values())
        ):
            raise ValueError(f"Step {step_id} 缺少冻结drag参数")
        return parameters
    if profile == "semantic_control_value" and operation == "set_slider_value":
        after_state = (
            ((action.get("semantics") or {}).get("effect") or {}).get(
                "after_state"
            )
            or {}
        )
        parameters = {
            "expected_minimum": after_state.get("range_minimum"),
            "expected_maximum": after_state.get("range_maximum"),
        }
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            for value in parameters.values()
        ):
            raise ValueError(f"Step {step_id} 缺少冻结Slider范围")
        return parameters
    if profile in {"collection_assertion", "ocr_assertion"}:
        parameters = resolve_implementation_parameters(
            brief,
            step_id,
            action.get("id"),
            operation,
        )
        if parameters is None:
            raise ValueError(
                f"Step {step_id} 操作 {operation} 缺少冻结实现参数"
            )
        return parameters
    return {}


def _merge_compiled_parameters(step_id, operation, value, frozen):
    value = dict(value or {})
    frozen = dict(frozen or {})
    for key in set(value) & set(frozen):
        if value[key] != frozen[key]:
            raise ValueError(
                f"Step {step_id} 操作 {operation} 的值来源与冻结参数不一致: {key}"
            )
    if "value" in value and "expected" in frozen:
        if value["value"] != frozen["expected"]:
            raise ValueError(
                f"Step {step_id} 操作 {operation} 的值与冻结expected不一致"
            )
        value.pop("value")
    return {**value, **frozen}


def _compile_runtime_bindings(design):
    bindings = {}
    binding_owners = {}
    producer_positions = {}
    operations = [
        operation
        for step in design.get("steps") or ()
        for operation in step.get("operations") or ()
    ]
    for position, operation in enumerate(operations):
        runtime_value = operation.get("runtime_value")
        op = str(operation.get("operation") or "")
        if not runtime_value:
            if op in {"save_text", "save_attr"}:
                raise ValueError(f"{op}缺少runtime_value.produces")
            continue
        if op not in {"save_text", "save_attr"}:
            raise ValueError(
                f"只有save_text/save_attr可以声明runtime_value: {op}"
            )
        if not isinstance(runtime_value, dict):
            raise ValueError("runtime_value必须是object")
        producer = _runtime_value_key(runtime_value.get("produces"))
        if not producer:
            raise ValueError(f"{op}缺少runtime_value.produces")
        if producer in bindings:
            raise ValueError(f"重复runtime producer: {producer}")
        binding = _runtime_binding_name(producer)
        if binding in binding_owners:
            raise ValueError(
                "runtime producer编译为重复binding: "
                f"{producer}/{binding_owners[binding]}"
            )
        bindings[producer] = binding
        binding_owners[binding] = producer
        producer_positions[producer] = position
    consumed = set()
    for position, operation in enumerate(operations):
        source = operation.get("value_source")
        if not isinstance(source, dict) or source.get("kind") != "runtime":
            continue
        producer = _runtime_value_key(source.get("producer"))
        if not producer:
            raise ValueError("runtime value source缺少producer")
        if producer not in bindings:
            raise ValueError(f"runtime value source引用未知producer: {producer}")
        if producer_positions[producer] >= position:
            raise ValueError(f"runtime consumer早于producer: {producer}")
        consumed.add(producer)
    unconsumed = sorted(set(bindings) - consumed)
    if unconsumed:
        raise ValueError(f"runtime producer没有consumer: {unconsumed}")
    return bindings


def _runtime_value_key(value):
    return " ".join(str(value or "").split()).casefold()


def _runtime_binding_name(value):
    raw = _runtime_value_key(value)
    slug = _safe_name(raw)
    if slug[0].isdigit():
        slug = f"value_{slug}"
    if raw == slug and len(slug) <= 64:
        return slug
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    return f"{slug[:53]}_{digest}"


def _compile_scenario_model(design, brief, target_steps, actions_by_step):
    design_steps = {
        str(item["step_id"]): item
        for item in design.get("steps") or ()
    }
    concepts = []
    concept_steps = {}
    for target in target_steps:
        step_id = str(target["id"])
        selected = design_steps[step_id]
        for relationship in ("consumes", "produces", "observes"):
            for concept in selected.get(relationship) or ():
                name = _concept_name(concept)
                if name not in concepts:
                    concepts.append(name)
                concept_steps.setdefault(name, []).append(
                    (step_id, relationship)
                )
    if not concepts:
        concepts = [
            f"step_{index}_outcome"
            for index, _target in enumerate(target_steps, start=1)
        ]
        for index, target in enumerate(target_steps):
            step_id = str(target["id"])
            relationship = (
                "observes"
                if str(target.get("semantic_type") or "").casefold() == "then"
                else "produces"
            )
            concept_steps.setdefault(concepts[index], []).append(
                (step_id, relationship)
            )
    concept_ids = {
        concept: f"state-{_safe_name(concept)}"
        for concept in concepts
    }
    states = []
    for concept in concepts:
        references = [
            f"step:{step_id}"
            for step_id, _relationship in concept_steps.get(concept, ())
        ]
        states.append({
            "state_id": concept_ids[concept],
            "name": concept.replace("_", " "),
            "kind": (
                "outcome"
                if any(
                    relationship == "observes"
                    for _step_id, relationship in concept_steps.get(
                        concept,
                        (),
                    )
                )
                else "business_state"
            ),
            "support": [{
                "authority": "feature_declared",
                "references": list(dict.fromkeys(references)),
            }],
        })
    steps = []
    for target in target_steps:
        step_id = str(target["id"])
        selected = design_steps[step_id]
        role = {
            "given": "precondition",
            "when": "business_action",
            "then": "business_assertion",
        }.get(str(target.get("semantic_type") or target.get("keyword") or "").casefold())
        steps.append({
            "step_id": step_id,
            "role": role,
            "consumes": [
                concept_ids[_concept_name(item)]
                for item in selected.get("consumes") or ()
            ],
            "produces": [
                concept_ids[_concept_name(item)]
                for item in selected.get("produces") or ()
            ],
            "observes": [
                concept_ids[_concept_name(item)]
                for item in selected.get("observes") or ()
            ],
            "reason": str(
                selected.get("intent")
                or "Compiled from the Step design."
            ),
            "support": [{
                "authority": "feature_declared",
                "references": [f"step:{step_id}"],
            }],
        })
    transitions = []
    step_positions = {
        str(target["id"]): index
        for index, target in enumerate(target_steps)
    }
    for concept, uses in concept_steps.items():
        producers = [
            step_id for step_id, relationship in uses
            if relationship == "produces"
        ]
        consumers = [
            step_id for step_id, relationship in uses
            if relationship in {"consumes", "observes"}
        ]
        for producer in producers:
            for consumer in consumers:
                if step_positions[producer] >= step_positions[consumer]:
                    continue
                transitions.append({
                    "from_step_id": producer,
                    "to_step_id": consumer,
                    "state_ids": [concept_ids[concept]],
                    "reason": f"The {concept} concept flows forward.",
                    "support": [{
                        "authority": "feature_declared",
                        "references": [
                            f"scenario:{(brief.get('target') or {}).get('scenario', {}).get('id')}"
                        ],
                    }],
                })
    return {
        "model_version": "1.0",
        "summary": str(design.get("scenario_intent") or design.get("summary")),
        "states": states,
        "steps": steps,
        "transitions": transitions,
    }


def _compile_ambiguities(design, brief):
    selections = {
        str(item.get("ambiguity_id") or ""): item
        for item in design.get("ambiguity_choices") or ()
        if isinstance(item, dict) and item.get("ambiguity_id")
    }
    ambiguity_ids = {
        str(item.get("ambiguity_id") or "")
        for item in brief.get("ambiguities") or ()
        if item.get("ambiguity_id")
    }
    unknown = sorted(set(selections) - ambiguity_ids)
    if unknown:
        raise ValueError(f"Design引用未知ambiguity: {unknown}")
    result = []
    for ambiguity in brief.get("ambiguities") or ():
        ambiguity_id = str(ambiguity.get("ambiguity_id") or "")
        selected = selections.get(ambiguity_id)
        routing = str(ambiguity.get("routing") or "")
        allowed_items = list(ambiguity.get("allowed_outcomes") or ())
        has_user_outcome = any(
            item.get("authority") == "user" for item in allowed_items
        )
        ai_outcomes = {
            str(item.get("outcome") or ""): item
            for item in allowed_items
            if item.get("authority") == "ai"
        }
        if routing == "user_decision_required":
            if selected is not None:
                raise ValueError(
                    f"Design不能回答用户权威ambiguity: {ambiguity_id}"
                )
            continue
        if selected is None:
            if ai_outcomes and not has_user_outcome:
                raise ValueError(
                    f"Design缺少ambiguity choice: {ambiguity_id}"
                )
            continue
        outcome = str(selected.get("outcome") or "")
        if outcome not in ai_outcomes:
            raise ValueError(f"Design ambiguity outcome无效: {ambiguity_id}/{outcome}")
        result.append({
            "ambiguity_id": ambiguity_id,
            "outcome": outcome,
            "action_ids": list(ambiguity.get("action_ids") or ()),
            "evidence_ids": list(ambiguity.get("evidence_ids") or ()),
            "candidate_id": selected.get("candidate_id"),
            "decision_ids": [],
            "reason": str(selected.get("reason") or "AI selected this allowed outcome."),
        })
    return result


def _compile_unresolved_issues(design, brief):
    ambiguities = {
        str(item.get("ambiguity_id") or ""): item
        for item in brief.get("ambiguities") or ()
        if isinstance(item, dict) and item.get("ambiguity_id")
    }
    selections = {
        str(item.get("ambiguity_id") or ""): item
        for item in design.get("ambiguity_choices") or ()
        if isinstance(item, dict) and item.get("ambiguity_id")
    }
    result = {}
    actions_by_step = {}
    for action in brief.get("actions") or ():
        step_id = str(action.get("step_id") or "")
        action_id = str(action.get("id") or "")
        if step_id and action_id and action.get("role") != "noise":
            actions_by_step.setdefault(step_id, []).append(action_id)
    for ambiguity_id, selection in selections.items():
        if selection.get("outcome") != "generate_issue_placeholder":
            continue
        ambiguity = ambiguities.get(ambiguity_id) or {}
        allowed = next((
            item
            for item in ambiguity.get("allowed_outcomes") or ()
            if item.get("outcome") == "generate_issue_placeholder"
            and item.get("authority") == "ai"
            and item.get("effect") == "issue_placeholder"
        ), None)
        if allowed is None:
            raise ValueError(
                f"Design ambiguity outcome无效: "
                f"{ambiguity_id}/generate_issue_placeholder"
            )
        step_id = str(ambiguity.get("step_id") or "")
        if not step_id:
            raise ValueError(
                f"placeholder ambiguity缺少step_id: {ambiguity_id}"
            )
        issue_id = "generation-issue-" + hashlib.sha256(
            f"{brief.get('request_id')}:{step_id}:{ambiguity_id}".encode(
                "utf-8"
            )
        ).hexdigest()[:16]
        result.setdefault(step_id, []).append({
            "issue_id": issue_id,
            "issue_type": str(
                ambiguity.get("code") or "unresolved_generation"
            ),
            "step_id": step_id,
            "ambiguity_id": ambiguity_id,
            "action_ids": list(actions_by_step.get(step_id) or ()),
            "source_action_ids": list(ambiguity.get("action_ids") or ()),
            "evidence_ids": list(ambiguity.get("evidence_ids") or ()),
            "reason": str(selection.get("reason") or "").strip(),
        })
    return result


def _compile_step_behavior(
        selected,
        brief,
        step_id,
        step_file,
        actions,
        target_step,
    ):
    value = dict(selected.get("step_behavior") or {})
    strategy = str(value.get("strategy") or "create")
    reason = str(
        value.get("reason")
        or "Compiled from the AI Step behavior strategy."
    )
    exact_candidates = _exact_step_behavior_candidates(
        brief,
        target_step,
        step_file,
    )
    if strategy == "create":
        if exact_candidates:
            raise ValueError(
                "当前Step范围已存在精确匹配定义；行为不复用时必须"
                "选择绑定候选的modify"
            )
        return {
            "strategy": strategy,
            "resolution": {
                "strategy": "create",
                "candidate_id": None,
                "reason": reason,
            },
        }
    if strategy not in {"reuse", "modify"}:
        raise ValueError(f"step behavior strategy无效: {strategy}")
    candidate_id = str(value.get("candidate_id") or "")
    candidate = _implementation_candidate(brief, candidate_id)
    if candidate is None or candidate.get("kind") != "step_definition":
        raise ValueError(f"Design引用未知Step candidate: {candidate_id}")
    scope = (
        (((brief.get("target") or {}).get("scenario") or {}).get(
            "step_scope_binding"
        ) or {}).get("resolved_step_scope")
        or {}
    )
    visible_files = {
        str(path).replace("\\", "/")
        for path in scope.get("files") or ()
    }
    candidate_path = str(candidate.get("path") or "").replace("\\", "/")
    if visible_files and candidate_path not in visible_files:
        raise ValueError(
            f"Design Step candidate不在当前Step scope: {candidate_id}"
        )
    if strategy == "modify" and candidate_path != str(step_file).replace(
            "\\", "/"
    ):
        raise ValueError(
            f"Design不能修改当前Step所属文件之外的定义: {candidate_id}"
        )
    exact_candidate_ids = {
            str(item.get("candidate_id") or "")
            for item in exact_candidates
    }
    if strategy == "modify" and candidate_id not in exact_candidate_ids:
        raise ValueError(
            "当前Step范围的modify必须绑定精确匹配的Step candidate: "
            f"{candidate_id}"
        )
    if strategy == "reuse":
        matched_contract = _matched_step_pattern_contract(
            candidate,
            target_step,
        )
        mappings = _compile_step_behavior_action_mappings(
            value.get("action_mappings"),
            candidate,
            actions,
            step_id,
            brief,
            target_step,
        )
        return {
            "strategy": strategy,
            "step": {
                "behavior_owner": "existing_step_definition",
                "behavior_file": candidate.get("path"),
                "behavior_resolution": {
                    "strategy": "reuse",
                    "candidate_id": candidate_id,
                    "reason": reason,
                    "step_decorator": matched_contract["decorator"],
                    "step_pattern": matched_contract["pattern"],
                    "action_mappings": mappings,
                },
                "covered_action_ids": [
                    item["action_id"] for item in mappings
                ],
                "page_object": None,
                "locator_file": None,
                "data_file": None,
                "operations": [],
                "locators": [],
                "ignored_action_ids": [],
            },
        }
    matched_contract = _matched_step_pattern_contract(candidate, target_step)
    return {
        "strategy": strategy,
        "resolution": {
            "strategy": "modify",
            "candidate_id": candidate_id,
            "reason": reason,
            "symbol": candidate.get("symbol"),
            "step_decorator": matched_contract["decorator"],
            "step_pattern": matched_contract["pattern"],
        },
        "behavior_file": candidate.get("path") or step_file,
    }


def _exact_step_behavior_candidates(brief, target_step, step_file):
    step_text = str((target_step or {}).get("text") or "")
    if not step_text:
        return []
    scope = (
        (((brief.get("target") or {}).get("scenario") or {}).get(
            "step_scope_binding"
        ) or {}).get("resolved_step_scope")
        or {}
    )
    visible_files = {
        str(path).replace("\\", "/")
        for path in scope.get("files") or ()
    }
    current_file = str(step_file or "").replace("\\", "/")
    result = []
    for candidate in (
            (brief.get("semantics") or {}).get("reuse_candidates") or ()
    ):
        if not isinstance(candidate, dict) or candidate.get("kind") != (
                "step_definition"
        ):
            continue
        candidate_path = str(candidate.get("path") or "").replace(
            "\\", "/"
        )
        if candidate_path != current_file or (
                visible_files and candidate_path not in visible_files
        ):
            continue
        if len(_matching_step_pattern_contracts(
            candidate,
            target_step,
        )) != 1:
            continue
        if step_text not in {
                str(item) for item in candidate.get("matched_step_texts") or ()
        }:
            continue
        if "exact_step_pattern" not in (
                candidate.get("reasons") or ()
        ):
            continue
        result.append(candidate)
    return result


def _matching_step_pattern_contracts(candidate, target_step):
    return [
        {
            "decorator": str(contract.get("decorator") or "").casefold(),
            "pattern": str(contract.get("pattern") or ""),
        }
        for contract in candidate_step_pattern_contracts(candidate)
        if step_pattern_contract_matches(contract, target_step)
    ]


def _matched_step_pattern_contract(candidate, target_step):
    contracts = _matching_step_pattern_contracts(candidate, target_step)
    if len(contracts) != 1:
        raise ValueError("Step candidate的匹配 Gherkin decorator/pattern不唯一")
    return contracts[0]


def _compile_step_behavior_action_mappings(
        mappings,
        candidate,
        actions,
        step_id,
    brief,
    target_step,
    ):
    sequence = list(candidate.get("call_sequence") or ())
    if not sequence:
        raise ValueError(
            "step_behavior reuse candidate缺少冻结call_sequence"
        )
    mappings = [dict(item) for item in mappings or ()]
    action_ids = set(actions)
    mapped_ids = [str(item.get("action_id") or "") for item in mappings]
    if set(mapped_ids) != action_ids or len(mapped_ids) != len(set(mapped_ids)):
        raise ValueError(
            f"Step {step_id} action_mappings必须精确覆盖当前Action"
        )
    call_indexes = [item.get("call_index") for item in mappings]
    if call_indexes != list(range(len(sequence))):
        raise ValueError(
            "step_behavior action_mappings必须按冻结call_sequence顺序完整映射"
        )
    result = []
    for mapping in mappings:
        action_id = str(mapping["action_id"])
        action = actions[action_id]
        call = sequence[mapping["call_index"]]
        operation = str(mapping["operation"])
        target = str(mapping["target"])
        if operation != str(call.get("operation") or ""):
            raise ValueError(
                f"Step {step_id} action {action_id} 映射操作不一致"
            )
        if target != str(call.get("target") or ""):
            raise ValueError(
                f"Step {step_id} action {action_id} 映射目标不一致"
            )
        action_target = action.get("target") or {}
        if target != str(action_target.get("locator_name") or ""):
            raise ValueError(
                f"Step {step_id} action {action_id} 映射目标与冻结Action不一致"
            )
        compatibility = operation_compatibility(operation, action)
        if compatibility["status"] == "incompatible":
            raise ValueError(
                f"Step {step_id} action {action_id} 映射操作与冻结Action不兼容"
            )
        expected_value = call.get("value")
        value_source = mapping.get("value_source")
        value_provenance = None
        if expected_value is not None:
            value, _source, _ids, value_provenance, _parameters = (
                _compile_value_source(
                    step_id,
                    value_source,
                    action_id,
                    actions,
                    brief,
                    target_step,
                    {},
                    operation,
                )
            )
            if value != expected_value:
                raise ValueError(
                    f"Step {step_id} action {action_id} 映射值与冻结调用不一致"
                )
        elif call.get("value_parameter") and value_source is None:
            raise ValueError(
                f"Step {step_id} action {action_id} 映射缺少value_source"
            )
        elif call.get("value_parameter"):
            parameter = str(call.get("value_parameter") or "")
            if parameter not in set(candidate.get("step_parameters") or ()):
                raise ValueError(
                    f"Step {step_id} action {action_id} 映射引用非Step参数"
                )
            if not _candidate_binds_value_parameter(
                    candidate,
                    target_step,
                    parameter,
            ):
                raise ValueError(
                    f"Step {step_id} action {action_id} 映射的value_parameter"
                    "未由匹配的 decorator 参数绑定"
                )
            _value, _source, _ids, value_provenance, _parameters = (
                _compile_value_source(
                    step_id,
                    value_source,
                    action_id,
                    actions,
                    brief,
                    target_step,
                    {},
                    operation,
                )
            )
        result.append({
            "action_id": action_id,
            "call_index": mapping["call_index"],
            "operation": operation,
            "target": target,
            "value_provenance": value_provenance,
        })
    return result


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


def _compile_table_use(step_id, value, table):
    table = table if isinstance(table, dict) and table else None
    if table is None:
        if value:
            raise ValueError(
                f"Design Step {step_id}没有Data Table，不能声明table_use"
            )
        return None
    if not isinstance(value, dict) or not value:
        raise ValueError(f"Design Step {step_id}缺少table_use")

    relationship = str(value.get("relationship") or "")
    relationship_values = {
        "independent_rows": ("each_row", True),
        "continuous_rows": ("each_row", False),
        "whole_table": ("whole_table", None),
        "scenario_state": ("scenario_state", None),
    }
    if relationship not in relationship_values:
        raise ValueError(
            f"Design Step {step_id} table relationship无效: {relationship}"
        )
    execution_owner = str(value.get("execution_owner") or "")
    owner_values = {
        "page": "page_object",
        "scenario": "scenario_context",
        "step": "step_definition",
    }
    if execution_owner not in owner_values:
        raise ValueError(
            f"Design Step {step_id} table execution owner无效: "
            f"{execution_owner}"
        )
    if relationship == "scenario_state" and execution_owner != "scenario":
        raise ValueError("scenario_state table必须由scenario持有")
    if relationship != "scenario_state" and execution_owner == "scenario":
        raise ValueError("只有scenario_state table可以由scenario持有")
    if not isinstance(value.get("order_matters"), bool):
        raise ValueError(
            f"Design Step {step_id} table order_matters必须是boolean"
        )
    state_name = str(value.get("state_name") or "").strip()
    context_key = _safe_name(state_name) if state_name else None
    if relationship == "scenario_state" and not context_key:
        raise ValueError("scenario_state table缺少state_name")
    if relationship != "scenario_state" and state_name:
        raise ValueError("只有scenario_state table可以声明state_name")

    consumption, reset_between_rows = relationship_values[relationship]
    usage = {
        "consumption": consumption,
        "shape": str(value.get("data_shape") or ""),
        "consumer": owner_values[execution_owner],
        "ordered": value["order_matters"],
        "reset_between_rows": reset_between_rows,
        "columns": {
            str(column): str(meaning)
            for column, meaning in dict(
                value.get("column_meanings") or {}
            ).items()
        },
        "context_key": context_key,
        "reason": str(value.get("reason") or "").strip(),
    }
    errors = validate_table_usage(step_id, usage, table, required=True)
    if errors:
        raise ValueError(f"Design table_use无效: {errors}")
    return usage


def _implementation_candidate(brief, candidate_id):
    candidate_id = str(candidate_id or "")
    candidates = list((brief.get("semantics") or {}).get("reuse_candidates") or ())
    for window in (brief.get("window_ownership") or {}).get("windows") or ():
        for owner in (window.get("owner_match") or {}).get("candidates") or ():
            candidates.extend(owner.get("method_candidates") or ())
    matches = [
        item for item in candidates
        if str(item.get("candidate_id") or "") == candidate_id
    ]
    identities = {
        (
            str(item.get("kind") or ""),
            str(item.get("path") or ""),
            str(item.get("symbol") or ""),
            str(item.get("file_sha256") or ""),
        )
        for item in matches
    }
    if len(identities) > 1:
        raise ValueError(
            f"Brief包含冲突implementation candidate: {candidate_id}"
        )
    return matches[0] if matches else None


def _compile_step_locators(operations, actions, owners):
    result = []
    seen = {}
    for operation in operations:
        owner = owners[operation["window_owner"]]
        action = actions[operation["target_action_id"]]
        action_root = str((action.get("target") or {}).get("root_name") or "")
        view_owner = operation.get("view_owner")
        view = (owner.get("views") or {}).get(view_owner) or {}
        root_name = str(
            view.get("active_locator")
            if view_owner and action_root != owner.get("evidence_root")
            else owner["root_locator"]
        )
        if root_name not in seen:
            item = {"name": root_name, "kind": "top_level"}
            if action_root and action_root != root_name:
                item["evidence_name"] = action_root
            result.append(item)
            seen[root_name] = None
        evidence_name = str((action.get("target") or {}).get("locator_name") or "")
        target_name = str(operation.get("target") or evidence_name)
        candidate_id = str(operation.get("locator_candidate_id") or "")
        if target_name in seen and seen[target_name] != candidate_id:
            raise ValueError(
                f"同一locator名称选择了冲突候选: {target_name}"
            )
        if target_name not in seen:
            item = {"name": target_name, "kind": "child"}
            if target_name != evidence_name:
                item["evidence_name"] = evidence_name
            if candidate_id:
                item["locator_candidate_id"] = candidate_id
            result.append(item)
            seen[target_name] = candidate_id
    return result


def _step_file_from_brief(brief, *, step_id=None):
    binding = (
        (((brief.get("target") or {}).get("scenario") or {}).get(
            "step_scope_binding"
        ) or {}).get("resolved_step_scope")
        or {}
    )
    bound_file = str(binding.get("entry_file") or "")
    step_files = binding.get("step_behavior_files") or {}
    if step_id and step_files.get(step_id):
        return str(step_files[step_id])
    if bound_file:
        return bound_file
    tags = list(((brief.get("target") or {}).get("feature") or {}).get("tags") or ())
    value = next((
        tag.split(":", 1)[1]
        for tag in tags
        if str(tag).startswith(("stepfile:", "step_file:", "steps:", "step:"))
    ), None)
    if value:
        value = str(value).replace(".", "/").strip("/")
        if not value.endswith(".py"):
            value += ".py"
        return f"Bdd/steps/{value}"
    feature_path = str(((brief.get("target") or {}).get("feature") or {}).get("source_relpath") or "feature.feature")
    return f"Bdd/steps/{_safe_name(feature_path.rsplit('/', 1)[-1].rsplit('.', 1)[0])}_step.py"


def _owner_id(root_name):
    return f"owner-{_safe_name(root_name)}"


def _safe_name(value):
    value = re.sub(r"[^0-9A-Za-z_]+", "_", str(value)).strip("_").lower()
    return re.sub(r"_+", "_", value) or "generated"


def _public_owner_name(root_name, business_name=None):
    value = str(business_name or "").strip()
    if not value:
        return _safe_name(str(root_name).replace("_window_", "_"))
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", value):
        raise ValueError(
            "window business_name必须是2-64位ASCII snake_case: "
            f"{value}"
        )
    if value.endswith(("_window", "_page")):
        raise ValueError(
            "window business_name只表达业务名称，不能包含技术后缀: "
            f"{value}"
        )
    if _has_machine_identity_suffix(value):
        raise ValueError(
            "window business_name不能包含内部身份后缀: "
            f"{value}"
        )
    return value


def _has_machine_identity_suffix(value):
    return bool(re.search(r"(?:_|-)[0-9a-f]{6,16}$", str(value), re.I))


def _page_package_name(path):
    parts = str(path or "").replace("\\", "/").split("/")
    return _safe_name(parts[-2]) if len(parts) >= 2 else "generated"


def _candidate_has_machine_identity(candidate):
    values = [
        candidate.get("root_locator"),
        _page_package_name(candidate.get("page_object")),
        Path(str(candidate.get("root_locator_file") or "")).parent.name,
    ]
    return any(_has_machine_identity_suffix(value) for value in values)


def _method_name(operation, target=None):
    suffix = "_" + _safe_name(target) if target else ""
    return f"generated_{_safe_name(operation)}{suffix}"


def _page_class_name(path):
    package = str(path).replace("\\", "/").split("/")[-2]
    return "".join(part.capitalize() for part in package.split("_")) + "Page"


def _concept_name(value):
    if isinstance(value, dict):
        value = value.get("name")
    name = _safe_name(value)
    if not name:
        raise ValueError("GenerationDesign concept不能为空")
    return name


def _table_value(table, reference):
    table = table if isinstance(table, dict) else {}
    headings = list(table.get("headings") or ())
    if reference not in headings:
        return None
    column = headings.index(reference)
    rows = list(table.get("rows") or ())
    if len(rows) != 1 or column >= len(rows[0]):
        return None
    return rows[0][column]
