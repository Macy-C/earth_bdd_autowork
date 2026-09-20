from __future__ import annotations

import base64
import difflib
import hashlib
import json
import os
import secrets
from pathlib import Path, PurePosixPath


GENERATION_DIFF_VERSION = "1.0"
GENERATION_DIFF_SUMMARY_VERSION = "1.0"
DEFAULT_DIFF_PAGE_BYTES = 12 * 1024
MAX_DIFF_PAGE_BYTES = 32 * 1024
DEFAULT_DIFF_SUMMARY_FILE_LIMIT = 20


def build_generation_diff(project_root, report_path, report):
    project_root = Path(project_root).resolve()
    report_path = Path(report_path).resolve()
    baseline = _load_baseline(report_path, report)
    baseline_by_path = {
        str(item.get("path") or ""): item
        for item in baseline["files"]
    }
    changed = list(report.get("changed_files") or ())
    sections = []
    for relative in changed:
        before = _baseline_bytes(baseline_by_path.get(relative), relative)
        after_path = (project_root / relative).resolve()
        after_path.relative_to(project_root)
        after = after_path.read_bytes() if after_path.is_file() else None
        sections.append(_file_diff(relative, before, after))
    content = "".join(sections)
    encoded = content.encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    output = report_path.parent / f"implementation-{digest}.diff"
    if output.exists() and output.read_bytes() != encoded:
        raise ValueError("Generation implementation diff conflicts")
    if not output.exists():
        _write_bytes_atomic(output, encoded)
    return {
        "generation_diff_version": GENERATION_DIFF_VERSION,
        "transaction_id": report.get("transaction_id"),
        "path": output.relative_to(report_path.parents[3]).as_posix(),
        "sha256": digest,
        "size": len(encoded),
        "file_count": len(changed),
        "encoding": "utf-8",
        "format": "unified_diff",
    }


def load_generation_diff(session_dir, pointer, *, offset=0, limit=None):
    session_dir = Path(session_dir).resolve()
    pointer = dict(pointer or {})
    if not generation_diff_pointer_is_valid(pointer):
        raise ValueError("Generation implementation diff pointer无效")
    path = (session_dir / str(pointer.get("path") or "")).resolve()
    root = (session_dir / "ai" / "generation-transactions").resolve()
    expected = (
        root
        / str(pointer.get("transaction_id") or "")
        / f"implementation-{pointer.get('sha256')}.diff"
    ).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError("Generation implementation diff path越界") from error
    if path != expected or not path.is_file():
        raise ValueError("Generation implementation diff path无效")
    content = path.read_bytes()
    if any((
        pointer.get("generation_diff_version") != GENERATION_DIFF_VERSION,
        pointer.get("encoding") != "utf-8",
        pointer.get("format") != "unified_diff",
        not isinstance(pointer.get("file_count"), int),
        pointer.get("file_count", -1) < 0,
        pointer.get("size") != len(content),
        pointer.get("sha256") != hashlib.sha256(content).hexdigest(),
    )):
        raise ValueError("Generation implementation diff identity无效")
    offset = int(offset or 0)
    if offset < 0 or offset > len(content):
        raise ValueError("Generation implementation diff offset无效")
    try:
        content[:offset].decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("Generation implementation diff offset无效") from error
    page_limit = min(
        max(1, int(limit or DEFAULT_DIFF_PAGE_BYTES)),
        MAX_DIFF_PAGE_BYTES,
    )
    end = min(len(content), offset + page_limit)
    while end < len(content) and end > offset:
        try:
            content[offset:end].decode("utf-8")
            break
        except UnicodeDecodeError:
            end -= 1
    if end == offset and end < len(content):
        end += 1
        while end <= len(content):
            try:
                content[offset:end].decode("utf-8")
                break
            except UnicodeDecodeError:
                end += 1
    page = content[offset:end].decode("utf-8")
    return {
        "generation_diff_query_version": "1.0",
        "status": "projected",
        "artifact": pointer,
        "page": {
            "offset": offset,
            "bytes": end - offset,
            "total_bytes": len(content),
            "next_offset": end if end < len(content) else None,
        },
        "content": page,
    }


def build_generation_diff_summary(
        session_dir,
        pointer,
        *,
        max_files=DEFAULT_DIFF_SUMMARY_FILE_LIMIT,
    ):
    content = _load_diff_content(session_dir, pointer).decode("utf-8")
    max_files = max(0, int(max_files))
    files = _diff_file_summaries(content)
    if len(files) != pointer.get("file_count"):
        raise ValueError("Generation implementation diff summary file count mismatch")
    displayed = files[:max_files]
    additions = sum(item["additions"] for item in files)
    deletions = sum(item["deletions"] for item in files)
    return {
        "implementation_diff_summary_version": GENERATION_DIFF_SUMMARY_VERSION,
        "source": "implementation_diff",
        "transaction_id": pointer.get("transaction_id"),
        "file_count": len(files),
        "total_file_count": len(files),
        "displayed_file_count": len(displayed),
        "files_truncated": len(displayed) < len(files),
        "additions": additions,
        "deletions": deletions,
        "summary": _change_summary_text(len(files), additions, deletions),
        "files": displayed,
    }


