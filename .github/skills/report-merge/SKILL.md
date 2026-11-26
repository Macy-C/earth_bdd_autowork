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

Add `--target PATH` before the action only for a user-specified final report;
otherwise the command uses the configured project path. Use `--example-id ID` for a
single Example, omit `--scenario` only for explicit whole-Feature deletion, and add
`--preview` before the action for read-only previews. On `blocked`, clarify using the
returned candidates and retry with `--expected-target-sha256 HASH` before the action.
On `completed` and `verified`, report the returned result briefly without reading core
implementations, inspecting session history, or writing extra verification scripts.

## Ownership And Scope

Reuse [the existing report core](../../../autowork_core/runtime/reporting/final_report.py)
and [project entry](../../../Bdd/final_report.py), which remain independently usable
from CLI or any IDE without this Skill. Do not implement merging in this Skill, run
product tests, upload reports, delete report/attachment files, or alter environments;
for creation, rerun merging, whole-group replacement, and failure analysis rules,
follow [ai/prompts/report-merge.md](../../../ai/prompts/report-merge.md).