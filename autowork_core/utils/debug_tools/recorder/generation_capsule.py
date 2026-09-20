from __future__ import annotations

import base64
import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path

from config.paths import Paths
from autowork_core.utils.debug_tools.recorder.generation_validation import (
    snapshot_runtime_variable_calls,
)
from autowork_core.utils.debug_tools.recorder.models import SCHEMA_VERSION
from autowork_core.utils.debug_tools.recorder.writer import write_json_atomic


GENERATION_CAPSULE_VERSION = "1.0"
GENERATION_INPUT_SNAPSHOT_VERSION = "1.0"
GENERATION_INPUT_SOURCE_VERSION = "1.0"


def build_generation_capsule(
        request,
        state,
        contract_lease,
        *,
        project_root=None,
        input_roots=(),
        exact_files=(),
        source_paths=(),
        created_at=None,
    ):
    request = copy.deepcopy(request or {})
    state = copy.deepcopy(state or {})
    project_root = Path(project_root or Paths.BASE_DIR).resolve()
    snapshot = snapshot_generation_inputs(
        project_root,
        input_roots=input_roots,
        exact_files=exact_files,
    )
    source_files = snapshot_source_files(
        project_root,
        snapshot,
        source_paths=source_paths,
    )
    value = {
        "schema_version": SCHEMA_VERSION,
        "generation_capsule_version": GENERATION_CAPSULE_VERSION,
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
        "brief": copy.deepcopy(state.get("brief") or {}),
        "generation_contract_lease": copy.deepcopy(contract_lease or {}),
        "generation_input_snapshot_fingerprint": _fingerprint(snapshot),
        "generation_input_snapshot": snapshot,
        "source_files": source_files,
    }
    value["capsule_fingerprint"] = generation_capsule_fingerprint(value)
    value["capsule_id"] = "generation-capsule-" + value[
        "capsule_fingerprint"
    ][:16]
    return value


def persist_generation_capsule(session_dir, capsule):
    session_dir = Path(session_dir).resolve()
    if not generation_capsule_identity_is_valid(capsule):
        raise ValueError("Generation Capsule identity invalid")
    output = _capsule_path(
        session_dir,
        (capsule.get("request") or {}).get("request_id"),
        capsule.get("capsule_fingerprint"),
    )
    if output.exists():
        existing = _read_json(output)
        if existing != capsule:
            raise ValueError(f"Generation Capsule fingerprint conflict: {output}")
        return output, copy.deepcopy(existing)
    output.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(output, capsule)
    return output, copy.deepcopy(capsule)


def generation_capsule_pointer(session_dir, capsule, path):
    session_dir = Path(session_dir).resolve()
    path = Path(path).resolve()
    expected = _capsule_path(
        session_dir,
        (capsule.get("request") or {}).get("request_id"),
        capsule.get("capsule_fingerprint"),
    )
    if path != expected or not generation_capsule_identity_is_valid(capsule):
        raise ValueError("Generation Capsule path or identity invalid")
    return {
        "generation_capsule_version": GENERATION_CAPSULE_VERSION,
        "path": path.relative_to(session_dir).as_posix(),
        "capsule_id": capsule.get("capsule_id"),
        "capsule_fingerprint": capsule.get("capsule_fingerprint"),
        "request_id": (capsule.get("request") or {}).get("request_id"),
        "generation_input_snapshot_fingerprint": capsule.get(
            "generation_input_snapshot_fingerprint"
        ),
        "source_file_count": len(capsule.get("source_files") or ()),
    }


def generation_capsule_pointer_is_valid(pointer):
    if not isinstance(pointer, dict):
        return False
    required = {
        "generation_capsule_version",
        "path",
        "capsule_id",
        "capsule_fingerprint",
        "request_id",
        "generation_input_snapshot_fingerprint",
        "source_file_count",
    }
    return bool(
        set(pointer) == required
        and pointer.get("generation_capsule_version")
        == GENERATION_CAPSULE_VERSION
        and pointer.get("path")
        and pointer.get("capsule_id")
        and pointer.get("capsule_fingerprint")
        and pointer.get("request_id")
        and pointer.get("generation_input_snapshot_fingerprint")
        and isinstance(pointer.get("source_file_count"), int)
        and not isinstance(pointer.get("source_file_count"), bool)
        and pointer.get("source_file_count") >= 0
    )


