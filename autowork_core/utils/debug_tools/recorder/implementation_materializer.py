from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import yaml

from autowork_core.utils.debug_tools.recorder.implementation_manifest import (
    implementation_manifest_identity_is_valid,
)
from autowork_core.utils.debug_tools.recorder.generation_file_lock import (
    generation_path_has_reparse_point,
    generation_file_lease_write_guard,
)
from autowork_core.utils.debug_tools.recorder.writer import (
    _atomic_write_text,
    dump_yaml,
    write_json_atomic,
)


MATERIALIZATION_VERSION = "1.0"


def materialize_implementation_scaffold(
        project_root,
        manifest,
        generation_input_snapshot,
    *,
    lease,
    journal_path,
    ):
    project_root = Path(project_root).resolve()
    if not implementation_manifest_identity_is_valid(manifest):
        raise ValueError("Implementation Manifest identity invalid")
    baseline_files = (generation_input_snapshot or {}).get("files")
    if not isinstance(baseline_files, dict):
        raise ValueError("Generation input snapshot is invalid")

    writable = set(manifest.get("allowed_changes") or ())
    immutable = set(manifest.get("read_only_reuse") or ())
    writes = {}
    tasks = []

    for marker in manifest.get("package_markers") or ():
        if marker.get("strategy") != "create":
            continue
        path = _system_path(marker, writable, immutable, expected_suffix=".py")
        content = '"""Generated package marker."""\n'
        writes[path] = content
        tasks.append({
            "kind": "package_marker",
            "path": path,
            "sha256": _sha256_text(content),
        })

    locator_documents = {}
    for task in manifest.get("locator_patch") or ():
        raw_path = str((task or {}).get("file") or "")
        if raw_path in immutable:
            continue
        path = _system_path(
            task,
            writable,
            immutable,
            expected_suffix={".yaml", ".yml"},
            key="file",
        )
        key = task.get("key")
        patch = task.get("patch")
        if not isinstance(key, str) or not key:
            raise ValueError(f"Locator patch key must be a string: {key!r}")
        if not isinstance(patch, dict) or not patch:
            raise ValueError(f"Locator patch must be a non-empty object: {path}:{key}")
        if path not in locator_documents:
            locator_documents[path] = _load_locator_document(project_root / path)
        document = locator_documents[path]
        existing = document.get(key)
        if existing is not None and existing != patch:
            enrich = (
                task.get("operation") == "ensure_or_enrich"
                and isinstance(existing, dict)
                and all(patch.get(field) == value for field, value in existing.items())
            )
            refine_content = (
                task.get("operation") == "ensure_or_refine_content"
                and _is_content_identity_refinement(existing, patch)
            )
            if not enrich and not refine_content:
                raise ValueError(
                    f"Locator patch conflicts with existing key: {path}:{key}"
                )
        document[key] = patch

    for path, document in locator_documents.items():
        content = dump_yaml(document)
        writes[path] = content
        tasks.append({
            "kind": "locator_document",
            "path": path,
            "keys": list(document),
            "sha256": _sha256_text(content),
        })

    journal_path = Path(journal_path).resolve()
    with generation_file_lease_write_guard(project_root, lease):
        journal = _load_journal(journal_path)
        if journal:
            _validate_journal(
                journal,
                journal_path=journal_path,
                manifest=manifest,
                lease=lease,
                writes=writes,
            )
            if journal.get("status") == "committed":
                audit = journal.get("audit") or {}
                if system_materialization_matches(project_root, audit):
                    return audit
                raise ValueError("Committed materialization journal drifted")
            _restore_journal(project_root, journal)
        for relative in writes:
            _require_frozen_path(project_root, relative, baseline_files)
        journal = _build_journal(
            project_root,
            manifest,
            writes,
            journal_path,
        )
        write_json_atomic(journal_path, journal)
        try:
            for item in journal["files"]:
                relative = item["path"]
                _atomic_write_text(project_root / relative, writes[relative])
                item["committed"] = True
                _seal_journal(journal)
                write_json_atomic(journal_path, journal)
        except Exception:
            _restore_journal(project_root, journal)
            journal["status"] = "rolled_back"
            _seal_journal(journal)
            write_json_atomic(journal_path, journal)
            raise

    audit = {
        "materialization_version": MATERIALIZATION_VERSION,
        "manifest_id": manifest.get("implementation_manifest_id"),
        "manifest_fingerprint": manifest.get(
            "implementation_manifest_fingerprint"
        ),
        "status": "materialized",
        "system_owned_files": sorted(writes),
        "tasks": sorted(tasks, key=lambda item: (item["path"], item["kind"])),
        "journal_path": str(journal_path),
    }
    with generation_file_lease_write_guard(project_root, lease):
        journal = _load_journal(journal_path)
        journal["status"] = "committed"
        journal["audit"] = audit
        _seal_journal(journal)
        write_json_atomic(journal_path, journal)
    return audit


