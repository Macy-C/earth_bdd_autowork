from __future__ import annotations

import base64
import hashlib
import inspect
import json
import os
import re
from pathlib import Path

import yaml

from autowork_core.page import BasePage
from autowork_core.utils.debug_tools.recorder.ai_capability_registry import (
    capability_by_name,
)

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
MATERIALIZATION_CANDIDATE_VERSION = "1.0"


def build_implementation_scaffold_candidate(
        project_root,
        manifest,
        generation_input_snapshot,
    *,
    transaction_id=None,
    candidate_files=None,
    ):
    project_root = Path(project_root).resolve()
    if not implementation_manifest_identity_is_valid(manifest):
        raise ValueError("Implementation Manifest identity invalid")
    baseline_files = (generation_input_snapshot or {}).get("files")
    if not isinstance(baseline_files, dict):
        raise ValueError("Generation input snapshot is invalid")

    writes, tasks = _implementation_scaffold_writes(
        project_root,
        manifest,
    )
    candidate_writes, candidate_tasks = _implementation_candidate_file_writes(
        manifest,
        candidate_files,
    )
    overlap = sorted(set(writes) & set(candidate_writes))
    if overlap:
        raise ValueError(f"AI candidate files overlap system scaffolds: {overlap}")
    writes.update(candidate_writes)
    tasks.extend(candidate_tasks)
    for relative in writes:
        _require_frozen_path(project_root, relative, baseline_files)
    value = {
        "materialization_candidate_version": (
            MATERIALIZATION_CANDIDATE_VERSION
        ),
        "transaction_id": str(transaction_id or "") or None,
        "manifest_id": manifest.get("implementation_manifest_id"),
        "manifest_fingerprint": manifest.get(
            "implementation_manifest_fingerprint"
        ),
        "status": "candidate_prepared",
        "system_owned_files": sorted(writes),
        "files": [{
            "path": path,
            "before_exists": baseline_files.get(path) is not None,
            "before_sha256": (
                (baseline_files.get(path) or {}).get("sha256")
            ),
            "content": _editor_delivery_text(writes[path]),
            "sha256": _sha256_text(writes[path]),
        } for path in sorted(writes)],
        "tasks": sorted(tasks, key=lambda item: (item["path"], item["kind"])),
    }
    value["candidate_fingerprint"] = _candidate_fingerprint(value)
    return value


def persist_implementation_scaffold_candidate(
        session_dir,
        transaction_id,
        candidate,
        *,
        output_dir,
    ):
    session_dir = Path(session_dir).resolve()
    output_dir = Path(output_dir).resolve()
    if not implementation_scaffold_candidate_identity_is_valid(
            candidate,
            transaction_id=transaction_id,
    ):
        raise ValueError("Implementation candidate identity invalid")
    fingerprint = candidate["candidate_fingerprint"]
    path = output_dir / f"workspace-candidate-{fingerprint}.json"
    expected_root = (
        session_dir / "ai" / "generation-transactions" / transaction_id
    ).resolve()
    if output_dir != expected_root:
        raise ValueError("Implementation candidate output path invalid")
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing != candidate:
            raise ValueError("Implementation candidate fingerprint conflict")
    else:
        write_json_atomic(path, candidate)
    return {
        "materialization_candidate_version": MATERIALIZATION_CANDIDATE_VERSION,
        "transaction_id": transaction_id,
        "path": path.relative_to(session_dir).as_posix(),
        "candidate_fingerprint": fingerprint,
        "file_count": len(candidate.get("files") or ()),
    }


def implementation_scaffold_candidate_audit(candidate, pointer):
    pointer = dict(pointer or {})
    if not implementation_scaffold_candidate_identity_is_valid(
            candidate,
            transaction_id=pointer.get("transaction_id"),
    ):
        raise ValueError("Implementation candidate identity invalid")
    if any((
        pointer.get("candidate_fingerprint")
        != candidate.get("candidate_fingerprint"),
        pointer.get("file_count") != len(candidate.get("files") or ()),
    )):
        raise ValueError("Implementation candidate pointer mismatch")
    return {
        "materialization_candidate_version": (
            MATERIALIZATION_CANDIDATE_VERSION
        ),
        "manifest_id": candidate.get("manifest_id"),
        "manifest_fingerprint": candidate.get("manifest_fingerprint"),
        "status": "candidate_prepared",
        "system_owned_files": list(
            candidate.get("system_owned_files") or ()
        ),
        "tasks": json.loads(json.dumps(
            candidate.get("tasks") or [],
            ensure_ascii=False,
        )),
        "candidate": pointer,
    }


