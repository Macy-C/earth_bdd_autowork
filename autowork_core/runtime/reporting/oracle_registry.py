from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import psutil

from autowork_core.utils.debug_tools.recorder.writer import write_json_atomic


ORACLE_REGISTRY_VERSION = "1.1"
ORACLE_RECEIPT_VERSION = "1.0"
ORACLE_EVIDENCE_VERSION = "1.0"
RUNTIME_MATRIX_RECEIPT_VERSION = "1.1"
ENVIRONMENT_SNAPSHOT_VERSION = "1.0"
EXECUTION_BINDING_VERSION = "1.0"
PROJECT_ORACLE_REGISTRY_PATH = Path("Bdd/ai/quality/oracles.json")


_ORACLES = {
    "fail_closed": {
        "version": "1.0",
        "independent": True,
        "allowed_statuses": {"passed"},
        "evidence_type": "expected_failure_snapshot",
        "outcome": "fail_closed",
        "adapter": "run_result_failed",
    },
}


def oracle_registry_projection(project_root=None):
    oracles = dict(_ORACLES)
    if project_root is not None:
        oracles.update(_load_project_oracles(project_root))
    projection = {
        "oracle_registry_version": ORACLE_REGISTRY_VERSION,
        "oracles": {
            oracle_id: {
                "oracle_id": oracle_id,
                "version": value["version"],
                "independent": value["independent"],
                "allowed_statuses": sorted(value["allowed_statuses"]),
                "evidence_type": value["evidence_type"],
                "outcome": value["outcome"],
                "adapter": value["adapter"],
                **{
                    key: value[key]
                    for key in (
                        "field",
                        "expected",
                        "error_contains",
                        "backend",
                        "window",
                        "control",
                        "property",
                        "process",
                    )
                    if key in value
                },
                "definition_fingerprint": _oracle_definition_fingerprint(
                    oracle_id,
                    value,
                ),
            }
            for oracle_id, value in sorted(oracles.items())
        },
    }
    projection["fingerprint"] = _fingerprint(projection)
    return projection


def validate_runtime_matrix_receipt(
        receipt,
        risk_policy,
        provenance,
        *,
        project_root=None,
    ):
    errors = []
    if not isinstance(receipt, dict):
        return ["runtime matrix receipt missing"]
    payload = {
        key: value for key, value in receipt.items()
        if key != "fingerprint"
    }
    if any((
        receipt.get("runtime_matrix_receipt_version")
        != RUNTIME_MATRIX_RECEIPT_VERSION,
        receipt.get("fingerprint") != _fingerprint(payload),
        receipt.get("risk_policy_fingerprint")
        != (risk_policy or {}).get("fingerprint"),
        receipt.get("generation_provenance") != provenance,
    )):
        errors.append("runtime matrix receipt identity invalid")
    variants = receipt.get("variants")
    if not isinstance(variants, list):
        return [*errors, "runtime matrix variants invalid"]
    by_role = {}
    registry = oracle_registry_projection(project_root)
    if registry.get("fingerprint") != (risk_policy or {}).get(
            "oracle_registry_fingerprint"):
        errors.append("oracle registry fingerprint mismatch")
    for variant in variants:
        if not isinstance(variant, dict):
            errors.append("runtime matrix variant invalid")
            continue
        role = str(variant.get("role") or "")
        if not role or role in by_role:
            errors.append(f"runtime matrix duplicate or missing role: {role}")
            continue
        by_role[role] = variant
        if variant.get("generation_provenance") != provenance:
            errors.append(f"runtime matrix provenance mismatch: {role}")
        if not str(variant.get("run_result_id") or ""):
            errors.append(f"runtime matrix run result missing: {role}")
        if not str(variant.get("run_result_fingerprint") or ""):
            errors.append(f"runtime matrix run fingerprint missing: {role}")
        try:
            run_result = load_persisted_matrix_run_result(
                project_root,
                variant,
            )
        except (OSError, TypeError, ValueError) as error:
            run_result = None
            errors.append(
                f"runtime matrix persisted Run Result invalid: {role}: {error}"
            )
        outcome = str(variant.get("outcome") or "")
        required = next((
            item for item in (risk_policy or {}).get("required_matrix") or ()
            if item.get("role") == role
        ), None)
        if required is None:
            errors.append(f"runtime matrix undeclared role: {role}")
        elif outcome not in set(required.get("allowed_outcomes") or ()):
            errors.append(f"runtime matrix outcome invalid: {role}/{outcome}")
        oracle_outcomes, oracle_errors = _validate_oracle_receipts(
            variant.get("oracle_receipts"),
            role,
            provenance,
            registry,
            project_root=project_root,
            run_result=run_result,
        )
        errors.extend(oracle_errors)
        try:
            _verify_environment_snapshot(
                project_root,
                role,
                variant.get("environment_snapshot"),
                run_result=run_result,
                oracle_receipts=variant.get("oracle_receipts"),
            )
        except (OSError, TypeError, ValueError) as error:
            errors.append(
                f"runtime matrix environment snapshot invalid: {role}: {error}"
            )
        if outcome and outcome not in oracle_outcomes:
            errors.append(
                f"runtime matrix outcome oracle missing: {role}/{outcome}"
            )
    required_roles = {
        str(item.get("role") or "")
        for item in (risk_policy or {}).get("required_matrix") or ()
        if item.get("role")
    }
    missing = sorted(required_roles - set(by_role))
    extra = sorted(set(by_role) - required_roles)
    if missing:
        errors.append(f"runtime matrix missing roles: {missing}")
    if extra:
        errors.append(f"runtime matrix extra roles: {extra}")
    return errors


