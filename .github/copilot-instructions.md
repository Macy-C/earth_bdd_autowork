# BDD Autowork Repository Instructions

For architecture or Recorder maintenance, read `ai/context/project.md`; for
asset lifecycle, migration, or host sync, read
`docs/维护/5.资产迁移与知识维护.md`. Current code, tests, and Recorder evidence are
the source of truth; local Copilot memory is only a cache. For normal
Recorder Generation work uses the explicitly selected workspace Agent and its
internal `recorder-generate` Skill, which
delegates to `ai/prompts/recorder-generate.md`,
`ai/instructions/bdd-generation.md`, the compact Job context, and named
evidence/code candidates.

- Preserve immutable raw evidence and append-only timeline edits. Keep V5
  file-based and fail-closed; never weaken Request, projection, Decision, Plan,
  transaction, PIC, scope, or Plan-to-Code validation to pass a workflow.
- Rules provide evidence, candidates, and safety gates; AI owns implementation
  reasoning. Reuse established owners such as `CaptureRuntime`,
  `RecorderWorkbench`, and `OperationCoordinator`.
- Before claiming current behavior, inspect its owner and a direct call path or
  relevant test. For material uncertainty, first use current evidence or one
  low-risk check; if unresolved, ask a concrete question with alternatives and
  consequences. Never guess or ask the user to settle evidence-checkable facts.
- Before substantial work, state one work package: user-visible outcome,
  authoritative evidence, allowed and excluded scope, next mutable action, and
  completion proof. A failed check may repair the same owner only; if it changes
  the goal, owner, protocol, attempt identity, allowed scope, or acceptance
  model, stop and seek approval before expanding work. A bare "continue", "next",
  "ok", or "try again" only performs the uniquely stated next action in the
  current work package; otherwise ask.
- Exception: inside the explicitly selected Recorder Generation Agent for a
  Workbench-created Job, do not output maintenance work packages, hypothesis
  narration, recovery/control-plane commentary, or per-Step progress. That
  journey is product generation: consume the already-claimed Job, ask at most
  one business batch, use batch context/candidates, and return the terminal
  Job result.
- If investigation no longer produces a falsifiable owner hypothesis, cheapest
  discriminating check, and small reversible edit, treat it as drift. Stop
  expanding searches, diagnostics, and adjacent fixes; restate the hypothesis
  and make the smallest testable change, or ask the user to choose among
  concrete scope alternatives. Do not present broad exploration or repeated
  failed patch attempts as progress.
- Before changing a user-facing workflow, state the normal user goal, action,
  automated system/AI responsibility, observable result, and remaining manual
  work. Do not call hidden, relocated, or renamed technical controls an
  optimization while users still perform their work; automate it or explain why
  it remains necessary and provide a safe path.
- Tests, budgets, benchmarks, replay, generated artifacts, and static checks
  are evidence, not product goals. Do not create a replacement Job, Transaction,
  or Run, or claim success solely to improve a proxy metric; a new attempt cannot
  reset, replace, or satisfy an earlier attempt's timing or outcome.
- Analysis, review, retrospective, design, and rollback planning are read-only
  unless the user explicitly authorizes implementation. Rollback requires an
  approved commit, change-set, path set, or hunk boundary; never infer a broader
  boundary from timestamps or conversation wording. Never revert unrelated
  changes; after an edit, validate the narrow behavior before expanding scope.
- For substantive project engineering, follow `project.md`'s context hygiene,
  Shadow, and validation workflow; Recorder maintenance also follows its
  Adaptive Work Loop. Do not apply fixed context/tool cutoffs or auto-treat a
  restored state as truth.
- Collaboration Review/Promotion run only on explicit request. Reports are
  proposals; rules change only for exact candidate IDs approved in the current
  request.
- For approval, authorization, rollback, Recorder Decision, and Collaboration
  Promotion requests, also apply [Approval Governance](instructions/approval-governance.instructions.md).
- Recorder lifecycle rules apply only while executing a bound Recorder
  Generation Job. Ordinary edits under `Bdd/` do not activate the
  Recorder Job protocol.