def load_implementation_scaffold_candidate(
        session_dir,
        pointer,
        *,
        transaction_id,
    ):
    session_dir = Path(session_dir).resolve()
    pointer = dict(pointer or {})
    required = {
        "materialization_candidate_version",
        "transaction_id",
        "path",
        "candidate_fingerprint",
        "file_count",
    }
    if set(pointer) != required or any((
        pointer.get("materialization_candidate_version")
        != MATERIALIZATION_CANDIDATE_VERSION,
        pointer.get("transaction_id") != transaction_id,
        not isinstance(pointer.get("file_count"), int),
        isinstance(pointer.get("file_count"), bool),
        pointer.get("file_count", -1) < 0,
    )):
        raise ValueError("Implementation candidate pointer invalid")
    fingerprint = str(pointer.get("candidate_fingerprint") or "")
    expected = (
        session_dir
        / "ai"
        / "generation-transactions"
        / transaction_id
        / f"workspace-candidate-{fingerprint}.json"
    ).resolve()
    path = (session_dir / str(pointer.get("path") or "")).resolve()
    if path != expected or not path.is_file():
        raise ValueError("Implementation candidate path invalid")
    try:
        candidate = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Implementation candidate cannot be loaded") from error
    if any((
        not implementation_scaffold_candidate_identity_is_valid(
            candidate,
            transaction_id=transaction_id,
        ),
        candidate.get("candidate_fingerprint") != fingerprint,
        len(candidate.get("files") or ()) != pointer.get("file_count"),
    )):
        raise ValueError("Implementation candidate identity invalid")
    return candidate


def implementation_scaffold_candidate_identity_is_valid(
        value,
        *,
        transaction_id=None,
    ):
    if not isinstance(value, dict) or set(value) != {
        "materialization_candidate_version",
        "transaction_id",
        "manifest_id",
        "manifest_fingerprint",
        "status",
        "system_owned_files",
        "files",
        "tasks",
        "candidate_fingerprint",
    }:
        return False
    files = value.get("files")
    tasks = value.get("tasks")
    if any((
        value.get("materialization_candidate_version")
        != MATERIALIZATION_CANDIDATE_VERSION,
        transaction_id is not None
        and value.get("transaction_id") != transaction_id,
        not value.get("manifest_id"),
        not value.get("manifest_fingerprint"),
        value.get("status") != "candidate_prepared",
        not isinstance(files, list),
        not isinstance(tasks, list),
    )):
        return False
    paths = []
    for item in files:
        if not isinstance(item, dict) or set(item) != {
            "path",
            "before_exists",
            "before_sha256",
            "content",
            "sha256",
        }:
            return False
        path = str(item.get("path") or "")
        content = item.get("content")
        if any((
            not path,
            not isinstance(item.get("before_exists"), bool),
            item.get("before_exists") is False
            and item.get("before_sha256") is not None,
            item.get("before_exists") is True
            and not isinstance(item.get("before_sha256"), str),
            not isinstance(content, str),
            item.get("sha256") != _sha256_text(content),
        )):
            return False
        paths.append(path)
    if paths != sorted(set(paths)):
        return False
    if value.get("system_owned_files") != paths:
        return False
    return value.get("candidate_fingerprint") == _candidate_fingerprint(value)


def implementation_scaffold_candidate_matches(project_root, candidate):
    return implementation_scaffold_candidate_workspace_audit(
        project_root,
        candidate,
    )["matches"]