def load_persisted_matrix_run_result(project_root, variant):
    if project_root is None or not isinstance(variant, dict):
        raise ValueError("project root or variant missing")
    project_root = Path(project_root).resolve()
    output_root = (project_root / "artifacts/run-results").resolve()
    relative = Path(str(variant.get("run_result_path") or ""))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Run Result path invalid")
    path = (project_root / relative).resolve()
    try:
        path.relative_to(output_root)
    except ValueError as error:
        raise ValueError("Run Result path outside output root") from error
    if not path.name.startswith("run-result-") or path.suffix != ".json":
        raise ValueError("Run Result filename invalid")
    content = path.read_bytes()
    if any((
        hashlib.sha256(content).hexdigest()
        != variant.get("run_result_file_sha256"),
        len(content) != variant.get("run_result_file_size"),
    )):
        raise ValueError("Run Result file fingerprint mismatch")
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Run Result file invalid") from error
    if not isinstance(value, dict):
        raise ValueError("Run Result must be an object")
    if any((
        value.get("run_result_id") != variant.get("run_result_id"),
        value.get("fingerprint") != variant.get("run_result_fingerprint"),
    )):
        raise ValueError("Run Result identity mismatch")
    return value


def seal_runtime_matrix_receipt(value):
    result = dict(value or {})
    result["runtime_matrix_receipt_version"] = RUNTIME_MATRIX_RECEIPT_VERSION
    result.pop("fingerprint", None)
    result["fingerprint"] = _fingerprint(result)
    return result


def publish_runtime_matrix_receipt(project_root, value):
    project_root = Path(project_root).resolve()
    receipt = seal_runtime_matrix_receipt(value)
    transaction_id = str(
        (receipt.get("generation_provenance") or {}).get("transaction_id")
        or ""
    )
    if not transaction_id:
        raise ValueError("Runtime matrix transaction_id is required")
    output_root = project_root / "artifacts/run-results/runtime-matrices"
    output_root.mkdir(parents=True, exist_ok=True)
    path = output_root / f"runtime-matrix-{receipt['fingerprint'][:16]}.json"
    if not path.exists():
        path.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    index_path = output_root / "latest-by-transaction.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        index = {}
    if not isinstance(index, dict):
        index = {}
    index[transaction_id] = {
        "path": path.name,
        "fingerprint": receipt["fingerprint"],
    }
    index_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def latest_runtime_matrix_receipt(project_root, transaction_id):
    output_root = (
        Path(project_root).resolve()
        / "artifacts/run-results/runtime-matrices"
    )
    index_path = output_root / "latest-by-transaction.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
        record = index.get(str(transaction_id)) or {}
        path = (output_root / str(record.get("path") or "")).resolve()
        path.relative_to(output_root.resolve())
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError, AttributeError):
        return None
    if (
        not isinstance(value, dict)
        or value.get("fingerprint") != record.get("fingerprint")
        or seal_runtime_matrix_receipt(value) != value
    ):
        return None
    return path, value


