# Recorder Adjust

Revise one Recorder GenerationDesign without editing Bdd files.

1. Accept one immutable `ai/requests/request_*.json` and inspect it:

   ```powershell
   python -m autowork_core.utils.debug_tools.recorder.generation_workflow inspect <request-path>
   ```

2. Route by state:
   - `ready`: a valid PlanV4.2 already exists; report it and stop.
   - `needs_adjustment`: ask the one Decision batch and submit declared option IDs.
   - `draft`: submit one complete GenerationDesignV1, whether the internal
     `next_action` says `submit_window_owned_plan` or
     `submit_decision_constrained_plan`.
   - `forensic`: expand only named evidence/candidates, then submit one complete
     Design covering every AI-authority ambiguity.
   - `failed`: revise only Design choices implicated by the bound failure once.
   - `blocked`/`stale`: report the exact evidence/refresh action and stop.
   - `running`/`completed`: do not replace the Design or Plan here.

3. Follow `ai/instructions/bdd-generation.md`. Design declares summary,
   window ownership/reuse, Step business intent, implementation strategy,
   operations, target Actions, value sources, table/runtime relationships,
   method/Step reuse choices, and AI ambiguity choices. Do not submit Scenario
   Model, Plan AST, paths, locators, Action/evidence proof sets, Annotation
   trace, or user Decision outcomes; the compiler owns them.

4. Submit exactly once:

   ```powershell
   python -m autowork_core.utils.debug_tools.recorder.generation_workflow design <request-path> --design-file <design.json>
   ```

   On rejection only, run read-only `validate-design`, repair the named
   AI-repairable choice, and resubmit once. If correct behavior requires a
   framework change, report `framework_defect` and stop.

5. Inspect once more and report status, `plan_path`, and remaining forensic
   requirements. Do not call `prepare`; this prompt never opens a Transaction.