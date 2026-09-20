from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path

from autowork_core.page import (
    BasePage,
    WindowPage,
    WindowView,
    get_page,
    get_script_page,
)
from autowork_core.utils.debug_tools.recorder.models import SCHEMA_VERSION
from autowork_core.utils.debug_tools.recorder.ai_capability_registry import (
    AI_CAPABILITY_REGISTRY_VERSION,
    capability_by_name,
    contract_api_groups,
    debug_api_names,
    plan_operation_names,
    validate_base_page_action_classification,
)
from autowork_core.utils.debug_tools.recorder.ai_context_envelope import (
    compact_ai_context_envelope_contract,
)
from autowork_core.utils.debug_tools.recorder.generation_design import (
    GENERATION_AMBIGUITY_CHOICE_PATCH_VERSION,
    GENERATION_ASSERTION_CHOICE_PATCH_VERSION,
    GENERATION_METHOD_CHOICE_PATCH_VERSION,
    GENERATION_NAMING_PATCH_VERSION,
    GENERATION_OPERATION_CHOICE_PATCH_VERSION,
    GENERATION_VALUE_SOURCE_CHOICE_PATCH_VERSION,
    compact_generation_design_contract,
)
from autowork_core.utils.debug_tools.recorder.implementation_manifest import (
    compact_implementation_manifest_contract,
)
from autowork_core.utils.debug_tools.recorder.implementation_materializer import (
    MATERIALIZATION_CANDIDATE_VERSION,
)
from autowork_core.utils.debug_tools.recorder.generation_task_bundle import (
    GENERATION_TASK_BUNDLE_VERSION,
    GENERATION_TASK_FRAGMENT_VERSION,
    MAX_ACTIONS_PER_FRAGMENT,
    MAX_BUNDLE_INDEX_BYTES,
    MAX_FRAGMENT_BYTES,
)
from autowork_core.utils.debug_tools.recorder.generation_diff import (
    GENERATION_DIFF_VERSION,
    GENERATION_DIFF_SUMMARY_VERSION,
)
from autowork_core.utils.debug_tools.recorder.generation_capsule import (
    GENERATION_CAPSULE_VERSION,
)
from autowork_core.utils.debug_tools.recorder.generation_job import (
    GENERATION_JOB_LEASE_VERSION,
    GENERATION_JOB_VERSION,
)
from autowork_core.utils.debug_tools.recorder.generation_job_result import (
    GENERATION_JOB_RESULT_VERSION,
)
from autowork_core.utils.debug_tools.recorder.generation_profile import (
    GENERATION_PROFILE_REGISTRY_VERSION,
    GENERATION_PROFILE_VERSION,
)
from autowork_core.utils.debug_tools.recorder.transaction_integrity import (
    TRANSACTION_VERSION,
)
from autowork_core.utils.debug_tools.recorder.writer import write_json_atomic


GENERATION_CONTRACT_VERSION = "6.96"
FRAMEWORK_CONTRACT_VERSION = "3.7"
GENERATION_CONTRACT_LEASE_VERSION = "1.0"

ALLOWED_BASE_PAGE_APIS = contract_api_groups()
DEBUG_ONLY_BASE_PAGE_APIS = debug_api_names()


