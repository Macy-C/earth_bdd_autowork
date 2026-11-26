# BDD Autowork Repository Instructions

Current code, tests, and runtime artifacts are the source of truth; local Copilot
memory and Shadow recovery pointers are advisory caches.

## Architecture Red Lines

- **Layer separation**: `Bdd/` owns host project assets (Features, Steps,
  WindowPage/WindowView, locators/data YAML). `autowork_core/` owns reusable
  framework internals with no product-specific semantics. Neither `autowork_core/`
  nor `Bdd/` may import `framework_validation/`.
- **Single run-evidence owner**: `RunEvidenceCollector` owns normal BDD execution
  evidence, failure screenshots, and recording; reports and knowledge export read
  its sealed snapshot instead of re-capturing, and lightweight debug tools do not
  implicitly create a capture run.
- **Decoupled AI layer**: Core capabilities keep standalone Python/CLI entrypoints
  and must not depend on AI or a specific IDE. Keep one canonical workflow body per
  task under `ai/prompts/`, thin platform adapters under `.github/`, and AI-only
  helpers grouped by capability under `ai/tools/`.

## Reference Routing

- Detailed architecture and runtime lifecycle: `docs/维护文档/2.架构设计.md`.
- Asset lifecycle, migration, or host sync: `docs/维护文档/5.资产迁移与知识维护.md`.
- AI asset placement and entry index: `ai/README.md` and `.github/COPILOT.md`.
- Ordinary Feature/Scenario implementation: `ai/prompts/bdd-knowledge-generate.md`
  and knowledge-base role bindings in `Bdd/config.yaml` (`WEKNORA_SETTING.KNOWLEDGE_BASES`).

## Working & Approval Boundaries

- **Evidence before claims or questions**: Inspect the owning code and a direct
  call path or test before claiming behavior. Resolve checkable facts with low-risk
  checks first; ask one concrete question with options and consequences only when
  material ambiguity remains.
- **Read-only default and scoped edits**: Analysis, review, retrospective, design,
  and rollback planning are read-only unless implementation is requested. Keep edits
  within scope and preserve unrelated working-tree changes.
- **Explicit approval & rollback scope**: Approval defines scope, never substitutes
  for missing runtime facts or validation. A bare "继续" / "continue" / "ok" runs
  only the single explicitly stated next step; confirm before expanding goal, owner,
  or scope. Rollback or destructive changes require an explicit commit, file set, or
  hunk boundary. Collaboration Review/Promotion run only on explicit request, and
  rules change only for exact candidate IDs approved in the current turn (vague
  phrases like "都行" or "应用有益规则" are not approval).
- **Environment reuse**: Check the supplied or workspace-selected Python interpreter
  read-only and invoke it directly; if unresolved, ask which existing interpreter to
  use. Unless explicitly requested, do not run setup/install tools, create or switch
  environments, install Python/`uv`/packages/extensions, or modify interpreter/PATH
  settings; report missing dependencies for user direction.
- **Focused validation & honest delivery**: Run narrow checks covering the change
  and widen scope only when crossing public contracts, lifecycle boundaries, or
  conflicting results. Report unverified runtime or external device checks explicitly.
- **Context hygiene**: Use semantic milestones and Shadow recovery without fixed
  turn/tool cutoffs; never auto-treat a restored state as truth.
