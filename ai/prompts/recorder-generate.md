# Recorder Generate

Accept only a Workbench-created Generation Job path. Never answer Decisions,
switch Profile, replace the Request, or reopen admission here.

```powershell
$gw = "autowork_core.utils.debug_tools.recorder.generation_workflow"
python -m $gw inspect-job <job-path>
python -m $gw start-job <job-path> --expected-epoch <epoch>
python -m $gw retire-job <job-path> --expected-epoch <epoch> --reason <reason> [--claim-id <id>]
python -m $gw design-contract
python -m $gw design-job <job-path> --claim-id <claim-id> --expected-epoch <epoch> --design-file <design.json>
python -m $gw prepare-job <job-path> --claim-id <id> --expected-epoch <epoch>
python -m $gw validate-job-implementation <report> --claim-id <id> --expected-epoch <epoch>
python -m $gw finish-job <report> --claim-id <id> --expected-epoch <epoch>
```

Use latest claim/epoch. Expand named uncertainties with `job-evidence`,
`job-compare-takes`, or `job-action-knowledge`. Edit only `ai_editable_changes`.
Before `prepare-job`, use `retire-job` for terminal authority/evidence gaps;
after `prepare-job`, use `abort-job`.

If `execution.runtime_policy=allowed`, run the bound profile and reconcile:

```powershell
python -B -m Bdd.runner <feature> --generation-transaction-report <report> --execution-request <request-path>
python -m $gw reconcile-job-runtime <job-path> --claim-id <id> --expected-epoch <epoch>
```

Repeat reconciliation only when the Job advances to required Oracle evidence.
Report completion from a terminal content-addressed Job Result.