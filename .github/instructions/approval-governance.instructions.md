---
description: "Use when a request mentions approval, approve, review approval, promotion approval, 审批, 批准, 授权, 回滚授权, PIC authorization, Recorder Decision, or Collaboration Promotion."
name: "Approval Governance"
---

# Approval Governance

- Treat approval as a current-request scope boundary, not as evidence. User consent must not replace missing Recorder evidence, runtime facts, signed artifacts, or validation required by [Recorder 设计](../../docs/维护/3.Recorder设计.md).
- Classify the approval before acting:
  - Collaboration Promotion: only exact candidate IDs approved in the current request count. Review output is a proposal, not approval; use `/collaboration-promote` and follow [维护者指南](../../docs/维护/1.维护者指南.md).
  - Recorder Generation or Decision: only a bound Workbench Job may drive generation. Ask only for user-owned business truth, specification conflicts, evidence repair authority, or PIC authorization exposed by the Job; never ask the user to choose locators, owners, operations, code structure, or validation shortcuts.
  - Scope continuation: a bare "继续", "ok", "try again", or similar approval runs only the uniquely stated next action in the current work package. If the next action changes goal, owner, protocol, attempt identity, allowed scope, or acceptance model, ask for a new explicit approval.
  - Rollback or destructive change: require an explicit commit, change set, path set, or hunk boundary. Never infer rollback scope from timestamps, session history, or broad wording.
- For ambiguous approval, ask one concrete question with choices and consequences. Do not proceed on vague phrases such as "都行", "按你觉得好的", or "应用有益规则" when exact approval is required.
- When answering an approval request, name the approved boundary, the next mutable action, and the validation or proof that will close it.
- Keep analysis, review, retrospective, and rollback planning read-only until the user explicitly approves implementation.