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
from autowork_core.utils.debug_tools.recorder.generation_design import (
    compact_generation_design_contract,
)
from autowork_core.utils.debug_tools.recorder.implementation_manifest import (
    compact_implementation_manifest_contract,
)
from autowork_core.utils.debug_tools.recorder.writer import write_json_atomic


GENERATION_CONTRACT_VERSION = "6.17"
FRAMEWORK_CONTRACT_VERSION = "3.1"
GENERATION_CONTRACT_LEASE_VERSION = "1.0"

ALLOWED_BASE_PAGE_APIS = contract_api_groups()
DEBUG_ONLY_BASE_PAGE_APIS = debug_api_names()


def build_generation_contract(manifest):
    framework_contract = _framework_contract()
    design_contract = compact_generation_design_contract()
    implementation_contract = compact_implementation_manifest_contract()
    contract = {
        "schema_version": SCHEMA_VERSION,
        "generation_contract_version": GENERATION_CONTRACT_VERSION,
        "generation_file_lease_version": "2.0",
        "generation_design_contract": {
            "version": design_contract["design_version"],
            "fingerprint": _hash_value(design_contract),
        },
        "implementation_manifest_contract": {
            "version": implementation_contract[
                "implementation_manifest_version"
            ],
            "fingerprint": _hash_value(implementation_contract),
        },
        "framework_contract": framework_contract,
        "purpose": (
            "Generate evidence-traceable BDD code through a lightweight V3 "
            "Generation Brief with automatic forensic escalation and validation."
        ),
        "entrypoint": "ai/requests/<request-id>.json",
        "v3_workflow": {
            "inspect": (
                "python -m autowork_core.utils.debug_tools.recorder."
                "generation_workflow inspect <request-path>"
            ),
            "plan": (
                "python -m autowork_core.utils.debug_tools.recorder."
                "generation_workflow plan <request-path> "
                "[--step-id <step-id> | --section <section>]"
            ),
            "action_knowledge": (
                "python -m autowork_core.utils.debug_tools.recorder."
                "generation_workflow action-knowledge <request-path> "
                "[--step-id <step-id> --action-id <action-id>] "
                "[--operation <operation>]"
            ),
            "design_contract": (
                "python -m autowork_core.utils.debug_tools.recorder."
                "generation_workflow design-contract"
            ),
            "design": (
                "python -m autowork_core.utils.debug_tools.recorder."
                "generation_workflow design <request-path> "
                "--design-file <design.json>"
            ),
            "prepare": (
                "python -m autowork_core.utils.debug_tools.recorder."
                "generation_workflow prepare <request-path>"
            ),
            "finish": (
                "python -m autowork_core.utils.debug_tools.recorder."
                "generation_workflow finish <report-path>"
            ),
            "abort": (
                "python -m autowork_core.utils.debug_tools.recorder."
                "generation_workflow abort <report-path> --reason <reason>"
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
                "AI reads fact-first Generation Brief 4.4 by default and queries only disputed Evidence, Takes, or named operation capabilities on demand.",
                "AI reads content-addressed Plan Context 1.1 by default; the full immutable GenerationPlan remains backend identity and expands only through the plan query.",
                "Completed regeneration may reuse its bound Request and Plan; its own prior transaction result does not stale the Request, while newer feedback or other relevant memory still requires rematerialization.",
                "Semantic Reconciler must classify all default evidence before fast generation.",
                "AI submits GenerationDesignV1 semantic and implementation choices; the deterministic compiler creates one revision-bound GenerationPlanV4.2 with Scenario Model, target/value provenance, typed ambiguity, window, method resolution, runtime bindings, and a Generation Contract lease. Legacy Plan-shaped Intent remains readable.",
                "Generation Contract binds the exact GenerationDesign and Implementation Manifest contract versions and fingerprints; either schema change requires Request rematerialization.",
                "Inspect is side-effect-free for transaction state; prepare opens the running transaction lease.",
                "Prepare deterministically derives and freezes ImplementationManifestV1.7 from the validated Plan, Brief, and generation-root snapshot; AI edits only ai_editable_changes and treats system_owned_changes and read_only_reuse as immutable.",
                "Action Knowledge 1.2 projects Step/Action-scoped value_source qualification for AI-named operations without exposing values or ranking operations; the compiler re-resolves every source.",
                "Code Reuse Index 2.2 exposes only linear direct Page-operation call_sequence as exact-reuse proof; nested, conditional, helper, or otherwise non-linear methods do not receive exact sequence proof.",
                "Exact Page method reuse verifies the content-addressed candidate, ordered operation/target sequence, and generated Step method call arguments; locator read-only status requires a frozen locator/window-root candidate.",
                "Only user-authority ambiguity uses one revision-bound Decision Pack batch; AI submits one complete Design and the system compiles Plan structure and proof.",
                "Design covers every target Step; the system derives Scenario roles/support, owners/paths, locators, Action/Evidence closure, each exact annotation_ids set, Plan trace, and transaction lease.",
                "Forensic reads only required_forensic_evidence before adjustment.",
                "Finish runs revision, Annotation lease, Python, locator, Step scope, policy, controlled-PIC, evidence, and Plan-to-Code validation automatically.",
                "Every newly created nested Bdd/page_obj package contains an import-free __init__.py marker; it may be empty or docstring-only and never contains imports or re-exports.",
                "The default Brief exposes frozen facts and evidence-bound constraints, not semantic operation recommendations. AI forms operation candidates before querying Action Knowledge; only objectively incompatible target/runtime combinations are rejected, while unknown requires investigation or runtime validation.",
                "Legacy artifacts enter only through legacy_import and never reactivate an old state machine.",
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
                "artifact": "ai/generation-briefs/<request-id>.json",
                "required": True,
                "access": "default",
                "purpose": "Default compact AI context with reconciled actions, risk, plan, and revision seal.",
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
                "artifact": "GenerationTransaction.implementation_manifest",
                "required": True,
                "access": "after_prepare",
                "purpose": "Content-addressed edit task projection: allowed files, Gherkin patterns, dynamic inputs, methods, receivers, locator patches, package markers, and protected paths.",
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
                "PIC is default-deny and may be proposed only after structured locator failure and a passed cross-frame template audit.",
                "Generate a PIC locator only from a running transaction's passed pic_authorization_audit; copy exactly template_source to Bdd/data/target_data_path and use the frozen locator_name, Region locator, and threshold.",
                "Never call direct PIC APIs in generated Python; normal planned actions consume the authorized named PIC locator.",
                "An authorized PIC Region locator must reference the same sole top-level Root declared by its Plan window_owner.",
                "Root may reference only a top-level window; Region references Child/XPath.",
                "A desktop window package has exactly one top-level Root; View YAML files declare no top-level Root and reference only their package Root.",
                "Use WindowPage for one stable top-level Window and WindowView for subpages inside that Window.",
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
                    "Compile every generated locator with compile_locators before acceptance.",
                ],
            },
            "page_objects": {
                "location": "Reuse or create an appropriate module under Bdd/page_obj/.",
                "rules": [
                    "Use existing BasePage APIs before introducing a custom action.",
                    "A WindowPage exposes each planned WindowView through one direct property annotated with -> ViewClass and returning self.get_view(ViewClass), with ViewClass directly imported from the Plan view_object module.",
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
                "Use target_generation_ready for a targeted request; do not require unrelated pending Steps to be complete.",
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
                "generation_workflow design <request-path> "
                "--design-file <design.json>"
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
        "legacy_compatibility": {
            "intent_contract": (
                "python -m autowork_core.utils.debug_tools.recorder."
                "generation_workflow intent-contract"
            ),
            "adjust": (
                "python -m autowork_core.utils.debug_tools.recorder."
                "generation_workflow adjust <request-path> "
                "--plan-file <plan.json>"
            ),
            "policy": (
                "Historical Plan-shaped input remains readable only; new "
                "generation must use GenerationDesignV1 through design."
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
                "compatibility screenshots/ui tree; additional windows use windows/window-XXX/."
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
            "Validate each window_owner resolution against the frozen Brief candidate when reusing; an explained create_new override remains advisory and is reported as a warning.",
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
            "focused_execution": (
                "python -B -m Bdd.runner <source-feature> "
                "--generation-transaction-report <report-path> "
                "--execution-request <request-path>"
            ),
            "focused_execution_policy": (
                "Run only when Request ExecutionProfileV1 has runtime_policy=allowed; "
                "not_configured and external_manual stop as static_validated/runtime_not_run. "
                "Never infer launch or attach behavior from project defaults."
            ),
        },
    }
    contract["contract_hash"] = _hash_value(contract)
    return contract


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
        },
        "rules": [
            "one WindowPage per stable desktop top-level Window",
            "one top-level Root per window locator package",
            "WindowView shares its WindowPage Root",
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