def seal_oracle_receipt(value, *, project_root=None):
    result = dict(value or {})
    oracle_id = str(result.get("oracle_id") or "")
    registry = oracle_registry_projection(project_root)
    registration = (registry.get("oracles") or {}).get(oracle_id)
    if registration is None:
        raise ValueError(f"Oracle is not registered: {oracle_id}")
    result["oracle_receipt_version"] = ORACLE_RECEIPT_VERSION
    result["producer"] = "framework_oracle_runner"
    result["oracle_registry_version"] = ORACLE_REGISTRY_VERSION
    result["oracle_registry_fingerprint"] = registry["fingerprint"]
    result["oracle_version"] = registration["version"]
    result["oracle_definition_fingerprint"] = registration[
        "definition_fingerprint"
    ]
    result["outcome"] = registration["outcome"]
    result.pop("fingerprint", None)
    result["fingerprint"] = _fingerprint(result)
    return result


def execute_registered_oracle(
        project_root,
        oracle_id,
        evidence_path,
        provenance,
        *,
        run_result=None,
    execution_binding=None,
    ):
    project_root = Path(project_root).resolve()
    registry = oracle_registry_projection(project_root)
    registration = (registry.get("oracles") or {}).get(str(oracle_id))
    if registration is None:
        raise ValueError(f"Oracle is not registered: {oracle_id}")
    evidence_path = Path(evidence_path).resolve()
    evidence_root = (
        project_root / "artifacts/run-results/oracle-evidence"
    ).resolve()
    try:
        relative = evidence_path.relative_to(project_root).as_posix()
        evidence_path.relative_to(evidence_root)
    except ValueError as error:
        raise ValueError("Oracle evidence path is outside trusted root") from error
    observation = _capture_registered_observation(
        registration,
        run_result,
        execution_binding,
    )
    if not _captured_observation_is_valid(
            observation,
            registration,
            execution_binding,
    ):
        raise ValueError("Framework Oracle observation identity is invalid")
    evidence = {
        "oracle_evidence_version": ORACLE_EVIDENCE_VERSION,
        "producer": "framework_oracle_runner",
        "oracle_id": str(oracle_id),
        "oracle_definition_fingerprint": registration[
            "definition_fingerprint"
        ],
        "generation_provenance": provenance,
        "run_result_id": (
            (run_result or {}).get("run_result_id")
            if isinstance(run_result, dict)
            else None
        ),
        "run_result_fingerprint": (
            (run_result or {}).get("fingerprint")
            if isinstance(run_result, dict)
            else None
        ),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "observation": observation,
    }
    evidence["fingerprint"] = _fingerprint(evidence)
    write_json_atomic(evidence_path, evidence)
    content = evidence_path.read_bytes()
    passed = _execute_oracle_adapter(registration, evidence, run_result)
    return seal_oracle_receipt({
        "oracle_id": str(oracle_id),
        "status": "passed" if passed else "failed",
        "generation_provenance": provenance,
        "run_result_id": (
            (run_result or {}).get("run_result_id")
            if isinstance(run_result, dict)
            else None
        ),
        "evidence": [{
            "evidence_type": registration["evidence_type"],
            "path": relative,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
        }],
    }, project_root=project_root)


def validate_oracle_receipts(
        project_root,
        receipts,
        provenance,
        *,
        run_result,
        role="simple_path",
    ):
    project_root = Path(project_root).resolve()
    registry = oracle_registry_projection(project_root)
    outcomes, errors = _validate_oracle_receipts(
        receipts,
        str(role),
        provenance,
        registry,
        project_root=project_root,
        run_result=run_result,
    )
    return {
        "status": "passed" if not errors else "failed",
        "outcomes": sorted(outcomes),
        "errors": errors,
    }


def capture_registered_execution_binding(
        project_root,
        oracle_id,
        *,
        process_id,
        window_handle,
    ):
    registry = oracle_registry_projection(project_root)
    registration = (registry.get("oracles") or {}).get(str(oracle_id))
    if registration is None:
        raise ValueError(f"Oracle is not registered: {oracle_id}")
    if registration.get("adapter") != "uia_property_equals":
        raise ValueError("Execution binding is only valid for UIA Oracle")
    return _live_execution_binding(
        registration,
        process_id=int(process_id),
        window_handle=int(window_handle),
    )


