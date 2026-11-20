"""Document hashes, explicit file upload, and read-only processing status."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from http.client import HTTPException
from uuid import uuid4

from autowork_core.knowledge.weknora_client import (
    PublishError,
    RemoteHTTPError,
    WeKnoraClient,
    resolve_kb_role_selector,
    verify_resolved_kb_role,
)


HASH_LABEL = "\u6587\u6863\u54c8\u5e0c\uff08SHA256\uff09\uff1a"
FRAMEWORK_ROOT = Path(__file__).resolve().parents[2]


def _hash_line_index(text: str) -> int | None:
    indices = []
    fence = None
    for index, line in enumerate(text.removeprefix("\ufeff").splitlines()):
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
            continue
        if fence:
            continue
        if re.match(r"^#{2,6}\s", line):
            break
        if line.startswith(HASH_LABEL):
            indices.append(index)
    if len(indices) > 1:
        raise ValueError("Document header contains more than one hash field")
    return indices[0] if indices else None


def calculate_document_hash(text: str) -> str:
    normalized = text.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.splitlines(keepends=True)
    index = _hash_line_index(normalized)
    if index is not None:
        del lines[index]
    return hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()


def extract_document_hash(text: str) -> str | None:
    index = _hash_line_index(text)
    if index is None:
        return None
    line = text.removeprefix("\ufeff").splitlines()[index]
    value = line[len(HASH_LABEL):].strip().strip("`")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise ValueError("Document hash field must contain a SHA256 value")
    return value.lower()


def _relative_doc_path(path: str | Path, *, root: Path | None = None) -> tuple[str, str]:
    resolved = Path(path).resolve()
    docs_root = ((Path(root) if root is not None else FRAMEWORK_ROOT) / "docs").resolve()
    if resolved.is_relative_to(docs_root):
        rel = resolved.relative_to(docs_root).as_posix()
    else:
        rel = resolved.name
    folder = "" if "/" not in rel else rel.rsplit("/", 1)[0]
    return rel, folder


def framework_documents(root: Path | None = None) -> list[Path]:
    directory = ((Path(root) if root is not None else FRAMEWORK_ROOT) / "docs").resolve()
    if not directory.is_dir():
        raise ValueError(f"Expected documentation directory at {directory}")
    result = sorted(
        (path for path in directory.rglob("*.md") if path.is_file()),
        key=lambda item: item.relative_to(directory).as_posix(),
    )
    if not result:
        raise ValueError(f"Expected at least one Markdown document under {directory}")
    return result


def _resolve_input_paths(paths: list[str | Path] | None = None, *, root: Path | None = None) -> list[Path]:
    if not paths:
        return framework_documents(root)
    result: list[Path] = []
    for item in paths:
        candidate = Path(item)
        if candidate.is_dir():
            resolved_dir = candidate.resolve()
            found = sorted(
                (p for p in resolved_dir.rglob("*.md") if p.is_file()),
                key=lambda p: p.relative_to(resolved_dir).as_posix(),
            )
            if not found:
                raise ValueError(f"No Markdown documents found under {candidate}")
            result.extend(found)
        else:
            result.append(candidate)
    return result


def _read_document(path: str | Path) -> tuple[Path, bytes, str]:
    resolved = Path(path).resolve()
    if resolved.suffix.lower() != ".md":
        raise ValueError("Expected a Markdown document")
    content = resolved.read_bytes()
    return resolved, content, content.decode("utf-8")


def inspect_document(path: str | Path, *, root: Path | None = None) -> dict:
    resolved, _content, text = _read_document(path)
    current = calculate_document_hash(text)
    declared = extract_document_hash(text)
    relative_path, folder_path = _relative_doc_path(resolved, root=root)
    return {
        "path": str(resolved),
        "file_name": resolved.name,
        "relative_path": relative_path,
        "folder_path": folder_path,
        "local_hash": current,
        "declared_hash": declared,
        "needs_refresh": declared != current,
    }


def refresh_document_hash(path: str | Path, *, root: Path | None = None) -> dict:
    resolved, original, text = _read_document(path)
    prefix = "\ufeff" if text.startswith("\ufeff") else ""
    body = text.removeprefix("\ufeff")
    lines = body.splitlines(keepends=True)
    index = _hash_line_index(body)
    if index is None:
        newline = "\r\n" if "\r\n" in body else "\n"
        index = 1 if lines and lines[0].startswith("# ") and lines[0].endswith("\n") else 0
        lines.insert(index, HASH_LABEL + newline)
    prepared = "".join(lines)
    digest = calculate_document_hash(prepared)
    ending = re.search(r"(\r\n|\n|\r)$", lines[index])
    lines[index] = f"{HASH_LABEL}`{digest}`" + (ending.group(1) if ending else "")
    updated = (prefix + "".join(lines)).encode("utf-8")
    if updated != original:
        resolved.write_bytes(updated)
    return {**inspect_document(resolved, root=root), "changed": updated != original}


def compare_document_hash(path: str | Path, remote_hash: str | None) -> dict:
    local = refresh_document_hash(path)
    if local["needs_refresh"]:
        raise ValueError("Source changed during hash refresh; compare again with the current file")
    if remote_hash is not None:
        remote_hash = remote_hash.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", remote_hash):
            raise ValueError("Remote hash must be a SHA256 value, not document content")
    matches = local["declared_hash"] == remote_hash
    return {
        **local,
        "hash_refreshed": local["changed"],
        "remote_hash": remote_hash,
        "status": "in_sync" if matches else "needs_update",
        "reason": "same_hash" if matches else "remote_hash_missing" if remote_hash is None else "hash_differs",
    }


def prepare_document_update(
    path: str | Path,
    remote_hash: str | None,
    *,
    knowledge_base: str | None = None,
    document_id: str,
    allow_cross_role: bool = False,
) -> dict:
    if not str(document_id or "").strip():
        raise ValueError("An update comparison requires a remote document ID")
    selector = resolve_kb_role_selector(
        knowledge_base, expected_role="FRAMEWORK_DOCS", allow_cross_role=allow_cross_role,
    )
    comparison = compare_document_hash(path, remote_hash)
    needs_update = comparison["status"] == "needs_update"
    return {
        **comparison,
        "operation": "update",
        "knowledge_base": selector,
        "document_id": document_id,
        "comparison_status": comparison["status"],
        "status": "needs_confirmation" if needs_update else "in_sync",
        "requires_confirmation": needs_update,
        "remote_written": False,
        "confirmation_notice": "A confirmed update uploads a new document, associates an old-version status tag, and deletes only this old ID after verification. The document ID changes; old references are not migrated.",
        "message": "Confirm updating this remote document" if needs_update else "Remote document is already up to date",
    }


def compare_documents(items, *, knowledge_base: str | None = None, allow_cross_role: bool = False) -> dict:
    selector = resolve_kb_role_selector(
        knowledge_base, expected_role="FRAMEWORK_DOCS", allow_cross_role=allow_cross_role,
    )
    documents = []
    for path, document_id, remote in items:
        try:
            if not isinstance(document_id, str) or not document_id.strip():
                raise ValueError("A remote document ID or explicit - for no match is required")
            if remote in {"unreadable", "not-found", "conflict"}:
                current = refresh_document_hash(path)
                status, reason = {
                    "unreadable": ("unable_to_check", "remote_read_failed"),
                    "not-found": ("remote_missing", "exact_filename_not_found"),
                    "conflict": ("conflict", "multiple_same_name_documents"),
                }[remote]
                current.update(status=status, reason=reason)
            else:
                if document_id == "-":
                    raise ValueError("A verified remote field requires a matched document ID")
                current = compare_document_hash(path, None if remote == "absent" else remote)
            current["document_id"] = document_id
        except (OSError, ValueError) as error:
            current = {"path": str(path), "document_id": document_id, "status": "unable_to_check", "reason": "invalid_input", "message": str(error)}
        documents.append(current)
    return {"knowledge_base": selector, "documents": documents, "remote_written": False}


def _configured_client(
    knowledge_base: str | None = None,
    *,
    expected_role: str | None = "FRAMEWORK_DOCS",
    allow_cross_role: bool = False,
) -> tuple[WeKnoraClient, str]:
    from config.settings import settings

    selector = resolve_kb_role_selector(
        knowledge_base, expected_role=expected_role, allow_cross_role=allow_cross_role,
    )
    client = WeKnoraClient(settings.weknora_api_url, settings.weknora_api_key)
    records = client.records("/knowledge-bases")
    matches = [item for item in records if selector in {item.get("id"), item.get("name")}]
    if len(matches) != 1:
        raise PublishError("knowledge_base_not_unique", "Target must resolve to exactly one accessible knowledge base", stage="preflight")
    verify_resolved_kb_role(
        matches[0], expected_role=expected_role, allow_cross_role=allow_cross_role,
    )
    return client, matches[0]["id"]


def _processing_status(document: dict) -> dict:
    parse_status = document.get("parse_status") or "unknown"
    if not isinstance(parse_status, str):
        status = "unknown"
    elif parse_status in {"pending", "processing", "finalizing"}:
        status = "processing"
    elif parse_status == "completed":
        status = "completed"
    elif parse_status == "failed":
        status = "processing_failed"
    elif parse_status == "deleting":
        status = "deleting"
    else:
        status = "unknown"
    summary_status = document.get("summary_status") or "unknown"
    result = {"status": status, "parse_status": parse_status, "summary_status": summary_status}
    if document.get("error_message"):
        result["error_message"] = str(document["error_message"])[:500]
    if summary_status == "failed":
        result["warning"] = "Summary failed; completed does not mean every enrichment task succeeded"
    return result


def _redact_result(value, client: WeKnoraClient):
    secret = client._headers.get("X-API-Key")
    if isinstance(value, dict):
        return {key: _redact_result(item, client) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_result(item, client) for item in value]
    return value.replace(secret, "[redacted]") if isinstance(value, str) and secret else value


def _safe_error(error: Exception, client: WeKnoraClient) -> dict:
    result = {
        "code": getattr(error, "code", "request_failed"),
        "stage": getattr(error, "stage", None) or "request",
        "message": str(error)[:500],
    }
    details = getattr(error, "details", None)
    if details:
        result["details"] = dict(details)
    return _redact_result(result, client)


def _existing_document_hash(document: dict) -> str | None:
    for field in ("custom_metadata", "metadata"):
        metadata = document.get(field) or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except ValueError:
                continue
        if isinstance(metadata, dict) and metadata.get("document_sync_hash"):
            value = str(metadata["document_sync_hash"]).lower()
            if re.fullmatch(r"[0-9a-f]{64}", value):
                return value
    return None


def _batch_client(
    knowledge_base: str | None,
    context: dict | None,
    *,
    expected_role: str | None = "FRAMEWORK_DOCS",
    allow_cross_role: bool = False,
):
    selector = resolve_kb_role_selector(
        knowledge_base, expected_role=expected_role, allow_cross_role=allow_cross_role,
    )
    call_kwargs = (
        {"expected_role": expected_role, "allow_cross_role": allow_cross_role}
        if (expected_role != "FRAMEWORK_DOCS" or allow_cross_role)
        else {}
    )
    if context is None:
        return _configured_client(selector, **call_kwargs)
    if selector not in context:
        context[selector] = _configured_client(selector, **call_kwargs)
    return context[selector]


def document_status(
    document_id: str,
    *,
    knowledge_base: str | None = None,
    _batch_context: dict | None = None,
) -> dict:
    if not str(document_id or "").strip():
        raise ValueError("A document ID is required")
    client, kb_id = _batch_client(
        knowledge_base, _batch_context, expected_role="FRAMEWORK_DOCS", allow_cross_role=True,
    )
    document = client.document(document_id, kb_id)
    result = {
        "knowledge_base_id": kb_id,
        "document_id": document_id,
        "file_name": document.get("file_name"),
        "read_only": True,
        **_processing_status(document),
    }
    return _redact_result(result, client)


def _upload_markdown(
    client: WeKnoraClient, kb_id: str, name: str, content: bytes, digest: str, *,
    remote_file_name: str | None = None, tag_ids: list[str] | None = None, metadata: dict | None = None,
) -> dict:
    from autowork_core.knowledge.weknora_client import quote_id

    destination = remote_file_name or name
    if any(character in name + destination for character in '\r\n\x00"'):
        raise ValueError("Invalid filename for file upload")
    boundary = "AutoworkDocument" + uuid4().hex
    fields = {"fileName": destination, "metadata": json.dumps({**(metadata or {}), "document_sync_hash": digest})}
    if tag_ids:
        fields["tag_ids"] = ",".join(tag_ids)
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode("utf-8")
        for key, value in fields.items()
    ]
    parts.extend([
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\nContent-Type: text/markdown\r\n\r\n'.encode("utf-8"),
        content,
        f"\r\n--{boundary}--\r\n".encode("ascii"),
    ])
    response = client.request(
        f"/knowledge-bases/{quote_id(kb_id)}/knowledge/file", method="POST",
        data=b"".join(parts), content_type=f"multipart/form-data; boundary={boundary}", operation="upload",
    )
    document = response.get("data")
    if not isinstance(document, dict) or not isinstance(document.get("id"), str) or not document["id"].strip():
        raise PublishError("upload_outcome_unknown", "Upload returned no document ID; check remote state before retrying", stage="upload")
    if document.get("knowledge_base_id") not in {None, kb_id}:
        raise PublishError("identity_conflict", "Upload response belongs to another knowledge base", stage="upload")
    return document


def upload_document(
    path: str | Path, *, knowledge_base: str, allow_duplicate: bool = False,
    root: Path | None = None, _batch_context: dict | None = None,
) -> dict:
    prepared = refresh_document_hash(path, root=root)
    resolved, content, text = _read_document(path)
    digest = prepared["declared_hash"]
    relative_path = prepared["relative_path"]
    target_folder = prepared["folder_path"]
    base = {**prepared, "hash_refreshed": prepared["changed"]}
    if prepared["needs_refresh"] or extract_document_hash(text) != digest or calculate_document_hash(text) != digest:
        return {**base, "status": "source_changed", "message": "Source changed after hash refresh; no upload was sent", "upload_outcome": "not_sent"}
    client, kb_id = _batch_client(knowledge_base, _batch_context)
    base.update(knowledge_base_id=kb_id)
    matches = [
        item for item in client.documents(kb_id)
        if item.get("file_name") == resolved.name and (item.get("folder_path") or "").strip("/") == target_folder
    ]
    if len(matches) > 1:
        return {**base, "status": "needs_confirmation", "reason": "multiple_same_name_documents", "document_ids": [item["id"] for item in matches], "upload_outcome": "not_sent"}
    if matches:
        existing = client.document(matches[0]["id"], kb_id)
        if resolved.read_bytes() != content:
            return {**base, "status": "source_changed", "upload_outcome": "not_sent"}
        if _existing_document_hash(existing) == digest or (existing.get("file_hash") or "").strip().lower() == hashlib.md5(content).hexdigest():
            return _redact_result({**base, "upload_outcome": "already_present", "document_id": existing["id"], **_processing_status(existing)}, client)
        if not allow_duplicate:
            return {**base, "status": "needs_confirmation", "reason": "same_name_requires_add_or_update_choice", "document_id": existing["id"], "upload_outcome": "not_sent"}
    if resolved.read_bytes() != content:
        return {**base, "status": "source_changed", "upload_outcome": "not_sent"}
    reused = False
    try:
        document = _upload_markdown(
            client, kb_id, resolved.name, content, digest,
            remote_file_name=relative_path if target_folder else None,
            metadata={"document_sync_path": relative_path} if target_folder else None,
        )
    except RemoteHTTPError as error:
        duplicate = error.payload.get("data") if isinstance(error.payload, dict) else None
        if (
            error.status == 409 and isinstance(error.payload, dict)
            and error.payload.get("code") == "duplicate_file"
            and isinstance(duplicate, dict) and isinstance(duplicate.get("id"), str) and duplicate["id"].strip()
        ):
            try:
                document = client.document(duplicate["id"], kb_id)
            except (PublishError, OSError, HTTPException) as lookup_error:
                return {**base, "status": "upload_outcome_unknown", "upload_outcome": "unknown", "candidate_document_id": duplicate["id"], "error": _safe_error(lookup_error, client)}
            if document.get("file_name") != resolved.name or (
                _existing_document_hash(document) != digest
                and (document.get("file_hash") or "").strip().lower() != hashlib.md5(content).hexdigest()
            ):
                return {**base, "status": "needs_confirmation", "reason": "unverified_duplicate_response", "document_id": document["id"], "upload_outcome": "not_confirmed"}
            reused = True
        else:
            unknown = error.status == 408 or 500 <= error.status < 600
            return {**base, "status": "upload_outcome_unknown" if unknown else "upload_failed", "upload_outcome": "unknown" if unknown else "rejected", "error": _safe_error(error, client)}
    except (PublishError, OSError, HTTPException) as error:
        return {**base, "status": "upload_outcome_unknown", "upload_outcome": "unknown", "error": _safe_error(error, client)}
    if not reused and document["id"] in {item["id"] for item in matches}:
        return {**base, "status": "upload_outcome_unknown", "upload_outcome": "unknown", "candidate_document_id": document["id"], "reason": "server_returned_preexisting_id"}
    result = {**base, "document_id": document["id"], "upload_outcome": "already_present" if reused else "accepted", "new_document_created": not reused}
    try:
        if not reused:
            document = client.document(document["id"], kb_id)
        if document.get("file_name") != resolved.name or (
            target_folder and "folder_path" in document and (document.get("folder_path") or "").strip("/") != target_folder
        ):
            raise PublishError("identity_conflict", "Remote filename or folder differs from the selected file", stage="upload verification")
        result.update(_processing_status(document))
    except (PublishError, OSError, HTTPException) as error:
        result.update(status="status_unavailable", error=_safe_error(error, client))
    try:
        result["local_changed_after_submission"] = resolved.read_bytes() != content
    except OSError:
        result["local_changed_after_submission"] = True
        result["local_warning"] = "Source file is no longer readable; upload outcome is unchanged"
    if matches and not reused:
        result["preserved_document_ids"] = [item["id"] for item in matches]
    return _redact_result(result, client)


def move_documents_to_folder(
    client: WeKnoraClient, kb_id: str, document_ids: list[str], folder_path: str,
) -> dict:
    normalized = str(folder_path or "").strip("/")
    response = client.request(
        "/knowledge/folder",
        method="POST",
        payload={"kb_id": kb_id, "knowledge_ids": list(document_ids), "folder_path": normalized},
        operation="move_folder",
    )
    for document_id in document_ids:
        verified = client.document(document_id, kb_id)
        if (verified.get("folder_path") or "").strip("/") != normalized:
            raise PublishError("folder_move_unverified", f"Document {document_id} folder_path was not updated to {normalized}", stage="move_folder")
    data = response.get("data")
    if isinstance(data, dict):
        return data
    return {"moved_count": len(document_ids), "folder_path": normalized}


def _remote_document_hash(client: WeKnoraClient, document: dict, *, local_md5: str, local_hash: str) -> tuple[bool, str | None]:
    meta_hash = _existing_document_hash(document)
    if meta_hash is not None:
        return meta_hash == local_hash, meta_hash
    remote_md5 = (document.get("file_hash") or "").strip().lower()
    if remote_md5 and remote_md5 == local_md5:
        return True, local_hash
    from autowork_core.knowledge.weknora_client import quote_id
    content = client.request(f"/knowledge/{quote_id(document['id'])}/download", raw=True)
    if isinstance(content, bytes):
        extracted = extract_document_hash(content.decode("utf-8-sig"))
        return extracted == local_hash, extracted
    return False, None


def plan_directory_sync(
    paths: list[str | Path] | None = None,
    *,
    knowledge_base: str | None = None,
    allow_cross_role: bool = False,
    root: Path | None = None,
    task_root: Path | None = None,
    _client_context: tuple[WeKnoraClient, str] | None = None,
) -> dict:
    from autowork_core.knowledge.document_update import TASK_ROOT, _load, task_status

    selector = resolve_kb_role_selector(
        knowledge_base, expected_role="FRAMEWORK_DOCS", allow_cross_role=allow_cross_role,
    )
    selected = _resolve_input_paths(paths, root=root)
    local_items: list[dict] = []
    seen_rel: set[str] = set()
    name_counts: dict[str, int] = {}
    for path in selected:
        refreshed = refresh_document_hash(path, root=root)
        resolved, raw_bytes, text = _read_document(path)
        if refreshed["needs_refresh"] or extract_document_hash(text) != refreshed["declared_hash"] or calculate_document_hash(text) != refreshed["declared_hash"]:
            raise ValueError(f"Source changed during hash refresh: {resolved}")
        rel_path = refreshed["relative_path"]
        if rel_path in seen_rel:
            raise ValueError(f"Duplicate relative document path: {rel_path}")
        seen_rel.add(rel_path)
        name_counts[refreshed["file_name"]] = name_counts.get(refreshed["file_name"], 0) + 1
        local_items.append({
            **refreshed,
            "hash_refreshed": refreshed["changed"],
            "local_md5": hashlib.md5(raw_bytes).hexdigest(),
        })

    client, kb_id = _client_context if _client_context is not None else _configured_client(selector)
    knowledge_base = selector
    remote_docs = client.documents(kb_id)

    active_by_doc: dict[str, dict] = {}
    tasks_dir = Path(task_root or TASK_ROOT).resolve()
    if tasks_dir.is_dir():
        for task_file in sorted(tasks_dir.glob("*/task.json")):
            try:
                record = _load(task_file.parent.name, tasks_dir)
                if record.get("knowledge_base_id") == kb_id and record.get("status") not in {"completed", "cancelled"}:
                    summary = task_status(record["task_id"], task_root=tasks_dir)
                    summary.pop("history", None)
                    for key in ("old_document_id", "new_document_id"):
                        if record.get(key):
                            active_by_doc[record[key]] = summary
            except (OSError, ValueError, KeyError):
                continue

    remote_entries = []
    for doc in remote_docs:
        folder = (doc.get("folder_path") or "").strip("/")
        fname = doc.get("file_name") or ""
        rel = f"{folder}/{fname}" if folder else fname
        remote_entries.append({
            "raw": doc,
            "id": doc["id"],
            "file_name": fname,
            "folder_path": folder,
            "remote_rel_path": rel,
        })

    claimed_ids: set[str] = set()
    in_sync: list[dict] = []
    to_move: list[dict] = []
    to_upload: list[dict] = []
    to_update: list[dict] = []
    to_delete: list[dict] = []
    active_tasks: list[dict] = []
    conflicts: list[dict] = []

    for local in local_items:
        rel_path = local["relative_path"]
        target_folder = local["folder_path"]
        fname = local["file_name"]
        exact_matches = [r for r in remote_entries if r["remote_rel_path"] == rel_path and r["id"] not in claimed_ids]
        if exact_matches:
            active_hits = [r for r in exact_matches if r["id"] in active_by_doc]
            if active_hits:
                for r in exact_matches:
                    claimed_ids.add(r["id"])
                active_tasks.append({
                    "relative_path": rel_path,
                    "path": local["path"],
                    "document_ids": [r["id"] for r in exact_matches],
                    "task": active_by_doc[active_hits[0]["id"]],
                })
                continue
            if len(exact_matches) > 1:
                matching = []
                for r in exact_matches:
                    matched, r_hash = _remote_document_hash(client, r["raw"], local_md5=local["local_md5"], local_hash=local["local_hash"])
                    if matched:
                        matching.append((r, r_hash))
                if len(matching) == 1:
                    chosen, chosen_hash = matching[0]
                    claimed_ids.add(chosen["id"])
                    in_sync.append({
                        "path": local["path"],
                        "relative_path": rel_path,
                        "folder_path": target_folder,
                        "file_name": fname,
                        "document_id": chosen["id"],
                        "local_hash": local["local_hash"],
                        "remote_hash": chosen_hash,
                        "hash_refreshed": local["hash_refreshed"],
                        "status": "in_sync",
                    })
                    continue
                for r in exact_matches:
                    claimed_ids.add(r["id"])
                conflicts.append({
                    "path": local["path"],
                    "relative_path": rel_path,
                    "reason": "multiple_same_path_documents",
                    "document_ids": [r["id"] for r in exact_matches],
                })
                continue

            chosen = exact_matches[0]
            claimed_ids.add(chosen["id"])
            matched, r_hash = _remote_document_hash(client, chosen["raw"], local_md5=local["local_md5"], local_hash=local["local_hash"])
            if matched:
                in_sync.append({
                    "path": local["path"],
                    "relative_path": rel_path,
                    "folder_path": target_folder,
                    "file_name": fname,
                    "document_id": chosen["id"],
                    "local_hash": local["local_hash"],
                    "remote_hash": r_hash,
                    "hash_refreshed": local["hash_refreshed"],
                    "status": "in_sync",
                })
            else:
                to_update.append({
                    "path": local["path"],
                    "relative_path": rel_path,
                    "from_folder_path": chosen["folder_path"],
                    "target_folder_path": target_folder,
                    "file_name": fname,
                    "document_id": chosen["id"],
                    "local_hash": local["local_hash"],
                    "remote_hash": r_hash,
                    "hash_refreshed": local["hash_refreshed"],
                    "status": "needs_update",
                })
            continue

        if name_counts.get(fname, 0) == 1:
            relocated = [r for r in remote_entries if r["file_name"] == fname and r["id"] not in claimed_ids]
            if len(relocated) == 1:
                chosen = relocated[0]
                if chosen["id"] in active_by_doc:
                    claimed_ids.add(chosen["id"])
                    active_tasks.append({
                        "relative_path": rel_path,
                        "path": local["path"],
                        "document_ids": [chosen["id"]],
                        "task": active_by_doc[chosen["id"]],
                    })
                    continue
                claimed_ids.add(chosen["id"])
                matched, r_hash = _remote_document_hash(client, chosen["raw"], local_md5=local["local_md5"], local_hash=local["local_hash"])
                if matched:
                    to_move.append({
                        "path": local["path"],
                        "relative_path": rel_path,
                        "from_folder_path": chosen["folder_path"],
                        "to_folder_path": target_folder,
                        "file_name": fname,
                        "document_id": chosen["id"],
                        "local_hash": local["local_hash"],
                        "remote_hash": r_hash,
                        "hash_refreshed": local["hash_refreshed"],
                        "status": "needs_folder_move",
                    })
                else:
                    to_update.append({
                        "path": local["path"],
                        "relative_path": rel_path,
                        "from_folder_path": chosen["folder_path"],
                        "target_folder_path": target_folder,
                        "file_name": fname,
                        "document_id": chosen["id"],
                        "local_hash": local["local_hash"],
                        "remote_hash": r_hash,
                        "hash_refreshed": local["hash_refreshed"],
                        "status": "needs_update",
                    })
                continue
            elif len(relocated) > 1:
                for r in relocated:
                    claimed_ids.add(r["id"])
                conflicts.append({
                    "path": local["path"],
                    "relative_path": rel_path,
                    "reason": "multiple_same_name_documents",
                    "document_ids": [r["id"] for r in relocated],
                })
                continue

        to_upload.append({
            "path": local["path"],
            "relative_path": rel_path,
            "folder_path": target_folder,
            "file_name": fname,
            "local_hash": local["local_hash"],
            "hash_refreshed": local["hash_refreshed"],
            "status": "missing_remote",
        })

    if not paths:
        for r in remote_entries:
            if r["id"] in claimed_ids:
                continue
            if r["id"] in active_by_doc:
                active_tasks.append({
                    "relative_path": r["remote_rel_path"],
                    "document_ids": [r["id"]],
                    "task": active_by_doc[r["id"]],
                })
                continue
            to_delete.append({
                "document_id": r["id"],
                "file_name": r["file_name"],
                "folder_path": r["folder_path"],
                "remote_rel_path": r["remote_rel_path"],
                "reason": "extra_remote_document",
                "status": "extra_remote",
            })

    has_changes = bool(to_move or to_upload or to_update or to_delete)
    result = {
        "operation": "sync_plan",
        "knowledge_base": knowledge_base,
        "knowledge_base_id": kb_id,
        "status": "conflict" if conflicts else "needs_sync" if has_changes else "in_sync",
        "requires_confirmation": has_changes,
        "remote_written": False,
        "summary": {
            "local_total": len(local_items),
            "remote_total": len(remote_entries),
            "in_sync": len(in_sync),
            "to_move": len(to_move),
            "to_upload": len(to_upload),
            "to_update": len(to_update),
            "to_delete": len(to_delete),
            "active_tasks": len(active_tasks),
            "conflicts": len(conflicts),
        },
        "in_sync": in_sync,
        "to_move": to_move,
        "to_upload": to_upload,
        "to_update": to_update,
        "to_delete": to_delete,
        "active_tasks": active_tasks,
        "conflicts": conflicts,
    }
    return _redact_result(result, client)


def apply_directory_sync(
    paths: list[str | Path] | None = None,
    *,
    knowledge_base: str | None = None,
    allow_cross_role: bool = False,
    delete_extra: bool = True,
    root: Path | None = None,
    task_root: Path | None = None,
    background: bool = True,
) -> dict:
    from autowork_core.knowledge.document_update import submit_update
    from autowork_core.knowledge.weknora_client import quote_id

    selector = resolve_kb_role_selector(
        knowledge_base, expected_role="FRAMEWORK_DOCS", allow_cross_role=allow_cross_role,
    )
    client, kb_id = _configured_client(selector)
    knowledge_base = selector
    plan = plan_directory_sync(
        paths,
        knowledge_base=selector,
        allow_cross_role=allow_cross_role,
        root=root,
        task_root=task_root,
        _client_context=(client, kb_id),
    )
    if plan["conflicts"]:
        return _redact_result({
            **plan,
            "operation": "sync_apply",
            "status": "blocked_by_conflicts",
            "remote_written": False,
        }, client)

    moved: list[dict] = []
    uploaded: list[dict] = []
    updated: list[dict] = []
    deleted: list[dict] = []
    errors: list[dict] = []

    moves_by_folder: dict[str, list[dict]] = {}
    for item in plan["to_move"]:
        moves_by_folder.setdefault(item["to_folder_path"], []).append(item)
    for folder, items in moves_by_folder.items():
        ids = [item["document_id"] for item in items]
        try:
            move_documents_to_folder(client, kb_id, ids, folder)
            for item in items:
                moved.append({**item, "status": "moved"})
        except (PublishError, OSError, ValueError, HTTPException) as error:
            errors.append({"stage": "move_folder", "folder_path": folder, "document_ids": ids, **_safe_error(error, client)})

    batch_context = {knowledge_base: (client, kb_id), kb_id: (client, kb_id)}
    for item in plan["to_upload"]:
        try:
            res = upload_document(item["path"], knowledge_base=kb_id, root=root, _batch_context=batch_context)
            uploaded.append(res)
            if res.get("status") not in {"processing", "completed"}:
                errors.append({"stage": "upload", "relative_path": item["relative_path"], "status": res.get("status")})
        except (PublishError, OSError, ValueError, HTTPException) as error:
            errors.append({"stage": "upload", "relative_path": item["relative_path"], **_safe_error(error, client)})

    for item in plan["to_update"]:
        try:
            res = submit_update(
                item["path"],
                knowledge_base=kb_id,
                document_id=item["document_id"],
                expected_local_hash=item["local_hash"],
                expected_remote_hash=item["remote_hash"],
                target_folder_path=item["target_folder_path"],
                task_root=task_root,
                background=background,
            )
            updated.append(res)
            if res.get("status") not in {"running", "completed", "in_sync"}:
                errors.append({"stage": "update", "relative_path": item["relative_path"], "status": res.get("status")})
        except (PublishError, OSError, ValueError, HTTPException) as error:
            errors.append({"stage": "update", "relative_path": item["relative_path"], **_safe_error(error, client)})

    if delete_extra and not errors:
        for item in plan["to_delete"]:
            doc_id = item["document_id"]
            try:
                response = client.request(f"/knowledge/{quote_id(doc_id)}", method="DELETE", operation="delete_extra")
                data = response.get("data") or {}
                deleted.append({
                    **item,
                    "status": "delete_submitted",
                    "delete_task_id": data.get("task_id") if isinstance(data, dict) else None,
                })
            except (PublishError, OSError, ValueError, HTTPException) as error:
                errors.append({"stage": "delete_extra", "document_id": doc_id, **_safe_error(error, client)})

    status = "failed" if errors else "running" if any(u.get("status") == "running" for u in updated) else "completed"
    result = {
        "operation": "sync_apply",
        "knowledge_base": knowledge_base,
        "knowledge_base_id": kb_id,
        "status": status,
        "remote_written": bool(moved or uploaded or updated or deleted),
        "in_sync_count": len(plan["in_sync"]),
        "moved": moved,
        "uploaded": uploaded,
        "updated": updated,
        "deleted": deleted,
        "skipped_delete_count": 0 if delete_extra and not errors else len(plan["to_delete"]),
        "active_tasks": plan["active_tasks"],
        "errors": errors,
    }
    return _redact_result(result, client)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect", "refresh"):
        command = commands.add_parser(name)
        command.add_argument("paths", nargs="*", help="Markdown paths or directories; defaults to all Markdown files under docs/")
    sync_plan = commands.add_parser("sync-plan", help="Refresh all docs/ hashes and reconcile remote folder hierarchy, missing, updated, and extra files")
    sync_plan.add_argument("paths", nargs="*", help="Optional Markdown paths or directories; defaults to all docs/")
    sync_plan.add_argument("--kb", default=None, help="REST knowledge-base name or ID; defaults to WEKNORA_SETTING.KNOWLEDGE_BASES.FRAMEWORK_DOCS")
    sync_plan.add_argument("--task-root", help=argparse.SUPPRESS)
    sync_apply = commands.add_parser("sync-apply", help="Apply a confirmed directory sync: move folders, upload missing, update changed, and delete extra")
    sync_apply.add_argument("paths", nargs="*", help="Optional Markdown paths or directories; defaults to all docs/")
    sync_apply.add_argument("--kb", default=None, help="REST knowledge-base name or ID; defaults to WEKNORA_SETTING.KNOWLEDGE_BASES.FRAMEWORK_DOCS")
    sync_apply.add_argument("--confirm", action="store_true", help="Confirm remote folder moves, uploads, updates, and extra deletions")
    sync_apply.add_argument("--no-delete", action="store_true", help="Only move, upload missing, and update changed documents without deleting extra remote documents")
    sync_apply.add_argument("--task-root", help=argparse.SUPPRESS)
    compare = commands.add_parser("compare", aliases=["check"], help="Refresh the local hash, then compare a verified remote field")
    compare.add_argument("path")
    compare.add_argument("--remote-hash", help="The field read from the matching remote document; omit if absent")
    batch = commands.add_parser("compare-many", help="Compare small observed fields in one local batch; no MCP JSON transport")
    batch.add_argument("--kb", default=None, help="REST knowledge-base name or ID; defaults to WEKNORA_SETTING.KNOWLEDGE_BASES.FRAMEWORK_DOCS")
    batch.add_argument("--item", action="append", nargs=3, required=True, metavar=("PATH", "DOCUMENT_ID", "HASH_OR_STATE"), help="Repeat per file; state is absent, unreadable, not-found, or conflict")
    batch.add_argument("--compact", action="store_true", help="Print necessary per-file fields once")
    update = commands.add_parser("update", help="Refresh and compare; request confirmation only when different, without remote writes")
    update.add_argument("path")
    update.add_argument("--remote-hash", help="Verified remote header hash; omit only when its absence is verified")
    update.add_argument("--kb", default=None, help="REST knowledge-base name or ID; defaults to WEKNORA_SETTING.KNOWLEDGE_BASES.FRAMEWORK_DOCS")
    update.add_argument("--document-id", required=True)
    update.add_argument("--confirm", action="store_true", help="Confirm new upload, old status-tag marking and later deletion of this exact old ID")
    update.add_argument("--expected-local-hash", help="Local hash explicitly shown in the confirmed update comparison")
    update.add_argument("--task-root", help=argparse.SUPPRESS)
    for name in ("task-status", "task-resume", "task-cancel"):
        command = commands.add_parser(name)
        command.add_argument("task_id")
        command.add_argument("--task-root", help=argparse.SUPPRESS)
        if name == "task-status":
            command.add_argument("--summary", action="store_true", help="Omit history only; retain current errors, warnings and worker state")
    upload = commands.add_parser("upload", help="Upload only; never replace or delete existing documents")
    upload.add_argument("paths", nargs="+", help="Explicit local Markdown files to upload")
    upload.add_argument("--kb", default=None, help="REST knowledge-base name or ID; defaults to WEKNORA_SETTING.KNOWLEDGE_BASES.FRAMEWORK_DOCS")
    upload.add_argument("--allow-duplicate", action="store_true", help="Only after explicitly choosing to add a separate same-name document")
    status = commands.add_parser("status", help="Query one document without uploading, reparsing or deleting")
    status.add_argument("document_ids", nargs="+")
    status.add_argument("--kb", default=None, help="REST knowledge-base name or ID; defaults to WEKNORA_SETTING.KNOWLEDGE_BASES.FRAMEWORK_DOCS")
    args = parser.parse_args(argv)
    try:
        if args.command == "sync-plan":
            result = plan_directory_sync(args.paths or None, knowledge_base=args.kb, task_root=args.task_root)
        elif args.command == "sync-apply":
            if not args.confirm:
                raise ValueError("Directory sync execution requires --confirm after reviewing sync-plan")
            result = apply_directory_sync(
                args.paths or None,
                knowledge_base=args.kb,
                delete_extra=not args.no_delete,
                task_root=args.task_root,
            )
        elif args.command == "compare-many":
            result = compare_documents(args.item, knowledge_base=args.kb)
            if args.compact:
                keys = {"path", "document_id", "local_hash", "remote_hash", "status", "reason", "message"}
                result["documents"] = [{key: value for key, value in item.items() if key in keys} for item in result["documents"]]
        elif args.command in {"compare", "check"}:
            result = compare_document_hash(args.path, args.remote_hash)
        elif args.command == "update":
            if args.confirm:
                from autowork_core.knowledge.document_update import submit_update
                if not args.expected_local_hash:
                    raise ValueError("Confirmed update requires --expected-local-hash from the comparison shown to the user")
                result = submit_update(
                    args.path, knowledge_base=args.kb, document_id=args.document_id,
                    expected_local_hash=args.expected_local_hash, expected_remote_hash=args.remote_hash,
                    task_root=args.task_root,
                )
            else:
                result = prepare_document_update(args.path, args.remote_hash, knowledge_base=args.kb, document_id=args.document_id)
        elif args.command in {"task-status", "task-resume", "task-cancel"}:
            from autowork_core.knowledge.document_update import cancel_task, launch_worker, task_status
            if args.command == "task-status":
                result = task_status(args.task_id, task_root=args.task_root)
                if args.summary:
                    history = result.get("history", [])
                    result = {key: value for key, value in result.items() if key != "history"}
                    result.update(history_count=len(history), history_omitted=True)
            elif args.command == "task-resume":
                result = launch_worker(args.task_id, task_root=args.task_root, resume=True)
            else:
                result = cancel_task(args.task_id, task_root=args.task_root)
        elif args.command in {"upload", "status"}:
            items = _resolve_input_paths(args.paths) if args.command == "upload" else args.document_ids
            documents = []
            batch_context = {}
            for item in items:
                try:
                    current = upload_document(item, knowledge_base=args.kb, allow_duplicate=args.allow_duplicate, _batch_context=batch_context) if args.command == "upload" else document_status(item, knowledge_base=args.kb, _batch_context=batch_context)
                except (PublishError, OSError, ValueError, HTTPException) as error:
                    current = {"status": "unable_to_check", "input": str(item), "code": getattr(error, "code", "invalid_input"), "message": str(error)}
                    if getattr(error, "stage", None):
                        current["stage"] = error.stage
                    if isinstance(error, RemoteHTTPError):
                        current["http_status"] = error.status
                    if args.command == "upload":
                        current["upload_outcome"] = "not_sent"
                documents.append(current)
            from config.settings import settings
            resolved_kb = args.kb or getattr(settings, "weknora_framework_docs_kb", "") or None
            result = {"knowledge_base": resolved_kb, "documents": documents}
        else:
            paths = _resolve_input_paths(args.paths)
            operation = refresh_document_hash if args.command == "refresh" else inspect_document
            result = {"documents": [operation(path) for path in paths]}
    except (OSError, ValueError, PublishError, HTTPException) as error:
        print(json.dumps({"status": "unable_to_check", "code": getattr(error, "code", "invalid_input"), "message": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.command == "sync-plan":
        return 1 if result.get("status") == "conflict" else 2 if result.get("status") == "needs_sync" else 0
    if args.command == "sync-apply":
        return 0 if result.get("status") in {"running", "completed"} else 1
    if args.command == "compare-many":
        statuses = {item["status"] for item in result["documents"]}
        return 1 if "unable_to_check" in statuses else 0 if statuses <= {"in_sync"} else 2
    if "task_id" in result:
        return 0 if result["status"] in {"running", "completed", "cancelled"} else 1
    if args.command in {"upload", "status"}:
        statuses = {item["status"] for item in result["documents"]}
        if statuses & {"unable_to_check", "upload_failed", "upload_outcome_unknown", "status_unavailable", "source_changed", "processing_failed"}:
            return 1
        return 0 if statuses <= {"processing", "completed"} else 2
    return 2 if result.get("status") in {"needs_update", "needs_confirmation"} else 0


if __name__ == "__main__":
    raise SystemExit(main())