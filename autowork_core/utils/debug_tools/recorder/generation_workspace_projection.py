from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path, PurePosixPath

from config.paths import Paths
from autowork_core.utils.debug_tools.recorder.models import SCHEMA_VERSION
from autowork_core.utils.debug_tools.recorder.writer import write_json_atomic


GENERATION_WORKSPACE_PROJECTION_VERSION = "1.0"
GENERATION_WORKSPACE_PROJECTION_SUMMARY_VERSION = "1.0"


def build_generation_workspace_projection(
        request,
        brief,
        generation_input_snapshot,
        source_files,
        *,
        project_root=None,
        terminal_results=(),
        created_at=None,
    ):
    request = copy.deepcopy(request or {})
    brief = copy.deepcopy(brief or {})
    snapshot = copy.deepcopy(generation_input_snapshot or {})
    files = snapshot.get("files") or {}
    if snapshot.get("snapshot_version") != "1.0" or not isinstance(files, dict):
        raise ValueError("Generation workspace projection snapshot invalid")
    project_root = Path(project_root or Paths.BASE_DIR).resolve()
    source_paths = {
        str(item.get("path") or "").replace("\\", "/")
        for item in source_files or ()
        if isinstance(item, dict) and item.get("path")
    }
    file_entries = [
        _file_entry(path, record, source_paths)
        for path, record in sorted(files.items())
    ]
    generated_assets = _generated_assets(files, terminal_results)
    package_markers = _package_markers(files)
    value = {
        "schema_version": SCHEMA_VERSION,
        "generation_workspace_projection_version": (
            GENERATION_WORKSPACE_PROJECTION_VERSION
        ),
        "created_at": str(
            created_at or datetime.now().isoformat(timespec="milliseconds")
        ),
        "project_root": str(project_root),
        "request": {
            "request_id": request.get("request_id"),
            "request_fingerprint": request.get("request_fingerprint"),
            "revision_seal": (
                (request.get("revision_snapshot") or {}).get("seal")
            ),
            "path": request.get("request_path"),
        },
        "brief": {
            "brief_fingerprint": brief.get("brief_fingerprint"),
            "brief_version": brief.get("brief_version"),
            "step_count": len((brief.get("target") or {}).get("steps") or ()),
            "action_count": len(brief.get("actions") or ()),
        },
        "generation_input_snapshot_fingerprint": _fingerprint(snapshot),
        "source_file_paths": sorted(source_paths),
        "existing_assets": {
            "files": file_entries,
        },
        "generated_assets": generated_assets,
        "required_structure_files": {
            "package_markers": package_markers,
        },
        "summary": {
            "file_count": len(file_entries),
            "source_file_count": len(source_paths),
            "step_file_count": sum(
                1 for item in file_entries if item["kind"] == "step"
            ),
            "page_object_file_count": sum(
                1 for item in file_entries if item["kind"] == "page_object"
            ),
            "locator_file_count": sum(
                1 for item in file_entries if item["kind"] == "locator"
            ),
            "data_file_count": sum(
                1 for item in file_entries if item["kind"] == "data"
            ),
            "package_marker_count": sum(
                1 for item in package_markers if item["status"] == "present"
            ),
            "missing_package_marker_count": sum(
                1 for item in package_markers if item["status"] == "missing"
            ),
            "generated_present_count": len(generated_assets["present"]),
            "generated_missing_count": len(generated_assets["missing"]),
            "generated_unchanged_count": len(generated_assets["unchanged"]),
            "generated_unknown_count": len(generated_assets["unknown"]),
            "user_modified_count": len(generated_assets["user_modified"]),
        },
    }
    value["projection_fingerprint"] = generation_workspace_projection_fingerprint(
        value,
    )
    value["projection_id"] = "workspace-projection-" + value[
        "projection_fingerprint"
    ][:16]
    return value


def persist_generation_workspace_projection(session_dir, projection):
    session_dir = Path(session_dir).resolve()
    if not generation_workspace_projection_identity_is_valid(projection):
        raise ValueError("Generation Workspace Projection identity invalid")
    output = _projection_path(
        session_dir,
        (projection.get("request") or {}).get("request_id"),
        projection.get("projection_fingerprint"),
    )
    if output.exists():
        existing = _read_json(output)
        if existing != projection:
            raise ValueError(
                f"Generation Workspace Projection fingerprint conflict: {output}"
            )
        return output, copy.deepcopy(existing)
    output.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(output, projection)
    return output, copy.deepcopy(projection)