def build_generation_contract(manifest):
    framework_contract = _framework_contract()
    design_contract = compact_generation_design_contract()
    implementation_contract = compact_implementation_manifest_contract()
    envelope_contract = compact_ai_context_envelope_contract()
    contract = {
        "schema_version": SCHEMA_VERSION,
        "generation_contract_version": GENERATION_CONTRACT_VERSION,
        "generation_file_lease_version": "2.0",
        "generation_profile_contract": {
            "registry_version": GENERATION_PROFILE_REGISTRY_VERSION,
            "profile_version": GENERATION_PROFILE_VERSION,
        },
        "generation_job_contract": {
            "version": GENERATION_JOB_VERSION,
            "lease_version": GENERATION_JOB_LEASE_VERSION,
            "service_level_version": "1.0",
            "workload_unit": "bdd_step",
            "large_job_step_threshold": 100,
            "max_supported_step_count": 600,
            "direct_design": "retired_from_product_entrypoint",
            "naming_patch": "minimal_ai_patch_for_system_baseline_name_blanks",
            "ambiguity_choice_patch": "minimal_ai_patch_for_frozen_ai_ambiguity_outcomes",
            "assertion_choice_patch": "minimal_ai_patch_for_frozen_assertion_candidate_keys",
            "method_choice_patch": "minimal_ai_patch_for_frozen_page_method_candidates",
            "operation_choice_patch": "minimal_ai_patch_for_frozen_operation_choice_sets",
            "value_source_choice_patch": "minimal_ai_patch_for_frozen_available_value_sources",
            "business_review": "job_bound_user_authority_workset_before_generation",
            "business_questions": "business_review_answer_batch_ai_ambiguity_in_job",
            "business_question_interaction": (
                "advance_job_business_review_then_single_answer_batch"
            ),
            "business_question_display": (
                "compact_cli_declared_options_with_verified_media_links"
            ),
            "business_question_media": (
                "verified_workspace_links_or_explicit_unavailable"
            ),
            "business_fact_patch": "job_bound_freeform_business_facts_only",
            "deny_only_pic": "system_default_deny_not_user_question",
            "typed_patch_submission": "atomic_overrides_before_baseline_compile",
            "host_guard": "no_external_output_reads_and_current_job_actions_only",
            "candidate_preflight": (
                "complete_system_bundle_staged_before_transaction_and_system_materialization"
            ),
            "candidate_delivery": (
                "system_materializer_with_implementation_diff"
            ),
            "terminal_failure": "primary_owner_stage_before_health_warnings",
            "invalid_ai_design": "revise_same_design_same_job",
            "run_job_phase": "implementation_only",
            "input_capsule": {
                "version": GENERATION_CAPSULE_VERSION,
                "rule": (
                    "Job generation inputs, including the bound Feature, are "
                    "sealed before AI generation; "
                    "live workspace is checked only by the commit gate."
                ),
            },
            "current_content_projection": {
                "version": "1.0",
                "rule": (
                    "Each newly admitted Job binds a current workspace content "
                    "projection to the same input snapshot fingerprint as the "
                    "Generation Capsule; it is the upstream fact source for "
                    "existing assets, generated assets, and required structure "
                    "files. Stage 2 makes Implementation Manifest consume the "
                    "projection for package markers, read-only reuse, and asset "
                    "resolution. Stage 3 makes Candidate Preflight materialize "
                    "only the projection-defined workspace slice for source files, "
                    "read-only structure files, and direct local import dependencies. "
                    "Stage 4 makes Job Result and entrypoint projections show the "
                    "bound projection fingerprint plus reuse/modify/new/missing/"
                    "user-modified summary counts without exposing file lists."
                ),
            },
            "timeout_behavior": "continue_same_job_and_report",
            "timing_coverage": {
                "agent_hook": "complete_host_observation",
                "manual_cli": "incomplete",
            },
        },
        "generation_result_contract": {
            "job_result_version": GENERATION_JOB_RESULT_VERSION,
            "transaction_version": TRANSACTION_VERSION,
            "implementation_diff_version": GENERATION_DIFF_VERSION,
            "implementation_diff_summary_version": GENERATION_DIFF_SUMMARY_VERSION,
        },
        "generation_placeholder_policy": {
            "version": "1.0",
            "terminal_failure_only_for": [
                "job_identity_or_cas_mismatch",
                "request_brief_plan_or_contract_integrity",
                "write_scope_or_protected_path_violation",
                "transaction_result_tamper",
                "pic_authorization_violation",
                "system_candidate_invalid",
            ],
            "typed_placeholder_for": [
                "non_security_generation_gap",
                "locator_or_reuse_uncertainty",
                "pos_or_ocr_fallback_uncertainty",
                "window_close_uncertainty",
                "top_level_root_without_criteria_pos_fallback",
                "unsupported_scroll_review",
                "unsupported_assertion_implementation",
                "invalid_technical_value_binding",
                "control_final_state_uncertainty",
                "local_implementation_gap",
                "plan_conformance_gap",
            ],
            "efficiency_rule": (
                "After full-scenario generation starts, only super blockers "
                "may stop the Job. Empty top-level popup roots with valid POS "
                "use low-confidence coordinates; without POS they use typed "
                "placeholder. Non-security locator, window-close, "
                "scroll review, unsupported assertion, technical binding, "
                "control-state, reuse, or implementation uncertainty becomes "
                "a typed placeholder or low-confidence implementation and is "
                "reported in the final Job Result; no repeated AI confirmation "
                "loop, Job replacement, or rerecord attempt during generation."
            ),
        },
        "generation_design_contract": {
            "version": design_contract["design_version"],
            "fingerprint": _hash_value(design_contract),
            "role": "internal_plan_input_model",
            "product_entry": "retired",
        },
        "generation_naming_patch_contract": {
            "version": GENERATION_NAMING_PATCH_VERSION,
            "patch_type": "naming",
            "fields": ["target_names", "business_names"],
            "submit_command": "advance-job --target-name",
            "scope": "only unresolved public-name blanks from the system baseline",
        },
        "generation_ambiguity_choice_patch_contract": {
            "version": GENERATION_AMBIGUITY_CHOICE_PATCH_VERSION,
            "patch_type": "ambiguity_choice",
            "fields": ["choices.ambiguity_id", "choices.outcome"],
            "submit_command": "advance-job --ambiguity-choice",
            "scope": (
                "only AI-authority frozen plan_coverage outcomes that do "
                "not require candidate, value, locator, method, or Plan "
                "structure changes"
            ),
        },
        "generation_assertion_choice_patch_contract": {
            "version": GENERATION_ASSERTION_CHOICE_PATCH_VERSION,
            "patch_type": "assertion_choice",
            "fields": ["choices.ambiguity_id", "choices.candidate_key"],
            "submit_command": "advance-job --assertion-choice",
            "scope": (
                "only frozen assertion implementation candidate keys from "
                "assertion_implementation ambiguity facts; operations, "
                "parameters, values, and provenance are re-resolved from the "
                "frozen candidate"
            ),
        },
        "generation_method_choice_patch_contract": {
            "version": GENERATION_METHOD_CHOICE_PATCH_VERSION,
            "patch_type": "method_choice",
            "fields": ["choices.step_id", "choices.candidate_id"],
            "submit_command": "advance-job --method-choice",
            "scope": (
                "only frozen Page method candidate_id for a Step whose "
                "baseline operations exactly match the candidate call_sequence; "
                "method bodies, operations, values, windows, locators, and "
                "proof remain system-owned"
            ),
        },
        "generation_operation_choice_patch_contract": {
            "version": GENERATION_OPERATION_CHOICE_PATCH_VERSION,
            "patch_type": "operation_choice",
            "fields": [
                "choices.step_id",
                "choices.action_id",
                "choices.choice_key",
            ],
            "submit_command": "advance-job --operation-choice",
            "scope": (
                "only choice_key values from the frozen OperationChoiceSet "
                "for a Step/Action; operation strings, values, windows, "
                "locators, methods, and proof remain system-owned"
            ),
        },
        "generation_value_source_choice_patch_contract": {
            "version": GENERATION_VALUE_SOURCE_CHOICE_PATCH_VERSION,
            "patch_type": "value_source_choice",
            "fields": [
                "choices.step_id",
                "choices.action_id",
                "choices.operation",
                "choices.source.kind",
                "choices.source.reference/action_id",
            ],
            "submit_command": "advance-job --value-source-choice",
            "scope": (
                "only status=available value_source qualification shapes "
                "for a frozen Step/Action/operation; values and provenance "
                "are re-resolved by the compiler"
            ),
        },
        "generation_decision_authority_matrix": (
            _generation_decision_authority_matrix()
        ),
        "implementation_manifest_contract": {
            "version": implementation_contract[
                "implementation_manifest_version"
            ],
            "fingerprint": _hash_value(implementation_contract),
        },
        "implementation_candidate_contract": {
            "version": MATERIALIZATION_CANDIDATE_VERSION,
            "workspace_write_owner": "system_materializer",
            "prepare_writes_workspace": False,
            "advance_job_writes_system_owned_workspace": True,
            "query": "job-implementation-candidate",
            "normal_delivery_write_channel": "system_materializer",
            "normal_review_channel": "implementation_diff",
            "candidate_index_role": "internal_diagnostic_only",
            "native_edit_operation": "retired_from_product_delivery",
            "file_operation_field": "lines[].op",
            "manifest_operation_field": "files[].operation",
            "create_file_tool": None,
            "replace_file_tool": None,
            "manifest_field": "candidate_manifest",
            "index_field": "candidate_index",
            "line_window_field": "candidate_index.recommended_read",
            "editable_list_field": "candidate_index.lines",
            "source_file_field": "candidate_index.source_root + lines[].source",
            "target_file_field": "lines[].target",
            "verification": "target_sha256_must_equal_expected_sha256",
            "delivery_review": "implementation_diff",
            "shell_or_python_workspace_write": False,
            "candidate_query_mode": "internal_diagnostic_compact_index_without_all",
            "preflight": "passed_before_system_materialization",
        },
        "ai_context_envelope_contract": {
            "version": envelope_contract["ai_context_envelope_version"],
            "fingerprint": envelope_contract["contract_fingerprint"],
        },
        "generation_task_bundle_contract": {
            "version": GENERATION_TASK_BUNDLE_VERSION,
            "fragment_version": GENERATION_TASK_FRAGMENT_VERSION,
            "max_actions_per_fragment": MAX_ACTIONS_PER_FRAGMENT,
            "max_fragment_bytes": MAX_FRAGMENT_BYTES,
            "max_default_index_bytes": MAX_BUNDLE_INDEX_BYTES,
            "ordinary_static_service_level_target_seconds": 300,
            "large_static_service_level_target_seconds": 1800,
            "applies_to": "large_jobs_only",
            "query": "job-task-bundle",
            "rules": [
                "Fragment JSON bytes, index metadata, Job, Request, Brief, Profile, and Contract identities are content-bound.",
                "Fragments are read-only context pages; they never own independent designs, leases, attempts, or submissions.",
                "Paged TaskBundle context never prevents the system baseline attempt; fragments are read only after advance-job still returns design_required.",
            ],
        },
        "framework_contract": framework_contract,
        "purpose": (
            "Generate evidence-traceable BDD code through an immutable "
            "Generation Job with one optional post-claim business answer batch and fail-closed "
            "Plan/Transaction validation."
        ),
        "entrypoint": "ai/generation-jobs/<request-id>/job-<fingerprint>.json",
        "job_workflow": {
            "inspect": (
                "python -m autowork_core.utils.debug_tools.recorder."
                "generation_workflow inspect-job <job-path>"
            ),
            "settle": (
                "python -m autowork_core.utils.debug_tools.recorder."
                "generation_workflow settle-job <job-path>"
            ),
            "advance": (
                "python -m autowork_core.utils.debug_tools.recorder."
                "generation_workflow advance-job <job-path>"
            ),
            "submit_answers": "retired: Workbench submits Request Decision Answers before Job claim",
            "evidence": (
                "python -m autowork_core.utils.debug_tools.recorder."
                "generation_workflow job-evidence <job-path> "
                "[--evidence-id <id> | --step-id <step-id> | --action-id <action-id>]"
            ),
            "design_context": (
                "python -m autowork_core.utils.debug_tools.recorder."
                "generation_workflow job-design-context <job-path> "
                "[--step-id <step-id>] # only after generate-job returns design_required"
            ),
            "task_bundle": (
                "python -m autowork_core.utils.debug_tools.recorder."
                "generation_workflow job-task-bundle <job-path> "
                "[--fragment-id <fragment-id>]"
            ),
            "compare_takes": (
                "python -m autowork_core.utils.debug_tools.recorder."
                "generation_workflow job-compare-takes <job-path> --step-id <step-id>"
            ),
            "action_knowledge": (
                "python -m autowork_core.utils.debug_tools.recorder."
                "generation_workflow job-action-knowledge <job-path> "
                "[--step-id <step-id> --action-id <action-id>] [--operation <operation>]"
            ),
            "design_contract": (
                "python -m autowork_core.utils.debug_tools.recorder."
                "generation_workflow design-contract"
            ),
            "generate": (
                "python -m autowork_core.utils.debug_tools.recorder."
                "generation_workflow advance-job <job-path> "
                "[--target-name <step-id/action-id=name> | --ambiguity-choice <ambiguity=outcome> | --assertion-choice <ambiguity=candidate> | --method-choice <step=candidate> | --operation-choice <step/action=choice> | --value-source-choice <step/action/operation=source>]"
            ),
            "implementation_packet": (
                "python -m autowork_core.utils.debug_tools.recorder."
                "generation_workflow job-implementation-packet <report-path> "
                "[--step-id <step-id> | --path <path>]"
            ),
            "implementation_candidate": (
                "python -m autowork_core.utils.debug_tools.recorder."
                "generation_workflow job-implementation-candidate "
                "<report-path>"
            ),
            "implementation_diff": (
                "python -m autowork_core.utils.debug_tools.recorder."
                "generation_workflow job-code-diff <report-path> "
                "[--offset <byte-offset>] [--limit <bytes>]"
            ),
            "risk_modes": ["fast", "clarify", "forensic", "blocked"],
            "workflow_states": [
                "draft",
                "ready",
                "needs_adjustment",
                "forensic",
                "blocked",
                "stale",
                "running",
                "completed",
                "failed",
            ],
            "rules": [
                "RequestV3 is immutable and contains facts only.",
                "ai/workflow/<request-id>.json is the only runtime state source.",
                "AI reads a fingerprinted GenerationDesignContextV1 by default; the immutable full Brief remains compiler authority and omitted detail expands only through frozen Job queries.",
                "AI reads content-addressed Plan Context 1.1 by default; the full immutable GenerationPlan remains backend identity and expands only through the plan query.",
                "Completed regeneration may reuse its bound Request and Plan; its own prior transaction result does not stale the Request, while newer feedback or other relevant memory still requires rematerialization.",
                "Semantic Reconciler must classify all default evidence before fast generation.",
                "Evidence completeness is a pre-entry gate: an evidence gap cannot create an active Generation Job.",
                "Workbench submission gate only blocks incomplete target Steps, hard evidence/readiness blockers, request refresh, and Job creation failures; user-authority Decision questions are handled after the Job is submitted to Copilot.",
                "advance-job projects pending Decision Pack user-authority/authorization questions directly as one business_answers_required batch before full generation; business_review_required, business_option_answers_required, submit-business-review, and submit-business-answers are retired from the normal product path.",
                "Authority questions are asked once through one vscode_askQuestions call with clickable options and returned per-question allowFreeformInput flags; selected options and allowed freeform answers are submitted through submit-business-review-answers and backend validation turns accepted facts into Decision Answers and Plan business_facts constraints.",
                "submit-business-answers, submit-business-facts, and submit-business-review are lower-level migration/helper commands, not normal-path answer transports.",
                "AI-authority business ambiguities remain in the Job and are resolved by typed patch or Plan selection; they must not become Workbench answer blockers.",
                "The Agent never owns Decision answer binding or persistence; unanswered user-authority questions stop before full generation and backend Decision Pack validates the Job-bound submission.",
                "Only questions requiring user authority or authorization enter the Answers batch; a PIC candidate without an authorized alternative is denied by system policy and never becomes a user question.",
                "Every pre-generation answer question projects a user-facing askQuestions payload; askQuestions is allowed only when the current Job control projects business_questions and never during typed patch or technical uncertainty stages.",
                "advance-job first uses system-owned baseline facts to compile GenerationPlanV4.2; naming_patch_required, ambiguity_choice_required/AmbiguityChoicePatchV1, assertion_choice_required/AssertionChoicePatchV1, method_choice_required/MethodChoicePatchV1, operation_choice_required/OperationChoicePatchV1, and value_source_choice_required/ValueSourceChoicePatchV1 are collected into typed_patch_requirement_batch 1.0 when possible so the Agent submits all complete direct typed arguments in one command; untyped design_required is blocked as a classification defect because complete GenerationDesign product entry is retired.",
                "Single and mixed typed patches share one atomic compiler path: naming overrides and every typed choice are applied before the baseline is built, then the complete Design is validated once.",
                "Every typed patch required response carries typed_patch_requirements 1.0, and multi-gap responses carry typed_patch_requirement_batch 1.0 with requirement_id, complete, facts, choices, submit_arguments, optional recommended_arguments, forbidden_fields, missing_fields, and deduplicated allowed_queries; complete=true forbids expanding job-design-context, and unbounded job-design-context is blocked while a typed patch is active.",
                "Only complete=false plus an explicit allowed_query may authorize a bounded follow-up query. Missing typed_patch_requirements, a missing batch for multiple gaps, or complete=false without allowed_query is a framework defect, not Agent discretion.",
                "Recorder workflow JSON is protocol data and must not be delegated to execution_subagent or any summary-only runner. A summary missing next_commands, typed batch, candidate files, or terminal result is a protocol output defect; the Agent may use only returned Job-bound candidate manifest/diff page queries, never chat resource files or external terminal-output files, and must not rerun bare advance-job.",
                "Terminal completed/failed advance-job and generate-job output is a fixed result envelope: terminal_result is the user-facing result object, implementation_diff_summary.files is a bounded file-stat preview, next_commands.diff_review is the complete review entry, and workspace projection, delivery/visibility duplicate summaries, job lifecycle, stages, raw changed_files, full report, Plan, Manifest, and candidate payloads are omitted from the normal terminal envelope.",
                "When typed_patch_requirement_batch is complete and every requirement has recommendation.classification=system_verified_unique, advance-job submits that batch itself before returning to the Agent; returned complete batches remain AI-decision boundaries. The Agent must submit returned direct typed arguments in one advance-job command after at most one concise stage-level progress message, without repeated explanation or querying job-design-context; if recommended=true, next_commands.primary is already fully filled and compact output omits requirements by default. A complete batch without next_commands.primary is a framework defect: stop, do not rerun bare advance-job, inspect CLI help, or reconstruct commands.",
                "Host Control denies vscode_askQuestions, retired submit-job-answers, chat-session-resources, external terminal-output reads, job-implementation-candidate --all, full manifest reads on the normal path, and unbound source reads in every generation phase; candidate index and native-edit source reads are internal diagnostics, not the normal system-owned delivery path.",
                "While a typed Design marker is active, Host Control denies project source/rule/memory reads and unbounded workflow commands; only the current host result, advance-job, and an explicitly bounded Step query remain available.",
                "Generation Contract binds the exact GenerationDesign and Implementation Manifest contract versions and fingerprints; either schema change requires Request rematerialization.",
                "Every newly admitted Generation Job persists generation_workspace_projection as the current content package: it is bound to the same generation_input_snapshot_fingerprint as the Capsule and records existing assets, generated-result presence, and required package markers. Implementation Manifest consumes it for current_content_projection, package marker create/reuse, read_only_reuse, and asset_resolution; Candidate Preflight consumes it to materialize the projection-defined workspace slice, including source files, read-only structure files, and direct local import dependencies, while snapshot drift fails closed. Job Result and normal entrypoints expose workspace_projection_summary with the same projection_fingerprint and compact reuse/modify/new/missing/user_modified counts; retired terminal results are audit evidence only, and a generated file is reusable only if it still exists in the current workspace projection.",
                "Implementation Packet freezes every Step page_binding, including all cross-WindowPage operation owners; the renderer rejects unbound receiver expressions.",
                "Before a complete system-owned candidate gets a Transaction/file lease or system-materialization commit, Candidate Preflight reconstructs Capsule sources in a private staging root and reuses Python, locator, Step-scope, and Plan-to-Code validators.",
                "Candidate Preflight failure publishes terminal system_candidate_invalid with implementation owner evidence and leaves Bdd unchanged; the Agent may not hand-edit or replay that candidate.",
                "Replacing a current Generation Job archives that Job's frozen Brief before any fresh Brief/Decision Pack is written; retired Jobs must remain inspectable without depending on the mutable request brief path.",
                "System materialization owns normal candidate text boundaries, including platform line endings and a stable final newline; the Agent must not copy system-owned candidate content, recalculate hashes, normalize newlines, delete/recreate existing targets, or repair SHA mismatches by shell commands.",
                "Inspect is side-effect-free for transaction state; advance-job is the normal Agent entrypoint for claim progression, transaction preparation, validation, and finish.",
                "advance-job runs system-owned deterministic stages in one command until it reaches an AI-decision, terminal, or failure boundary; advance_summary.deterministic_progression lists the internal orchestration stages that were already executed, and the Agent must not split settle/start/prepare/validate/finish into manual sub-commands.",
                "Successful Job commands return the authoritative job_transition for the next claim/epoch CAS operation; callers never infer an epoch increment.",
                "The static service-level timer includes time before the first tool call only when a trusted host adapter supplies command_sent_at; without it, the current CLI may report exceeded from an over-target agent-time lower bound but can never report within_target.",
                "User-perceived generation time includes host command errors, repeated rule reads, terminal rendering, and all Agent preamble work; Workbench-pasted full commands are the normal Agent entry and runtime hints are fallback-only when the full command is unavailable.",
                "Recorder Host Control records an observation-only agent wait ledger for bound UserPromptSubmit, PreToolUse, PostToolUse, and Stop events; Job Result projects it under generation_timing_ledger.agent_tool_timing without including it in Result, Transaction, Plan, or Job identity.",
                "Job Result, compact CLI output, and Workbench DTOs project generation_progress as user-visible progress: delivery_write_channel, changed count, review_channel, and system materialization status are the normal delivery facts; candidate index file/window counts and waits among candidate_index_read, source_reads, editor_edits, and after_native_edit are internal diagnostic attribution only. Silence and SLA status are not completion proof.",
                "The editor_edits_done_to_after_native_edit_request wait segment is attribution-only legacy evidence: Hook observes waits but never auto-runs after_native_edit. Undone, partial, or SHA-mismatched materialization fails closed through Transaction validation and must not be repaired by shell/Python writes.",
                "If the current terminal Job Result is completed and its Transaction, Contract, Code Manifest, and code snapshots still match, admission returns that completed result instead of creating a new Design Job.",
                "Service-level exceedance never terminates, replaces, retires, or resets the Job; the same Job continues to a terminal result.",
                "A failed terminal projection reports the authoritative Job Result category, first failed owner stage, and owner reason before service-level health warnings.",
                "After full-scenario generation starts, only trust-boundary failures and an internally invalid deterministic system candidate terminate the Job. Other representable non-security gaps, including POS/OCR fallback uncertainty and non-hard window-close uncertainty, become typed issue placeholders or low-confidence implementations rather than user questions, rerecord prompts, or Agent repair loops.",
                "Typed issue placeholders are system-owned fast degradation: the system must not spend repeated AI confirmation or query loops before choosing the placeholder path; each placeholder covers only its frozen Action IDs and renders an ordered unresolved_generation_issue call, while other Actions in the same Step continue through the validated Plan.",
                "Paged TaskBundle fragments are read-only context and never create independent attempts, leases, submissions, timing scopes, or a default full-Design path; advance-job attempts the system baseline before AI reads fragments.",
                "job-implementation-packet and job-implementation-candidate read only the current running Job's matching prepared Transaction, Manifest, candidate, and committed file lease.",
                "A running Transaction whose Generation Contract or current Implementation Manifest derivation is stale is superseded inside the same Job: the old file lease is released, the Job remains in implementation, and advance-job prepares a fresh content-addressed candidate instead of failing or serving the old candidate.",
                "Normal advance-job materializes preflight-passed system-owned candidates with the system materializer, records implementation_receipt.delivery_write_channel=system_materializer, validates target SHA, publishes implementation_diff review, and finishes the same Transaction without Agent Bdd edits. job-implementation-candidate, candidate_index, native_edit source files, native_edit_plan, and edit_batches are internal diagnostics, not the normal product path. A repeated candidate_mismatch is a host delivery defect, not an Agent debugging loop.",
                "When candidate_index is present for diagnostics, compact advance-job output hides full editable path arrays, including candidate_files, native_edit_candidate_files, ai_editable_changes, and system_owned_files; normal next_commands must not emit candidate index reads, native-edit source reads, agent_editor_edit, or after_native_edit.",
                "An unapplied, partially applied, modified, or undone candidate_file fails validation; explicit system materialization must validate its journal and receipt before finish.",
                "advance-job deterministically derives and freezes ImplementationManifestV1.16 from the validated Plan, Brief, and generation-root snapshot; AI semantically owns choices, system materialization owns system-owned writes, candidate artifacts are internal diagnostics, and read_only_reuse remains immutable.",
                "A current-recording-verified Locator reuse retains its existing key exactly; the Manifest protects that key when the same YAML adds another verified control. Unproven, stale, conflicting, or generated-suffix reuse becomes a typed maintenance placeholder, not a rerecord request.",
                "Action Knowledge 1.2 projects Step/Action-scoped value_source qualification for AI-named operations without exposing values or ranking operations; the compiler re-resolves every source.",
                "When one value_source qualification is status=available for a frozen operation, the system baseline selects it and the compiler re-resolves the value; AI must not expand it into full GenerationDesign.",
                "When multiple status=available value_source qualifications remain for a frozen Step/Action/operation, advance-job returns value_source_choice_required; AI submits only the selected source shape and never a value or provenance.",
                "Code Reuse Index 2.2 exposes only linear direct Page-operation call_sequence as exact-reuse proof; nested, conditional, helper, or otherwise non-linear methods do not receive exact sequence proof.",
                "Exact Page method reuse verifies the content-addressed candidate, ordered operation/target sequence, and generated Step method call arguments; locator read-only status requires a frozen locator/window-root candidate.",
                "A single frozen Step behavior reuse candidate with complete ordered Action mappings is selected by the system baseline; it must not force full GenerationDesign just because a placeholder outcome also exists.",
                "A single frozen Page method reuse candidate with content-addressed source hash and exact ordered operation/target call_sequence is selected by the system baseline when no value mapping is required.",
                "Multiple frozen Page method candidates with exact operation/target call_sequence matches use method_choice_required; AI submits only one candidate_id and never a method body, operation, value, locator, window, or proof.",
                "A single implement_with_frozen_evidence outcome backed by frozen target evidence is selected by the system baseline; a parallel placeholder outcome alone must not force full GenerationDesign.",
                "A single frozen assertion implementation candidate is consumed by the system baseline; AI must not expand it into full GenerationDesign when target and parameters are already frozen.",
                "Multiple frozen assertion implementation candidates use assertion_choice_required; AI submits only one candidate_key and never operation, parameters, expected values, or provenance.",
                "Only user-authority ambiguity uses one revision-bound Decision Pack batch; after naming_patch_required, AI submits only stable public names; after ambiguity_choice_required, AI submits only frozen ambiguity outcome choices; after assertion_choice_required, AI submits only frozen assertion candidate keys; after method_choice_required, AI submits only frozen Page method candidate ids; after operation_choice_required, AI submits only frozen operation choice keys; after value_source_choice_required, AI submits only frozen available source shapes; remaining untyped design_required is blocked and never prepares a complete GenerationDesign draft.",
                "Design covers every target Step; the system derives Scenario roles/support, owners/paths, locators, Action/Evidence closure, each exact annotation_ids set, Plan trace, and transaction lease.",
                "Forensic reads only required_forensic_evidence before adjustment.",
                "Finish runs revision, Annotation lease, Python, locator, Step scope, policy, controlled-PIC, evidence, and Plan-to-Code validation automatically.",
                "A successful Transaction publishes one content-addressed implementation diff plus implementation_diff_summary file-level additions/deletions; for generated new files it records git_diff_visibility intent-to-add status so VS Code/Git can show source-control new-file diffs without staging content. The system materializer reports delivery_write_channel=system_materializer and review_channel=implementation_diff as the normal user-facing code delivery for system-owned writes, not pasted diff text or Agent/native edit claims.",
                "Every newly created nested Bdd/page_obj package contains an import-free __init__.py marker; it may be empty or docstring-only and never contains imports or re-exports.",
                "The default Brief exposes frozen facts and evidence-bound constraints, not semantic operation recommendations. AI forms operation candidates before querying Action Knowledge; only objectively incompatible target/runtime combinations are rejected, while unknown requires investigation or runtime validation.",
                "When the system baseline cannot derive a direct operation, OperationChoiceSet freezes the AI-visible operation boundary with choice_key, Step/Action, target fingerprint, compatibility status, basis, and value-source requirement; AI submits only the choice_key.",
            ],
        },
        "read_order": [
            {
                "artifact": "ai/workflow/<request-id>.json",
                "required": True,
                "access": "default",
                "purpose": "Read the only runtime status, next action, revision, Brief pointer, Plan pointer, and transaction result.",
            },
            {
                "artifact": "inspect.design_context",
                "required": True,
                "access": "default",
                "purpose": "Fingerprint-bound GenerationDesignContextV1 with target Steps, Action facts, ownership, and reuse candidates for one Design.",
            },
            {
                "artifact": "ai/generation-briefs/<request-id>.json",
                "required": False,
                "access": "on_demand_frozen_job_query",
                "purpose": "Immutable full Brief authority for compiler and explicit job-design-context expansion; not default AI context.",
            },
            {
                "artifact": "inspect.plan_context",
                "required": True,
                "access": "default_when_ready",
                "purpose": "Read compact Scenario Model, ownership, operation order, data use, and result status for generation or regeneration.",
            },
            {
                "artifact": "ai/plans/<request-id>/plan-*.json",
                "required": True,
                "access": "backend_identity",
                "purpose": "Immutable Plan identity for prepare, finish, proof, Code Manifest, and explicit on-demand expansion.",
            },
            {
                "artifact": "job-implementation-packet",
                "required": False,
                "access": "diagnostic_or_explicit_file_context_after_prepare",
                "purpose": "Job-bound Manifest packet projection for diagnostics or an explicitly requested Step/file context; not part of the normal Agent delivery path.",
            },
            {
                "artifact": "job-implementation-candidate",
                "required": False,
                "access": "internal_diagnostic_after_prepare",
                "purpose": "Preflight-passed content-addressed candidate index for diagnostics. The normal system-owned path does not use Agent file edits: advance-job writes with delivery_write_channel=system_materializer, validates, and publishes review_channel=implementation_diff. candidate_index.path, candidate_index.source_root plus lines[].source, lines[].target, lines[].op, candidate_manifest, native_edit_plan, and edit_batches are not the product path. --all, external output files, full manifest reads on the normal path, unlisted source path reads, per-file terminal content queries, and helper parsing commands are forbidden.",
            },
            {
                "artifact": "inspect.ai_capabilities",
                "required": True,
                "access": "default",
                "purpose": "Use the compact registry-derived Plan operations and real BasePage signatures.",
            },
            {
                "artifact": "ai/prompts/recorder-generate.md",
                "required": True,
                "access": "default",
                "purpose": "Follow the focused generation workflow without loading maintenance context.",
            },
            {
                "artifact": "ai/instructions/bdd-generation.md",
                "required": True,
                "access": "default",
                "purpose": "Apply generated Bdd asset ownership and coding rules.",
            },
            {
                "artifact": "generation-contract.json",
                "required": False,
                "access": "backend_only",
                "purpose": "Bind framework identity and full validation policy without loading it into normal AI context.",
            },
            {
                "artifact": "<request>/evidence_context.path",
                "required": False,
                "access": "backend_or_forensic_only",
                "purpose": "The Reconciler and finish audit consume this. AI reads only evidence IDs named by a forensic Workflow State.",
            },
            {
                "artifact": "<request>/memory_context.path",
                "required": False,
                "access": "backend_only",
                "purpose": "The Reconciler summarizes relevant advisory memory into the Brief; do not read the journal on the normal AI path.",
            },
            {
                "artifact": "target-index.json",
                "required": False,
                "access": "backend_only",
                "purpose": "Request materialization resolves stable Feature, Scenario, Examples, and Step ids/keys.",
            },
            {
                "artifact": "readiness.json",
                "required": False,
                "access": "backend_only",
                "purpose": "Workflow State owns the resulting blocked/ready decision.",
            },
            {
                "artifact": "<take>/evidence/graph.json",
                "required": False,
                "access": "backend_or_forensic_only",
                "purpose": "The Reconciler consumes the immutable evidence source; AI opens only graph items named by forensic routing.",
            },
            {
                "artifact": "<take>/take.json",
                "required": False,
                "access": "backend_or_forensic_only",
                "purpose": "Read selected windows only when a named forensic conflict requires them.",
            },
            {
                "artifact": "<take>/actions.effective.json",
                "required": False,
                "access": "backend_or_forensic_only",
                "purpose": "The Brief already contains reconciled effective operations; open the source only for named forensic conflicts.",
            },
            {
                "artifact": "<take>/timeline-state.json",
                "required": False,
                "access": "backend_or_forensic_only",
                "purpose": "Revision and included actions are sealed by Request/Workflow; open only for named forensic conflicts.",
            },
            {
                "artifact": "<take>/locator-candidates.effective.yaml",
                "required": False,
                "access": "backend_or_forensic_only",
                "purpose": "Use the reconciled locator evidence in the Brief; inspect source candidates only when forensic routing names them.",
            },
            {
                "artifact": "<take>/ui/tree-diff.json",
                "required": False,
                "access": "forensic_only",
                "purpose": "Open only when required_forensic_evidence names a tree conflict.",
            },
            {
                "artifact": "<take>/action-media.json",
                "required": False,
                "access": "forensic_only",
                "purpose": "Open only when required_forensic_evidence names action-level visual evidence.",
            },
            {
                "artifact": "<take>/media-index.json",
                "required": False,
                "access": "forensic_only",
                "purpose": "Open only to resolve media IDs named by forensic routing.",
            },
        ],
        "evidence_precedence": [
            "revision-bound GenerationPlanV4.2 with Scenario Model, target/value Action provenance, ambiguity, window, method ownership, runtime bindings, and Contract lease",
            "actions.effective.json human-corrected action timeline",
            "recorded Step text, Examples values, Data Table, and text block",
            "actions.auto.json automatic action derivation",
            "validated locator candidate that points back to the recorded target",
            "before/after tree diff and target property changes",
            "event screenshot and contact-sheet frame",
            "video frame extracted at media-index event video_ms",
            "full video only when the structured and still-image evidence is insufficient",
        ],
        "action_mapping": {
            "click": {
                "framework_api": [
                    "self.click(locator_name)",
                    "self.click(locator_name, offset_x, offset_y)",
                ],
                "parameter_rule": (
                    "offset_x/offset_y are system-frozen together only for "
                    "position-sensitive container clicks. AI cannot invent "
                    "or modify the recorded offset."
                ),
                "confidence": "high when target locator is validated",
            },
            "double_click": {
                "framework_api": "self.double_click(locator_name)",
                "confidence": "high when target locator is validated",
            },
            "right_click": {
                "framework_api": "self.right_click(locator_name)",
                "confidence": "high when target locator is validated",
            },
            "keyboard": {
                "framework_api": [
                    "self.input_text(locator_name, data_name)",
                    "self.send_text_keys(locator_name, keys)",
                ],
                "selection_rule": (
                    "Use input_text for entered text; use send_text_keys for shortcuts, "
                    "navigation keys, or non-text key sequences."
                ),
            },
            "scroll": {
                "framework_api": "self.scroll_to(target, direction, steps)",
                "confidence": "review_required until direction and amount are recorded explicitly",
            },
            "select_option": {
                "framework_api": (
                    "self.select_dropdown_option(locator_name, option)"
                ),
                "selection_rule": (
                    "Use one semantic call for ComboBox set/select. The action "
                    "waits, expands when supported, selects, and verifies the "
                    "result. Require a declared or observed option value; raw "
                    "clicks cannot replace the semantic operation."
                ),
            },
            "observe": {
                "framework_api": [
                    "self.assert_exists(locator_name)",
                    "self.assert_visible(locator_name)",
                    "self.assert_enabled(locator_name)",
                    "self.assert_text_equal(locator_name, expected)",
                    "self.assert_attr_equal(locator_name, attr_name, expected)",
                    "self.assert_collection_equal(locator_name, expected)",
                    (
                        "self.assert_ocr_contains("
                        "expected, region=region_locator)"
                    ),
                    (
                        "self.assert_ocr_not_contains("
                        "expected, region=region_locator)"
                    ),
                ],
                "selection_rule": (
                    "Select the assertion from the Step wording, typed "
                    "Observation Intent, target properties, and frozen "
                    "runtime evidence. Canvas OCR assertions require a "
                    "content-addressed Region receipt. Never invent an "
                    "expected value."
                ),
            },
            "drag": {
                "framework_api": (
                    "self.drag_by_offset(locator_name, delta_x, delta_y)"
                ),
                "confidence": (
                    "high only when source locator is validated and non-zero "
                    "delta_x/delta_y are frozen from the recorded action"
                ),
            },
            "middle_click": {
                "framework_api": None,
                "confidence": "manual_page_object_method_required",
            },
        },
        "action_role_contract": {
            "business": "Generate the Step's core business operation, normally through a Page Object.",
            "setup": "Generate or reuse prerequisite/Given setup separately from the core operation.",
            "assertion": "Use the action and target as assertion evidence; choose assert_* from confirmed intent.",
            "noise": "Do not generate this action or its locator; retain only in review history.",
            "transport": "Preserve window/navigation sequence, but do not infer a business assertion from it.",
        },
        "table_usage_contract": {
            "rule": (
                "A Step Data Table is raw business data and does not imply iteration. "
                "Every generated table Step must declare table_usage in PlanV4.2."
            ),
            "consumptions": {
                "each_row": (
                    "The Step parses context.table once. Scenario-specific row loops may stay "
                    "in the Step; stable reusable loops may delegate to a Page Object."
                ),
                "whole_table": (
                    "The Step converts context.table to the declared list/mapping/object/records "
                    "shape, then either consumes it locally or delegates one stable Page method."
                ),
                "scenario_state": (
                    "The Step explicitly stores parsed table data at the declared context_key "
                    "for later Steps; it does not call a Page Object."
                ),
            },
            "requirements": [
                "Use only columns declared by table_usage and preserve their names.",
                "Do not hardcode Data Table rows in generated Python.",
                "For each_row, honor ordered and reset_between_rows in the declared consumer.",
                "When business intent is ambiguous, require one Table Usage Decision instead of guessing from table shape.",
                "Never generate or call run_case_matrix.",
            ],
        },
        "runtime_binding_contract": {
            "scope": "current Scenario",
            "producer_operations": ["save_attr", "save_text"],
            "producer_field": "result_binding",
            "consumer_source": "runtime.<binding>",
            "implementation_reader": "get_variable",
            "forbidden_generated_api": "set_variable",
            "rules": [
                "AI chooses the relationship; equal values and Step adjacency are advisory only.",
                "Each binding has one prior F9-backed producer and at least one consumer.",
                "The producer strategy must be supported by frozen readable text or property facts.",
                "Consumers in Steps with Examples arguments explicitly declare argument=null; naming an argument is rejected as a declared-source conflict.",
                "Runtime sources cannot silently replace explicit declared sources.",
                "Plan-to-Code verifies producer, consumer, Page parameter flow, and added variable calls.",
            ],
        },
        "locator_contract": {
            "priority": list(manifest["locator_policy"]["priority"]),
            "excluded": list(manifest["locator_policy"]["excluded"]),
            "rules": [
                "Reuse an existing equivalent locator before creating a new locator.",
                "Use Child only when validation status is unique and target_matches is true.",
                "Use XPath only when validation status is unique and target_matches is true.",
                "Use OCR only with a recorded Region and visual corroboration.",
                "Use POS only as the final fallback and preserve all four coordinate values.",
                "A top-level popup without stable Root criteria may use a rootless BasePage plus isolated pos.yaml only when every selected Action has a frozen POS locator; it has no WindowView, Root locator, structural locator, or reuse owner and emits the POS fallback issue.",
                "PIC is default-deny and may be proposed only after structured locator failure and a passed cross-frame template audit.",
                "Generate a PIC locator only from a running transaction's passed pic_authorization_audit; copy exactly template_source to Bdd/data/target_data_path and use the frozen locator_name, Region locator, and threshold.",
                "Never call direct PIC APIs in generated Python; normal planned actions consume the authorized named PIC locator.",
                "An authorized PIC Region locator must reference the same sole top-level Root declared by its Plan window_owner.",
                "Root may reference only a top-level window; Region references Child/XPath.",
                "A WindowPage locator package has exactly one top-level Root; same-window View YAML files follow the existing WindowView model.",
                "Use WindowPage for one stable business top-level Window. Use WindowView for owned subpages; a transient child HWND does not by itself create another business Page.",
                "Observed runtime window titles remain evidence and are not promoted automatically into Root locator criteria.",
                "Declare every long-lived top-level Root in locator YAML; generated code must not call set_root.",
                "Generated Step Definitions and Page Objects must not contain inline locator dictionaries.",
            ],
        },
        "output_contract": {
            "mode": "draft_then_validate",
            "feature": "Do not rewrite source.feature unless explicitly requested.",
            "step_definitions": {
                "location": "Resolve the Feature Step scope under Bdd/steps/.",
                "rules": [
                    "Preserve the exact Gherkin Step expression.",
                    "Use page_method only for existing or evidence-supported reusable behavior; otherwise use step_inline_base_api for scenario-specific linear actions.",
                    "Do not duplicate an existing Step Definition.",
                    "Use get_page(context, PageClass) for a reusable business Page Object.",
                    "Import each PageClass directly from the Plan window_owner page_object module; generated Steps do not use package re-exports or dynamic factories.",
                    "For page_method, call page.method(...) or page.<declared_view_owner>.method(...).",
                    "For step_inline_base_api, call planned BasePage APIs directly on the canonical page or declared view, in operation order, using named locators from that window package.",
                    "Cross-window scenario-specific actions remain explicit in the Step through declared Page owners; repeated stable workflows may be extracted later.",
                    "Do not use get_script_page as a fallback for unknown ownership.",
                ],
            },
            "locators": {
                "location": "Reuse or create an appropriate YAML file under Bdd/locators/.",
                "rules": [
                    "Keep top-level Root locators separate from element locators.",
                    "Reuse or add top-level Root definitions in YAML instead of registering runtime Roots in generated Python.",
                    "A planned WindowView always shares its parent WindowPage Root; its YAML declares no top-level Root and active_locator names a child locator in that View.",
                    "Compile every generated locator with compile_locators before acceptance.",
                ],
            },
            "page_objects": {
                "location": "Reuse or create an appropriate module under Bdd/page_obj/.",
                "rules": [
                    "Use existing BasePage APIs before introducing a custom action.",
                    "A WindowPage exposes each planned WindowView through one direct property annotated with -> ViewClass and returning self.get_view(ViewClass), with ViewClass directly imported from the Plan view_object module.",
                    "A generated WindowView never declares an independent root_locator; it loads its locator file into the parent WindowPage package.",
                    "Do not create a Feature/Scenario-named Page merely to wrap recorded action order.",
                    "Form operation candidates from business intent and frozen facts before querying Action Knowledge; query only named candidates in deliberation order.",
                    "Action Knowledge separates capability facts, possibly incomplete maintainer guidance, and static assessment. Guidance never authorizes or blocks a Plan; static compatible is not runtime proof. Explain the choice and preserve frozen Slider bounds when set_slider_value is used.",
                    "Keep business meaning out of autowork_core/actions/.",
                ],
            },
            "data": {
                "location": "Store reusable values under Bdd/data/ when the Step contains data.",
                "rules": [
                    "Preserve Examples values, Step Data Tables, and text blocks.",
                    "Do not replace literal business expectations with guessed values.",
                ],
            },
        },
        "targeting_contract": {
            "catalog": "<recording-root>/catalog.json",
            "capability_catalog": "<recording-root>/capabilities.json",
            "session_index": "ai/target-index.json",
            "request_pattern": "ai/requests/request_*.json",
            "rules": [
                "Search resolved capabilities and existing code before generating a new implementation.",
                "When a generation request is provided, generate only its target Steps.",
                "Prefer exact id/key/ordinal/name matches before unique partial text matches.",
                "Use target_capture_generation_candidate for a targeted request; do not treat readiness as admission.",
                "Do not merge evidence from different runs unless explicitly requested.",
            ],
        },
        "confidence_contract": {
            "high": [
                "complete action evidence envelope",
                "validated unique Child or XPath that matches the recorded target",
                "stable target identity across linked events",
                "target state or media outcome corroborates the action when available",
            ],
            "review_required": [
                "OCR or POS fallback",
                "ambiguous/missing locator validation",
                "contradictory target identity, focus, state, or media evidence",
                "partial action evidence envelope for a required operation",
                "drag or middle click",
                "scroll direction/amount unavailable",
                "IME/composed text cannot be confirmed from target Value or screenshots",
                "assertion expected value is not present in Step or recorded evidence",
            ],
            "rules": [
                "Never silently upgrade review_required evidence to high confidence.",
                "A Window, Pane, Group, Custom, Document, or Canvas structural role is not by itself weak-target evidence.",
                "Keep structural role separate from locator validation, interaction identity, and outcome evidence.",
            ],
        },
        "memory_contract": {
            "role": "Advisory project experience for AI reasoning; never a deterministic code-ownership rule.",
            "precedence": [
                "current explicit user instruction",
                "current code and current Recorder evidence",
                "user-confirmed project memory",
                "accepted historical outcomes",
                "historical generation results",
                "provisional AI insights",
            ],
            "authorities": [
                "user_confirmed",
                "code_verified",
                "generation_result",
                "historical_case",
                "ai_inferred",
            ],
            "feedback": ["accepted", "revised", "rejected"],
            "rules": [
                "Explain why every used memory applies to the current target.",
                "Record rejected memories and the reason they do not apply.",
                "Use revised/rejected outcomes as corrections or negative examples.",
                "Treat ai_inferred insights as provisional until user-confirmed.",
                "Surface conflicting memories instead of silently choosing one.",
                "Memory failures must degrade gracefully and must not block generation.",
            ],
        },
        "plan_contract": {
            "command": (
                "python -m autowork_core.utils.debug_tools.recorder."
                "generation_workflow generate-job <job-path> "
                "[typed patch arguments only]"
            ),
            "protocol": "ai/plans/<request-id>/plan-*.json",
            "evidence_recovery": "ai/recovery/<request-id>.json",
            "mode": "compiled_from_generation_design",
            "covers": [
                "business intent",
                "ordered operations and ignored action IDs",
                "input values, sources, and bindings",
                "data source and binding",
                "assertion property/comparator/expected source",
                "pause-state meaning",
                "fallback policy",
                "Page Object ownership",
                "desktop WindowPage and WindowView ownership",
                "operation implementation_method and matching Gherkin Step delegation",
                "cross-window orchestration and window transitions",
                "recoverable capture/tree/action evidence gaps",
            ],
            "allowed_when_evidence_survives": [
                "capture errors with surviving structured actions and media",
                "missing after screenshot with event frames or video",
                "non-comparable trees for action Steps when media corroborates the outcome",
                "no normalized actions when raw events plus visual evidence survive",
            ],
            "cannot_override": [
                "invalid manifest or unresolved target Step identity",
                "missing selected Take directory",
                "simultaneously missing structured events/actions and visual evidence",
                "corrupt evidence that cannot be parsed or opened",
            ],
            "rule": (
                "The Reconciler produces facts, candidates, typed ambiguities, and agent tasks without drafting operations. "
                "Only user authority may request one structured Decision batch; "
                "the selected options constrain one GenerationDesignV1 compiled into GenerationPlanV4.2."
            ),
        },
        "media_contract": {
            "index": "<take>/media-index.json",
            "action_index": "<take>/action-media.json",
            "event_clock": "events.jsonl monotonic_ms, relative to input_capture_start",
            "video_clock": "media-index events[].video_ms, relative to step.mp4 start",
            "still_images": [
                "screenshots/before.png",
                "screenshots/after.png",
                "events.jsonl screenshot paths",
                "contact-sheet.png",
            ],
            "analysis_rule": (
                "For each action read action-media before and after first. Use commit/video_ms "
                "to inspect motion only when still evidence is insufficient. Do not treat Step "
                "before/after as an individual action result."
            ),
            "multi_window_rule": (
                "Read take.json target_windows and window_evidence. The primary window owns the "
                "screenshots/ui tree; additional windows use windows/window-XXX/."
            ),
            "window_lifecycle_rule": (
                "Use window_lifecycle first_seen/last_seen/opened/closed and admission. "
                "selected/automatic windows are trusted capture scope; provisional windows require "
                "a validated GenerationPlanV4.2 ambiguity resolution. An expected close "
                "may generate close/wait-not-exists behavior."
            ),
            "frame_extraction": (
                "python -m autowork_core.utils.debug_tools.recorder.media "
                "<take_dir> --event <event_id>"
            ),
        },
        "validation_contract": [
            "Compile generated Python modules.",
            "Compile all generated locator YAML through compile_locators.",
            "Run duplicate/undefined Step checks or a focused Behave dry-run.",
            "Report unresolved evidence instead of emitting unverified executable behavior.",
            "Do not overwrite existing business files without reviewing their current contents.",
            "Every generation decision claim must cite valid Evidence IDs and cover the request's minimum evidence set.",
            "Every default-selected Evidence ID must be claim-cited, explicitly used, or explicitly skipped with a reason; context consumption coverage must equal 1.0.",
            "Validate page_method operations inside their declared WindowPage/WindowView method and step_inline_base_api operations inside the matching Gherkin Step through canonical Page/View bindings; preserve global operation order and compile each window locator package atomically.",
            "For new PlanV4.2 code, validate canonical direct imports and typed get_page assignments; accept only page[.view].method for page_method or planned BasePage APIs for step_inline_base_api.",
            "Validate each window_owner resolution against the frozen Brief candidate when reusing; an explained create_new override of an explicit reuse_existing suggestion remains advisory and is reported as a warning, while ambiguous owner candidates remain context for generated-owner maintenance.",
            "Validate each implementation_resolution as reuse, modify, or create against the frozen method candidate and transaction change set.",
            "Reject newly generated set_root calls and inline locator dictionaries; preserve pre-existing code through a policy baseline.",
        ],
        "validation_commands": {
            "python_compile": (
                "python -B -m compileall -q <changed-python-paths>"
            ),
            "locator_compile": (
                "Load changed YAML with yaml.safe_load and call "
                "autowork_core.common.compile.compile_locators."
            ),
            "step_scope": (
                "python -m autowork_core.runtime.step_validation Bdd/steps "
                "--feature-path <source-feature>"
            ),
            "runtime_execution": (
                "python -B -m Bdd.runner <source-feature> "
                "--generation-transaction-report <report-path> "
                "--execution-request <request-path>"
            ),
            "runtime_execution_policy": (
                "Run only when Request ExecutionProfileV1 has runtime_policy=allowed; "
                "not_configured and external_manual stop as static_validated/runtime_not_run. "
                "Never infer launch or attach behavior from project defaults."
            ),
        },
    }
    contract["contract_hash"] = _hash_value(contract)
    return contract


