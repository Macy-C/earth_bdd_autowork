from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from config.paths import Paths


RUN_RESULT_VERSION = "1.3"
SUPPORTED_RUN_RESULT_VERSIONS = {"1.1", "1.2", RUN_RESULT_VERSION}
RUN_RESULT_PROVENANCE_VERSION = "1.0"
CURRENT_RUN_RESULT_PROVENANCE_VERSION = "1.1"
GENERATION_TRANSACTION_ENV = "AUTOWORK_GENERATION_TRANSACTION_REPORT"
GENERATION_PROVENANCE_FIELDS = {
    "provenance_version",
    "request_id",
    "request_fingerprint",
    "evidence_fingerprint",
    "revision_seal",
    "annotation_snapshot_fingerprint",
    "plan_id",
    "plan_fingerprint",
    "transaction_id",
    "completion_fingerprint",
    "result_fingerprint",
    "runtime_code_snapshot_fingerprint",
    "implementation_snapshot",
    "runtime_risk_policy_fingerprint",
}
LEGACY_GENERATION_PROVENANCE_FIELDS = (
    GENERATION_PROVENANCE_FIELDS - {"runtime_risk_policy_fingerprint"}
)


def generation_provenance_matches(run_result, expected):
    if not isinstance(run_result, dict) or not isinstance(expected, dict):
        return False
    actual = run_result.get("generation_provenance")
    return bool(
        _generation_provenance_is_valid(actual)
        and _generation_provenance_is_valid(expected)
        and actual == expected
    )


def load_generation_provenance(report_path, *, project_root=None):
    from autowork_core.utils.debug_tools.recorder.capability import (
        validate_completed_transaction_artifact_source,
        validate_accepted_transaction_capability_source,
    )
    from autowork_core.utils.debug_tools.recorder.generation_job import (
        generation_job_lease_is_valid,
    )
    from autowork_core.utils.debug_tools.recorder.workflow_state import (
        load_workflow_state,
    )

    project_root = Path(project_root or Paths.BASE_DIR).resolve()
    path = Path(str(report_path or ""))
    path = path.resolve() if path.is_absolute() else (
        project_root / path
    ).resolve()
    try:
        path.relative_to(project_root)
    except ValueError as error:
        raise ValueError(
            f"Generation transaction report 路径越界: {path}"
        ) from error
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(
            f"无法读取 Generation transaction report: {path}"
        ) from error
    if not isinstance(report, dict):
        raise ValueError("Generation transaction report 必须是 object")
    job_lease = report.get("generation_job_lease") or {}
    if generation_job_lease_is_valid(job_lease):
        session_dir, request, plan, _runtime_verification = (
            validate_completed_transaction_artifact_source(
                path,
                report,
                project_root=project_root,
            )
        )
        state = load_workflow_state(
            session_dir,
            report.get("request_id"),
        )
        pointer = state.get("current_job") or {}
        execution = state.get("job_execution") or {}
        transaction = execution.get("transaction") or {}
        if any((
            state.get("status") != "running",
            execution.get("phase") not in {"runtime", "oracle"},
            pointer.get("job_id") != job_lease.get("job_id"),
            pointer.get("job_fingerprint")
            != job_lease.get("job_fingerprint"),
            pointer.get("nonce") != job_lease.get("job_nonce"),
            transaction.get("transaction_id")
            != report.get("transaction_id"),
            transaction.get("result_fingerprint")
            != report.get("result_fingerprint"),
        )):
            raise ValueError(
                "Generation Job runtime phase与Transaction不一致"
            )
    else:
        session_dir, request, plan, _runtime_verification = (
            validate_accepted_transaction_capability_source(
                path,
                report,
                project_root=project_root,
            )
        )
    try:
        session_dir.relative_to(project_root)
    except ValueError as error:
        raise ValueError("Generation transaction Session 路径越界") from error
    provenance = generation_provenance_from_artifacts(
        request,
        plan,
        report,
    )
    if not _generation_provenance_is_valid(provenance):
        raise ValueError("Generation transaction provenance 不完整")
    return provenance


