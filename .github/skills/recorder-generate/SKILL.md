---
name: recorder-generate
description: "Generate validated BDD Autowork code from one Workbench Recorder Generation Job. Use only inside the selected Recorder Generation Agent."
argument-hint: "Absolute path to a Workbench-created Generation Job JSON"
user-invocable: false
---

# Recorder Generate

This procedure is for the selected `Recorder Generation` Agent only. The Job,
returned `next_commands`, and `ai/instructions/bdd-generation.md` are the source
of truth. The full module is
`autowork_core.utils.debug_tools.recorder.generation_workflow`.

- **RG-ENTRY**: Run the pasted full `advance-job` command first in the main
  terminal tool. Do not delegate Recorder workflow commands to
  `execution_subagent` or any summary-only runner. Treat JSON stdout as protocol
  data. A summary that omits `next_commands`, typed batch, candidate files, or
  terminal result is a protocol output defect; use only returned Job-bound
  follow-up queries such as candidate manifest/diff pages, not chat resource files or
  terminal-output files. It is not permission to rerun bare `advance-job`. Do not inspect
  raw Job JSON, manually claim/recreate a Job, or reread rules on the normal path.
  Use one literal full module command for returned workflow commands; no shell
  variables, chained commands, redirection, helper parsing commands, or CLI help.
  Treat `advance_summary.deterministic_progression` as proof of system-owned
  stages already executed in this `advance-job`; do not split
  settle/start/prepare/validate/finish into manual sub-commands.
- **RG-AUTH**: User-authority/authorization questions are owned by the Decision
  Pack and handled after Workbench submits the Job. When `advance-job` returns
  one `business_answers_required` batch, treat `next_commands.ask_questions` as
  the complete UI payload: call `vscode_askQuestions` once immediately with that
  payload's questions/options/freeform flags, without summarizing, rewriting,
  explaining, or expanding the questions first. Then map the returned selection
  only through `selection_map`/`answer_argument_contract` and submit
  options/freeform answers through `submit-business-review-answers`. Never run
  `submit-business-review` or `submit-business-answers` on the normal path,
  invoke `vscode_askQuestions` without current `business_questions`, write
  answers JSON, map freeform text to an option by guess, or turn technical
  uncertainty into a user question.
- **RG-TYPED**: A complete batch is self-contained: choose from its compact
  `facts` and `choices`, fill its `submit_arguments`, and run its primary command
  once. You may emit one concise stage-level progress message before this
  command; do not repeat the explanation, reread rules, read source files, or
  explore. Complete batches marked
  `recommendation.classification=system_verified_unique` are auto-submitted by
  `advance-job` before returning; any complete batch that is returned here
  remains an AI-decision boundary. Prefer an exact `verified_name_candidates` entry. Incomplete batches
  may use only their named bounded query. When `recommended=true`, run the primary command
  without reading requirements. If a complete batch has no primary
  command, stop and report the framework defect; never rerun bare `advance-job`
  or inspect CLI help to rebuild it. Do not edit `naming-patch.json` or submit a
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
  source/path searching, reformatting, normalization, generated source in chat,
  combined patches, or per-file narration.
  Stage-level progress messages are allowed only at system materialization,
  explicit diagnostic handoff, and terminal result.
  Candidate diagnostics may expose `generation_progress` and the
  observation-only `agent_wait_ledger`; diagnostic buckets include
  `candidate_index_read`, `source_reads`, `editor_edits`, `after_native_edit`,
  and `editor_edits_done_to_after_native_edit_request`. Silence and SLA are not
  product completion. Treat `editor_edits_done_to_after_native_edit_request` as
  attribution only. Undone, partial, or SHA-mismatched materialization fails closed
  through Transaction validation; never repair them with shell/Python writes. If
  `candidate_mismatches` is reported, stop and report it; do not reapply
  candidate files.
- **RG-RESULT**: Use `terminal_result`, `implementation_diff_summary`,
  `unresolved_issues`, and `next_commands` directly. Report changed-file count,
  write/review channel, runtime/oracle state, service level, issues,
  `implementation_diff_summary.summary`, and the returned file-level diff stat
  preview. If `implementation_diff_summary.files_truncated=true`, label it as a
  preview with displayed/total counts and use `job-code-diff` for the complete
  file stats and diff. Do not list raw `changed_files` or normal-success
  reports. Run returned `job-code-diff` only when explicit diff review is required.
- **RG-STOP**: Continue non-super technical gaps as returned issue/low-confidence
  output. Stop only on the authoritative failure category and owner reason. SLA
  exceeded never replaces, retires, resets, or restarts the Job.

Never write `Bdd/` as the Agent on the normal system-materialized path, write through shell/Python, query `job-implementation-candidate --all`, read the full manifest on the normal path,
reformat candidate text, compose a multi-file patch, or emit setup/per-file progress.