def implementation_scaffold_candidate_workspace_audit(
        project_root,
        candidate,
    ):
    if not implementation_scaffold_candidate_identity_is_valid(candidate):
        return {
            "status": "invalid_candidate",
            "matches": False,
            "files": [],
        }
    project_root = Path(project_root).resolve()
    files = []
    for item in candidate.get("files") or ():
        relative = str(item["path"])
        path = (project_root / relative).resolve()
        try:
            path.relative_to(project_root)
            content = path.read_bytes()
        except (OSError, ValueError) as error:
            files.append({
                "path": relative,
                "status": "missing",
                "comparison": "unavailable",
                "expected_sha256": item["sha256"],
                "actual_sha256": None,
                "expected_size": len(item["content"].encode("utf-8")),
                "actual_size": 0,
                "error": f"{type(error).__name__}: {error}",
            })
            continue
        actual_sha256 = hashlib.sha256(content).hexdigest()
        expected_content = item["content"].encode("utf-8")
        if actual_sha256 == item["sha256"]:
            status = "matches"
            comparison = "byte_exact"
        elif _normalized_utf8_text(content) == _normalized_utf8_text(
                expected_content,
        ):
            status = "modified"
            comparison = "utf8_text_normalized"
        else:
            status = "modified"
            comparison = "content_mismatch"
        files.append({
            "path": relative,
            "status": status,
            "comparison": comparison,
            "expected_sha256": item["sha256"],
            "actual_sha256": actual_sha256,
            "expected_size": len(expected_content),
            "actual_size": len(content),
            "error": None,
        })
    matches = all(item["status"] == "matches" for item in files)
    return {
        "status": "matches" if matches else "mismatch",
        "matches": matches,
        "files": files,
    }


def _normalized_utf8_text(content):
    try:
        text = bytes(content).decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if text and not text.endswith("\n"):
        text += "\n"
    return text


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
    writes, tasks = _implementation_scaffold_writes(
        project_root,
        manifest,
    )
    writable = set(manifest.get("allowed_changes") or ())
    immutable = set(manifest.get("read_only_reuse") or ())
    if not set(writes).issubset(writable) or set(writes) & immutable:
        raise ValueError("System materialization writes outside Manifest scope")

    return _materialize_implementation_writes(
        project_root,
        manifest,
        generation_input_snapshot,
        lease=lease,
        journal_path=journal_path,
        writes=writes,
        tasks=tasks,
    )


def materialize_implementation_candidate(
        project_root,
        manifest,
        generation_input_snapshot,
        candidate,
    *,
    lease,
    journal_path,
    ):
    project_root = Path(project_root).resolve()
    if not implementation_manifest_identity_is_valid(manifest):
        raise ValueError("Implementation Manifest identity invalid")
    if not implementation_scaffold_candidate_identity_is_valid(candidate):
        raise ValueError("Implementation candidate identity invalid")
    if any((
        candidate.get("manifest_id") != manifest.get("implementation_manifest_id"),
        candidate.get("manifest_fingerprint")
        != manifest.get("implementation_manifest_fingerprint"),
    )):
        raise ValueError("Implementation candidate does not match Manifest")
    writes = {
        str(item.get("path") or ""): str(item.get("content") or "")
        for item in candidate.get("files") or ()
        if isinstance(item, dict) and item.get("path")
    }
    if sorted(writes) != sorted(candidate.get("system_owned_files") or ()):
        raise ValueError("Implementation candidate file scope invalid")
    writable = set(manifest.get("allowed_changes") or ())
    immutable = set(manifest.get("read_only_reuse") or ())
    if not set(writes).issubset(writable) or set(writes) & immutable:
        raise ValueError("Implementation candidate writes outside Manifest scope")
    for item in candidate.get("files") or ():
        content = str(item.get("content") or "")
        if item.get("sha256") != _sha256_text(content):
            raise ValueError(
                f"Implementation candidate file hash mismatch: {item.get('path')}"
            )
    return _materialize_implementation_writes(
        project_root,
        manifest,
        generation_input_snapshot,
        lease=lease,
        journal_path=journal_path,
        writes=writes,
        tasks=list(candidate.get("tasks") or []),
    )


