# BDD Autowork Generation Rules

## Goal And Authority

- Generate runnable, maintainable automation from the Feature, frozen evidence,
  and the Job's sealed Generation Capsule, not live workspace reads during
  generation. Feature/Rule/Examples/Data Table declarations are business
  authority; ask only for absent/conflicting business truth, missing evidence,
  or controlled PIC authorization.
- Each new Job also binds `generation_workspace_projection`, the current content
  package for existing assets, generated-result presence, and required structure
  files. Implementation Manifest consumes it for `current_content_projection`,
  package marker create/reuse, `read_only_reuse`, and `asset_resolution`.
  Candidate Preflight consumes it to materialize the projection-defined workspace
  slice for source files, read-only structure files, and direct local import
  dependencies; do not replace returned candidate/index commands or read live
  workspace because of it.
- Non-terminal entrypoints may expose `workspace_projection_summary` with the
  bound `projection_fingerprint` and compact reuse/modify/new/missing/
  user-modified counts. Terminal completed/failed entrypoints return a fixed
  result envelope instead; treat retired terminal results as audit evidence only.
  Existence and user modification are system facts from the current projection.
- Current facts, sealed capsule source, and frozen evidence outrank advisory
  memory/candidates. Live workspace code is checked by the commit gate, not
  used as a generation input after the Job is claimed.
- Canonical Action `command`, `observed_after`, and `business_expectation` are
  distinct. Never replace a command with its observed result or promote an
  observed result to a business expectation.
- AI owns the global operation, target/value role, Step/Page/View ownership,
  reuse, stable business naming, table use, and runtime relationship design.
  The system owns frozen candidates, proof, paths, scope, validation, and any
  deterministic Design/implementation slice it can prove from frozen facts.
  Once business questions are answered, already-correct deterministic content
  is generated directly instead of being re-designed by AI. The user owns
  unresolved business truth, evidence correction during recording/review, and
  PIC consent.

## Workflow And Design

- Start only from an immutable Workbench Job that Workbench has already claimed
  before copying its path. If the pasted Job is still ready, unclaimed, or lacks
  a `job_transition` in the `inspect-job` result, stop with a Host defect; do
  not manually claim or recover it. Do not read raw Job JSON to decide
  runtime phase: `job_transition` is a command projection. When Workbench has
  pasted a full command, run that command directly and reuse its Python runtime
  for returned `next_commands`; read `.copilot/recorder-runtime/python.json`
  only when no full command was provided. Workbench submission only blocks
  incomplete target Steps, hard evidence/readiness blockers, refresh, and Job
  creation failures. User-authority and authorization questions are owned by the
  Decision Pack and handled after the Job is submitted: `advance-job` projects
  the answer batch directly as `business_answers_required`. Present it once by calling
  `vscode_askQuestions` once with all questions, clickable options, and the
  returned per-question `allowFreeformInput` flags; preserve each question's `message` so its
  action-scoped verified before/after `file:///` screenshot links stay inside the question,
  not in a separate pre-question note; then submit selected options/freeform answers
  through `submit-business-review-answers` using only the returned selection/answer
  contract, and submit freeform only for questions whose payload allows it. Backend Decision Pack/BusinessFactPatch
  owns binding and persistence. AI-authority business ambiguities stay in the
  Job and are resolved by typed patch or Plan selection. A Job with missing
  Decision Answers must stop before full generation. `vscode_askQuestions` is
  allowed only for the current Job's projected `business_questions`; answer
  drafts, retired `submit-job-answers`, normal-path `submit-business-review`,
  and normal-path `submit-business-answers`
  are not product transports for Recorder Generation.
  A PIC candidate with no valid authorize option is system-denied and is not a
  user question.
  Additional/freeform answers contain only Step-bound business text and cannot
  patch technical Plan fields. Do not switch Profile or replace Request/Job.
  Do not run terminal, PowerShell, Python `-c`, here-doc, or here-string
  commands to parse, filter, or inspect the current Job response; do not read
  `chat-session-resources/content.txt` or other chat resource files to rebuild
  questions.