def generation_provenance_from_artifacts(request, plan, report):
    request = request if isinstance(request, dict) else {}
    plan = plan if isinstance(plan, dict) else {}
    report = report if isinstance(report, dict) else {}
    annotation = request.get("annotation_snapshot") or {}
    policy_fingerprint = (
        report.get("runtime_risk_policy") or {}
    ).get("fingerprint")
    provenance = {
        "provenance_version": (
            CURRENT_RUN_RESULT_PROVENANCE_VERSION
            if policy_fingerprint
            else RUN_RESULT_PROVENANCE_VERSION
        ),
        "request_id": request.get("request_id"),
        "request_fingerprint": request.get("request_fingerprint"),
        "evidence_fingerprint": request.get("evidence_fingerprint"),
        "revision_seal": (
            request.get("revision_snapshot") or {}
        ).get("seal"),
        "annotation_snapshot_fingerprint": annotation.get(
            "snapshot_fingerprint"
        ),
        "plan_id": plan.get("plan_id"),
        "plan_fingerprint": plan.get("plan_fingerprint"),
        "transaction_id": report.get("transaction_id"),
        "completion_fingerprint": report.get("completion_fingerprint"),
        "result_fingerprint": report.get("result_fingerprint"),
        "runtime_code_snapshot_fingerprint": report.get(
            "runtime_code_snapshot_fingerprint"
        ),
        "implementation_snapshot": json.loads(json.dumps(
            report.get("implementation_snapshot") or [],
            ensure_ascii=False,
        )),
    }
    if policy_fingerprint:
        provenance["runtime_risk_policy_fingerprint"] = policy_fingerprint
    return provenance


def _generation_provenance_is_valid(value):
    if not isinstance(value, dict):
        return False
    version = value.get("provenance_version")
    fields = set(value)
    if version == CURRENT_RUN_RESULT_PROVENANCE_VERSION:
        expected_fields = GENERATION_PROVENANCE_FIELDS
    elif version == RUN_RESULT_PROVENANCE_VERSION:
        expected_fields = LEGACY_GENERATION_PROVENANCE_FIELDS
    else:
        return False
    if fields != expected_fields or not isinstance(
            value.get("implementation_snapshot"), list):
        return False
    return all(
        isinstance(value.get(field), str) and bool(value.get(field))
        for field in expected_fields
        - {"provenance_version", "implementation_snapshot"}
    )