def _materialize_implementation_writes(
        project_root,
        manifest,
        generation_input_snapshot,
    *,
    lease,
    journal_path,
    writes,
    tasks,
    ):
    project_root = Path(project_root).resolve()
    if not implementation_manifest_identity_is_valid(manifest):
        raise ValueError("Implementation Manifest identity invalid")
    baseline_files = (generation_input_snapshot or {}).get("files")
    if not isinstance(baseline_files, dict):
        raise ValueError("Generation input snapshot is invalid")

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
                if not item.get("byte_exact"):
                    _atomic_write_text(
                        project_root / relative,
                        _editor_delivery_text(writes[relative]),
                    )
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
        "written_files": [
            item["path"]
            for item in journal.get("files") or []
            if not item.get("byte_exact")
        ],
        "skipped_byte_exact_files": [
            item["path"]
            for item in journal.get("files") or []
            if item.get("byte_exact")
        ],
        "changed_file_count": sum(
            1 for item in journal.get("files") or []
            if not item.get("byte_exact")
        ),
        "tasks": tasks,
        "journal_path": str(journal_path),
    }
    with generation_file_lease_write_guard(project_root, lease):
        journal = _load_journal(journal_path)
        journal["status"] = "committed"
        journal["audit"] = audit
        _seal_journal(journal)
        write_json_atomic(journal_path, journal)
    return audit


def _implementation_scaffold_writes(project_root, manifest):
    writable = set(manifest.get("allowed_changes") or ())
    immutable = set(manifest.get("read_only_reuse") or ())
    protected_locator_keys = {
        (str(item.get("file") or ""), str(item.get("key") or ""))
        for item in manifest.get("protected_locator_keys") or ()
        if isinstance(item, dict)
    }
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
    declared_locator_keys = _declared_locator_keys(manifest, immutable)
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
        _remove_equivalent_generated_locator_keys(
            document,
            path,
            key,
            patch,
            protected_locator_keys,
        )
        document[key] = patch

    for path, document in locator_documents.items():
        _prune_stale_generated_locator_keys(
            document,
            path,
            declared_locator_keys.get(path) or set(),
            protected_locator_keys,
        )
        content = dump_yaml(document)
        writes[path] = content
        tasks.append({
            "kind": "locator_document",
            "path": path,
            "keys": list(document),
            "sha256": _sha256_text(content),
        })

    for scaffold in manifest.get("python_scaffolds") or ():
        path = _system_path(
            scaffold,
            writable,
            immutable,
            expected_suffix=".py",
        )
        content = _render_python_scaffold(scaffold)
        writes[path] = content
        tasks.append({
            "kind": f"python_{scaffold.get('kind')}",
            "path": path,
            "sha256": _sha256_text(content),
        })
    return writes, tasks


def _implementation_candidate_file_writes(manifest, candidate_files):
    ai_editable = set(manifest.get("ai_editable_changes") or ())
    if not candidate_files:
        return {}, []
    writable = set(manifest.get("allowed_changes") or ())
    immutable = set(manifest.get("read_only_reuse") or ())
    writes = {}
    tasks = []
    for item in candidate_files or ():
        if not isinstance(item, dict):
            raise ValueError("Implementation candidate file must be an object")
        path = _candidate_file_path(item, writable, immutable)
        if path not in ai_editable:
            raise ValueError(
                f"AI candidate file is not declared ai_editable: {path}"
            )
        if path in writes:
            raise ValueError(f"Duplicate AI candidate file: {path}")
        content = item.get("content")
        if not isinstance(content, str):
            raise ValueError(f"AI candidate file content must be text: {path}")
        content_sha256 = _sha256_text(content)
        sha256 = item.get("sha256")
        if sha256 is not None and sha256 != content_sha256:
            raise ValueError(f"AI candidate file hash mismatch: {path}")
        writes[path] = content
        tasks.append({
            "kind": "ai_candidate_file",
            "path": path,
            "sha256": content_sha256,
        })
    missing = sorted(ai_editable - set(writes))
    if missing:
        raise ValueError(f"AI candidate files missing content: {missing}")
    return writes, tasks


def _candidate_file_path(item, writable, immutable):
    value = str((item or {}).get("path") or "")
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Invalid AI candidate file path: {value}")
    normalized = path.as_posix()
    if normalized in immutable or normalized not in writable:
        raise ValueError(f"AI candidate file path is not writable: {normalized}")
    return normalized


def _declared_locator_keys(manifest, immutable):
    result = {}
    for task in manifest.get("locator_patch") or ():
        path = str((task or {}).get("file") or "")
        key = str((task or {}).get("key") or "")
        if path and key and path not in immutable:
            result.setdefault(path, set()).add(key)
    return result