- Use the current compact command result and exact `job_transition`. Expand only
  named facts that can change Design. Avoid `--full`, full Graph/media/Plan/report.
- In a bound Skill journey, call `advance-job` first and continue with returned
  `next_commands`; run Recorder workflow commands in the main terminal tool,
  never through `execution_subagent` or a summary-only runner. Treat JSON stdout
  as protocol data. A summary missing `next_commands`, typed batch, candidate
  files, or terminal result is a protocol output defect; use only returned
  Job-bound follow-up queries such as candidate manifest/diff pages, not chat resource
  files or terminal-output files. It is not permission to rerun bare
  `advance-job`.
  Returned workflow commands must be one literal full Python module command; do
  not use shell variables, chained commands, redirection, helper parsing commands,
  or CLI help on the normal path.
  Normal Jobs may return `business_answers_required`; present all Decision Pack
  authority questions once through `vscode_askQuestions`, preserving per-question
  `file:///` screenshot-link `message` evidence, and run the returned answer command before
  continuing. If the
  system baseline can prove the whole frozen Job, it submits a
  `system_generated/system_baseline` Plan, freezes workspace candidate files,
  materializes system-owned changes through `system_materializer`, validates,
  and finishes with an implementation diff review channel. `candidate_index` and
  `candidate_manifest` remain diagnostic artifacts, not the normal edit list or
  delivery path. `advance-job` runs system-owned deterministic stages until it
  reaches an AI-decision, terminal, or failure boundary; use
  `advance_summary.deterministic_progression` as the record of already executed
  internal stages and do not split settle/start/prepare/validate/finish into
  manual sub-commands. If `advance-job` returns
  `design_required`, use its typed patch requirement batch and submit all
  complete direct typed patch arguments in one command. Query
  `job-design-context` only when the batch lists an explicit bounded
  `allowed_query`.
  Direct Jobs omit TaskBundle; paged Jobs may expose one as bounded input,
  but the normal journey does not dispatch fragments or divide global design
  ownership. Step count alone never selects a reasoning strategy.
- If Design validation rejects an AI choice, repair the same draft against the
  reported issue and resubmit under the same Job claim/epoch. Do not create
  another Job or submit Plan AST, paths, locators, proof sets, or user Decision
  outcomes.
- For `naming_patch_required`, use returned `naming_requirements` as the naming
  fact source. If they include target display/control/root/name hints, submit
  the direct NamingPatch without expanding `job-design-context`.
- All typed patch required responses must include `typed_patch_requirements`
  and, when more than one gap exists, `typed_patch_requirement_batch`.
  `complete=true` forbids expanding `job-design-context`; `complete=false` may
  use only its explicit `allowed_query`. Submit all complete requirements in
  one `advance-job` command. A single concise stage-level progress message is
  allowed before the command; repeated explanation, rule rereads, source reads,
  and exploratory analysis are not. A complete batch whose every requirement has
  `recommendation.classification=system_verified_unique` is submitted by
  `advance-job` itself before returning to the Agent; other returned complete
  batches remain AI-decision boundaries. If `recommended=true`, run
  `next_commands.primary` as already filled and do not read `requirements`. If a complete batch lacks
  `next_commands.primary`, stop and report a framework defect; do not rerun bare
  `advance-job`, inspect CLI help, or reconstruct commands. Missing requirements are framework defects, not
  permission for broad exploration.
- 生成期非超级问题转占位：after full-scenario generation starts, only
  identity/integrity, authorization, write-scope/lease/path, or system-candidate
  failures stop the Job. OCR/POS 能形成可执行兜底时不应直接阻断；
  empty popup roots with only valid POS actions use a rootless BasePage and
  isolated POS locator file, else placeholder;
  locator/reuse/window-close/scroll, unsupported assertion, technical binding,
  control-state, implementation/modeling uncertainty becomes issue/Review.
  未分类缺口仍是框架分类缺失；no outer fallback.