def generation_workspace_projection_pointer(session_dir, projection, path):
    session_dir = Path(session_dir).resolve()
    path = Path(path).resolve()
    expected = _projection_path(
        session_dir,
        (projection.get("request") or {}).get("request_id"),
        projection.get("projection_fingerprint"),
    )
    if path != expected or not generation_workspace_projection_identity_is_valid(
            projection,
    ):
        raise ValueError("Generation Workspace Projection path or identity invalid")
    summary = projection.get("summary") or {}
    return {
        "generation_workspace_projection_version": (
            GENERATION_WORKSPACE_PROJECTION_VERSION
        ),
        "path": path.relative_to(session_dir).as_posix(),
        "projection_id": projection.get("projection_id"),
        "projection_fingerprint": projection.get("projection_fingerprint"),
        "request_id": (projection.get("request") or {}).get("request_id"),
        "generation_input_snapshot_fingerprint": projection.get(
            "generation_input_snapshot_fingerprint"
        ),
        "file_count": summary.get("file_count"),
        "package_marker_count": summary.get("package_marker_count"),
        "missing_package_marker_count": summary.get(
            "missing_package_marker_count"
        ),
    }


def generation_workspace_projection_pointer_is_valid(pointer):
    if not isinstance(pointer, dict):
        return False
    required = {
        "generation_workspace_projection_version",
        "path",
        "projection_id",
        "projection_fingerprint",
        "request_id",
        "generation_input_snapshot_fingerprint",
        "file_count",
        "package_marker_count",
        "missing_package_marker_count",
    }
    return bool(
        set(pointer) == required
        and pointer.get("generation_workspace_projection_version")
        == GENERATION_WORKSPACE_PROJECTION_VERSION
        and pointer.get("path")
        and pointer.get("projection_id")
        and pointer.get("projection_fingerprint")
        and pointer.get("request_id")
        and pointer.get("generation_input_snapshot_fingerprint")
        and _non_negative_int(pointer.get("file_count"))
        and _non_negative_int(pointer.get("package_marker_count"))
        and _non_negative_int(pointer.get("missing_package_marker_count"))
    )


def load_generation_workspace_projection(session_dir, pointer):
    session_dir = Path(session_dir).resolve()
    pointer = copy.deepcopy(pointer or {})
    if not generation_workspace_projection_pointer_is_valid(pointer):
        raise ValueError("Generation Workspace Projection pointer invalid")
    expected = _projection_path(
        session_dir,
        pointer.get("request_id"),
        pointer.get("projection_fingerprint"),
    )
    path = (session_dir / str(pointer.get("path") or "")).resolve()
    if path != expected or not path.is_file():
        raise ValueError("Generation Workspace Projection path invalid")
    try:
        projection = _read_json(path)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Generation Workspace Projection cannot be loaded") from error
    if not generation_workspace_projection_identity_is_valid(projection):
        raise ValueError("Generation Workspace Projection identity invalid")
    if generation_workspace_projection_pointer(session_dir, projection, path) != pointer:
        raise ValueError("Generation Workspace Projection pointer mismatch")
    return projection


def generation_workspace_projection_identity_is_valid(projection):
    if not isinstance(projection, dict):
        return False
    request = projection.get("request") or {}
    summary = projection.get("summary") or {}
    if any((
            projection.get("schema_version") != SCHEMA_VERSION,
            projection.get("generation_workspace_projection_version")
            != GENERATION_WORKSPACE_PROJECTION_VERSION,
            not projection.get("created_at"),
            not request.get("request_id"),
            not request.get("request_fingerprint"),
            not request.get("revision_seal"),
            not projection.get("generation_input_snapshot_fingerprint"),
            not isinstance((projection.get("existing_assets") or {}).get("files"), list),
            not isinstance(
                (projection.get("required_structure_files") or {}).get(
                    "package_markers"
                ),
                list,
            ),
            not _non_negative_int(summary.get("file_count")),
            not _non_negative_int(summary.get("package_marker_count")),
            not _non_negative_int(summary.get("missing_package_marker_count")),
    )):
        return False
    actual = generation_workspace_projection_fingerprint(projection)
    return bool(
        projection.get("projection_fingerprint") == actual
        and projection.get("projection_id") == f"workspace-projection-{actual[:16]}"
    )