def _generation_decision_authority_matrix():
    fields = [
        "decision_id",
        "system_when",
        "user_when",
        "ai_when",
        "forbidden",
        "validation_owner",
    ]
    decisions = [
        {
            "decision_id": "generation_admission",
            "system_when": "Request, Brief, readiness, profile, contract lease, evidence completeness, and source freshness are valid.",
            "user_when": "Before Job creation: repair or rerecord evidence. After pasted advance-job but before full generation: answer the Job-bound option batch for user-authority or authorization blockers.",
            "ai_when": "Never owns admission truth; Agent may only consume a Workbench-created claimed Job.",
            "forbidden": "Creating an active Job from failed admission, missing evidence, stale source, or unclaimed state.",
            "validation_owner": "generation_profile.project_generation_admission + generation_job_service.start_generation_job",
        },
        {
            "decision_id": "business_questions_decision_pack",
            "system_when": "DecisionPack projects one revision-bound option batch for user-authority or authorization questions and validates optional freeform BusinessFactPatch facts; AI-authority ambiguities stay in the Job context.",
            "user_when": "User answers declared options in the pre-generation Job-bound batch, and may provide separate freeform business text when the option set is insufficient; accepted submission automatically continues the same generation action.",
            "ai_when": "Resolves AI-authority business ambiguities through typed patch or Plan selection inside the claimed Job; transports user-authority selected options and structured freeform answers through submit-business-review-answers; never writes Answers JSON.",
            "forbidden": "Agent askQuestions prompts, answer drafts, retired submit-job-answers, duplicate media links, terminal parsing, external output reads, technical fields in Answers or BusinessFactPatch, evidence repair by answer, option guessing from freeform text, or repeated generation-phase questions.",
            "validation_owner": "decision_pack + generation_job_service.submit_generation_job_business_answers/submit_generation_job_business_facts + workflow_service._compiled_decision_patch",
        },
        {
            "decision_id": "window_owner",
            "system_when": "Existing WindowPage owner or new owner shape is proven by frozen root identity, owner candidates, and project scope.",
            "user_when": "Only when business ownership of a window is ambiguous in the settled DecisionPack.",
            "ai_when": "Only selects among frozen owner candidates or supplies a stable business owner name when the system cannot derive one.",
            "forbidden": "Changing root facts, criteria, root path, candidate proof, or claiming an unrecorded owner.",
            "validation_owner": "generation_design._compile_window_owners + generation_plan.validate_generation_plan",
        },
        {
            "decision_id": "window_view",
            "system_when": "Child-window evidence binds a same-root WindowView candidate, action_ids, locator_file, active_locator, and parent root.",
            "user_when": "Never for technical View structure; only upstream business window ownership can be user-owned.",
            "ai_when": "Only names or selects a View from frozen candidates when system proof is not unique.",
            "forbidden": "Independent View root, changed action_ids, changed native ownership proof, or unbound active_locator.",
            "validation_owner": "generation_design._validate_locked_child_window_views + implementation_manifest",
        },
        {
            "decision_id": "locator_key_locator_reuse",
            "system_when": "A verified existing locator key, legal public evidence name, or frozen locator candidate uniquely matches the recorded target.",
            "user_when": "Never for locator naming or technical locator choice.",
            "ai_when": "Only supplies public names for unresolved non-public names after reuse and legal evidence names fail.",
            "forbidden": "Overriding verified reuse keys, target fingerprints, locator mappings, roots, or snapshot proof.",
            "validation_owner": "locator_reuse + generation_design._locator_reuse_match + generation_validation",
        },
        {
            "decision_id": "operation_selection",
            "system_when": "Canonical command, semantic control state, implementation constraints, or exactly compatible operation is determined from evidence.",
            "user_when": "Only when the requested business action or expectation is ambiguous, before generation.",
            "ai_when": "Only chooses among registered compatible Plan operations when evidence leaves semantic implementation choice open.",
            "forbidden": "Incompatible operations, unregistered APIs, hidden technical controls, or changing target/value facts.",
            "validation_owner": "ai_capability_registry + action_knowledge + generation_design._compile_operation",
        },
        {
            "decision_id": "target_action_value_action",
            "system_when": "target_action_id, value_action_ids, action order, and action evidence are frozen by Request/Brief.",
            "user_when": "Never during generation; missing actions require evidence repair before Job admission.",
            "ai_when": "Never owns action identity; may only reference frozen IDs allowed by its minimal input schema.",
            "forbidden": "Inventing, swapping, ignoring, or reassigning recorded Action IDs outside Decision constraints.",
            "validation_owner": "generation_plan.validate_generation_plan + validate_plan_conformance",
        },
        {
            "decision_id": "value_source",
            "system_when": "Recorded value, feature literal, examples value, declared binding, runtime producer/consumer, or semantic literal is uniquely resolved.",
            "user_when": "When business meaning, table relationship, or declared value authority conflicts or is missing.",
            "ai_when": "Only selects from frozen value-source candidates when all values and provenance remain system/user-owned.",
            "forbidden": "Inventing literal values, replacing examples/feature data, or moving values between target and value Actions.",
            "validation_owner": "value_authority + generation_design._compile_value_source + generation_plan",
        },
        {
            "decision_id": "table_usage",
            "system_when": "No table exists, a single inferred table use is proven, or a user DecisionPack answer fixes the relationship.",
            "user_when": "When table rows, whole table, scenario state, reset behavior, or business relationship is ambiguous.",
            "ai_when": "Only maps confirmed table relationship into implementation ownership when system cannot derive the target structure.",
            "forbidden": "Treating table shape or business relationship as AI fact, altering table data, or skipping required user authority.",
            "validation_owner": "table_usage + decision_pack + generation_design._compile_table_use",
        },
        {
            "decision_id": "assertion_semantics",
            "system_when": "Observation intent, annotation, feature literal, examples value, or frozen assertion candidate proves expected behavior.",
            "user_when": "When expected business outcome, comparator, or observed-vs-expected meaning is missing or conflicting.",
            "ai_when": "Only selects a registered assertion operation from frozen candidates after target and expected value authority are fixed.",
            "forbidden": "Writing target facts, expected values, observed runtime evidence, or assertion proof.",
            "validation_owner": "semantic_reconciler assertion ambiguities + generation_design + generation_plan",
        },
        {
            "decision_id": "step_behavior_reuse_modify_create",
            "system_when": "Exact current-scope modify candidate or one frozen reuse candidate with complete ordered Action mappings is proven.",
            "user_when": "Never for technical reuse; user only resolves business scope or behavior conflicts before generation.",
            "ai_when": "Only chooses candidate_id/action_mappings among frozen Step candidates when more than one safe behavior remains.",
            "forbidden": "Changing call_sequence, candidate proof, Step scope, Gherkin pattern, or covering unmapped Actions.",
            "validation_owner": "generation_design._compile_step_behavior + generation_validation.validate_selected_generation_source_snapshot",
        },
        {
            "decision_id": "page_method_reuse_create",
            "system_when": "Existing Page method sequence, method scope, target/value mapping, and source hash are frozen and exactly verified.",
            "user_when": "Never for method selection; business behavior ambiguity must be resolved before Plan.",
            "ai_when": "Only selects among frozen method candidates or authors candidate method body within Manifest scope.",
            "forbidden": "Using hidden helpers as proof, changing candidate source, bypassing source hash, or selecting non-linear calls as exact proof.",
            "validation_owner": "code_reuse_index + generation_design._compile_method_resolution + generation_validation",
        },
        {
            "decision_id": "runtime_scenario_state",
            "system_when": "Producer/consumer bindings, scenario state names, runtime values, and step order are uniquely derived from evidence or user answers.",
            "user_when": "When the business state meaning or expected runtime handoff is unresolved.",
            "ai_when": "Only supplies semantic names or chooses among frozen runtime relationships without changing values.",
            "forbidden": "Creating runtime state to paper over missing evidence, replacing feature/examples values, or inventing producers.",
            "validation_owner": "generation_design runtime roles + generation_plan scenario model validation",
        },
        {
            "decision_id": "ocr_pos_pic_fallback",
            "system_when": "OCR/POS/PIC fallback evidence, coordinates, image assets, and PIC authorization are frozen and valid.",
            "user_when": "Only explicit PIC authorization or business acceptance when required by policy.",
            "ai_when": "Only selects a permitted fallback operation over frozen evidence when policy allows it.",
            "forbidden": "New PIC use, hidden screenshot inference, coordinate invention, or weakening locator priority.",
            "validation_owner": "generation_pic_policy + generation_design + generation_validation PIC audits",
        },
        {
            "decision_id": "implementation_file_scope",
            "system_when": "Manifest freezes allowed_changes, read_only_reuse, system_owned_changes, protected keys, paths, hashes, and lease.",
            "user_when": "Never during generation; user may accept or undo final editor changes.",
            "ai_when": "Only chooses semantics; normal system-owned writes are performed by system_materializer, and candidate artifacts are diagnostics rather than an Agent write path.",
            "forbidden": "Shell writes, path traversal, unleased files, read-only reuse edits, or weakening validation to pass.",
            "validation_owner": "implementation_manifest + generation_file_lock + generation_validation",
        },
        {
            "decision_id": "candidate_delivery_keep_undo",
            "system_when": "Content-addressed candidates are frozen under the Transaction lease, written by system_materializer on the normal path, recorded in implementation_receipt, and reviewed through implementation_diff.",
            "user_when": "User reviews the terminal Result changed count and implementation diff.",
            "ai_when": "Agent reports delivery_write_channel, review_channel, changed count, issues, and system_materializer delivery; it does not apply system-owned candidate_index lines on the normal path.",
            "forbidden": "Python/shell writes, Agent Bdd edits on the system-owned path, hiding changed counts, or treating a patch/draft/native edit claim as system delivery.",
            "validation_owner": "generation_orchestrator + job-implementation-candidate + finish_generation_transaction",
        },
        {
            "decision_id": "validation_result",
            "system_when": "Revision, contract, annotation, Python, locator, Step scope, policy, PIC, evidence, candidate, and Plan-to-Code checks pass.",
            "user_when": "User reviews terminal Job Result, warnings, and editor diff outcome.",
            "ai_when": "Only repairs manifest-scoped validation issues without changing Job goal, owner, scope, or acceptance model.",
            "forbidden": "Reporting intermediate states as success, replacing Jobs to reset timing, or hiding unresolved failures.",
            "validation_owner": "finish_generation_transaction + generation_job_result + runtime/oracle handoff",
        },
    ]
    return {
        "version": "1.0",
        "fields": fields,
        "decisions": decisions,
    }