def generation_diff_summary_is_valid(value, *, pointer=None):
    if not isinstance(value, dict):
        return False
    files = value.get("files")
    if not isinstance(files, list) or not all(
            _diff_file_summary_is_valid(item) for item in files
    ):
        return False
    file_count = value.get("file_count")
    displayed_file_count = value.get("displayed_file_count")
    additions = value.get("additions")
    deletions = value.get("deletions")
    total_file_count = value.get("total_file_count")
    if any(
        not isinstance(item, int) or isinstance(item, bool) or item < 0
        for item in (
            file_count,
            displayed_file_count,
            additions,
            deletions,
            total_file_count,
        )
    ):
        return False
    if any((
        value.get("implementation_diff_summary_version")
        != GENERATION_DIFF_SUMMARY_VERSION,
        value.get("source") != "implementation_diff",
        displayed_file_count != len(files),
        displayed_file_count > file_count,
        total_file_count != file_count,
        bool(value.get("files_truncated")) != (displayed_file_count < file_count),
        value.get("summary") != _change_summary_text(
            file_count,
            additions,
            deletions,
        ),
    )):
        return False
    if pointer is not None and any((
        value.get("transaction_id") != pointer.get("transaction_id"),
        value.get("file_count") != pointer.get("file_count"),
    )):
        return False
    return True


def generation_diff_pointer_is_valid(value, *, transaction_id=None):
    required = {
        "generation_diff_version",
        "transaction_id",
        "path",
        "sha256",
        "size",
        "file_count",
        "encoding",
        "format",
    }
    if not isinstance(value, dict) or set(value) != required:
        return False
    pointer_transaction_id = str(value.get("transaction_id") or "")
    digest = str(value.get("sha256") or "")
    relative = PurePosixPath(str(value.get("path") or ""))
    expected = PurePosixPath(
        "ai",
        "generation-transactions",
        pointer_transaction_id,
        f"implementation-{digest}.diff",
    )
    return bool(
        value.get("generation_diff_version") == GENERATION_DIFF_VERSION
        and pointer_transaction_id.startswith("transaction-")
        and (transaction_id is None or pointer_transaction_id == transaction_id)
        and len(digest) == 64
        and all(character in "0123456789abcdef" for character in digest)
        and relative == expected
        and isinstance(value.get("size"), int)
        and not isinstance(value.get("size"), bool)
        and value.get("size", -1) >= 0
        and isinstance(value.get("file_count"), int)
        and not isinstance(value.get("file_count"), bool)
        and value.get("file_count", -1) >= 0
        and value.get("encoding") == "utf-8"
        and value.get("format") == "unified_diff"
    )


def _load_diff_content(session_dir, pointer):
    session_dir = Path(session_dir).resolve()
    pointer = dict(pointer or {})
    if not generation_diff_pointer_is_valid(pointer):
        raise ValueError("Generation implementation diff pointer无效")
    path = (session_dir / str(pointer.get("path") or "")).resolve()
    root = (session_dir / "ai" / "generation-transactions").resolve()
    expected = (
        root
        / str(pointer.get("transaction_id") or "")
        / f"implementation-{pointer.get('sha256')}.diff"
    ).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError("Generation implementation diff path越界") from error
    if path != expected or not path.is_file():
        raise ValueError("Generation implementation diff path无效")
    content = path.read_bytes()
    if any((
        pointer.get("generation_diff_version") != GENERATION_DIFF_VERSION,
        pointer.get("encoding") != "utf-8",
        pointer.get("format") != "unified_diff",
        not isinstance(pointer.get("file_count"), int),
        pointer.get("file_count", -1) < 0,
        pointer.get("size") != len(content),
        pointer.get("sha256") != hashlib.sha256(content).hexdigest(),
    )):
        raise ValueError("Generation implementation diff identity无效")
    return content


def _diff_file_summaries(content):
    files = []
    current = None
    for line in str(content or "").splitlines():
        if line.startswith("diff --git a/"):
            if current is not None:
                files.append(current)
            current = _new_diff_file_summary(line)
            continue
        if current is None:
            continue
        if line == "new file mode 100644":
            current["change_type"] = "created"
        elif line == "deleted file mode 100644":
            current["change_type"] = "deleted"
        elif line.startswith("Binary files "):
            current["binary"] = True
        elif line.startswith("+++") or line.startswith("---"):
            continue
        elif line.startswith("+"):
            current["additions"] += 1
        elif line.startswith("-"):
            current["deletions"] += 1
    if current is not None:
        files.append(current)
    return files


def _new_diff_file_summary(line):
    payload = line[len("diff --git a/"):]
    before, separator, after = payload.partition(" b/")
    path = after if separator else before
    return {
        "path": path,
        "change_type": "modified",
        "additions": 0,
        "deletions": 0,
        "binary": False,
    }