def _is_content_identity_refinement(existing, patch):
    if not isinstance(existing, dict) or not isinstance(patch, dict):
        return False
    removed = set(existing) - set(patch)
    return bool(removed) and removed <= {"name", "title"} and all(
        existing.get(field) == value
        for field, value in patch.items()
    )


def system_materialization_matches(project_root, audit):
    if not isinstance(audit, dict):
        return False
    if audit.get("materialization_version") != MATERIALIZATION_VERSION:
        return False
    tasks = audit.get("tasks")
    if not isinstance(tasks, list):
        return False
    project_root = Path(project_root).resolve()
    for task in tasks:
        if not isinstance(task, dict) or not task.get("path"):
            return False
        path = (project_root / str(task["path"])).resolve()
        try:
            path.relative_to(project_root)
            content = path.read_bytes()
        except (OSError, ValueError):
            return False
        if hashlib.sha256(content).hexdigest() != task.get("sha256"):
            return False
    return sorted(audit.get("system_owned_files") or ()) == sorted(
        {
            str(task["path"])
            for task in tasks
        }
    )


def rollback_implementation_scaffold(
        project_root,
        audit,
        *,
        lease,
        manifest,
        journal_path,
    ):
    if not isinstance(audit, dict) or not audit.get("journal_path"):
        return
    project_root = Path(project_root).resolve()
    journal_path = Path(journal_path).resolve()
    if Path(str(audit["journal_path"])).resolve() != journal_path:
        raise ValueError("Materialization audit journal path mismatch")
    with generation_file_lease_write_guard(project_root, lease):
        journal = _load_journal(journal_path)
        if not journal or journal.get("status") == "rolled_back":
            return
        _validate_rollback_journal(
            journal,
            journal_path=journal_path,
            manifest=manifest,
            lease=lease,
            audit=audit,
        )
        _restore_journal(project_root, journal)
        journal["status"] = "rolled_back"
        journal.pop("audit", None)
        _seal_journal(journal)
        write_json_atomic(journal_path, journal)


def _system_path(
        task,
        writable,
        immutable,
        *,
        expected_suffix,
        key="path",
    ):
    value = str((task or {}).get(key) or "")
    path = Path(value)
    suffixes = (
        {expected_suffix}
        if isinstance(expected_suffix, str)
        else set(expected_suffix)
    )
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or path.suffix.casefold() not in suffixes
    ):
        raise ValueError(f"Invalid system materialization path: {value}")
    normalized = path.as_posix()
    if normalized in immutable or normalized not in writable:
        raise ValueError(
            f"System materialization path is not writable: {normalized}"
        )
    return normalized