def publish_environment_snapshot(
        project_root,
        role,
        run_result,
        oracle_receipts,
        effective_environment,
    ):
    from autowork_core.runtime.reporting.run_result_bridge import (
        run_result_identity_is_valid,
    )

    project_root = Path(project_root).resolve()
    if not run_result_identity_is_valid(run_result):
        raise ValueError("Environment snapshot Run Result is invalid")
    if not isinstance(oracle_receipts, list) or not oracle_receipts:
        raise ValueError("Environment snapshot requires Oracle receipts")
    evidence_records = []
    execution_bindings = []
    evidence_root = (
        project_root / "artifacts/run-results/oracle-evidence"
    ).resolve()
    for receipt in oracle_receipts:
        if not isinstance(receipt, dict):
            raise ValueError("Environment snapshot Oracle receipt is invalid")
        if any((
            receipt.get("producer") != "framework_oracle_runner",
            receipt.get("run_result_id") != run_result.get("run_result_id"),
        )):
            raise ValueError("Environment snapshot Oracle receipt mismatch")
        for record in receipt.get("evidence") or ():
            path = (project_root / str(record.get("path") or "")).resolve()
            path.relative_to(evidence_root)
            content = path.read_bytes()
            if any((
                hashlib.sha256(content).hexdigest() != record.get("sha256"),
                len(content) != record.get("size"),
            )):
                raise ValueError("Environment snapshot Oracle evidence drifted")
            value = json.loads(content.decode("utf-8"))
            if not isinstance(value, dict) or value.get(
                    "producer") != "framework_oracle_runner":
                raise ValueError("Environment snapshot evidence is untrusted")
            evidence_records.append({
                "path": path.relative_to(project_root).as_posix(),
                "fingerprint": value.get("fingerprint"),
            })
            binding = (value.get("observation") or {}).get(
                "execution_binding"
            )
            if binding is not None and binding not in execution_bindings:
                execution_bindings.append(binding)
    environment = dict(effective_environment or {})
    if any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in environment.items()
    ):
        raise ValueError("Effective environment must contain string values")
    snapshot = {
        "environment_snapshot_version": ENVIRONMENT_SNAPSHOT_VERSION,
        "producer": "framework_runtime_matrix",
        "role": str(role),
        "run_result_id": run_result.get("run_result_id"),
        "run_result_fingerprint": run_result.get("fingerprint"),
        "oracle_evidence": sorted(
            evidence_records,
            key=lambda item: item["path"],
        ),
        "execution_bindings": execution_bindings,
        "effective_environment_fingerprint": _fingerprint(environment),
    }
    snapshot["fingerprint"] = _fingerprint(snapshot)
    output_root = (
        project_root / "artifacts/run-results/environment-snapshots"
    )
    output_root.mkdir(parents=True, exist_ok=True)
    path = output_root / (
        f"environment-{str(role)}-{snapshot['fingerprint'][:16]}.json"
    )
    write_json_atomic(path, snapshot)
    content = path.read_bytes()
    return {
        "path": path.relative_to(project_root).as_posix(),
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
    }


