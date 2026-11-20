---
name: report-merge
description: "Use when the user asks to generate a final test report, merge new or rerun results, delete specified Feature/Scenario/Example results, regenerate report HTML, preview changes, or analyze a Spark/BDD report. Supports explicit report paths and optional AI analysis."
argument-hint: "生成最终报告；合并本次结果；删除指定结果；重新生成 HTML；指定报告路径；只预览或分析"
---

# Report Merge

Interpret the requested action before using tools. The complete workflow for
creation, merging, and analysis is
[ai/prompts/report-merge.md](../../../ai/prompts/report-merge.md).

## Fixed Command Dispatch

For ordinary delete, render, and outline requests, this invocation adapter is
sufficient; do not load the longer workflow or core implementation first.
Use the existing confirmed Python executable and put global options before the action:

```text
PYTHON -B -m Bdd.final_report --json delete --feature-name "FEATURE" --scenario "SCENARIO"
PYTHON -B -m Bdd.final_report --json render
PYTHON -B -m Bdd.final_report --json inspect
```

Add `--target PATH` before the action only for a user-specified final report.
The command otherwise resolves the configured project path. An Example uses
`--example-id ID`; whole-Feature deletion omits scenario only when explicitly requested.
For preview add `--preview` before the action. On `blocked`, use the returned
candidates for the necessary clarification and retry the confirmed scope with
`--expected-target-sha256 HASH` before the action. On `completed` and `verified`,
return the result briefly without another inspection or verification script.

For ordinary delete, render, and report-outline requests, use the fixed JSON
commands in that workflow directly. Do not reread core implementations, inspect
session history, or invent verification scripts. Successful commands already
verify their results; report the returned result briefly. A blocked scope returns
candidates and a report hash for one necessary clarification and guarded retry.

Reuse [the existing report core](../../../autowork_core/runtime/reporting/final_report.py).
The [project entry](../../../Bdd/final_report.py), existing functions, and CLI
remain independently usable. Do not implement merging in this Skill or add an
AI-owned merge tool.

Use an existing confirmed interpreter. Do not install dependencies, create or
switch environments, modify project configuration, run tests, or upload reports
while executing a user's report task.

Normal rerun merging updates the results present in the source and preserves
unmentioned target results. Preview and execution must use the same scope and
options. Preview and analysis are read-only; analysis is not required for merging.
Only an explicit whole-group replacement request permits whole-group replacement.

Deleting results requires an explicit action and exact report scope. Reuse the
core's in-memory delete preview and file-level delete; do not delete report or
attachment files. HTML-only rendering reads the final JSON and must not rewrite
that JSON, merge history, or attachments. Neither action requires a new run report.