def publish_run_result(report_data, *, report_path, project_root=None):
    project_root = Path(project_root or Paths.BASE_DIR).resolve()
    report_path = Path(report_path).resolve()
    output_root = project_root / "artifacts" / "run-results"
    output_root.mkdir(parents=True, exist_ok=True)
    features = []
    for feature in (report_data or {}).get("features") or ():
        source_relpath = _project_relative(project_root, feature.get("file"))
        scenarios = []
        for scenario in _iter_scenarios(feature.get("scenarios") or ()):
            steps = []
            for step in scenario.get("steps") or ():
                attachments = [
                    item
                    for item in (
                        _attachment_record(
                            project_root,
                            report_path.parent,
                            attachment,
                        )
                        for attachment in step.get("attachments") or ()
                    )
                    if item is not None
                ]
                step_record = {
                    "keyword": str(step.get("keyword") or ""),
                    "name": str(step.get("name") or ""),
                    "status": str(step.get("status") or "unknown"),
                    "error": str(step.get("error") or "") or None,
                    "error_detail": str(step.get("errorDetail") or "") or None,
                    "attachments": attachments,
                }
                diagnostic = _runtime_diagnostic_record(
                    step.get("diagnostic")
                )
                if diagnostic is not None:
                    step_record["diagnostic"] = diagnostic
                steps.append(step_record)
            scenario_attachments = [
                item
                for item in (
                    _attachment_record(
                        project_root,
                        report_path.parent,
                        attachment,
                    )
                    for attachment in scenario.get("attachments") or ()
                )
                if item is not None
            ]
            scenarios.append({
                "name": str(scenario.get("name") or ""),
                "outline_name": str(scenario.get("outlineName") or "") or None,
                "example_id": str(scenario.get("exampleId") or "") or None,
                "params": dict(scenario.get("params") or {}),
                "status": str(scenario.get("status") or "unknown"),
                "steps": steps,
                "attachments": scenario_attachments,
            })
        features.append({
            "name": str(feature.get("name") or ""),
            "source_relpath": source_relpath,
            "status": str(feature.get("status") or "unknown"),
            "scenarios": scenarios,
        })
    payload = {
        "run_result_version": RUN_RESULT_VERSION,
        "started_at": (report_data or {}).get("startedAt"),
        "published_at": datetime.now().isoformat(timespec="seconds"),
        "duration": (report_data or {}).get("duration"),
        "status": _aggregate_status(features),
        "report": _snapshot_report(
            project_root,
            output_root,
            report_path,
        ),
        "features": features,
    }
    transaction_report = str(
        os.environ.get(GENERATION_TRANSACTION_ENV) or ""
    ).strip()
    if transaction_report:
        payload["generation_provenance"] = load_generation_provenance(
            transaction_report,
            project_root=project_root,
        )
    fingerprint = run_result_fingerprint(payload)
    payload["run_result_id"] = "run-result-" + fingerprint[:16]
    payload["fingerprint"] = fingerprint
    output = output_root / f"{payload['run_result_id']}.json"
    if not output.exists():
        _write_json_atomic(output, payload)
    _write_json_atomic(output_root / "latest.json", {
        "run_result_id": payload["run_result_id"],
        "fingerprint": fingerprint,
        "path": output.name,
    })
    return output


def latest_matching_run_result(
        feature_relpath,
        scenario_name,
        *,
        example_id=None,
        generation_provenance=None,
        project_root=None,
):
    project_root = Path(project_root or Paths.BASE_DIR).resolve()
    output_root = project_root / "artifacts" / "run-results"
    matches = []
    for path in output_root.glob("run-result-*.json") if output_root.exists() else ():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not run_result_identity_is_valid(value):
            continue
        if (
            generation_provenance is not None
            and not generation_provenance_matches(
                value,
                generation_provenance,
            )
        ):
            continue
        for feature in value.get("features") or ():
            if _normalized_path(feature.get("source_relpath")) != _normalized_path(feature_relpath):
                continue
            for scenario in feature.get("scenarios") or ():
                name = scenario.get("outline_name") or scenario.get("name")
                if str(name or "") != str(scenario_name or ""):
                    continue
                if str(scenario.get("example_id") or "") != str(example_id or ""):
                    continue
                matches.append((
                    _run_result_sort_key(value, path),
                    path,
                    value,
                    scenario,
                ))
    if not matches:
        return None
    _sort_key, path, value, scenario = max(matches, key=lambda item: item[0])
    return path, value, scenario


def _run_result_sort_key(value, path):
    return (
        str(value.get("published_at") or ""),
        str(value.get("started_at") or ""),
        str(value.get("run_result_id") or ""),
        Path(path).name,
    )


def verified_file_path(record, *, project_root=None):
    if not isinstance(record, dict):
        return None
    raw_path = record.get("path")
    expected_digest = str(record.get("sha256") or "")
    expected_size = record.get("size")
    if not raw_path or not expected_digest or expected_size is None:
        return None
    project_root = Path(project_root or Paths.BASE_DIR).resolve()
    path = (project_root / str(raw_path)).resolve()
    relative = _project_relative(project_root, path)
    if relative is None or not path.is_file():
        return None
    try:
        if path.stat().st_size != int(expected_size):
            return None
    except (OSError, TypeError, ValueError):
        return None
    if _file_sha256(path) != expected_digest:
        return None
    return relative