def ensure_generation_contract(session_dir, *, write=True):
    session_dir = Path(session_dir).resolve()
    manifest_path = session_dir / "manifest.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.is_file()
        else {
            "locator_policy": {
                "priority": ["child", "xpath", "ocr", "pos"],
                "excluded": ["pic"],
            },
        }
    )
    if not isinstance(manifest, dict):
        raise ValueError(f"Recorder manifest必须是object: {manifest_path}")
    current = build_generation_contract(manifest)
    if write:
        path = session_dir / "ai" / "generation-contract.json"
        existing = {}
        if path.is_file():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = {}
        if existing != current:
            write_json_atomic(path, current)
    return current


def generation_contract_lease(session_dir, *, write=True):
    contract = ensure_generation_contract(session_dir, write=write)
    framework = contract.get("framework_contract") or {}
    value = {
        "generation_contract_lease_version": (
            GENERATION_CONTRACT_LEASE_VERSION
        ),
        "generation_contract_version": contract.get(
            "generation_contract_version"
        ),
        "framework_contract_version": framework.get("version"),
        "contract_hash": contract.get("contract_hash"),
        "api_signature_hash": framework.get("api_signature_hash"),
        "design_contract_fingerprint": (
            contract.get("generation_design_contract") or {}
        ).get("fingerprint"),
        "implementation_manifest_contract_fingerprint": (
            contract.get("implementation_manifest_contract") or {}
        ).get("fingerprint"),
        "contract_content_fingerprint": _hash_value(contract),
    }
    value["lease_fingerprint"] = _hash_value(value)
    return value