def load_generation_capsule(session_dir, pointer):
    session_dir = Path(session_dir).resolve()
    pointer = copy.deepcopy(pointer or {})
    if not generation_capsule_pointer_is_valid(pointer):
        raise ValueError("Generation Capsule pointer invalid")
    expected = _capsule_path(
        session_dir,
        pointer.get("request_id"),
        pointer.get("capsule_fingerprint"),
    )
    path = (session_dir / str(pointer.get("path") or "")).resolve()
    if path != expected or not path.is_file():
        raise ValueError("Generation Capsule path invalid")
    try:
        capsule = _read_json(path)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Generation Capsule cannot be loaded") from error
    if not generation_capsule_identity_is_valid(capsule):
        raise ValueError("Generation Capsule identity invalid")
    if generation_capsule_pointer(session_dir, capsule, path) != pointer:
        raise ValueError("Generation Capsule pointer mismatch")
    return capsule


def generation_capsule_identity_is_valid(capsule):
    if not isinstance(capsule, dict):
        return False
    request = capsule.get("request") or {}
    snapshot = capsule.get("generation_input_snapshot") or {}
    source_files = capsule.get("source_files")
    if any((
        capsule.get("schema_version") != SCHEMA_VERSION,
        capsule.get("generation_capsule_version")
        != GENERATION_CAPSULE_VERSION,
        not capsule.get("created_at"),
        not request.get("request_id"),
        not request.get("request_fingerprint"),
        not request.get("revision_seal"),
        not (capsule.get("brief") or {}).get("brief_fingerprint"),
        not (capsule.get("generation_contract_lease") or {}).get(
            "lease_fingerprint"
        ),
        capsule.get("generation_input_snapshot_fingerprint")
        != _fingerprint(snapshot),
        not _generation_input_snapshot_is_valid(snapshot),
        not _source_files_are_valid(source_files),
        not _source_files_match_snapshot(source_files, snapshot),
    )):
        return False
    actual = generation_capsule_fingerprint(capsule)
    return bool(
        capsule.get("capsule_fingerprint") == actual
        and capsule.get("capsule_id") == f"generation-capsule-{actual[:16]}"
    )


def generation_capsule_fingerprint(capsule):
    value = {
        key: item
        for key, item in copy.deepcopy(capsule or {}).items()
        if key not in {"capsule_id", "capsule_fingerprint"}
    }
    return _fingerprint(value)


def generation_capsule_input_snapshot(capsule):
    if not generation_capsule_identity_is_valid(capsule):
        raise ValueError("Generation Capsule identity invalid")
    return copy.deepcopy(capsule.get("generation_input_snapshot") or {})


def generation_capsule_source_files(capsule, *, paths=None):
    if not generation_capsule_identity_is_valid(capsule):
        raise ValueError("Generation Capsule identity invalid")
    wanted = None if paths is None else {str(path).replace("\\", "/") for path in paths}
    return [
        copy.deepcopy(item)
        for item in capsule.get("source_files") or ()
        if wanted is None or item.get("path") in wanted
    ]


def snapshot_generation_inputs(project_root, *, input_roots, exact_files=()):
    project_root = Path(project_root).resolve()
    roots = tuple(Path(root) for root in input_roots or ())
    exact = tuple(Path(path) for path in exact_files or ())
    files = {}
    for root in roots:
        directory = project_root / root
        if not directory.exists():
            continue
        for path in sorted(
            item
            for item in directory.rglob("*")
            if item.is_file() or item.is_symlink()
        ):
            if _ignored_source_path(path):
                continue
            relative = path.relative_to(project_root).as_posix()
            files[relative] = _snapshot_file_record(path)
    for relative in exact:
        path = project_root / relative
        if path.is_file() or path.is_symlink():
            files[relative.as_posix()] = _snapshot_file_record(path)
    return {
        "snapshot_version": GENERATION_INPUT_SNAPSHOT_VERSION,
        "roots": [path.as_posix() for path in roots],
        "exact_files": [path.as_posix() for path in exact],
        "files": files,
    }


def snapshot_source_files(project_root, generation_input_snapshot, *, source_paths=()):
    project_root = Path(project_root).resolve()
    selected = {
        str(path).replace("\\", "/")
        for path in source_paths or ()
        if str(path or "")
    }
    records = []
    for relative, snapshot in sorted(
            (generation_input_snapshot.get("files") or {}).items()
    ):
        if selected and relative not in selected:
            continue
        path = (project_root / relative).resolve()
        try:
            path.relative_to(project_root)
        except ValueError as error:
            raise ValueError(f"Generation source path escapes project: {relative}") from error
        records.append(_source_file_record(relative, path, snapshot))
    return records