- Single and mixed typed patches use one atomic compilation path: merge all
  naming overrides and typed choices before building and validating baseline.
  While a typed marker is active, do not read project source, rules, prompts,
  Skills, or memory to recover from a rejected patch.
- Cover every Step/Action and preserve Given/When/Then. Use `step_inline` for
  scenario order/assertions and `page_method` only for reusable behavior.
- Every operation selects a registered operation, recorded window Root, exact
  `target_action_id`, and reason. Investigate `unknown`; `compatible` is not
  runtime proof and guidance may be wrong.
- Value sources are `recorded_action`, `feature_literal`, `semantic_literal`,
  `examples`, `data_table`, or `runtime`. Recorded values are operation-specific;
  feature literals must occur in frozen text; semantic literals are only for
  listed final-state operations. User outcomes come only from Answers.

## Reuse, Naming, And Code

- Inspect reusable candidates and dependencies; never copy/suffix to evade reuse.
- For a new owner whose recorded Root has an internal identity suffix, provide a
  stable ASCII snake_case `business_name`. Internal evidence IDs may remain in
  proof but never in public Page packages/classes or locator keys.
- Import Page/View classes directly and bind with `get_page`. No package
  re-exports, dynamic factories, direct pywinauto, inline locators, generated
  `set_root()`, fixed sleeps, placeholders, empty data files, or unrelated edits.
- One stable business window has one WindowPage. Use the existing WindowView
  model for owned subpages; a child HWND never creates another locator Root.
  Scenario order/assertions stay in Steps.
- Put targets in YAML: Child -> XPath -> OCR + Region -> POS. Preserve
  `evidence_name`; never cross Roots; PIC is exact and default-deny.
- A frozen `locator_reuse_match` is a verified current-recording match, not a
  naming suggestion: retain its existing locator key exactly and do not patch,
  rename, or replace it with the recorded temporary name. New verified targets
  in the same YAML may be added normally. When reuse cannot be proven or an
  existing key conflicts, select the typed issue placeholder; do not ask the
  user to rerecord solely for Locator maintenance.
- Observed titles are evidence, not Root criteria. Only explicit title/title_re
  constrains a Root; launch binds new scenario handles and never falls back.
- Select only frozen locators; never invent XPath/proof. Use strict `$loc:name`/
  `$name`; structural name is Accessible Name; OCR/PIC require Region.

## Tables And Runtime Values

- Preserve drag/scroll/collection parameters and semantic state. Proven
  incompatibility blocks; guidance does not.
- Use `remove_text` when the business declaration identifies one unique
  substring to delete from an editable Value; absence or repetition is a
  runtime failure, not permission to guess an occurrence or caret position.
- Parse a Data Table once. Design declares relationship, shape, owner, order,
  reset and columns. Never infer loops or hardcode rows.
- Runtime values require an F9-backed producer and explicit later consumer;
  equality/adjacency is not a binding and generated code never sets variables.

## Transaction, Execution, And Failure

- Do not edit business files before the Transaction is prepared. Do not edit
  business files as the Agent on the generated delivery path: `advance-job`
  materializes verified candidates through `system_materializer`. Query only
  needed Packet slices when an explicit diagnostic request names them; read-only
  assets remain immutable.