def _prune_stale_generated_locator_keys(
        document,
        path,
        declared_keys,
        protected_locator_keys,
    ):
    if not isinstance(document, dict):
        return
    for existing_key in list(document):
        if existing_key in declared_keys:
            continue
        if (path, existing_key) in protected_locator_keys:
            continue
        value = document.get(existing_key)
        if _is_stale_generated_locator_key(existing_key, value):
            document.pop(existing_key, None)


def _is_stale_generated_locator_key(key, value):
    if isinstance(value, dict) and value.get("top_level") is True:
        return True
    key = str(key or "")
    try:
        key.encode("ascii")
    except UnicodeEncodeError:
        return True
    lowered = key.casefold()
    return any(lowered.endswith(suffix) for suffix in (
        "_by_name",
        "_by_class",
        "_by_event_chain",
        "_by_ancestor",
        "_by_sibling",
        "_xpath",
        "_ocr",
        "_pos",
        "_index",
    ))


def _remove_equivalent_generated_locator_keys(
        document,
        path,
        key,
        patch,
        protected_locator_keys,
    ):
    if not isinstance(document, dict) or not isinstance(patch, dict):
        return
    for existing_key in list(document):
        if existing_key == key:
            continue
        if (path, existing_key) in protected_locator_keys:
            continue
        if not re.search(r"_[0-9a-f]{8}$", existing_key, flags=re.I):
            continue
        if document.get(existing_key) == patch:
            document.pop(existing_key, None)


def _is_content_identity_refinement(existing, patch):
    if not isinstance(existing, dict) or not isinstance(patch, dict):
        return False
    removed = set(existing) - set(patch)
    return bool(removed) and removed <= {"name", "title"} and all(
        existing.get(field) == value
        for field, value in patch.items()
    )


def _render_python_scaffold(scaffold):
    kind = str((scaffold or {}).get("kind") or "")
    if kind in {"window_page", "window_view", "rootless_pos_page"}:
        return _render_page_scaffold(scaffold.get("page") or {})
    if kind == "step_inline":
        return _render_step_scaffold(scaffold.get("steps") or ())
    raise ValueError(f"Unsupported Python scaffold kind: {kind}")


def _render_page_scaffold(page):
    class_name = str(page.get("class_name") or "")
    base_class = str(page.get("base_class") or "")
    if not class_name or base_class not in {
            "BasePage",
            "WindowPage",
            "WindowView",
    }:
        raise ValueError("Python Page scaffold identity is incomplete")
    lines = [f"from autowork_core.page import {base_class}"]
    views = list(page.get("views") or ())
    if base_class == "WindowPage" and views:
        lines.append("")
        for view in views:
            module = _python_module(view.get("path"))
            view_class = str(view.get("class_name") or "")
            if not module or not view_class:
                raise ValueError("Python View scaffold binding is incomplete")
            lines.append(f"from {module} import {view_class}")
    lines.extend(["", "", f"class {class_name}({base_class}):"])
    if base_class == "WindowPage":
        lines.append(
            f"    root_locator_file = {page.get('root_locator_file')!r}"
        )
        lines.append(f"    root_locator = {page.get('root_locator')!r}")
        for view in views:
            receiver = str(view.get("receiver") or "")
            view_class = str(view.get("class_name") or "")
            if not receiver or not view_class:
                raise ValueError("Python View property identity is incomplete")
            lines.extend([
                "",
                "    @property",
                f"    def {receiver}(self) -> {view_class}:",
                f"        return self.get_view({view_class})",
            ])
    elif base_class == "WindowView":
        lines.append(f"    locator_file = {page.get('locator_file')!r}")
        lines.append(
            "    active_locator = "
            + repr(f"${str(page.get('active_locator') or '').lstrip('$')}")
        )
    else:
        lines.append(f"    locator_file = {page.get('locator_file')!r}")
    return "\n".join(lines) + "\n"