def _validate_oracle_receipts(
        receipts,
        role,
        provenance,
        registry,
        *,
        project_root,
        run_result,
    ):
    if not isinstance(receipts, list) or not receipts:
        return set(), [f"independent oracle receipt missing: {role}"]
    errors = []
    seen = set()
    outcomes = set()
    for receipt in receipts:
        if not isinstance(receipt, dict):
            errors.append(f"oracle receipt invalid: {role}")
            continue
        oracle_id = str(receipt.get("oracle_id") or "")
        registration = (registry.get("oracles") or {}).get(oracle_id)
        payload = {
            key: value for key, value in receipt.items()
            if key != "fingerprint"
        }
        if any((
            registration is None,
            oracle_id in seen,
            receipt.get("oracle_receipt_version")
            != ORACLE_RECEIPT_VERSION,
            receipt.get("producer") != "framework_oracle_runner",
            receipt.get("oracle_registry_version")
            != ORACLE_REGISTRY_VERSION,
            receipt.get("oracle_registry_fingerprint")
            != registry.get("fingerprint"),
            receipt.get("fingerprint") != _fingerprint(payload),
            receipt.get("generation_provenance") != provenance,
            registration is not None
            and receipt.get("oracle_version") != registration["version"],
            registration is not None
            and receipt.get("oracle_definition_fingerprint")
            != registration["definition_fingerprint"],
            registration is not None
            and receipt.get("outcome") != registration["outcome"],
            registration is not None
            and receipt.get("status")
            not in registration["allowed_statuses"],
            not isinstance(receipt.get("evidence"), list),
            not receipt.get("evidence"),
        )):
            errors.append(f"oracle receipt identity or result invalid: {role}/{oracle_id}")
        if registration is not None and not _evidence_is_valid(
                receipt.get("evidence"),
                registration["evidence_type"],
        ):
            errors.append(
                f"oracle evidence invalid: {role}/{oracle_id}"
            )
        elif registration is not None:
            evidence_errors = _verify_oracle_evidence(
                project_root,
                receipt.get("evidence") or [],
                registration,
                run_result,
                provenance,
            )
            errors.extend(
                f"oracle evidence invalid: {role}/{oracle_id}: {error}"
                for error in evidence_errors
            )
        if receipt.get("run_result_id") != (
                (run_result or {}).get("run_result_id")
                if isinstance(run_result, dict)
                else None
        ):
            errors.append(
                f"oracle Run Result mismatch: {role}/{oracle_id}"
            )
        seen.add(oracle_id)
        if registration is not None:
            outcomes.add(str(registration.get("outcome") or ""))
    return outcomes, errors


def _evidence_is_valid(values, expected_type):
    return bool(
        isinstance(values, list)
        and values
        and all(
            isinstance(item, dict)
            and item.get("evidence_type") == expected_type
            and isinstance(item.get("path"), str)
            and bool(item["path"])
            and isinstance(item.get("sha256"), str)
            and len(item["sha256"]) == 64
            and isinstance(item.get("size"), int)
            and item["size"] >= 0
            for item in values
        )
    )