def _source_file_record(relative, path, snapshot):
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    if snapshot.get("is_symlink") is True:
        return {
            "source_file_version": GENERATION_INPUT_SOURCE_VERSION,
            "path": str(relative),
            "is_symlink": True,
            "link_target": snapshot.get("link_target"),
        }
    content = path.read_bytes()
    sha256 = hashlib.sha256(content).hexdigest()
    if sha256 != snapshot.get("sha256"):
        raise ValueError(f"Generation source snapshot drifted while sealing: {relative}")
    try:
        text = content.decode("utf-8")
        return {
            "source_file_version": GENERATION_INPUT_SOURCE_VERSION,
            "path": str(relative),
            "is_symlink": False,
            "sha256": sha256,
            "size": len(content),
            "content_encoding": "utf-8",
            "content": text,
        }
    except UnicodeDecodeError:
        return {
            "source_file_version": GENERATION_INPUT_SOURCE_VERSION,
            "path": str(relative),
            "is_symlink": False,
            "sha256": sha256,
            "size": len(content),
            "content_encoding": "base64",
            "content_base64": base64.b64encode(content).decode("ascii"),
        }


def _generation_input_snapshot_is_valid(snapshot):
    if not isinstance(snapshot, dict):
        return False
    files = snapshot.get("files")
    return bool(
        snapshot.get("snapshot_version") == GENERATION_INPUT_SNAPSHOT_VERSION
        and isinstance(snapshot.get("roots"), list)
        and isinstance(snapshot.get("exact_files"), list)
        and isinstance(files, dict)
    )


def _source_files_are_valid(source_files):
    if not isinstance(source_files, list):
        return False
    paths = []
    for item in source_files:
        if not isinstance(item, dict):
            return False
        path = str(item.get("path") or "")
        if not path or item.get("source_file_version") != GENERATION_INPUT_SOURCE_VERSION:
            return False
        if item.get("is_symlink") is True:
            if set(item) != {
                "source_file_version",
                "path",
                "is_symlink",
                "link_target",
            }:
                return False
        elif item.get("is_symlink") is False:
            encoding = item.get("content_encoding")
            if encoding == "utf-8":
                content = item.get("content")
                if not isinstance(content, str):
                    return False
                expected = hashlib.sha256(content.encode("utf-8")).hexdigest()
            elif encoding == "base64":
                try:
                    content = base64.b64decode(
                        str(item.get("content_base64") or ""),
                        validate=True,
                    )
                except (ValueError, TypeError):
                    return False
                expected = hashlib.sha256(content).hexdigest()
            else:
                return False
            if any((
                not isinstance(item.get("sha256"), str),
                item.get("sha256") != expected,
                not isinstance(item.get("size"), int),
                isinstance(item.get("size"), bool),
            )):
                return False
        else:
            return False
        paths.append(path)
    return paths == sorted(set(paths))


def _source_files_match_snapshot(source_files, snapshot):
    files = (snapshot or {}).get("files") or {}
    for item in source_files or ():
        path = str(item.get("path") or "")
        record = files.get(path)
        if record is None:
            return False
        if item.get("is_symlink") is True:
            if record.get("is_symlink") is not True:
                return False
            continue
        if any((
            record.get("is_symlink") is True,
            item.get("sha256") != record.get("sha256"),
            item.get("size") != record.get("size"),
        )):
            return False
    return True


def _snapshot_file_record(path):
    path = Path(path)
    if path.is_symlink():
        try:
            target = str(path.readlink())
        except OSError:
            target = None
        return {
            "is_symlink": True,
            "link_target": target,
        }
    record = {
        "sha256": _sha256_file(path),
        "size": path.stat().st_size,
    }
    if path.suffix.casefold() == ".py":
        calls = snapshot_runtime_variable_calls(path)
        if calls:
            record["runtime_variable_calls"] = calls
    return record


def _ignored_source_path(path):
    relative_parts = Path(path).parts
    return "__pycache__" in relative_parts or Path(path).suffix.casefold() in {
        ".pyc",
        ".pyo",
    }


def _capsule_path(session_dir, request_id, fingerprint):
    return (
        Path(session_dir)
        / "ai"
        / "generation-capsules"
        / str(request_id or "")
        / f"capsule-{fingerprint}.json"
    ).resolve()


def _sha256_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _fingerprint(value):
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))