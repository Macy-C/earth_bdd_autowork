"""Recoverable document replacement tasks; only confirmed old IDs may be deleted."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from uuid import uuid4

from autowork_core.knowledge import document_sync as sync
from autowork_core.knowledge.weknora_client import PublishError, RemoteHTTPError, quote_id


TASK_ROOT = sync.FRAMEWORK_ROOT / "artifacts" / "document-updates"
WAITING_PREFIX = "[\u65e7\u7248\u4fdd\u7559\u00b7\u65b0\u7248\u5904\u7406\u4e2d] "
DELETING_PREFIX = "[\u5f85\u5220\u9664] "
WAITING_TAG = "\u65e7\u7248\u4fdd\u7559\u00b7\u65b0\u7248\u5904\u7406\u4e2d"
DELETING_TAG = "\u5f85\u5220\u9664"
REQUEST_ERRORS = (PublishError, OSError, ValueError, HTTPException)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _root(task_root=None):
    return Path(task_root or TASK_ROOT).resolve()


def _task_path(task_id, task_root=None):
    if not isinstance(task_id, str) or not re.fullmatch(r"[0-9a-f]{32}", task_id):
        raise ValueError("Invalid document update task ID")
    return _root(task_root) / task_id / "task.json"


@contextmanager
def _lock(path, *, create=True):
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b" if create else "r+b") as stream:
        if create and not stream.tell():
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise PublishError("task_busy", "Another process owns this document update") from error
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _lock_path(task, task_root=None):
    identity = json.dumps([task["api_url"], task["knowledge_base_id"], task["old_document_id"]])
    return _root(task_root) / "locks" / (hashlib.sha256(identity.encode()).hexdigest() + ".lock")


def _save(task, task_root=None, client=None):
    if client is not None:
        task = sync._redact_result(task, client)
    path = _task_path(task["task_id"], task_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _load(task_id, task_root=None):
    task = json.loads(_task_path(task_id, task_root).read_text(encoding="utf-8"))
    if task.get("task_id") != task_id or task.get("schema_version") != 1:
        raise ValueError("Invalid document update task record")
    return task


def _event(task, stage, message, **details):
    task["stage"] = stage
    task["message"] = message
    task["updated_at"] = _now()
    task.setdefault("history", []).append({"at": task["updated_at"], "stage": stage, "message": message, **details})


def _worker_active(task_id, task_root=None):
    path = _task_path(task_id, task_root).with_name("worker.lock")
    if not path.is_file():
        return False
    try:
        with _lock(path, create=False):
            return False
    except PublishError as error:
        if error.code == "task_busy":
            return True
        raise
    except OSError:
        return None


def task_status(task_id, *, task_root=None):
    task = _load(task_id, task_root)
    keys = (
        "task_id", "status", "stage", "message", "api_url", "knowledge_base_id", "path",
        "old_document_id", "new_document_id", "target_folder_path", "local_hash", "remote_hash", "delete_task_id",
        "created_at", "updated_at", "error", "warnings", "history", "worker_pid",
        "worker_started_at", "old_title_restored", "old_delete_attempted",
        "parse_status", "summary_status", "last_checked_at",
        "marker_strategy", "marker_tag_ids", "old_status_marker", "marker_verified_at", "old_marker_restored",
    )
    result = {key: task[key] for key in keys if key in task}
    result["old_preserved"] = not task.get("old_delete_attempted", False)
    result["old_delete_status"] = "confirmed_deleted" if task["status"] == "completed" else "submitted_or_unknown" if task.get("old_delete_attempted") else "not_requested"
    result["worker_active"] = _worker_active(task_id, task_root)
    if result["worker_active"] is None:
        result["worker_state"] = "unknown"
    if task["status"] == "running" and task.get("worker_started_at") and result["worker_active"] is False:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(task["worker_started_at"])).total_seconds()
        if age > 10:
            result["status"] = "interrupted"
            result["message"] = "Worker is not running; task is preserved. Inspect and explicitly resume it"
        else:
            result["worker_state"] = "starting_or_stopped"
    result["read_only"] = True
    return result


def _action_result(task_id, task_root=None):
    result = task_status(task_id, task_root=task_root)
    result["read_only"] = False
    return result


def _client_for(task):
    from config.settings import settings
    client = sync.WeKnoraClient(settings.weknora_api_url, settings.weknora_api_key, timeout=30)
    if client.api_url.rstrip("/") != task["api_url"]:
        raise PublishError("server_changed", "Configured server differs from the confirmed update")
    return client


def _metadata(document, field):
    value = document.get(field) or {}
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError(f"Remote {field} is not an object")
    return value


def _tags(document):
    return sorted(set(document.get("tag_ids") or ()) | {
        item["id"] for item in document.get("tags") or () if isinstance(item, dict) and item.get("id")
    })


def _document(client, document_id, kb_id):
    document = client.document(document_id, kb_id)
    if document.get("id") != document_id or document.get("knowledge_base_id") != kb_id or document.get("type") != "file":
        raise PublishError("identity_conflict", "Document ID, knowledge base or file type changed")
    return document


def _download(client, document_id):
    content = client.request(f"/knowledge/{quote_id(document_id)}/download", raw=True)
    if not isinstance(content, bytes):
        raise ValueError("Expected original Markdown bytes")
    return content


def _snapshot(document, content):
    return {
        "file_name": document.get("file_name"), "folder_path": document.get("folder_path") or "",
        "title": document.get("title") or "", "file_hash": document.get("file_hash"),
        "source_sha256": hashlib.sha256(content).hexdigest(), "tag_ids": _tags(document),
        "custom_metadata": _metadata(document, "custom_metadata"),
        "metadata": _metadata(document, "metadata"),
        "enable_status": document.get("enable_status"), "parse_status": document.get("parse_status"),
    }


def _old_document(task, client):
    document = _document(client, task["old_document_id"], task["knowledge_base_id"])
    current = _snapshot(document, _download(client, document["id"]))
    original = task["old_snapshot"]
    excluded = {"tag_ids"} if task.get("marker_strategy") == "tags" else {"title"}
    if any(current[key] != value for key, value in original.items() if key not in excluded):
        raise PublishError("old_document_changed", "Old document changed; automatic mutation stopped")
    if task.get("marker_strategy") == "tags":
        baseline = set(original["tag_ids"])
        marker_ids = set(task.get("marker_tag_ids", {}).values())
        actual = set(current["tag_ids"])
        if not baseline <= actual or not actual <= baseline | marker_ids:
            raise PublishError("old_tags_changed", "Old document tags changed outside this task")
    elif current["title"] not in {original["title"], task["waiting_title"], task["deleting_title"]}:
        raise PublishError("old_title_changed", "Old title was edited outside this task")
    return document


def _ensure_marker_tags(task, client, task_root=None):
    path = f"/knowledge-bases/{quote_id(task['knowledge_base_id'])}/tags"
    existing = client.records(path)
    identities = {}
    for name in (WAITING_TAG, DELETING_TAG):
        matches = [item for item in existing if item.get("name") == name]
        if len(matches) > 1:
            raise PublishError("marker_tag_conflict", "Status tag name is ambiguous in the selected knowledge base")
        if not matches:
            try:
                created = client.request(path, method="POST", payload={"name": name}).get("data")
            except RemoteHTTPError as error:
                if error.status != 409:
                    raise
                matches = [item for item in client.records(path) if item.get("name") == name]
            else:
                matches = [created]
        if len(matches) != 1 or not isinstance(matches[0], dict) or matches[0].get("name") != name or not isinstance(matches[0].get("id"), str) or not matches[0]["id"]:
            raise PublishError("marker_tag_unverified", "Could not resolve the status tag in the selected knowledge base")
        identities[name] = matches[0]["id"]
    if len(set(identities.values())) != 2:
        raise PublishError("marker_tag_conflict", "The two status tags must have different IDs")
    if task.get("marker_tag_ids") and task["marker_tag_ids"] != identities:
        raise PublishError("marker_tag_changed", "Status tag identities changed; inspect before resuming")
    task["marker_tag_ids"] = identities
    _save(task, task_root, client)
    return identities


def _set_tag_marker(task, client, marker, task_root=None):
    marker_ids = _ensure_marker_tags(task, client, task_root)
    original_ids = set(task["old_snapshot"]["tag_ids"])
    document = _old_document(task, client)
    expected = sorted(original_ids | ({marker_ids[marker]} if marker is not None else set()))
    if _tags(document) != expected:
        client.request(
            "/knowledge/tags", method="PUT",
            payload={"kb_id": task["knowledge_base_id"], "updates": {task["old_document_id"]: expected}},
        )
    verified = _old_document(task, client)
    if _tags(verified) != expected:
        raise PublishError("marker_not_verified", "Document status tag association could not be verified")
    task["old_status_marker"] = marker
    task["marker_verified_at"] = _now()
    _event(task, task["stage"], "Old document status tag association verified", marker=marker, tag_ids=expected)
    _save(task, task_root, client)


def _set_title(task, client, title):
    document = _old_document(task, client)
    if document.get("title", "") != title:
        client.request(f"/knowledge/{quote_id(document['id'])}", method="PUT", payload={"title": title})
    if _old_document(task, client).get("title", "") != title:
        raise PublishError("title_not_verified", "Title update could not be verified")


def _restore_title(task, client):
    if task.get("old_delete_attempted"):
        return
    try:
        _set_title(task, client, task["old_snapshot"]["title"])
        task["old_title_restored"] = True
    except REQUEST_ERRORS as error:
        task["old_title_restored"] = False
        task.setdefault("warnings", []).append({"message": "Old title was not restored", "error": sync._safe_error(error, client)})


def _set_marker(task, client, marker, task_root=None):
    if task.get("marker_strategy") == "tags":
        _set_tag_marker(task, client, marker, task_root)
    else:
        _set_title(task, client, task["waiting_title"] if marker == WAITING_TAG else task["deleting_title"])


def _restore_marker(task, client, task_root=None):
    if task.get("marker_strategy") != "tags":
        _restore_title(task, client)
        return
    if task.get("old_delete_attempted"):
        return
    try:
        if task.get("marker_tag_ids"):
            _set_tag_marker(task, client, None, task_root)
        task["old_marker_restored"] = True
    except REQUEST_ERRORS as error:
        task["old_marker_restored"] = False
        task.setdefault("warnings", []).append({"message": "Old status marker was not cleared; tags were left intact", "error": sync._safe_error(error, client)})


def _new_document(task, client, *, verify_source=False):
    if not task.get("new_document_id") or task["new_document_id"] == task["old_document_id"]:
        raise PublishError("identity_conflict", "A separate new document ID is required")
    document = _document(client, task["new_document_id"], task["knowledge_base_id"])
    expected = task["old_snapshot"]
    expected_folder = task.get("target_folder_path", expected["folder_path"]) or ""
    if document.get("file_name") != task["file_name"] or (document.get("folder_path") or "").strip("/") != expected_folder.strip("/"):
        raise PublishError("new_document_changed", "New document filename or folder differs")
    if _metadata(document, "metadata").get("document_sync_task_id") != task["task_id"]:
        raise PublishError("identity_conflict", "New document does not belong to this update task")
    if verify_source:
        content = _download(client, document["id"])
        if hashlib.sha256(content).hexdigest() != task["source_sha256"] or sync.extract_document_hash(content.decode("utf-8")) != task["local_hash"]:
            raise PublishError("new_source_mismatch", "New original file does not match the confirmed upload")
        if document.get("title", "") != expected["title"] or _tags(document) != expected["tag_ids"] or _metadata(document, "custom_metadata") != expected["custom_metadata"]:
            raise PublishError("new_document_changed", "New title, tags or custom metadata differ")
    return document


def _check_local(task):
    if hashlib.sha256(Path(task["path"]).read_bytes()).hexdigest() != task["source_sha256"]:
        raise PublishError("local_source_changed", "Local file changed after confirmation; old document will not be deleted")


def _prepare_new(task, client):
    document = _new_document(task, client)
    content = _download(client, document["id"])
    if hashlib.sha256(content).hexdigest() != task["source_sha256"]:
        raise PublishError("new_source_mismatch", "New source differs from the confirmed upload")
    original = task["old_snapshot"]
    if document.get("title", "") not in {task["file_name"], original["title"]} or _tags(document) != original["tag_ids"]:
        raise PublishError("new_document_changed", "New document title or tags changed")
    current_metadata = _metadata(document, "custom_metadata")
    if current_metadata not in ({}, original["custom_metadata"]):
        raise PublishError("new_document_changed", "New custom metadata changed outside this task")
    if document.get("title", "") != original["title"] or current_metadata != original["custom_metadata"]:
        client.request(
            f"/knowledge/{quote_id(document['id'])}", method="PUT",
            payload={"title": original["title"], "custom_metadata": original["custom_metadata"]},
        )
    _new_document(task, client, verify_source=True)


def _reconcile_upload(task, client):
    candidates = []
    for item in client.documents(task["knowledge_base_id"]):
        if item.get("file_name") != task["file_name"] or item.get("id") in task["preexisting_ids"]:
            continue
        document = _document(client, item["id"], task["knowledge_base_id"])
        if _metadata(document, "metadata").get("document_sync_task_id") == task["task_id"]:
            if hashlib.sha256(_download(client, document["id"])).hexdigest() == task["source_sha256"]:
                candidates.append(document)
    if len(candidates) != 1:
        raise PublishError("upload_outcome_unknown", "Cannot identify exactly one upload for this task; no re-upload attempted")
    task["new_document_id"] = candidates[0]["id"]
    _event(task, "preparing_new", "Recovered the original submission by task identity and exact file bytes")


def submit_update(
    path, *, knowledge_base=None, document_id, expected_local_hash, expected_remote_hash,
    target_folder_path=None, allow_cross_role=False, task_root=None, background=True,
):
    prepared = sync.refresh_document_hash(path)
    resolved, content, text = sync._read_document(path)
    if prepared["needs_refresh"] or prepared["declared_hash"] != expected_local_hash or sync.calculate_document_hash(text) != expected_local_hash:
        raise PublishError("confirmation_expired", "Local file differs from the confirmed version; compare and confirm again")
    if expected_remote_hash is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", expected_remote_hash):
        raise ValueError("Expected remote hash must be a SHA256 value")
    selector = sync.resolve_kb_role_selector(
        knowledge_base, expected_role="FRAMEWORK_DOCS", allow_cross_role=allow_cross_role,
    )
    client, kb_id = sync._configured_client(selector)
    identity = {"api_url": client.api_url.rstrip("/"), "knowledge_base_id": kb_id, "old_document_id": document_id}
    root = _root(task_root)
    with _lock(_lock_path(identity, root)):
        for existing_path in root.glob("*/task.json"):
            existing = _load(existing_path.parent.name, root)
            if all(existing.get(key) == value for key, value in identity.items()) and existing["status"] != "cancelled":
                if existing["source_sha256"] != hashlib.sha256(content).hexdigest():
                    raise PublishError("active_update_conflict", f"Task {existing['task_id']} already owns this old document with another version; inspect or cancel it first")
                return _action_result(existing["task_id"], root)
        old = _document(client, document_id, kb_id)
        if old.get("file_name") != resolved.name or old.get("parse_status") != "completed" or old.get("enable_status") != "enabled":
            raise PublishError("old_document_unavailable", "Select a matching, completed and enabled old file")
        old_content = _download(client, document_id)
        actual_remote_hash = sync.extract_document_hash(old_content.decode("utf-8"))
        expected_remote_hash = expected_remote_hash.lower() if expected_remote_hash is not None else None
        if actual_remote_hash != expected_remote_hash:
            raise PublishError("confirmation_expired", "Remote source differs from the confirmed version; compare again")
        if actual_remote_hash == expected_local_hash:
            return {**prepared, "status": "in_sync", "message": "Remote document is already up to date", "remote_written": False}
        documents = client.documents(kb_id)
        old_snapshot = _snapshot(old, old_content)
        folder = (
            target_folder_path
            if target_folder_path is not None
            else (prepared.get("folder_path") or old_snapshot["folder_path"])
        ).strip("/")
        task_id = uuid4().hex
        task = {
            "schema_version": 1, "task_id": task_id, **identity,
            "path": str(resolved), "file_name": resolved.name, "local_hash": expected_local_hash,
            "remote_hash": actual_remote_hash, "source_sha256": hashlib.sha256(content).hexdigest(),
            "old_snapshot": old_snapshot, "target_folder_path": folder,
            "status": "running", "stage": "uploading",
            "marker_strategy": "tags",
            "created_at": _now(), "history": [], "preexisting_ids": [item["id"] for item in documents],
        }
        _check_local(task)
        _event(task, "uploading", "Confirmed upload followed by guarded old status tagging and deletion; old file untouched")
        _save(task, root, client)
        try:
            destination = folder + "/" + resolved.name if folder else resolved.name
            document = sync._upload_markdown(
                client, kb_id, resolved.name, content, expected_local_hash,
                remote_file_name=destination, tag_ids=task["old_snapshot"]["tag_ids"],
                metadata={
                    "document_sync_task_id": task_id,
                    "document_sync_replaces": document_id,
                    "document_sync_path": destination,
                },
            )
            if document["id"] in task["preexisting_ids"]:
                raise PublishError("identity_conflict", "Server returned a preexisting document ID, not a new version")
            task["new_document_id"] = document["id"]
            _event(task, "preparing_new", "New upload accepted; background task will preserve attributes and associate the old status tag")
        except REQUEST_ERRORS as error:
            task["status"] = "paused"
            task["error"] = sync._safe_error(error, client)
            if isinstance(error, RemoteHTTPError) and error.status not in {408, 409} and not 500 <= error.status < 600:
                _event(task, "upload_rejected", "Upload rejected; old document and title are unchanged", error=task["error"])
            else:
                _event(task, "upload_outcome_unknown", "Upload outcome unknown; reconcile before any retry; old document untouched", error=task["error"])
        _save(task, root, client)
    if background and task["status"] == "running":
        return launch_worker(task_id, task_root=root)
    return _action_result(task_id, root)


def advance_task(task_id, *, task_root=None, client=None, resume=False, deadline=None):
    task = _load(task_id, task_root)
    if client is None:
        client = _client_for(task)
    if client.api_url.rstrip("/") != task["api_url"]:
        raise PublishError("server_changed", "Configured server differs from the confirmed update")
    with _lock(_lock_path(task, task_root)):
        task = _load(task_id, task_root)
        if task["status"] in {"completed", "cancelled"} or (task["status"] != "running" and not resume):
            return _action_result(task_id, task_root)
        task["status"] = "running"
        if resume:
            previous_error = task.pop("error", None)
            _event(task, task["stage"], "Explicitly resumed the same update task; no upload retry is implied", previous_error=previous_error)
        try:
            task["last_checked_at"] = _now()
            if task["stage"] in {"uploading", "upload_outcome_unknown"}:
                _check_local(task)
                _old_document(task, client)
                _reconcile_upload(task, client)
                _save(task, task_root, client)
            if task["stage"] == "preparing_new":
                _check_local(task)
                _old_document(task, client)
                _prepare_new(task, client)
                _event(task, "marking_old", "New ID, source and inherited attributes verified; marking old version")
                _save(task, task_root, client)
            if task["stage"] in {"delete_submitting", "delete_pending"}:
                try:
                    document = _document(client, task["old_document_id"], task["knowledge_base_id"])
                except RemoteHTTPError as error:
                    if error.status != 404:
                        raise
                    kb = client.request(f"/knowledge-bases/{quote_id(task['knowledge_base_id'])}").get("data")
                    if not isinstance(kb, dict) or kb.get("id") != task["knowledge_base_id"]:
                        raise PublishError("identity_conflict", "Cannot verify knowledge-base access after missing document")
                    newer = _new_document(task, client, verify_source=True)
                    if newer.get("parse_status") != "completed" or newer.get("enable_status") != "enabled":
                        raise PublishError("replacement_unavailable", "Old document is absent but the new document is no longer available")
                    task["status"] = "completed"
                    task.pop("error", None)
                    try:
                        _check_local(task)
                    except REQUEST_ERRORS:
                        task.setdefault("warnings", []).append({"message": "The confirmed version was updated, but the current local file has changed or is unreadable"})
                    _event(task, "completed", "New version verified; old document deletion confirmed")
                else:
                    if document.get("parse_status") == "failed":
                        raise PublishError("delete_failed", "New version exists; old deletion failed or needs verification")
                    _event(task, "delete_pending", "Old deletion is submitted or uncertain; waiting for confirmed absence")
            elif task["stage"] in {"marking_old", "waiting_parse", "deleting_old"}:
                _check_local(task)
                _old_document(task, client)
                newer = _new_document(task, client)
                task["summary_status"] = newer.get("summary_status") or "unknown"
                if newer.get("parse_status") == "failed":
                    task["status"] = "failed"
                    task["error"] = {"code": "processing_failed", "message": str(newer.get("error_message") or "New document parsing failed")[:500]}
                    _restore_marker(task, client, task_root)
                    _event(task, "waiting_parse", "New parsing failed; old file retained; see status marker restoration result")
                elif newer.get("parse_status") in {"pending", "processing", "finalizing", "completed"}:
                    if task["stage"] == "marking_old" or task.get("old_title_restored") or task.get("old_marker_restored"):
                        _set_marker(task, client, WAITING_TAG, task_root)
                        task["old_marker_restored" if task.get("marker_strategy") == "tags" else "old_title_restored"] = False
                        _event(task, "waiting_parse", "New upload accepted; old version retained while parsing")
                    if newer.get("parse_status") != "completed":
                        if task.get("parse_status") != newer.get("parse_status"):
                            _event(task, "waiting_parse", f"New parsing status: {newer.get('parse_status')}; old version retained")
                        task["parse_status"] = newer.get("parse_status")
                    else:
                        newer = _new_document(task, client, verify_source=True)
                        if newer.get("enable_status") != "enabled":
                            raise PublishError("replacement_unavailable", "New parsing completed but the document is not enabled")
                        if newer.get("summary_status") == "failed":
                            task.setdefault("warnings", []).append({"message": "New main parsing completed but summary failed"})
                        _event(task, "deleting_old", "New source, title, folder and tags verified; preparing old deletion")
                        _save(task, task_root, client)
                        _set_marker(task, client, DELETING_TAG, task_root)
                        _check_local(task)
                        _old_document(task, client)
                        _new_document(task, client, verify_source=True)
                        if deadline is not None and time.monotonic() >= deadline:
                            raise PublishError("wait_timeout", "Worker wait budget expired before deletion; old file retained")
                        task["old_delete_attempted"] = True
                        _event(task, "delete_submitting", "Sending one deletion for the explicitly confirmed old document ID")
                        _save(task, task_root, client)
                        try:
                            response = client.request(f"/knowledge/{quote_id(task['old_document_id'])}", method="DELETE")
                        except RemoteHTTPError as error:
                            if error.status in {400, 401, 403, 405, 413, 422}:
                                task["old_delete_attempted"] = False
                                _event(task, "deleting_old", "Deletion explicitly rejected; old file retained. Fix the cause and explicitly resume")
                            raise
                        data = response.get("data") or {}
                        if not isinstance(data, dict) or not isinstance(data.get("task_id"), str) or not data["task_id"]:
                            raise PublishError("delete_outcome_unknown", "Delete returned no task ID; do not resubmit blindly")
                        task["delete_task_id"] = data["task_id"]
                        _event(task, "delete_pending", "Old deletion task accepted, not yet confirmed complete")
                else:
                    raise PublishError("unknown_processing_status", "New processing status is unknown; no deletion was sent")
            else:
                raise PublishError("task_needs_reconciliation", "Submission needs reconciliation before automatic cleanup")
        except REQUEST_ERRORS as error:
            task["status"] = "paused"
            task["error"] = sync._safe_error(error, client)
            _event(task, task["stage"], "Update paused; inspect the error and retained versions before resuming", error=task["error"])
            if isinstance(error, PublishError) and error.code in {"new_source_mismatch", "local_source_changed", "new_document_changed", "wait_timeout"}:
                _restore_marker(task, client, task_root)
        _save(task, task_root, client)
    return _action_result(task_id, task_root)


def launch_worker(task_id, *, task_root=None, resume=False):
    task = _load(task_id, task_root)
    with _lock(_lock_path(task, task_root)):
        task = _load(task_id, task_root)
        if _worker_active(task_id, task_root):
            return _action_result(task_id, task_root)
        if task["status"] in {"completed", "cancelled"} or (task["status"] != "running" and not resume):
            return _action_result(task_id, task_root)
        task["status"] = "running"
        if resume:
            previous_error = task.pop("error", None)
            _event(task, task["stage"], "Explicitly requested background recovery of this same update", previous_error=previous_error)
        arguments = [sys.executable, "-B", "-m", __name__, "worker", task_id, "--task-root", str(_root(task_root))]
        options = {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        try:
            log_path = _task_path(task_id, task_root).with_name("worker.log")
            with log_path.open("ab") as output:
                process = subprocess.Popen(arguments, cwd=sync.FRAMEWORK_ROOT, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, close_fds=True, **options)
            task["worker_pid"] = process.pid
            task["worker_started_at"] = _now()
            _event(task, task["stage"], "Independent background worker started; this is not update completion")
        except OSError as error:
            task["status"] = "paused"
            task["error"] = {"code": "worker_start_failed", "message": str(error)[:300]}
            _event(task, task["stage"], "Worker could not start; task is durable and can be resumed", error=task["error"])
        _save(task, task_root)
    return _action_result(task_id, task_root)


def cancel_task(task_id, *, task_root=None, client=None):
    task = _load(task_id, task_root)
    client = client or _client_for(task)
    if client.api_url.rstrip("/") != task["api_url"]:
        raise PublishError("server_changed", "Configured server differs from the confirmed update")
    with _lock(_lock_path(task, task_root)):
        task = _load(task_id, task_root)
        if task.get("old_delete_attempted"):
            raise PublishError("delete_already_submitted", "Cannot cancel or undo a deletion already submitted to WeKnora; query task status")
        _restore_marker(task, client, task_root)
        task["status"] = "cancelled"
        _event(task, task["stage"], "Automatic cleanup cancelled; uploaded new file, if any, is retained for inspection")
        _save(task, task_root, client)
    return _action_result(task_id, task_root)


def run_worker(task_id, *, task_root=None, poll_interval=15, max_wait=3600, once=False, client=None):
    if poll_interval <= 0 or max_wait <= 0:
        raise ValueError("Worker interval and wait budget must be positive")
    worker_lock = _task_path(task_id, task_root).with_name("worker.lock")
    with _lock(worker_lock):
        deadline = time.monotonic() + max_wait
        while True:
            if time.monotonic() >= deadline:
                task = _load(task_id, task_root)
                with _lock(_lock_path(task, task_root)):
                    task["status"] = "paused"
                    task["error"] = {"code": "wait_timeout", "message": "Worker wait budget expired; inspect before resuming"}
                    try:
                        _restore_marker(task, client or _client_for(task), task_root)
                    except REQUEST_ERRORS as error:
                        task.setdefault("warnings", []).append({"message": "Could not clear old status marker after worker timeout", "code": getattr(error, "code", "request_failed")})
                    _event(task, task["stage"], "Wait budget expired; no new upload or deletion sent. See old marker restoration result", error=task["error"])
                    _save(task, task_root)
                return _action_result(task_id, task_root)
            try:
                result = advance_task(task_id, task_root=task_root, client=client, deadline=deadline)
            except PublishError as error:
                if error.code != "task_busy":
                    task = _load(task_id, task_root)
                    with _lock(_lock_path(task, task_root)):
                        task["status"] = "paused"
                        task["error"] = {"code": error.code, "message": str(error)}
                        _event(task, task["stage"], "Worker paused before action; inspect configuration and error", error=task["error"])
                        _save(task, task_root)
                    return _action_result(task_id, task_root)
                result = task_status(task_id, task_root=task_root)
            if once or result["status"] != "running":
                return result
            threading.Event().wait(min(poll_interval, max(0, deadline - time.monotonic())))


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["worker"])
    parser.add_argument("task_id")
    parser.add_argument("--task-root")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run_worker(args.task_id, task_root=args.task_root, once=args.once)
    except REQUEST_ERRORS as error:
        print(json.dumps({"status": "paused", "code": getattr(error, "code", "worker_failed"), "message": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in {"running", "completed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())