def _require_frozen_path(project_root, relative, baseline_files):
    if generation_path_has_reparse_point(project_root, relative):
        raise ValueError(
            f"System materialization target is a reparse point: {relative}"
        )
    path = (project_root / relative).resolve()
    path.relative_to(project_root)
    baseline = baseline_files.get(relative)
    if baseline is None:
        if path.exists():
            raise ValueError(
                f"System materialization create target appeared after freeze: {relative}"
            )
        return
    if not path.is_file() or path.is_symlink():
        raise ValueError(
            f"System materialization baseline target changed type: {relative}"
        )
    if hashlib.sha256(path.read_bytes()).hexdigest() != baseline.get("sha256"):
        raise ValueError(
            f"System materialization baseline drifted: {relative}"
        )


def _load_locator_document(path):
    if not path.exists():
        return {}
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise ValueError(f"Locator document cannot be loaded: {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"Locator document must be an object: {path}")
    if any(not isinstance(key, str) or not key for key in value):
        raise ValueError(f"Locator document keys must be strings: {path}")
    if any(not isinstance(item, dict) for item in value.values()):
        raise ValueError(f"Locator entries must be objects: {path}")
    return dict(value)


def _sha256_text(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _build_journal(project_root, manifest, writes, journal_path):
    files = []
    for relative in sorted(writes):
        path = (project_root / relative).resolve()
        path.relative_to(project_root)
        original = path.read_bytes() if path.is_file() else None
        replacement = writes[relative].encode("utf-8")
        files.append({
            "path": relative,
            "original_base64": (
                base64.b64encode(original).decode("ascii")
                if original is not None
                else None
            ),
            "original_sha256": (
                hashlib.sha256(original).hexdigest()
                if original is not None
                else None
            ),
            "replacement_sha256": hashlib.sha256(replacement).hexdigest(),
            "committed": False,
        })
    journal = {
        "materialization_journal_version": "1.0",
        "status": "pending",
        "manifest_id": manifest.get("implementation_manifest_id"),
        "manifest_fingerprint": manifest.get(
            "implementation_manifest_fingerprint"
        ),
        "journal_path": str(journal_path),
        "files": files,
    }
    _seal_journal(journal)
    return journal


def _validate_journal(journal, *, journal_path, manifest, lease, writes):
    expected_paths = sorted(writes)
    system_owned = set(manifest.get("system_owned_changes") or ())
    lease_files = set((lease or {}).get("files") or ())
    files = journal.get("files")
    if any((
        journal.get("materialization_journal_version") != "1.0",
        journal.get("status") not in {"pending", "committed"},
        journal.get("manifest_id")
        != manifest.get("implementation_manifest_id"),
        journal.get("manifest_fingerprint")
        != manifest.get("implementation_manifest_fingerprint"),
        Path(str(journal.get("journal_path") or "")).resolve()
        != Path(journal_path).resolve(),
        not isinstance(files, list),
        journal.get("fingerprint") != _journal_fingerprint(journal),
    )):
        raise ValueError("Materialization journal identity mismatch")
    actual_paths = [
        str(item.get("path") or "")
        for item in files
        if isinstance(item, dict)
    ]
    if (
        len(actual_paths) != len(files)
        or actual_paths != expected_paths
        or not set(actual_paths).issubset(system_owned)
        or not set(actual_paths).issubset(lease_files)
    ):
        raise ValueError("Materialization journal file scope mismatch")
    for item in files:
        relative = str(item["path"])
        if item.get("replacement_sha256") != _sha256_text(writes[relative]):
            raise ValueError(
                f"Materialization journal replacement mismatch: {relative}"
            )
        _validate_original_record(item)
    if journal.get("status") == "committed":
        audit = journal.get("audit")
        if any((
            not isinstance(audit, dict),
            audit.get("manifest_id")
            != manifest.get("implementation_manifest_id"),
            audit.get("manifest_fingerprint")
            != manifest.get("implementation_manifest_fingerprint"),
            Path(str(audit.get("journal_path") or "")).resolve()
            != Path(journal_path).resolve(),
            sorted(audit.get("system_owned_files") or ()) != expected_paths,
        )):
            raise ValueError("Committed materialization audit mismatch")


def _validate_rollback_journal(
        journal,
        *,
        journal_path,
        manifest,
        lease,
        audit,
    ):
    expected_paths = sorted(audit.get("system_owned_files") or ())
    writes = {
        str(task.get("path") or ""): ""
        for task in audit.get("tasks") or ()
        if isinstance(task, dict) and task.get("path")
    }
    files = journal.get("files") or []
    if any((
        journal.get("materialization_journal_version") != "1.0",
        journal.get("status") not in {"pending", "committed"},
        journal.get("manifest_id")
        != manifest.get("implementation_manifest_id"),
        journal.get("manifest_fingerprint")
        != manifest.get("implementation_manifest_fingerprint"),
        Path(str(journal.get("journal_path") or "")).resolve()
        != Path(journal_path).resolve(),
        journal.get("fingerprint") != _journal_fingerprint(journal),
        sorted(str(item.get("path") or "") for item in files)
        != expected_paths,
        not set(expected_paths).issubset(
            set(manifest.get("system_owned_changes") or ())
        ),
        not set(expected_paths).issubset(set((lease or {}).get("files") or ())),
        set(writes) != set(expected_paths),
    )):
        raise ValueError("Materialization rollback journal mismatch")
    task_hashes = {
        str(task.get("path")): str(task.get("sha256") or "")
        for task in audit.get("tasks") or ()
        if isinstance(task, dict) and task.get("path")
    }
    for item in files:
        relative = str(item.get("path") or "")
        if item.get("replacement_sha256") != task_hashes.get(relative):
            raise ValueError(
                f"Materialization rollback replacement mismatch: {relative}"
            )
        _validate_original_record(item)


def _validate_original_record(item):
    original = item.get("original_base64")
    if original is None:
        if item.get("original_sha256") is not None:
            raise ValueError("Materialization journal original mismatch")
        return
    try:
        content = base64.b64decode(original.encode("ascii"), validate=True)
    except (ValueError, UnicodeError) as error:
        raise ValueError("Materialization journal original is invalid") from error
    if hashlib.sha256(content).hexdigest() != item.get("original_sha256"):
        raise ValueError("Materialization journal original hash mismatch")


def _seal_journal(journal):
    journal["fingerprint"] = _journal_fingerprint(journal)


def _journal_fingerprint(journal):
    payload = {
        key: value
        for key, value in dict(journal or {}).items()
        if key != "fingerprint"
    }
    return hashlib.sha256(json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _restore_journal(project_root, journal):
    for item in reversed(journal.get("files") or []):
        if generation_path_has_reparse_point(
            project_root,
            str(item.get("path") or ""),
        ):
            raise ValueError(
                "Materialization recovery target is a reparse point: "
                f"{item.get('path')}"
            )
        path = (project_root / str(item.get("path") or "")).resolve()
        path.relative_to(project_root)
        current = path.read_bytes() if path.is_file() else None
        current_sha256 = (
            hashlib.sha256(current).hexdigest()
            if current is not None
            else None
        )
        if current_sha256 == item.get("original_sha256"):
            continue
        if current_sha256 != item.get("replacement_sha256"):
            raise ValueError(
                "Materialization recovery found an unrelated file change: "
                f"{item.get('path')}"
            )
        original = item.get("original_base64")
        if original is None:
            path.unlink(missing_ok=True)
        else:
            content = base64.b64decode(original.encode("ascii"))
            _atomic_write_bytes(path, content)


def _load_journal(path):
    if not Path(path).is_file():
        return {}
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Materialization journal is invalid") from error
    if not isinstance(value, dict):
        raise ValueError("Materialization journal must be an object")
    return value


def _atomic_write_bytes(path, content):
    path = Path(path)
    temporary = path.with_name(f".{path.name}.restore.tmp")
    try:
        temporary.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
