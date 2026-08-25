# BDD Autowork Generation Rules

## Goal And Authority

- Generate correct, runnable, maintainable automation from the Feature, frozen
  recording evidence, and current project code. Protocol and speed are means,
  not the product goal.
- Feature/Rule/Examples/Data Table declarations are business authority. Do not
  ask the user to confirm a fact already stated there. Ask only when business
  facts are absent or contradictory, evidence is missing or contradictory, or
  controlled PIC authorization is required.
- Current user facts, code, and frozen evidence outrank memory. Memory and reuse
  candidates are advisory, never runtime proof.
- Canonical Action `command`, `observed_after`, and `business_expectation` are
  distinct. Never replace a command with its observed result or promote an
  observed result to a business expectation.
- AI owns Action grouping, operation and target choice, value authority,
  Step/Page ownership, reuse, locator and public business naming, table use,
  runtime relationships, and complete implementation reasoning. The compiler
  owns proof fields and physical paths. The user owns unresolved business truth,
  wrong/missing evidence, and PIC authorization.

## Workflow And Design

- Normal generation starts only from a Workbench-created immutable Generation
  Job. Decisions are answered before claim. After `start-job`, never answer a
  Decision, switch Profile, replace the Request, or ask the user mid-generation;
  terminate with a structured authority/evidence gap instead.
- Read `inspect-job` and the compact Brief first. Expand only named evidence, Takes,
  code candidates, Action Knowledge, or Decision media that could change the
  Design. Do not load full Graph/media/Plan by default.
- Submit one complete GenerationDesignV1. Do not submit Plan AST, file paths,
  locators, Action/evidence proof sets, or user Decision outcomes.
- Cover every target Step and effective business Action. Preserve Given/When/Then
  roles and explain business intent. Use `step_inline` for scenario-specific
  order/assertions and `page_method` only for independently reusable behavior.
- Every operation selects a registered operation, recorded window Root, exact
  `target_action_id`, and concise reason. Investigate `unknown`; static
  `compatible` is not runtime proof. Guidance can be wrong.
- Value sources are `recorded_action`, `feature_literal`, `semantic_literal`,
  `examples`, `data_table`, or `runtime`. `recorded_action` resolves an
  operation-specific command/state, never generic observed value.
  `feature_literal` references frozen `step_text`/`text_block`; if the whole
  reference is not one value, supply an exact non-empty literal present in that
  declaration. `semantic_literal` is only for contract-listed final-state
  operations, not assertions.
- All genuine user questions must already be complete in the Job admission
  receipt. AI selects only declared AI-authority outcomes; user outcomes come
  from frozen Answers; evidence-required outcomes terminate for correction or
  rerecording and cannot create a new Decision inside the Job.

## Reuse, Naming, And Code

- Search existing Step/Page/View/locator/data and confirmed Capability before
  creating files. Exact Gherkin or call-sequence matches do not prove behavior;
  inspect the implementation and dependencies. Never create `_2`, `_new`, or a
  copied variant to avoid a reuse decision.
- For a new owner whose recorded Root has an internal identity suffix, provide a
  stable ASCII snake_case `business_name`. Internal evidence IDs may remain in
  proof but never in public Page packages/classes or locator keys.
- Import planned Page/View classes directly and bind with `get_page`. Do not use
  package re-exports, dynamic factories, direct pywinauto, inline locator dicts,
  generated `set_root()`, fixed sleeps, placeholders, empty data files, or
  unrelated refactors.
- One top-level window has one WindowPage and one Root. Use WindowView only for a
  stable same-window subpage with an activation boundary. Keep one-off order,
  cross-window flow, and scenario assertions in Steps.
- Put targets in YAML. Priority: Child -> XPath -> OCR + Region -> POS. A rename
  uses the same Action's `evidence_name`; packages cannot cross Roots. PIC is
  default-deny and must use the exact authorized template, Region and threshold.
- Select only a frozen `locator_candidate_id`; omit it for the default. Never
  invent XPath/proof. Targets use `$loc:name`/`$name`, never bare; structural
  `name` is Accessible Name. OCR/PIC require Region; no default fallback.

## Tables And Runtime Values

- Preserve frozen drag offsets, scroll direction/steps, collection order/limits,
  and semantic control state. Prefer a final-state API only when independently
  justified; proven incompatibility blocks, guidance does not.
- Parse a Data Table once. Design declares business relationship, shape,
  execution owner, ordering, reset behavior, and every column meaning. Never
  infer iteration from shape or hardcode rows. Scenario-specific loops stay in
  the Step unless verified reusable Page behavior owns them.
- Runtime values require an F9-backed producer (`save_text`/`save_attr`) and a
  later explicit consumer. Do not infer links from equal values or adjacency,
  replace Feature/Examples authority, persist runtime values in data, or call
  generated `set_variable`.

## Transaction, Execution, And Failure

- Do not edit code before `prepare-job`. During a running GenerationTransaction edit
  only Manifest `ai_editable_changes`; Request, Brief, Answers, Plan, evidence,
  `system_owned_changes`, and `read_only_reuse` are immutable.
- `finish` owns actual diff, revision, Contract/Annotation/file leases, Python,
  locator, Step scope, policy, PIC, evidence, Code Manifest, and Plan-to-Code
  checks. Report success only for `completed`/`completed_no_changes` without
  errors. Use `abort` to archive drafts and restore the baseline.
- If correct behavior cannot be represented by the current Contract, or requires
  changing `autowork_core`, `framework_validation`, framework prompts/rules, or
  framework tests, abort and report a concrete `framework_defect`. Framework
  maintenance is a separate task; never turn it into a user business question.
- Runtime requires the Request's explicit Execution Profile and the exact
  completed Transaction. Without allowed execution, finish as
  `static_validated/runtime_not_run`; never infer launch, inherit project
  defaults, call a global product hook, or guess an attach target.
- A Behave pass is runtime evidence, not independent business-state proof. Use a
  registered Oracle when the journey/gate requires one.

## Efficiency

- Aim for one Request, one complete Design/Plan, and one Transaction on a simple
  journey. Re-query only after a discriminating failure.
- Reduce repeated projections, context reads, confirmations, and transactions;
  never gain speed by truncating facts or weakening validation.