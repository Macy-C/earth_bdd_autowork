# BDD Autowork Repository Instructions

For architecture or Recorder maintenance, read `ai/context/project.md`; for
asset lifecycle, migration, or host sync, read
`docs/维护/5.资产迁移与知识维护.md`. Current code, tests, and Recorder evidence are
the source of truth; local Copilot memory is only a cache. For normal
`/recorder-generate`, use `ai/prompts/recorder-generate.md`,
`ai/instructions/bdd-generation.md`, `generation_workflow inspect`, the compact
Brief, and named evidence/code candidates.

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
- Analysis, review, and risk-assessment requests are read-only unless the user
  explicitly asks to implement. Never revert unrelated changes; after an edit,
  validate the narrow behavior before expanding scope.
- For substantive project engineering, follow `project.md`'s context hygiene,
  Shadow, and validation workflow; Recorder maintenance also follows its
  Adaptive Work Loop. Do not apply fixed context/tool cutoffs or auto-treat a
  restored state as truth.
- Collaboration Review/Promotion run only on explicit request. Reports are
  proposals; rules change only for exact candidate IDs approved in the current
  request.
- For generated assets under `Bdd/features/**`, `Bdd/test_features/**`,
  `Bdd/steps/**`, `Bdd/page_obj/**`, `Bdd/locators/**`, or `Bdd/data/**`, follow
  `.github/instructions/bdd-generation.instructions.md`.