def _render_step_scaffold(steps):
    steps = [dict(step) for step in steps if isinstance(step, dict)]
    bindings_by_step = {
        str(step.get("step_id") or ""): _step_page_bindings(step)
        for step in steps
    }
    imports = {}
    for bindings in bindings_by_step.values():
        for binding in bindings:
            module = str(binding.get("module") or "")
            class_name = str(binding.get("class_name") or "")
            existing = imports.get(module)
            if existing is not None and existing != class_name:
                raise ValueError(
                    "Python Step scaffold Page import conflicts: "
                    f"{module}"
                )
            imports[module] = class_name
    if any(not module or not class_name for module, class_name in imports.items()):
        raise ValueError("Python Step scaffold Page import is incomplete")
    lines = [
        "from behave import given, then, when, step",
        "",
        "from autowork_core.page import get_page",
    ]
    if any(step.get("issue_template") for step in steps):
        lines.append(
            "from autowork_core.runtime.generation_issue import "
            "unresolved_generation_issue"
        )
    lines.extend(
        f"from {module} import {class_name}"
        for module, class_name in sorted(imports.items())
    )
    used_names = set()
    for step in sorted(steps, key=lambda item: int(item.get("order") or 0)):
        decorator = str(step.get("python_decorator") or "")
        parameters = [str(item) for item in step.get("function_parameters") or ()]
        bindings = bindings_by_step.get(str(step.get("step_id") or "")) or []
        issue_template = step.get("issue_template") or {}
        if not decorator or not parameters:
            raise ValueError("Python Step scaffold contract is incomplete")
        if not issue_template and not bindings:
            raise ValueError("Python Step scaffold contract is incomplete")
        function_name = _step_function_name(step.get("step_id"), used_names)
        used_names.add(function_name)
        lines.extend([
            "",
            "",
            decorator,
            f"def {function_name}({', '.join(parameters)}):",
        ])
        for binding in bindings:
            lines.append(
                f"    {binding['receiver']} = get_page("
                f"context, {binding['class_name']})"
            )
        operations = list(step.get("operations") or ())
        execution_items = []
        requires_interleaved_order = bool(issue_template and operations)
        for issue in issue_template.get("arguments") or ():
            action_order = int(issue.get("action_order") or 0)
            if action_order < 1 and requires_interleaved_order:
                raise ValueError("Python issue scaffold Action顺序无效")
            if action_order < 1:
                action_order = 0
            execution_items.append((action_order, 0, "issue", issue))
        for operation_index, operation in enumerate(operations, start=1):
            action_order = int(operation.get("action_order") or 0)
            if action_order < 1 and requires_interleaved_order:
                raise ValueError("Python operation scaffold Action顺序无效")
            if action_order < 1:
                action_order = int(operation.get("order") or operation_index)
            execution_items.append((action_order, 1, "operation", operation))
        if not execution_items:
            lines.append("    pass")
            continue
        bound_receivers = {binding["receiver"] for binding in bindings}
        for _order, _kind, item_kind, item in sorted(execution_items):
            if item_kind == "issue":
                lines.extend([
                    "    unresolved_generation_issue(",
                    f"        {item.get('issue_id')!r},",
                    f"        step_id={item.get('step_id')!r},",
                    f"        issue_type={item.get('issue_type')!r},",
                    "    )",
                ])
                continue
            operation = item
            operation_receiver = str(
                operation.get("receiver_expression") or ""
            ).split(".", 1)[0]
            if operation_receiver not in bound_receivers:
                raise ValueError(
                    "Python operation scaffold Page receiver is unbound: "
                    f"{operation_receiver}"
                )
            lines.append("    " + _render_operation_call(operation))
    return "\n".join(lines) + "\n"


def _step_page_bindings(step):
    raw = step.get("page_bindings")
    if raw is None:
        raw = [step.get("page") or {}]
    if not isinstance(raw, list):
        raise ValueError("Python Step scaffold Page bindings are invalid")
    result = []
    by_receiver = {}
    for item in raw:
        binding = dict(item) if isinstance(item, dict) else {}
        value = {
            "module": str(binding.get("module") or ""),
            "class_name": str(binding.get("class_name") or ""),
            "receiver": str(binding.get("receiver") or ""),
        }
        if not all(value.values()):
            raise ValueError("Python Step scaffold Page binding is incomplete")
        existing = by_receiver.get(value["receiver"])
        if existing is not None and existing != value:
            raise ValueError(
                "Python Step scaffold Page receiver conflicts: "
                f"{value['receiver']}"
            )
        if existing is None:
            result.append(value)
            by_receiver[value["receiver"]] = value
    return result


