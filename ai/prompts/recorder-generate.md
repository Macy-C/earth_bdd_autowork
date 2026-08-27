# Recorder Generate

Accept only a Workbench-created Generation Job. Do not answer Decisions, switch
Profile, replace the Request, or reopen admission.

```powershell
$gw = "autowork_core.utils.debug_tools.recorder.generation_workflow"
python -m $gw inspect-job <job>
python -m $gw start-job <job> --expected-epoch <epoch>
python -m $gw design-job <job-path> --claim-id <claim-id> --expected-epoch <epoch> --design-file <design.json>
python -m $gw prepare-job <job> --claim-id <claim> --expected-epoch <epoch>
python -m $gw job-implementation-packet <report> [--step-id <id> | --path <path>]
python -m $gw validate-job-implementation <report> --claim-id <claim> --expected-epoch <epoch>
python -m $gw finish-job <report> --claim-id <claim> --expected-epoch <epoch>
```

Read `design_context`; use the latest `job_transition` for every next CAS
command. Do not infer or re-inspect an epoch.
`job-design-context --step-id`, `job-evidence`, `job-compare-takes`, and
`job-action-knowledge` are only for detail that can change the Design; do not
use `inspect-job --full` normally. After prepare, read only the needed Packet
slice and edit only `ai_editable_changes`.

For an authority/evidence gap, use `retire-job` before prepare and `abort-job`
afterward. If runtime is allowed, run the bound profile and reconcile. Report
only the terminal content-addressed Job Result.