def generation_contract_lease_is_valid(value):
    if not isinstance(value, dict):
        return False
    fields = {
        "generation_contract_lease_version",
        "generation_contract_version",
        "framework_contract_version",
        "contract_hash",
        "api_signature_hash",
        "design_contract_fingerprint",
        "implementation_manifest_contract_fingerprint",
        "contract_content_fingerprint",
        "lease_fingerprint",
    }
    if set(value) != fields or value.get(
        "generation_contract_lease_version"
    ) != GENERATION_CONTRACT_LEASE_VERSION:
        return False
    if not all(
        isinstance(value.get(field), str) and bool(value.get(field))
        for field in fields - {"generation_contract_lease_version"}
    ):
        return False
    expected = _hash_value({
        key: item
        for key, item in value.items()
        if key != "lease_fingerprint"
    })
    return value.get("lease_fingerprint") == expected


def generation_contract_lease_matches(session_dir, value):
    return bool(
        generation_contract_lease_is_valid(value)
        and generation_contract_lease(session_dir, write=False) == value
    )


def current_framework_contract():
    return _framework_contract()


def compact_ai_capability_contract():
    framework = _framework_contract()
    plan_operations = set(framework["plan_operations"])
    methods = {
        name: signature
        for category in framework["allowed_base_page_apis"].values()
        for name, signature in category.items()
        if name in plan_operations
    }
    return {
        "ai_capability_registry_version": framework[
            "ai_capability_registry_version"
        ],
        "framework_contract_version": framework["version"],
        "api_signature_hash": framework["api_signature_hash"],
        "plan_operations": {
            name: _without_empty_capability_fields({
                "signature": _compact_ai_signature(methods[name]),
                "requires_value_action": capability_by_name(
                    name
                ).requires_value_action,
                "plan_validation_profile": (
                    None
                    if capability_by_name(name).plan_validation_profile
                    == "frozen_click_offset"
                    else capability_by_name(name).plan_validation_profile
                ),
            })
            for name in sorted(methods)
        },
        "policy": {
            "only_registered_operations": True,
            "special_parameter_and_evidence_rules_remain_fail_closed": True,
            "debug_only_apis_excluded": True,
        },
    }