def _verify_oracle_evidence(
        project_root,
        records,
        registration,
        run_result,
        provenance,
    ):
    if project_root is None:
        return ["project root missing"]
    project_root = Path(project_root).resolve()
    evidence_root = (
        project_root / "artifacts/run-results/oracle-evidence"
    ).resolve()
    errors = []
    values = []
    for record in records:
        path = (project_root / str(record.get("path") or "")).resolve()
        try:
            path.relative_to(evidence_root)
            content = path.read_bytes()
        except (OSError, ValueError):
            errors.append("evidence file missing or outside trusted root")
            continue
        if any((
            hashlib.sha256(content).hexdigest() != record.get("sha256"),
            len(content) != record.get("size"),
        )):
            errors.append("evidence file fingerprint mismatch")
            continue
        try:
            value = json.loads(content.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            errors.append("evidence file is not valid JSON")
            continue
        if not isinstance(value, dict):
            errors.append("evidence JSON must be an object")
            continue
        payload = {
            key: item for key, item in value.items()
            if key != "fingerprint"
        }
        if any((
            value.get("oracle_evidence_version")
            != ORACLE_EVIDENCE_VERSION,
            value.get("producer") != "framework_oracle_runner",
            value.get("oracle_id") != registration.get("oracle_id"),
            value.get("oracle_definition_fingerprint")
            != registration.get("definition_fingerprint"),
            value.get("generation_provenance") != provenance,
            value.get("run_result_id")
            != (run_result or {}).get("run_result_id"),
            value.get("run_result_fingerprint")
            != (run_result or {}).get("fingerprint"),
            not isinstance(value.get("observation"), dict),
            value.get("fingerprint") != _fingerprint(payload),
        )):
            errors.append("framework Oracle evidence identity invalid")
            continue
        values.append(value)
    if len(values) != len(records):
        return errors
    if not any(
        _execute_oracle_adapter(registration, value, run_result)
        for value in values
    ):
        errors.append("registered Oracle adapter did not pass")
    return errors


def _verify_environment_snapshot(
        project_root,
        role,
        record,
        *,
        run_result,
        oracle_receipts,
    ):
    if project_root is None or not isinstance(record, dict):
        raise ValueError("environment record missing")
    project_root = Path(project_root).resolve()
    environment_root = (
        project_root / "artifacts/run-results/environment-snapshots"
    ).resolve()
    path = (project_root / str(record.get("path") or "")).resolve()
    try:
        path.relative_to(environment_root)
        content = path.read_bytes()
    except (OSError, ValueError) as error:
        raise ValueError("environment file missing or outside trusted root") from error
    if any((
        hashlib.sha256(content).hexdigest() != record.get("sha256"),
        len(content) != record.get("size"),
    )):
        raise ValueError("environment file fingerprint mismatch")
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("environment file invalid") from error
    if not isinstance(value, dict):
        raise ValueError("environment snapshot must be an object")
    payload = {
        key: item for key, item in value.items()
        if key != "fingerprint"
    }
    if any((
        value.get("environment_snapshot_version")
        != ENVIRONMENT_SNAPSHOT_VERSION,
        value.get("producer") != "framework_runtime_matrix",
        value.get("role") != role,
        value.get("run_result_id") != (run_result or {}).get("run_result_id"),
        value.get("run_result_fingerprint")
        != (run_result or {}).get("fingerprint"),
        value.get("fingerprint") != _fingerprint(payload),
        not _is_sha256(value.get("effective_environment_fingerprint")),
    )):
        raise ValueError("environment role mismatch")
    expected_evidence = []
    expected_bindings = []
    for receipt in oracle_receipts or ():
        for evidence_record in (receipt or {}).get("evidence") or ():
            evidence_path = (
                project_root / str(evidence_record.get("path") or "")
            ).resolve()
            evidence_value = json.loads(
                evidence_path.read_text(encoding="utf-8")
            )
            expected_evidence.append({
                "path": evidence_path.relative_to(project_root).as_posix(),
                "fingerprint": evidence_value.get("fingerprint"),
            })
            binding = (evidence_value.get("observation") or {}).get(
                "execution_binding"
            )
            if binding is not None and binding not in expected_bindings:
                expected_bindings.append(binding)
    if any((
        value.get("oracle_evidence")
        != sorted(expected_evidence, key=lambda item: item["path"]),
        value.get("execution_bindings") != expected_bindings,
    )):
        raise ValueError("environment execution binding mismatch")


def _oracle_definition_fingerprint(oracle_id, registration):
    return _fingerprint({
        "oracle_registry_version": ORACLE_REGISTRY_VERSION,
        "oracle_id": oracle_id,
        "version": registration["version"],
        "independent": registration["independent"],
        "allowed_statuses": sorted(registration["allowed_statuses"]),
        "evidence_type": registration["evidence_type"],
        "outcome": registration["outcome"],
        "adapter": registration["adapter"],
        **{
            key: registration[key]
            for key in (
                "field",
                "expected",
                "error_contains",
                "backend",
                "window",
                "control",
                "property",
                "process",
            )
            if key in registration
        },
    })


def _load_project_oracles(project_root):
    path = Path(project_root).resolve() / PROJECT_ORACLE_REGISTRY_PATH
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Project oracle registry is invalid") from error
    if (
        not isinstance(value, dict)
        or value.get("oracle_registry_version") != ORACLE_REGISTRY_VERSION
        or not isinstance(value.get("oracles"), dict)
    ):
        raise ValueError("Project oracle registry shape is invalid")
    result = {}
    for oracle_id, definition in value["oracles"].items():
        if (
            not isinstance(oracle_id, str)
            or not oracle_id
            or oracle_id in _ORACLES
            or not isinstance(definition, dict)
        ):
            raise ValueError("Project oracle definition identity is invalid")
        adapter = str(definition.get("adapter") or "")
        outcome = str(definition.get("outcome") or "")
        if adapter not in {
            "uia_property_equals",
            "run_result_failure_contains",
        }:
            raise ValueError(f"Unsupported project oracle adapter: {adapter}")
        if outcome not in {"business_passed", "fail_closed"}:
            raise ValueError(f"Unsupported project oracle outcome: {outcome}")
        record = {
            "oracle_id": oracle_id,
            "version": str(definition.get("version") or "1.0"),
            "independent": True,
            "allowed_statuses": {"passed"},
            "evidence_type": str(definition.get("evidence_type") or ""),
            "outcome": outcome,
            "adapter": adapter,
        }
        for key in ("field", "expected", "error_contains"):
            if key in definition:
                record[key] = definition[key]
        if not record["evidence_type"]:
            raise ValueError("Project oracle evidence_type is required")
        if adapter == "uia_property_equals":
            window = definition.get("window")
            control = definition.get("control")
            property_name = str(definition.get("property") or "")
            process = definition.get("process")
            if (
                not isinstance(window, dict)
                or not window
                or not isinstance(control, dict)
                or not control
                or property_name not in {"value", "toggle_state", "text"}
                or "expected" not in definition
                or not _process_constraints_are_valid(process)
            ):
                raise ValueError(
                    "uia_property_equals oracle requires window, control, "
                    "property, expected, and process identity"
                )
            record.update({
                "backend": str(definition.get("backend") or "uia"),
                "window": dict(window),
                "control": dict(control),
                "property": property_name,
                "expected": definition.get("expected"),
                "process": dict(process),
            })
        if adapter == "run_result_failure_contains" and not record.get(
                "error_contains"):
            raise ValueError(
                "run_result_failure_contains oracle requires error_contains"
            )
        result[oracle_id] = record
    return result


def _execute_oracle_adapter(registration, evidence, run_result):
    adapter = registration["adapter"]
    if adapter == "uia_property_equals":
        actual = _dotted_value(evidence, "observation.actual")
        return actual == registration.get("expected")
    if adapter in {"run_result_failed", "run_result_failure_contains"}:
        if not isinstance(run_result, dict) or run_result.get("status") != "failed":
            return False
        expected = str(registration.get("error_contains") or "")
        if not expected:
            return True
        return expected in json.dumps(run_result, ensure_ascii=False)
    return False


def _capture_registered_observation(
        registration,
        run_result,
        execution_binding,
    ):
    adapter = registration["adapter"]
    if adapter == "uia_property_equals":
        if not _execution_binding_matches_registration(
                execution_binding,
                registration,
        ):
            raise ValueError("UIA Oracle execution binding is required")
        return _capture_uia_property(registration, execution_binding)
    if adapter in {"run_result_failed", "run_result_failure_contains"}:
        return {
            "status": (run_result or {}).get("status"),
            "run_result_id": (run_result or {}).get("run_result_id"),
        }
    raise ValueError(f"Unsupported Oracle adapter: {adapter}")


def _capture_uia_property(registration, expected_binding):
    from pywinauto import Desktop

    backend = str(registration.get("backend") or "uia")
    desktop = Desktop(backend=backend)
    matched_wrapper = _unique_live_window(
        desktop,
        registration["window"],
    )
    if int(matched_wrapper.handle) != int(expected_binding["window_handle"]):
        raise ValueError("Oracle window does not match execution binding")
    window = desktop.window(handle=int(expected_binding["window_handle"]))
    window.wait("visible", timeout=10)
    wrapper = window.child_window(**registration["control"]).wrapper_object()
    property_name = registration["property"]
    if property_name == "value":
        actual = wrapper.iface_value.CurrentValue
    elif property_name == "toggle_state":
        actual = {
            0: "unchecked",
            1: "checked",
            2: "indeterminate",
        }[int(wrapper.iface_toggle.CurrentToggleState)]
    else:
        actual = wrapper.window_text()
    process_id = int(wrapper.process_id())
    window_process_id = int(matched_wrapper.process_id())
    if any((
        process_id != window_process_id,
        process_id != int(expected_binding["process_id"]),
    )):
        raise ValueError("Oracle control and window process differ")
    binding = _live_process_binding(
        registration,
        process_id=process_id,
        window_handle=int(matched_wrapper.handle),
        window_process_id=window_process_id,
    )
    if binding != expected_binding:
        raise ValueError("Oracle execution binding changed before observation")
    return {
        "kind": "uia_property",
        "backend": backend,
        "window": dict(registration["window"]),
        "control": dict(registration["control"]),
        "property": property_name,
        "actual": actual,
        "execution_binding": binding,
    }


def _captured_observation_is_valid(
        observation,
        registration,
        expected_binding,
    ):
    if not isinstance(observation, dict):
        return False
    adapter = registration.get("adapter")
    if adapter in {"run_result_failed", "run_result_failure_contains"}:
        return True
    binding = observation.get("execution_binding")
    return bool(
        adapter == "uia_property_equals"
        and isinstance(binding, dict)
        and binding.get("execution_binding_version")
        == EXECUTION_BINDING_VERSION
        and isinstance(binding.get("process_id"), int)
        and binding.get("process_id") > 0
        and binding.get("window_process_id") == binding.get("process_id")
        and binding.get("control_process_id") == binding.get("process_id")
        and isinstance(binding.get("window_handle"), int)
        and binding.get("window_handle") > 0
        and isinstance(binding.get("process_create_time"), float)
        and binding.get("process_create_time") > 0
        and all(_is_sha256(binding.get(field)) for field in (
            "executable_path_fingerprint",
            "executable_sha256",
            "command_line_fingerprint",
            "process_constraints_fingerprint",
        ))
        and binding.get("process_constraints_fingerprint")
        == _fingerprint(registration.get("process") or {})
        and binding == expected_binding
    )


def _execution_binding_matches_registration(binding, registration):
    return bool(
        isinstance(binding, dict)
        and _captured_observation_is_valid(
            {"execution_binding": binding},
            registration,
            binding,
        )
    )


def _live_execution_binding(registration, *, process_id, window_handle):
    from pywinauto import Desktop

    backend = str(registration.get("backend") or "uia")
    wrapper = _unique_live_window(
        Desktop(backend=backend),
        registration["window"],
    )
    if any((
        int(wrapper.handle) != int(window_handle),
        int(wrapper.process_id()) != int(process_id),
    )):
        raise ValueError("Runtime instance does not match registered window")
    return _live_process_binding(
        registration,
        process_id=int(process_id),
        window_handle=int(window_handle),
        window_process_id=int(wrapper.process_id()),
    )


def _unique_live_window(desktop, criteria):
    from pywinauto.findwindows import (
        ElementAmbiguousError,
        ElementNotFoundError,
    )

    try:
        return desktop.window(**criteria).wrapper_object()
    except (ElementAmbiguousError, ElementNotFoundError) as error:
        raise ValueError(
            "Oracle window must resolve to exactly one live instance"
        ) from error


def _live_process_binding(
        registration,
        *,
        process_id,
        window_handle,
        window_process_id,
    ):
    process = psutil.Process(process_id)
    executable = Path(process.exe()).resolve()
    executable_content = executable.read_bytes()
    command_line = [str(value) for value in process.cmdline()]
    constraints = registration["process"]
    if not _process_matches_constraints(
            executable,
            executable_content,
            command_line,
            constraints,
    ):
        raise ValueError("Oracle process identity does not match registration")
    return {
        "execution_binding_version": EXECUTION_BINDING_VERSION,
        "process_id": int(process_id),
        "window_handle": int(window_handle),
        "window_process_id": int(window_process_id),
        "control_process_id": int(process_id),
        "process_create_time": float(process.create_time()),
        "executable_path_fingerprint": _fingerprint(
            str(executable).casefold()
        ),
        "executable_sha256": hashlib.sha256(executable_content).hexdigest(),
        "command_line_fingerprint": _fingerprint(command_line),
        "process_constraints_fingerprint": _fingerprint(constraints),
    }


def _process_constraints_are_valid(value):
    if not isinstance(value, dict):
        return False
    executable_sha256 = value.get("executable_sha256")
    command_values = value.get("command_line_contains")
    return bool(
        (executable_sha256 is None or _is_sha256(executable_sha256))
        and (
            command_values is None
            or isinstance(command_values, list)
            and command_values
            and all(isinstance(item, str) and item for item in command_values)
        )
        and (executable_sha256 or command_values)
    )


def _process_matches_constraints(executable, content, command_line, constraints):
    expected_sha256 = constraints.get("executable_sha256")
    if expected_sha256 and hashlib.sha256(content).hexdigest() != expected_sha256:
        return False
    command_text = "\n".join(command_line).casefold()
    return all(
        str(item).casefold() in command_text
        for item in constraints.get("command_line_contains") or ()
    )


def _is_sha256(value):
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value.casefold())
    )


def _dotted_value(value, field):
    current = value
    for part in str(field or "").split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _fingerprint(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
