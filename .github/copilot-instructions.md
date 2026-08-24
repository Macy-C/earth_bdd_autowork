# BDD Autowork Repository Instructions

Before architecture, Recorder maintenance, or migration work, read
`ai/context/project.md`. Treat that versioned document and current code/tests as
the source of truth; local Copilot memory is only a cache and may be missing or
stale. For a normal `/recorder-generate` task, do not load the full maintenance
context by default. Follow `ai/prompts/recorder-generate.md`,
`ai/instructions/bdd-generation.md`, the `generation_workflow inspect` output,
the compact Brief, and only named evidence/code candidates. Load
`ai/context/project.md` only if the task changes framework architecture or an
unresolved protocol/ownership question requires it.

- Preserve immutable raw recording evidence and append-only timeline edits.
- Keep the active V5 generation path file-based and fail-closed; do not add a
  database, service, queue, or free-text Plan mutation path.
- Keep rules as evidence, candidates, and safety gates. AI still owns complete
  implementation reasoning; Decision answers only constrain the Plan.
- Reuse the shared `CaptureRuntime`, `RecorderWorkbench`,
  `OperationCoordinator`, and established repository/application boundaries.
- Do not weaken Request, projection, Decision, Plan, transaction, PIC, scope,
  or Plan-to-Code validation merely to make a workflow pass.
- Before stating how the current repository implements, exposes, or
  automatically performs a behavior, inspect the owning implementation and one
  direct call path or relevant test. If that evidence is unavailable, state
  the uncertainty instead of inferring behavior from labels or documentation.
- When the user asks only for analysis, review, or risk assessment without
  explicitly requesting implementation, keep the task read-only. Classify
  findings as verified defects, hypotheses needing a discriminating check, or
  documented design behavior; report severity and next steps before any
  implementation. Do not batch-fix findings or run repeated full regressions
  until the user explicitly asks to implement. When implementation is also
  requested, proceed with the smallest verified slice.
- Never revert unrelated working-tree changes. Validate the narrow behavior
  immediately after the first edit.
- For substantive project engineering work other than normal Recorder
  generation, follow the reversible context
  hygiene in `ai/context/project.md`. Recognize semantic milestones internally,
  but never auto-discard context, auto-switch sessions, or stop at a fixed
  turn/token/tool budget. A new independent epic only prompts a fresh-session
  suggestion after the current task is handled.
- Summarize large tool output in default context while preserving the command,
  actionable errors, and raw-output path. Expand the original whenever detail
  is uncertain or relevant; this presentation rule never limits tools or
  evidence access.
- Keep framework documentation at a stable abstraction level: document
  reusable contracts, owners, configuration, and failure behavior. Keep
  product-specific Page names, executable order, fixed waits, device steps,
  and temporary debugging conclusions in project context, code/tests, or a
  scoped runbook. Examples must be generic or clearly scoped and must not
  become a second source of runtime truth.
- For substantive project engineering work, maintain the local Shadow
  Companion at valuable semantic milestones without asking for task-level
  approval. Follow `ai/context/project.md`: never write tool-by-tool logs,
  overwrite a conflicting active Capsule, or treat restored state as truth.
  Complete work deletes its Capsule; stale, ambiguous, or mismatched state
  always falls back to normal full investigation.
- For Recorder maintenance, follow the default Adaptive Work Loop in
  `ai/context/project.md`; never impose fixed turn/tool cutoffs or capability
  limits. Reuse only unchanged, current evidence and validation; uncertainty,
  user requests, repository gates, and new failures always require fresh work.
- Use focused checks while editing, then a final adversarial review and one
  risk-appropriate full Recorder regression after code and findings converge
  by default. After a full-suite failure, isolate the relevant tests before
  rerunning the suite. Any further full run requires a relevant edit,
  environment change, contradictory result, repository gate, or explicit user
  request; state the reason before starting. When sessions overlap, designate
  one session as the sole owner of desktop, OCR, ffmpeg, and real-window
  validation; other sessions avoid those resources.
- A host-project sync of an already validated framework release is not
  Recorder maintenance: do not copy `framework_validation/` or rerun its full
  suite by default. Run `framework_smoke` after the sync; run host tests only
  when host-owned `Bdd/` or `config/` also changed.
- Treat collaboration-review reports as proposals only. Do not update user
  memory, instructions, prompts, or AI context unless the user explicitly
  approves exact candidate IDs in the current request.
- Collaboration Review and Promotion are manual framework-maintainer
  workflows. Run them only when explicitly requested; never prompt for them
  from session counts, elapsed time, project activity, or SessionStart. The
  repository does not authenticate organizational roles, so operator
  authorization remains an external governance responsibility.
For generated assets under `Bdd/features/**`, `Bdd/test_features/**`,
`Bdd/steps/**`, `Bdd/page_obj/**`, `Bdd/locators/**`, or `Bdd/data/**`, also
follow `.github/instructions/bdd-generation.instructions.md`.