def _render_operation_call(operation):
    name = str(operation.get("operation") or "")
    receiver = str(operation.get("receiver_expression") or "")
    target = operation.get("target")
    if not name or not receiver or not target:
        raise ValueError("Python operation scaffold is incomplete")
    method = getattr(BasePage, name, None)
    if not callable(method):
        raise ValueError(f"Python operation scaffold API is unavailable: {name}")
    signature = inspect.signature(method)
    parameters = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.name != "self"
    ]
    positional = {0: repr(target)}
    value = operation.get("value")
    value_source = str(operation.get("value_source") or "")
    capability = capability_by_name(name)
    value_argument = capability.value_argument if capability else None
    rendered_value = None
    if value_source.startswith("examples."):
        rendered_value = value_source.split(".", 1)[1]
    elif value_source.startswith("data."):
        rendered_value = repr("$data:" + value_source.split(".", 1)[1])
    elif value is not None:
        rendered_value = repr(value)
    source_parameters = dict(operation.get("parameters") or {})
    if rendered_value is not None:
        value_index = value_argument[0] if value_argument is not None else 1
        positional[value_index] = rendered_value
        for index in range(1, value_index):
            parameter = parameters[index]
            if parameter.name not in source_parameters:
                raise ValueError(
                    "Python operation scaffold lacks positional parameter: "
                    f"{name}.{parameter.name}"
                )
            positional[index] = repr(source_parameters.pop(parameter.name))
    last_index = max(positional)
    if set(positional) != set(range(last_index + 1)):
        raise ValueError(f"Python operation scaffold has argument gap: {name}")
    arguments = [positional[index] for index in range(last_index + 1)]
    keywords = []
    proof_only = {
        "expected",
        "expected_source",
        "argument",
    }
    parameter_names = {parameter.name for parameter in parameters}
    occupied = {parameters[0].name} if parameters else set()
    if rendered_value is not None and value_argument is not None:
        occupied.add(value_argument[1])
    for key, value in source_parameters.items():
        if key in proof_only or key not in parameter_names:
            continue
        if key in occupied:
            continue
        keywords.append(f"{key}={value!r}")
    return f"{receiver}.{name}({', '.join([*arguments, *keywords])})"


def _python_module(path):
    value = str(path or "").replace("\\", "/")
    if value.endswith(".py"):
        value = value[:-3]
    return value.replace("/", ".")


def _step_function_name(step_id, used):
    suffix = "".join(
        character
        for character in str(step_id or "")
        if character.isascii() and character.isalnum()
    )[-12:].lower()
    base = f"step_{suffix or 'generated'}"
    candidate = base
    index = 2
    while candidate in used:
        candidate = f"{base}_{index}"
        index += 1
    return candidate


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
    return hashlib.sha256(
        _editor_delivery_text(value).encode("utf-8")
    ).hexdigest()


def _editor_delivery_text(value):
    text = str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.rstrip("\n") + "\n"
    return text.replace("\n", os.linesep)


def _candidate_fingerprint(value):
    payload = {
        key: item
        for key, item in dict(value or {}).items()
        if key != "candidate_fingerprint"
    }
    return hashlib.sha256(json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _build_journal(project_root, manifest, writes, journal_path):
    files = []
    for relative in sorted(writes):
        path = (project_root / relative).resolve()
        path.relative_to(project_root)
        original = path.read_bytes() if path.is_file() else None
        replacement = _editor_delivery_text(writes[relative]).encode("utf-8")
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
            "byte_exact": bool(
                original is not None
                and hashlib.sha256(original).hexdigest()
                == hashlib.sha256(replacement).hexdigest()
            ),
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
    writable = set(manifest.get("allowed_changes") or ())
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
        or not set(actual_paths).issubset(writable)
        or not set(actual_paths).issubset(lease_files)
    ):
        raise ValueError("Materialization journal file scope mismatch")
    for item in files:
        relative = str(item["path"])
        if item.get("replacement_sha256") != _sha256_text(writes[relative]):
            raise ValueError(
                f"Materialization journal replacement mismatch: {relative}"
            )
        if "byte_exact" in item and not isinstance(item.get("byte_exact"), bool):
            raise ValueError(
                f"Materialization journal byte_exact invalid: {relative}"
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
            set(manifest.get("allowed_changes") or ())
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