def _diff_file_summary_is_valid(value):
    if not isinstance(value, dict) or set(value) != {
        "path",
        "change_type",
        "additions",
        "deletions",
        "binary",
    }:
        return False
    return bool(
        value.get("path")
        and value.get("change_type") in {"created", "modified", "deleted"}
        and isinstance(value.get("additions"), int)
        and not isinstance(value.get("additions"), bool)
        and value.get("additions") >= 0
        and isinstance(value.get("deletions"), int)
        and not isinstance(value.get("deletions"), bool)
        and value.get("deletions") >= 0
        and isinstance(value.get("binary"), bool)
    )


def _change_summary_text(file_count, additions, deletions):
    return f"{file_count} files changed, +{additions} -{deletions}"
    return bool(
        value.get("generation_diff_version") == GENERATION_DIFF_VERSION
        and pointer_transaction_id.startswith("transaction-")
        and (transaction_id is None or pointer_transaction_id == transaction_id)
        and len(digest) == 64
        and all(character in "0123456789abcdef" for character in digest)
        and relative == expected
        and isinstance(value.get("size"), int)
        and not isinstance(value.get("size"), bool)
        and value.get("size", -1) >= 0
        and isinstance(value.get("file_count"), int)
        and not isinstance(value.get("file_count"), bool)
        and value.get("file_count", -1) >= 0
        and value.get("encoding") == "utf-8"
        and value.get("format") == "unified_diff"
    )


def _load_baseline(report_path, report):
    pointer = report.get("generation_baseline") or {}
    path = Path(str(pointer.get("path") or "")).resolve()
    if path != report_path.parent / "generation-baseline.json":
        raise ValueError("Generation baseline path invalid for diff")
    value = json.loads(path.read_text(encoding="utf-8"))
    payload = {
        key: item for key, item in value.items()
        if key != "fingerprint"
    }
    if any((
        value.get("transaction_id") != report.get("transaction_id"),
        value.get("fingerprint") != _fingerprint(payload),
        pointer.get("fingerprint") != value.get("fingerprint"),
    )):
        raise ValueError("Generation baseline identity invalid for diff")
    return value


def _baseline_bytes(item, relative):
    if item is None:
        raise ValueError(f"Generation diff缺少baseline记录: {relative}")
    if item.get("exists") is not True:
        return None
    try:
        content = base64.b64decode(
            str(item.get("content_base64") or "").encode("ascii"),
            validate=True,
        )
    except (ValueError, UnicodeError) as error:
        raise ValueError("Generation diff baseline content无效") from error
    if any((
        hashlib.sha256(content).hexdigest() != item.get("sha256"),
        len(content) != item.get("size"),
    )):
        raise ValueError("Generation diff baseline hash无效")
    return content


def _file_diff(relative, before, after):
    before_exists = before is not None
    after_exists = after is not None
    before = before if before_exists else b""
    after = after if after_exists else b""
    header = [f"diff --git a/{relative} b/{relative}\n"]
    if not before_exists:
        header.append("new file mode 100644\n")
    elif not after_exists:
        header.append("deleted file mode 100644\n")
    if b"\x00" in before or b"\x00" in after:
        return _binary_diff(
            header,
            relative,
            before,
            after,
            before_exists,
            after_exists,
        )
    try:
        before_text = before.decode("utf-8")
        after_text = after.decode("utf-8")
    except UnicodeDecodeError:
        return _binary_diff(
            header,
            relative,
            before,
            after,
            before_exists,
            after_exists,
        )
    body = _join_unified_diff(difflib.unified_diff(
        before_text.splitlines(keepends=True),
        after_text.splitlines(keepends=True),
        fromfile=f"a/{relative}" if before_exists else "/dev/null",
        tofile=f"b/{relative}" if after_exists else "/dev/null",
        lineterm="\n",
    ))
    return "".join(header) + body


def _binary_diff(header, relative, before, after, before_exists, after_exists):
    before_name = f"a/{relative}" if before_exists else "/dev/null"
    after_name = f"b/{relative}" if after_exists else "/dev/null"
    return "".join(header) + (
        f"Binary files {before_name} and {after_name} differ\n"
        f"before-sha256 {_content_digest(before, before_exists)}\n"
        f"after-sha256 {_content_digest(after, after_exists)}\n"
    )


def _join_unified_diff(lines):
    output = []
    for line in lines:
        output.append(line)
        if not line.endswith(("\n", "\r")):
            output.append("\n\\ No newline at end of file\n")
    return "".join(output)


def _content_digest(content, exists):
    return hashlib.sha256(content).hexdigest() if exists else "absent"


def _write_bytes_atomic(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _fingerprint(value):
    return hashlib.sha256(json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


__all__ = [
    "DEFAULT_DIFF_PAGE_BYTES",
    "DEFAULT_DIFF_SUMMARY_FILE_LIMIT",
    "GENERATION_DIFF_VERSION",
    "GENERATION_DIFF_SUMMARY_VERSION",
    "MAX_DIFF_PAGE_BYTES",
    "build_generation_diff",
    "build_generation_diff_summary",
    "generation_diff_pointer_is_valid",
    "generation_diff_summary_is_valid",
    "load_generation_diff",
]
