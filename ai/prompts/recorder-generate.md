# Recorder Generate

Follow `ai/instructions/bdd-generation.md`.

1. Inspect once:

   ```powershell
   python -m autowork_core.utils.debug_tools.recorder.generation_workflow inspect <request-path>
   ```

   Answer one Decision batch only for `needs_adjustment`. For `draft`, `ready`,
   or `forensic`, expand only named evidence/code candidates and submit one
   complete Design. Stop on `blocked`/`stale`. On `failed`, repair only the
   implicated AI choice once. Use compact `plan_context` for `completed`.

2. Submit the Design:

   ```powershell
   python -m autowork_core.utils.debug_tools.recorder.generation_workflow design-contract
   python -m autowork_core.utils.debug_tools.recorder.generation_workflow design <request-path> --design-file <design.json>
   ```

   Use `evidence`, `compare-takes`, `action-knowledge`, or `decision-media`
   only when needed. Rejected only: diagnose read-only with:

   ```powershell
   python -m autowork_core.utils.debug_tools.recorder.generation_workflow validate-design <request-path> --design-file <design.json>
   ```

   If the Contract cannot express it, report `framework_defect` and stop.

3. Transact:

   ```powershell
   python -m autowork_core.utils.debug_tools.recorder.generation_workflow prepare <request-path>
   python -m autowork_core.utils.debug_tools.recorder.generation_workflow validate-implementation <report>
   python -m autowork_core.utils.debug_tools.recorder.generation_workflow finish <report>
   ```

   Edit only `ai_editable_changes`. If repair cannot stay there, run
   `abort <report> --reason <reason>` and stop for separate maintenance.

4. Runtime: if Request `runtime_policy` is not `allowed`, report
   `static_validated/runtime_not_run`. Otherwise run only the bound profile:

   ```powershell
   python -B -m Bdd.runner <feature> --generation-transaction-report <report> --execution-request <request-path>
   ```