def generation_workspace_projection_fingerprint(projection):
    value = {
        key: item
        for key, item in copy.deepcopy(projection or {}).items()
        if key not in {"projection_id", "projection_fingerprint"}
    }
    return _fingerprint(value)


def generation_workspace_projection_summary(projection, *, manifest=None):
    projection = projection if isinstance(projection, dict) else {}
    summary = projection.get("summary") or {}
    planned = _planned_strategy_counts(manifest)
    generated_missing_count = int(_summary_value(
        projection,
        summary,
        "generated_missing_count",
    ) or 0)
    missing_package_marker_count = int(_summary_value(
        projection,
        summary,
        "missing_package_marker_count",
    ) or 0)
    generated_unchanged_count = int(_summary_value(
        projection,
        summary,
        "generated_unchanged_count",
    ) or 0)
    generated_unknown_count = int(_summary_value(
        projection,
        summary,
        "generated_unknown_count",
    ) or 0)
    user_modified_count = int(_summary_value(
        projection,
        summary,
        "user_modified_count",
    ) or 0)
    missing_count = generated_missing_count + missing_package_marker_count
    return {
        "generation_workspace_projection_summary_version": (
            GENERATION_WORKSPACE_PROJECTION_SUMMARY_VERSION
        ),
        "source": "generation_workspace_projection",
        "projection_id": projection.get("projection_id"),
        "projection_fingerprint": projection.get("projection_fingerprint"),
        "generation_input_snapshot_fingerprint": projection.get(
            "generation_input_snapshot_fingerprint"
        ),
        "counts_source": (
            "implementation_manifest"
            if planned.get("available")
            else "projection_current_facts"
        ),
        "reuse_count": planned.get("reuse_count", int(
            generated_unchanged_count
        ) + int(generated_unknown_count)),
        "modify_count": planned.get("modify_count", 0),
        "new_count": planned.get("new_count", 0),
        "missing_count": missing_count,
        "user_modified_count": user_modified_count,
        "existing_file_count": _summary_value(projection, summary, "file_count"),
        "source_file_count": _summary_value(
            projection,
            summary,
            "source_file_count",
        ),
        "generated_present_count": _summary_value(
            projection,
            summary,
            "generated_present_count",
        ),
        "generated_missing_count": generated_missing_count,
        "generated_unchanged_count": generated_unchanged_count,
        "generated_unknown_count": generated_unknown_count,
        "package_marker_count": _summary_value(
            projection,
            summary,
            "package_marker_count",
        ),
        "missing_package_marker_count": _summary_value(
            projection,
            summary,
            "missing_package_marker_count"
        ),
    }


def generation_workspace_projection_summary_is_valid(value):
    if not isinstance(value, dict):
        return False
    required_counts = {
        "reuse_count",
        "modify_count",
        "new_count",
        "missing_count",
        "user_modified_count",
    }
    return bool(
        value.get("generation_workspace_projection_summary_version")
        == GENERATION_WORKSPACE_PROJECTION_SUMMARY_VERSION
        and value.get("source") == "generation_workspace_projection"
        and value.get("projection_fingerprint")
        and value.get("generation_input_snapshot_fingerprint")
        and required_counts.issubset(value)
        and all(_non_negative_int(value.get(key)) for key in required_counts)
    )


def _planned_strategy_counts(manifest):
    if not isinstance(manifest, dict) or not manifest:
        return {"available": False}
    files = [
        item for item in manifest.get("files") or []
        if isinstance(item, dict)
    ]
    return {
        "available": True,
        "reuse_count": sum(1 for item in files if item.get("strategy") == "reuse"),
        "modify_count": sum(1 for item in files if item.get("strategy") == "modify"),
        "new_count": sum(1 for item in files if item.get("strategy") == "create"),
    }


def _summary_value(projection, summary, key):
    if key in summary:
        return summary.get(key)
    return projection.get(key)


def _file_entry(path, record, source_paths):
    record = record if isinstance(record, dict) else {}
    return {
        "path": str(path),
        "kind": _asset_kind(path),
        "sha256": record.get("sha256"),
        "size": record.get("size"),
        "is_symlink": bool(record.get("is_symlink") is True),
        "source_included": str(path) in source_paths,
        "package_marker": _is_page_package_marker(path),
    }


