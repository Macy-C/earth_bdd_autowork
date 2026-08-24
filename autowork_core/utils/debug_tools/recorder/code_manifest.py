from __future__ import annotations

import hashlib
import json
from pathlib import Path

from autowork_core.utils.debug_tools.recorder.project_memory import (
    snapshot_files,
)


CODE_MANIFEST_VERSION = "1.0"


def build_code_manifest(
        project_root,
        plan_audit,
        *,
        request_id,
        plan_fingerprint,
    ):
    project_root = Path(project_root).resolve()
    plan_audit = plan_audit if isinstance(plan_audit, dict) else {}
    errors = []
    calls = []
    paths = set()
    for record in plan_audit.get("ordered_call_records") or ():
        if not isinstance(record, dict):
            continue
        relative = _project_relative(
            project_root,
            record.get("path"),
        )
        if relative is None:
            errors.append(
                "Code Manifest 调用路径超出项目根目录: "
                f"{record.get('path')}"
            )
            continue
        paths.add(relative)
        calls.append({
            "path": relative,
            "line": int(record.get("line") or 0),
            "name": str(record.get("name") or ""),
            "receiver": str(record.get("receiver") or "") or None,
            "view_owner": str(record.get("view_owner") or "") or None,
        })
    implementation = []
    for record in plan_audit.get("implementation_trace") or ():
        if not isinstance(record, dict):
            continue
        relative = _project_relative(
            project_root,
            record.get("path"),
        )
        if relative is None:
            errors.append(
                "Code Manifest 实现路径超出项目根目录: "
                f"{record.get('path')}"
            )
            continue
        paths.add(relative)
        implementation.append({
            "step_id": str(record.get("step_id") or ""),
            "path": relative,
            "line": int(record.get("line") or 0),
            "call": str(record.get("call") or ""),
            "implementation_location": str(
                record.get("implementation_location") or ""
            ),
            "implementation_method": (
                str(record.get("implementation_method"))
                if record.get("implementation_method")
                else None
            ),
        })
    table_usages = [
        {
            "step_id": str(record.get("step_id") or ""),
            "consumption": record.get("value"),
            "status": str(record.get("status") or ""),
        }
        for record in plan_audit.get("operations") or ()
        if isinstance(record, dict) and record.get("op") == "table_usage"
    ]
    files = snapshot_files(sorted(paths), project_root=project_root)
    errors.extend(
        f"Code Manifest 实现文件不存在: {record.get('path')}"
        for record in files
        if not record.get("exists")
    )
    manifest = {
        "code_manifest_version": CODE_MANIFEST_VERSION,
        "request_id": str(request_id or ""),
        "plan_fingerprint": str(plan_fingerprint or ""),
        "status": (
            "passed"
            if plan_audit.get("status") == "passed" and not errors
            else "failed"
        ),
        "files": files,
        "calls": sorted(
            calls,
            key=lambda item: (item["path"], item["line"], item["name"]),
        ),
        "implementation": sorted(
            implementation,
            key=lambda item: (
                item["step_id"],
                item["path"],
                item["line"],
            ),
        ),
        "locator_keys": sorted({
            str(item)
            for item in plan_audit.get("locator_keys") or ()
            if item
        }),
        "table_usages": table_usages,
        "errors": errors,
    }
    fingerprint = code_manifest_fingerprint(manifest)
    manifest["code_manifest_id"] = "code-manifest-" + fingerprint[:16]
    manifest["code_manifest_fingerprint"] = fingerprint
    return manifest


def code_manifest_fingerprint(manifest):
    value = {
        key: item
        for key, item in dict(manifest or {}).items()
        if key not in {
            "code_manifest_id",
            "code_manifest_fingerprint",
        }
    }
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def code_manifest_identity_is_valid(manifest):
    if not isinstance(manifest, dict):
        return False
    fingerprint = code_manifest_fingerprint(manifest)
    return all((
        manifest.get("code_manifest_version") == CODE_MANIFEST_VERSION,
        bool(manifest.get("request_id")),
        bool(manifest.get("plan_fingerprint")),
        manifest.get("code_manifest_fingerprint") == fingerprint,
        manifest.get("code_manifest_id")
        == "code-manifest-" + fingerprint[:16],
    ))


def code_manifest_matches_transaction(
        manifest,
        *,
        request_id,
        plan_fingerprint,
        project_root,
        plan_audit,
):
    if not isinstance(manifest, dict):
        return False
    if not all((
        project_root,
        isinstance(plan_audit, dict),
        code_manifest_identity_is_valid(manifest),
        manifest.get("status") == "passed",
        not manifest.get("errors"),
        manifest.get("request_id") == request_id,
        manifest.get("plan_fingerprint") == plan_fingerprint,
    )):
        return False
    expected = build_code_manifest(
        project_root,
        plan_audit,
        request_id=request_id,
        plan_fingerprint=plan_fingerprint,
    )
    return manifest == expected


def _project_relative(project_root, value):
    if not value:
        return None
    path = Path(str(value))
    path = path.resolve() if path.is_absolute() else (
        project_root / path
    ).resolve()
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        return None