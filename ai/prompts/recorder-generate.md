# Recorder Generate

Run one Workbench-created, already-claimed Generation Job. The returned
`next_commands` and `ai/instructions/bdd-generation.md` are authoritative.

- **RG-ENTRY**: Run the pasted full `generation_workflow advance-job <job>`
  command immediately in the main terminal tool. Do not delegate Recorder
  workflow commands to `execution_subagent` or any summary-only runner. Treat
  JSON stdout as protocol data. A summary that omits `next_commands`, typed
  batch, candidate files, or terminal result is a protocol output defect; use
  only returned Job-bound follow-up queries such as candidate manifest/diff pages, not
  chat resource files or terminal-output files. It is not permission to rerun bare
  `advance-job`. Do not read raw Job JSON, claim/recreate a Job, or reconstruct
  a command on the normal path. Use one literal full module command for returned
  workflow commands; no shell variables, chained commands, redirection, helper
  parsing commands, or CLI help. Treat `advance_summary.deterministic_progression`
  as proof of system-owned stages already executed in this `advance-job`; do not
  split settle/start/prepare/validate/finish into manual sub-commands.
- **RG-AUTH**: User-authority/authorization questions are owned by the Decision
  Pack and handled after Workbench submits the Job. When `advance-job` returns
  one `business_answers_required` batch, treat `next_commands.ask_questions` as
  the complete UI payload: call `vscode_askQuestions` once immediately with that
  payload's questions, clickable options, per-question freeform flags, and each
  question's own `message` `file:///` screenshot links, without summarizing or
  rewriting the questions first. Then submit selected options/freeform answers
  through `submit-business-review-answers` using only `selection_map` and
  `answer_argument_contract`; freeform is allowed only where the payload allows
  it. Never run
  `submit-business-review` or `submit-business-answers` on the normal path,
  invoke `vscode_askQuestions` without current `business_questions`, write answer
  drafts, map freeform text to an option by guess, or use retired `submit-job-answers`.
- **RG-TYPED**: For `typed_patch_requirement_batch.complete=true`, choose only
  from compact `requirements[].facts` and `choices`, fill `submit_arguments`, and
  execute `next_commands.primary` once. You may emit one concise stage-level
  progress message before this command; do not repeat the explanation, reread
  rules, read source files, or explore. Do not query `job-design-context`.
  Complete batches marked `recommendation.classification=system_verified_unique`
  are auto-submitted by `advance-job` before returning; any complete batch that
  is returned here remains an AI-decision boundary. Prefer an exact
  `verified_name_candidates` entry. When `recommended=true`, run
  `next_commands.primary` without reading requirements. An incomplete batch may
  use only its explicit `allowed_query`. If a complete batch has no primary,
  stop and report the framework defect; never rerun bare `advance-job` or inspect
  CLI help to rebuild it. Never edit `naming-patch.json` or submit a
  `GenerationDesign`.
- **RG-CANDIDATE**: Normal delivery is system-owned. Do not read
  `candidate_index`, `candidate_manifest`, source files, or write `Bdd/` when
  `advance-job` can materialize system-owned candidates. A completed system
  delivery reports `delivery_write_channel=system_materializer`,
  `review_channel=implementation_diff`, and
  `host_native_edit_available=false`; report that diff review channel instead
  of Agent/native edit delivery.
  `candidate_index`, `candidate_manifest`, `native_edit_plan`, and
  `edit_batches` are diagnostics, not delivery instructions. Use
  `job-implementation-candidate` only when an explicit diagnostic request names
  it; never use it to write `Bdd/`, and never use `--all`, full manifest reads,
  source/path searches, reformatting, normalization, generated source in chat,
  combined patches, or per-file narration.
  Stage-level progress messages are allowed only at system materialization,
  explicit diagnostic handoff, and terminal result.
  Candidate diagnostics may expose `generation_progress` and
  `agent_wait_ledger`; diagnostic buckets include `candidate_index_read`,
  `source_reads`, `editor_edits`, `after_native_edit`, and
  `editor_edits_done_to_after_native_edit_request`. Treat them as
  observation-only evidence. If `editor_edits_done_to_after_native_edit_request`
  is returned, report it as attribution only. Do not claim Hook auto-ran after
  edit; undone, partial, or SHA-mismatched materialization must fail closed
  through Transaction validation, with no shell/Python repair. If
  `candidate_mismatches` is reported, stop and report it; do not reapply
  candidate files.
- **RG-RESULT**: Read `terminal_result`, `implementation_diff_summary`,
  `unresolved_issues`, and `next_commands` from the terminal envelope. Report
  status, issue, write/review channel, changed-file count,
  `implementation_diff_summary.summary`, and the returned file-level diff stat
  preview. If `implementation_diff_summary.files_truncated=true`, label the
  section as a preview with displayed/total counts and use `job-code-diff` for
  the complete file stats and diff. Do not list raw `changed_files`, reopen a
  report on normal completion, or claim host-native edit review for
  system-materialized files.
- **RG-STOP**: Only identity, integrity, authorization, scope/lease, or internal
  candidate failure stops a started Job. Other technical uncertainty returns as a
  low-confidence implementation or issue; failure text leads with the owner
  category and reason. SLA exceeded never replaces, retires, resets, or restarts
  the Job.