def _compact_ai_signature(signature):
    signature = str(signature or "")
    if signature.startswith("(self, "):
        signature = "(" + signature[len("(self, "):]
    elif signature == "(self)":
        signature = "()"
    prefix, separator, _suffix = signature.partition(", *,")
    if separator:
        return prefix + ")"
    markers = (
        ", offset_x=",
        ", timeout=",
        ", wait_type=",
        ", visual_timeout=",
    )
    cut = min(
        (
            index
            for marker in markers
            for index in [signature.find(marker)]
            if index >= 0
        ),
        default=-1,
    )
    return signature[:cut] + ")" if cut >= 0 else signature


def _without_empty_capability_fields(value):
    return {
        key: item
        for key, item in value.items()
        if item not in (None, False, [], {})
    }


def _framework_contract():
    validate_base_page_action_classification(BasePage)
    methods = {
        category: {
            name: str(inspect.signature(getattr(BasePage, name)))
            for name in names
        }
        for category, names in ALLOWED_BASE_PAGE_APIS.items()
    }
    factories = {
        "get_page": str(inspect.signature(get_page)),
        "get_script_page": str(inspect.signature(get_script_page)),
    }
    window_model = {
        "window_page": {
            "class": f"{WindowPage.__module__}.{WindowPage.__qualname__}",
            "required_attributes": ["root_locator_file", "root_locator"],
        },
        "window_view": {
            "class": f"{WindowView.__module__}.{WindowView.__qualname__}",
            "required_attributes": ["locator_file"],
            "optional_attributes": ["active_locator", "root_locator"],
        },
            "rootless_pos_page": {
                "class": f"{BasePage.__module__}.{BasePage.__qualname__}",
                "required_attributes": ["locator_file"],
                "forbidden_attributes": ["root_locator_file", "root_locator"],
            },
        "rules": [
            "one WindowPage per stable desktop top-level Window",
                "a special top-level popup with no stable Root criteria and only frozen POS Actions may use one rootless BasePage",
            "one top-level Root per window locator package",
            "WindowView shares its WindowPage Root by default",
            "an evidence-backed child-window WindowView may own one isolated Root",
            "a unique native top-level window projected as a non-Window UIA bridge resolves through its exact-class handle",
            "launch mode resolves top-level Roots only from handles absent from the pre-launch desktop snapshot and never falls back to pre-existing windows",
            "new WindowPage instances are Scenario-scoped",
        ],
    }
    debug_only_methods = {
        name: str(inspect.signature(getattr(BasePage, name)))
        for name in DEBUG_ONLY_BASE_PAGE_APIS
    }
    api_signature_hash = _hash_value({
        "base_page": methods,
        "debug_only_base_page": debug_only_methods,
        "page_factories": factories,
        "window_model": window_model,
    })
    return {
        "version": FRAMEWORK_CONTRACT_VERSION,
        "ai_capability_registry_version": AI_CAPABILITY_REGISTRY_VERSION,
        "plan_operations": sorted(plan_operation_names()),
        "api_signature_hash": api_signature_hash,
        "architecture": {
            "feature": "Business language only; do not embed UI locators or action mechanics.",
            "step_definition": "Parse Gherkin arguments, keep scenario-specific linear orchestration visible, and delegate only stable reusable behavior.",
            "page_object": "Own reusable business behavior and call BasePage APIs.",
            "actions": "Framework-generic behavior only; generated business code must not be added here.",
            "locators": "Store reusable UI targets under Bdd/locators as YAML.",
            "data": "Store reusable business values under Bdd/data as YAML.",
        },
        "allowed_base_page_apis": methods,
        "debug_only_base_page_apis": debug_only_methods,
        "page_factories": factories,
        "window_model": window_model,
        "resource_references": {
            "$name": "Strictly resolve according to the receiving locator/data/visual parameter.",
            "$loc:name": "Force locator lookup.",
            "$data:name": "Force data lookup.",
            "$$name": "Literal value beginning with $.",
            "plain_string": "Literal value according to the receiving API; generated long-lived locator arguments use strict references.",
        },
        "step_scope": {
            "rule": (
                "Each Scenario uses one deterministic layered registry: "
                "Feature, optional Rule, then optional Scenario/Outline. "
                "An exact child definition overrides its parent; a missing "
                "child definition falls back to the parent."
            ),
            "explicit_tags": ["stepfile:", "step_file:", "steps:", "step:"],
            "allowed_owners": [
                "Feature",
                "Rule",
                "Scenario",
                "Scenario Outline",
            ],
            "forbidden_owners": ["Background", "Examples"],
            "default": (
                "Infer one matching *_step.py for the Feature layer when no "
                "Feature tag is declared."
            ),
            "binding": (
                "Recorder evidence is scope-neutral. Before Request creation, "
                "ScopeBindingV1 compares current business structure with the "
                "recorded snapshot and freezes each target Step behavior_file."
            ),
            "constraint": (
                "Non-exact cross-layer overlaps are ambiguous. Lifecycle "
                "callbacks remain Feature-layer only. Examples cannot select "
                "different code for rows of one Outline."
            ),
        },
        "forbidden": [
            "PIC locators without a passed transaction-bound authorization, or direct PIC API calls",
            "business behavior in autowork_core/actions",
            "unverified ambiguous Child/XPath candidates",
            "invented expected values or data",
            "editing unrelated Features or Steps",
            "new set_root calls in Recorder-generated Python",
            "new inline locator dictionaries in Recorder-generated Python",
        ],
        "source_files": [
            "autowork_core/page/singleton.py",
            "autowork_core/runtime/step_scope.py",
            "autowork_core/utils/debug_tools/recorder/table_usage.py",
            "autowork_core/common/compile.py",
        ],
    }


def _hash_value(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()