def _asset_kind(path):
    value = PurePosixPath(str(path).replace("\\", "/"))
    if value.parts[:2] == ("Bdd", "steps"):
        return "step"
    if value.parts[:2] == ("Bdd", "page_obj"):
        return "page_object"
    if value.parts[:2] == ("Bdd", "locators"):
        return "locator"
    if value.parts[:2] == ("Bdd", "data"):
        return "data"
    if value.parts[:2] == ("Bdd", "test_features"):
        return "feature"
    return "other"


def _is_page_package_marker(path):
    value = PurePosixPath(str(path).replace("\\", "/"))
    return bool(
        value.parts[:2] == ("Bdd", "page_obj")
        and len(value.parts) > 3
        and value.name == "__init__.py"
    )


def _package_markers(files):
    required_by = {}
    for path in files:
        value = PurePosixPath(str(path).replace("\\", "/"))
        if (
                value.parts[:2] != ("Bdd", "page_obj")
                or len(value.parts) < 4
                or value.suffix != ".py"
                or value.name == "__init__.py"
        ):
            continue
        marker = (value.parent / "__init__.py").as_posix()
        required_by.setdefault(marker, set()).add(value.as_posix())
    result = []
    for marker, dependents in sorted(required_by.items()):
        record = files.get(marker) or {}
        result.append({
            "path": marker,
            "status": "present" if marker in files else "missing",
            "sha256": record.get("sha256"),
            "size": record.get("size"),
            "required_by": sorted(dependents),
        })
    return result


def _generated_assets(files, terminal_results):
    generated_paths = []
    previous_snapshots = {}
    for result in terminal_results or ():
        paths = _result_changed_files(result)
        generated_paths.extend(paths)
        for item in _result_file_snapshots(result):
            path = item["path"]
            if path in paths and path not in previous_snapshots:
                previous_snapshots[path] = item
    unique = sorted(dict.fromkeys(generated_paths))
    present = [path for path in unique if path in files]
    missing = [path for path in unique if path not in files]
    unchanged = []
    unknown = []
    user_modified = []
    for path in present:
        previous = previous_snapshots.get(path)
        if not previous:
            unknown.append(path)
            continue
        current = files.get(path) or {}
        if (
                current.get("sha256") == previous.get("sha256")
                and current.get("size") == previous.get("size")
        ):
            unchanged.append(path)
        else:
            user_modified.append({
                "path": path,
                "previous_sha256": previous.get("sha256"),
                "previous_size": previous.get("size"),
                "current_sha256": current.get("sha256"),
                "current_size": current.get("size"),
            })
    return {
        "source": "terminal_job_results",
        "present": present,
        "missing": missing,
        "unchanged": unchanged,
        "unknown": unknown,
        "user_modified": user_modified,
    }


def _result_changed_files(result):
    if not isinstance(result, dict):
        return []
    values = []
    for key in ("delivery_summary", "delivery_visibility"):
        values.extend((result.get(key) or {}).get("changed_files") or [])
    values.extend(result.get("changed_files") or [])
    for report in _result_audit_reports(result):
        values.extend(report.get("changed_files") or [])
    return [str(path).replace("\\", "/") for path in values if str(path or "")]


def _result_file_snapshots(result):
    if not isinstance(result, dict):
        return []
    records = []
    records.extend(result.get("file_snapshots") or [])
    for report in _result_audit_reports(result):
        records.extend(((report.get("code_manifest") or {}).get("files") or []))
        records.extend(report.get("implementation_snapshot") or [])
    values = []
    seen = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        path = str(record.get("path") or "").replace("\\", "/")
        if not path or path in seen:
            continue
        sha256 = record.get("sha256")
        size = record.get("size")
        if not isinstance(sha256, str) or not isinstance(size, int):
            continue
        seen.add(path)
        values.append({"path": path, "sha256": sha256, "size": size})
    return values


def _result_audit_reports(result):
    reports = []
    for key in ("terminal_report_audit", "transaction_report"):
        report = result.get(key) if isinstance(result, dict) else None
        if isinstance(report, dict):
            reports.append(report)
    return reports


def _projection_path(session_dir, request_id, fingerprint):
    return (
        Path(session_dir).resolve()
        / "ai"
        / "generation-workspace-projections"
        / str(request_id)
        / f"projection-{fingerprint}.json"
    )


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _fingerprint(value):
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _non_negative_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