def run_result_identity_is_valid(value):
    if not isinstance(value, dict):
        return False
    fingerprint = run_result_fingerprint(value)
    return all((
        value.get("run_result_version") in SUPPORTED_RUN_RESULT_VERSIONS,
        value.get("fingerprint") == fingerprint,
        value.get("run_result_id") == f"run-result-{fingerprint[:16]}",
    ))


def run_result_fingerprint(value):
    return _stable_hash({
        key: item
        for key, item in dict(value or {}).items()
        if key not in {"run_result_id", "fingerprint"}
    })


def _runtime_diagnostic_record(value):
    if not isinstance(value, dict):
        return None
    required = {
        "diagnostic_version",
        "code",
        "category",
        "stage",
        "summary",
    }
    if (
        value.get("diagnostic_version") != "1.0"
        or not required <= set(value)
        or any(not isinstance(value.get(key), str) for key in required)
    ):
        return None
    allowed = required | {
        "backend",
        "entry_point",
        "locator_name",
        "locator_kind",
        "root_name",
        "root_state",
        "wait_type",
        "timeout_seconds",
        "interval_seconds",
        "probe_count",
        "candidate_count",
        "last_state",
        "cause_type",
        "cause_message",
        "artifacts",
    }
    return {
        key: value[key]
        for key in allowed
        if key in value
    }


def _iter_scenarios(values):
    for scenario in values:
        if scenario.get("type") == "outline":
            for example in scenario.get("examples") or ():
                yield {
                    **example,
                    "outlineName": scenario.get("name"),
                }
        else:
            yield scenario


def _attachment_record(project_root, report_dir, attachment):
    raw = str((attachment or {}).get("path") or "")
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = (report_dir / path).resolve()
    return {
        "name": str((attachment or {}).get("name") or ""),
        "type": str((attachment or {}).get("type") or "file"),
        **_file_record(project_root, path),
    }


def _file_record(project_root, path):
    path = Path(path).resolve()
    relative = _project_relative(project_root, path)
    if relative is None:
        return {
            "path": None,
            "exists": False,
            "sha256": None,
            "size": None,
        }
    result = {
        "path": relative,
        "exists": path.is_file(),
        "sha256": None,
        "size": None,
    }
    if path.is_file():
        result["sha256"] = _file_sha256(path)
        result["size"] = path.stat().st_size
    return result


def _snapshot_report(project_root, output_root, source):
    source = Path(source).resolve()
    source_record = _file_record(project_root, source)
    if not source_record["exists"]:
        return source_record
    suffix = source.suffix.casefold()
    if not suffix or len(suffix) > 12 or not suffix[1:].isalnum():
        suffix = ".bin"
    snapshot = (
        Path(output_root)
        / "reports"
        / f"{source_record['sha256']}{suffix}"
    )
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    if not snapshot.exists():
        temporary = snapshot.with_name(
            f"{snapshot.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            shutil.copyfile(source, temporary)
            copied = _file_record(project_root, temporary)
            if any((
                copied["sha256"] != source_record["sha256"],
                copied["size"] != source_record["size"],
            )):
                raise RuntimeError("运行报告在快照期间发生变化")
            temporary.replace(snapshot)
        finally:
            temporary.unlink(missing_ok=True)
    snapshot_record = _file_record(project_root, snapshot)
    if any((
        snapshot_record["sha256"] != source_record["sha256"],
        snapshot_record["size"] != source_record["size"],
    )):
        raise RuntimeError("运行报告快照完整性校验失败")
    return snapshot_record


def _file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _project_relative(project_root, value):
    path = Path(str(value or ""))
    if not path.is_absolute():
        path = (project_root / path).resolve()
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        return None


def _normalized_path(value):
    return str(value or "").replace("\\", "/").casefold()


def _aggregate_status(features):
    statuses = {
        str(scenario.get("status") or "unknown")
        for feature in features
        for scenario in feature.get("scenarios") or ()
    }
    if "failed" in statuses:
        return "failed"
    if statuses and statuses <= {"passed"}:
        return "passed"
    if statuses and statuses <= {"skipped"}:
        return "skipped"
    return "unknown"


def _stable_hash(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _write_json_atomic(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)