- Prepare freezes candidates for new WindowPage/WindowView classes, typed View
  properties, package markers, locator documents, and linear Step-inline
  BasePage calls. Every Step freezes all top-level Page bindings used by its
  operations. Before Transaction/file lease/system write, a complete candidate
  is reconstructed with Capsule sources in a private staging root and must pass
  Python, locator, Step-scope, and Plan-to-Code validation. `system_candidate_invalid` is a terminal framework
  defect with no workspace changes; never hand-edit or replay that candidate.
  Normal `advance-job` materializes system-owned candidate files directly,
  records `delivery_write_channel=system_materializer`, validates, and finishes.
  `candidate_index`, `candidate_manifest`, `native_edit_plan`, and `edit_batches`
  are not delivery artifacts. Diagnostic candidate artifacts may expose
  `generation_progress` and the observation-only `agent_wait_ledger`; treat
  candidate index counts and wait buckets (`candidate_index_read`,
  `source_reads`, `editor_edits`, `after_native_edit`, and
  `editor_edits_done_to_after_native_edit_request`) as diagnostic evidence only.
  Hook must
  not auto-run `after_native_edit`, and undone, partial, or SHA-mismatched
  materialization fails closed through Transaction validation rather than
  Agent repair. If `candidate_mismatches` is reported, stop and report the
  system materialization or validation defect; do not reapply candidate files.

  Never use `--all`, external output files, full manifest reads on the normal path, helper parsing commands, or Chat/JSON source bodies, and never infer missing text from
  `expected_sha256`. The next `advance-job`
  validates and finishes the same
  Transaction.
- `finish` owns diff, revision, leases, Python/locator/Step scope, policy, PIC,
  evidence, Code Manifest and Plan-to-Code. Success requires completed status
  without errors; `abort` archives drafts, releases the lease, and delegates
  workspace reversion to the current host. System materialization plus
  `implementation_diff` is the default code delivery for system-owned
  candidates. Terminal Job results must expose `delivery_visibility`,
  `delivery_summary`, `implementation_diff_summary`, and `acceptance_summary`, including
  `delivery_write_channel=system_materializer`,
  `review_channel=implementation_diff`, and
  `host_native_edit_available=false` for system-materialized files, plus
  file-level diff stat entries with additions/deletions from the bound
  implementation diff. Terminal normal-path output exposes `terminal_result` as
  the single user-facing result object and keeps only a bounded
  `implementation_diff_summary.files` preview; complete file stats and diff are
  read through `job-code-diff`. It omits duplicate delivery/visibility summaries,
  workspace projection, job lifecycle, stages, raw changed_files, full report,
  Plan, Manifest, and candidate payloads. Do not paste full source into Chat. An undone or partially applied explicit native-edit
  candidate must fail closed.
- On failure, report the authoritative category, first failed owner stage, and
  owner reason before SLA or other health warnings.
- If correct behavior cannot be represented by the current Contract, or requires
  changing `autowork_core`, `framework_validation`, framework prompts/rules, or
  framework tests, abort and report a concrete `framework_defect`. Framework
  maintenance is a separate task; never turn it into a user business question.
- Runtime requires the explicit Execution Profile and exact Transaction. Without
  authorization finish `static_validated/runtime_not_run`; never guess launch,
  defaults, global hooks, or attach targets.
- Generation input drift is prevented by the sealed Capsule. If live workspace
  changes before materialization, report the commit gate category such as
  `commit_rebase_required`; do not reread live files to redesign the same Job.
- A Behave pass is runtime evidence, not independent business-state proof. Use a
  registered Oracle when the journey/gate requires one.

## Efficiency

- Aim for one Request, one complete Design/Plan, and one Transaction. In
  `generation_first`, do not report, query, or discuss each Step separately.
  Use one uninterrupted Job loop: one global context pass, selective Step/code
  expansion, candidate application, and static diff.
- Ordinary static Jobs have a 300-second service-level target; Jobs with at
  least 100 BDD Steps have an 1800-second target through 600 BDD Steps. The clock
  starts when the Job path is submitted to the selected Recorder Generation
  Agent and includes time before the first
  tool call. Workbench supplies the trusted command timestamp when copying the
  already-claimed Job path; Hook metadata is optional guard evidence, not the
  timing source. Exceeding the target never stops, replaces,
  retires, or resets the Job: continue the same Job and report `exceeded`. A local
  partition benchmark is not evidence that the host model met this end-to-end
  target.
- Reduce repeated projections, context reads, confirmations, and transactions;
  never gain speed by truncating facts or weakening validation.
- Validate selected operations in one Design-driven Action Knowledge batch.
  Read all small implementation candidates in one bounded query; fall back to
  per-path reads only when the response explicitly omits bodies for size.