"""Stateless `SHA-256` differential WeKnora synchronizer (`STATIC_UI_DOCS`, upload-verify-then-delete).

Enforces strict safety invariants:
1. Role isolation: binds to `STATIC_UI_DOCS` and refuses to write into `FRAMEWORK_DOCS`,
   `ENGINEERING_CASES`, or `PRODUCT_DOCS` unless `allow_cross_role=True`.
2. Zero-write idempotence: documents with matching `SHA256` and `parse_status == 'completed'`
   at the expected `folder_path` incur 0 write/upload requests.
3. Upload-verify-then-delete replacement: when a document's `SHA256` changes, the new version
   is uploaded first and verified `parse_status == 'completed'` BEFORE deleting the old `document_id`.
   If upload or parsing fails or times out, the working old document is NEVER deleted.
4. Optional orphan pruning: remote documents removed locally are deleted ONLY when `prune_removed=True`
   and zero upload/replace failures occurred in the current run.
"""

from __future__ import annotations

from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from autowork_core.knowledge.document_sync import (
    _existing_document_hash,
    _upload_markdown,
    calculate_document_hash,
    extract_document_hash,
    move_documents_to_folder,
    refresh_document_hash,
)
from autowork_core.knowledge.weknora_client import (
    PublishError,
    RemoteHTTPError,
    WeKnoraClient,
    quote_id,
    resolve_kb_role_selector,
    verify_resolved_kb_role,
)


RECOMMENDED_STATIC_UI_KB_CONFIG: Dict[str, Any] = {
    "type": "document",
    "vector_enabled": True,
    "keyword_enabled": True,
    "wiki_enabled": False,
    "graph_enabled": False,
    "chunking_config": {
        "strategy": "heading",
        "chunk_size": 1200,
        "chunk_overlap": 80,
        "enable_parent_child": False,
    },
}

_TRANSIENT_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})


def inspect_static_ui_kb_config(kb_record: Optional[Dict[str, Any]]) -> List[str]:
    """Audit target WeKnora KB settings and return actionable misconfiguration warnings (read-only)."""
    if not isinstance(kb_record, dict):
        return []
    warnings: List[str] = []

    qg = kb_record.get("question_generation_config")
    if isinstance(qg, dict) and qg.get("enabled") is True:
        warnings.append(
            "Target KB has question_generation enabled (causes 20-30x slower parsing and pollutes keyword search); "
            "recommended: disabled."
        )

    idx_strat = kb_record.get("indexing_strategy")
    if isinstance(idx_strat, dict):
        if idx_strat.get("wiki_enabled") is True:
            warnings.append("Target KB has wiki_enabled=true (LLM rewriting may corrupt exact @auto_id facts); recommended: false.")
        if idx_strat.get("graph_enabled") is True:
            warnings.append("Target KB has graph_enabled=true (high latency & noise for UI catalogs); recommended: false.")
        if idx_strat.get("keyword_enabled") is False:
            warnings.append("Target KB has keyword_enabled=false (breaks exact @auto_id / grep_chunks lookup); recommended: true.")

    chunk_cfg = kb_record.get("chunking_config")
    if isinstance(chunk_cfg, dict):
        strat = str(chunk_cfg.get("strategy") or "").strip().lower()
        if strat and strat != "heading":
            warnings.append(f"Target KB chunking strategy is {strat!r}; recommended: 'heading' to keep H2 control cards intact.")
        csize = chunk_cfg.get("chunk_size")
        if isinstance(csize, int) and csize < 1200:
            warnings.append(f"Target KB chunk_size={csize} < 1200 (may split 1150-char H2 sections mid-control); recommended: 1200.")
        if chunk_cfg.get("enable_parent_child") is True:
            warnings.append("Target KB has enable_parent_child=true (splits control cards into small child fragments); recommended: false.")

    return warnings


def _is_transient_weknora_error(exc: Exception) -> bool:
    if isinstance(exc, RemoteHTTPError):
        return exc.status in _TRANSIENT_HTTP_STATUSES
    if isinstance(exc, PublishError):
        return exc.code == "connection_failed"
    return False


def _call_with_retry(fn: Callable[[], Any], *, retries: int = 2, base_delay_sec: float = 1.0) -> Any:
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as exc:
            if attempt >= retries or not _is_transient_weknora_error(exc):
                raise
            attempt += 1
            time.sleep(base_delay_sec * attempt)


@dataclass
class SyncActionRecord:
    rel_path: str
    action: str  # in_sync | upload_new | replace_changed | move_folder | prune_orphan | error
    local_hash: str = ""
    remote_hash: str = ""
    old_document_id: str = ""
    new_document_id: str = ""
    detail: str = ""


@dataclass
class StaticUiSyncReport:
    knowledge_base: str
    knowledge_base_id: str
    dry_run: bool
    total_local_docs: int = 0
    in_sync_count: int = 0
    uploaded_new_count: int = 0
    replaced_count: int = 0
    moved_count: int = 0
    pruned_count: int = 0
    remote_orphan_count: int = 0
    failed_count: int = 0
    config_warnings: List[str] = field(default_factory=list)
    actions: List[SyncActionRecord] = field(default_factory=list)


def _resolve_static_ui_client(
    kb_selector: Optional[str],
    *,
    allow_cross_role: bool = False,
) -> Tuple[WeKnoraClient, str, str, Dict[str, Any]]:
    from config.settings import settings

    selector = resolve_kb_role_selector(
        kb_selector,
        expected_role="STATIC_UI_DOCS",
        allow_cross_role=allow_cross_role,
    )
    client = WeKnoraClient(settings.weknora_api_url, settings.weknora_api_key)
    records = _call_with_retry(lambda: client.records("/knowledge-bases"))
    matches = [item for item in records if selector in {item.get("id"), item.get("name")}]
    if len(matches) != 1:
        raise PublishError(
            "knowledge_base_not_unique",
            f"Target {selector!r} must resolve to exactly one accessible knowledge base",
            stage="preflight",
        )
    verify_resolved_kb_role(
        matches[0],
        expected_role="STATIC_UI_DOCS",
        allow_cross_role=allow_cross_role,
    )
    resolved_name = str(matches[0].get("name") or selector)
    return client, matches[0]["id"], resolved_name, matches[0]


def _extract_remote_rel_path_and_hash(
    client: WeKnoraClient,
    kb_id: str,
    doc_summary: Dict[str, Any],
) -> Tuple[Dict[str, Any], str, str]:
    """Return `(full_doc, canonical_rel_path, remote_sha256_or_empty)`."""
    doc_id = doc_summary["id"]
    has_folder_key = "folder_path" in doc_summary
    meta_hash = _existing_document_hash(doc_summary)

    full_doc = doc_summary
    if not has_folder_key or (meta_hash is None and "custom_metadata" not in doc_summary and "metadata" not in doc_summary):
        full_doc = client.document(doc_id, kb_id)
        meta_hash = _existing_document_hash(full_doc)

    fname = str(full_doc.get("file_name") or "").strip()
    folder = str(full_doc.get("folder_path") or "").strip("/")
    rel_path = f"{folder}/{fname}" if folder else fname

    if not meta_hash:
        try:
            raw_dl = client.request(f"/knowledge/{quote_id(doc_id)}/download", raw=True)
            if isinstance(raw_dl, bytes):
                meta_hash = extract_document_hash(raw_dl.decode("utf-8-sig", errors="replace")) or ""
        except Exception:
            meta_hash = ""

    return full_doc, rel_path, meta_hash or ""


def _emit_progress(enabled: bool, message: str) -> None:
    if enabled:
        print(f"[ui-inventory sync] {message}", file=sys.stderr, flush=True)


@dataclass
class _PendingDocTask:
    rel_path: str
    folder_path: str
    file_name: str
    local_hash: str
    raw_bytes: bytes
    mode: str  # upload_new | replace_changed | await_existing
    remote_hash: str = ""
    old_ids: List[str] = field(default_factory=list)
    new_id: str = ""
    verified_doc: Optional[Dict[str, Any]] = None


def _batch_wait_for_completed_parse(
    client: WeKnoraClient,
    kb_id: str,
    pending_tasks: List[_PendingDocTask],
    *,
    poll_interval_sec: float,
    poll_timeout_sec: float,
    max_workers: int,
    show_progress: bool,
) -> Tuple[List[_PendingDocTask], List[Tuple[_PendingDocTask, str]]]:
    """Concurrently poll all pending uploaded/in-flight documents until `completed`, `failed`, or timeout."""
    if not pending_tasks:
        return [], []

    remaining: Dict[str, _PendingDocTask] = {t.new_id: t for t in pending_tasks}
    completed_list: List[_PendingDocTask] = []
    failed_list: List[Tuple[_PendingDocTask, str]] = []
    total = len(pending_tasks)
    worker_cnt = max(1, min(max_workers, total))
    timeout_window = max(0.1, poll_timeout_sec)
    deadline = time.monotonic() + timeout_window
    last_reported_done = -1

    while remaining:
        progressed = False
        batch_ids = list(remaining.keys())

        def _check_one(doc_id: str) -> Tuple[str, Optional[Dict[str, Any]], Optional[str]]:
            try:
                return doc_id, client.document(doc_id, kb_id), None
            except Exception as exc:
                return doc_id, None, str(exc)

        results: List[Tuple[str, Optional[Dict[str, Any]], Optional[str]]] = []
        if worker_cnt == 1 or len(batch_ids) == 1:
            results = [_check_one(did) for did in batch_ids]
        else:
            with ThreadPoolExecutor(max_workers=min(worker_cnt, len(batch_ids))) as pool:
                futs = [pool.submit(_check_one, did) for did in batch_ids]
                for fut in as_completed(futs):
                    results.append(fut.result())

        for doc_id, doc, err in results:
            task = remaining.get(doc_id)
            if task is None:
                continue
            if err is not None:
                remaining.pop(doc_id, None)
                failed_list.append((task, f"Status check failed: {err}"))
                progressed = True
                continue
            assert doc is not None
            pstatus = doc.get("parse_status")
            if pstatus == "completed":
                remaining.pop(doc_id, None)
                if doc.get("enable_status") not in (None, "enabled"):
                    failed_list.append(
                        (
                            task,
                            f"Document {doc_id} completed parsing but enable_status={doc.get('enable_status')!r}",
                        )
                    )
                else:
                    task.verified_doc = doc
                    completed_list.append(task)
                progressed = True
            elif pstatus == "failed":
                remaining.pop(doc_id, None)
                err_msg = str(doc.get("error_message") or "WeKnora parse_status == failed")[:300]
                failed_list.append((task, err_msg))
                progressed = True

        done_cnt = len(completed_list) + len(failed_list)
        if progressed:
            # Reset idle timeout whenever forward progress happens on real batches (preserve strict short test timeouts)
            if timeout_window >= 5.0:
                deadline = time.monotonic() + timeout_window
            if done_cnt != last_reported_done:
                last_reported_done = done_cnt
                _emit_progress(
                    show_progress,
                    f"Parsed {len(completed_list)}/{total} completed "
                    f"({len(remaining)} processing, {len(failed_list)} failed)",
                )

        if not remaining:
            break
        if time.monotonic() >= deadline:
            for doc_id, task in list(remaining.items()):
                failed_list.append(
                    (
                        task,
                        f"Timed out waiting for document {doc_id} to complete parsing",
                    )
                )
            remaining.clear()
            break
        time.sleep(max(0.01, poll_interval_sec))

    return completed_list, failed_list


def sync_kb_docs_directory(
    kb_docs_dir: Path,
    *,
    kb_selector: Optional[str] = None,
    dry_run: bool = False,
    prune_removed: bool = False,
    allow_cross_role: bool = False,
    poll_interval_sec: float = 1.0,
    poll_timeout_sec: float = 300.0,
    max_workers: int = 4,
    show_progress: bool = False,
    client_override: Optional[Tuple[WeKnoraClient, str, str]] = None,
) -> StaticUiSyncReport:
    """Synchronize `dist/kb_docs/` into WeKnora `STATIC_UI_DOCS` safely by `SHA256` with concurrent batch upload."""
    docs_root = kb_docs_dir.resolve()
    if not docs_root.is_dir():
        raise ValueError(f"kb_docs directory does not exist: {docs_root}")

    md_files = sorted(
        (p for p in docs_root.rglob("*.md") if p.is_file()),
        key=lambda p: p.relative_to(docs_root).as_posix(),
    )

    local_entries: List[Dict[str, Any]] = []
    for p in md_files:
        refresh_document_hash(p)
        raw_bytes = p.read_bytes()
        text = raw_bytes.decode("utf-8")
        declared = extract_document_hash(text)
        computed = calculate_document_hash(text)
        if not declared or declared != computed:
            raise ValueError(f"Invalid or out-of-sync SHA256 header in {p}")
        rel_posix = p.relative_to(docs_root).as_posix()
        folder = "" if "/" not in rel_posix else rel_posix.rsplit("/", 1)[0]
        local_entries.append({
            "path": p,
            "rel_path": rel_posix,
            "folder_path": folder,
            "file_name": p.name,
            "raw_bytes": raw_bytes,
            "local_hash": declared,
            "local_md5": hashlib.md5(raw_bytes).hexdigest(),
        })

    kb_record: Optional[Dict[str, Any]] = None
    if client_override is not None:
        if len(client_override) == 4:
            client, kb_id, resolved_label, kb_record = client_override  # type: ignore[misc]
        else:
            client, kb_id, resolved_label = client_override
    else:
        client, kb_id, resolved_label, kb_record = _resolve_static_ui_client(
            kb_selector,
            allow_cross_role=allow_cross_role,
        )

    config_warnings = inspect_static_ui_kb_config(kb_record)
    for w in config_warnings:
        _emit_progress(True, f"WARNING: {w}")

    report = StaticUiSyncReport(
        knowledge_base=resolved_label,
        knowledge_base_id=kb_id,
        dry_run=dry_run,
        total_local_docs=len(local_entries),
        config_warnings=config_warnings,
    )

    # Index remote documents by canonical `rel_path` (and track same-filename root uploads if unfoldered)
    remote_summaries = _call_with_retry(lambda: client.documents(kb_id))
    remote_by_rel: Dict[str, List[Tuple[Dict[str, Any], str]]] = {}
    remote_root_by_fname: Dict[str, List[Tuple[Dict[str, Any], str]]] = {}

    for rsum in remote_summaries:
        full_doc, r_rel, r_hash = _extract_remote_rel_path_and_hash(client, kb_id, rsum)
        remote_by_rel.setdefault(r_rel, []).append((full_doc, r_hash))
        if "/" not in r_rel:
            remote_root_by_fname.setdefault(r_rel, []).append((full_doc, r_hash))

    matched_remote_ids: set[str] = set()
    upload_tasks: List[_PendingDocTask] = []
    await_existing_tasks: List[_PendingDocTask] = []
    move_root_tasks: List[Tuple[str, str, str, str, str]] = []  # (rel_p, folder, l_hash, r_hash, rid)

    _IN_FLIGHT_STATUSES = {"pending", "processing", "finalizing", "queued", "indexing"}

    # Phase 0: Classify every local document deterministically
    for item in local_entries:
        rel_p = item["rel_path"]
        folder = item["folder_path"]
        fname = item["file_name"]
        l_hash = item["local_hash"]
        raw_bytes = item["raw_bytes"]

        exact_cands = remote_by_rel.get(rel_p, [])
        root_same_hash_cands = [
            pair for pair in remote_root_by_fname.get(fname, [])
            if folder and pair[1] == l_hash and pair[0]["id"] not in matched_remote_ids
        ]

        # Case 1: Exact `rel_path` match already exists remotely
        if len(exact_cands) == 1:
            rdoc, r_hash = exact_cands[0]
            rid = rdoc["id"]
            matched_remote_ids.add(rid)
            pstatus = str(rdoc.get("parse_status") or "")
            if r_hash == l_hash and pstatus == "completed":
                report.in_sync_count += 1
                report.actions.append(
                    SyncActionRecord(
                        rel_path=rel_p,
                        action="in_sync",
                        local_hash=l_hash,
                        remote_hash=r_hash,
                        old_document_id=rid,
                    )
                )
                continue

            if r_hash == l_hash and pstatus in _IN_FLIGHT_STATUSES:
                if dry_run:
                    report.in_sync_count += 1
                    report.actions.append(
                        SyncActionRecord(
                            rel_path=rel_p,
                            action="in_sync",
                            local_hash=l_hash,
                            remote_hash=r_hash,
                            old_document_id=rid,
                            detail=f"dry-run: remote already processing identical hash ({pstatus})",
                        )
                    )
                else:
                    await_existing_tasks.append(
                        _PendingDocTask(
                            rel_path=rel_p,
                            folder_path=folder,
                            file_name=fname,
                            local_hash=l_hash,
                            raw_bytes=raw_bytes,
                            mode="await_existing",
                            remote_hash=r_hash,
                            old_ids=[rid],
                            new_id=rid,
                        )
                    )
                continue

            if dry_run:
                report.replaced_count += 1
                report.actions.append(
                    SyncActionRecord(
                        rel_path=rel_p,
                        action="replace_changed",
                        local_hash=l_hash,
                        remote_hash=r_hash,
                        old_document_id=rid,
                        detail="dry-run: would upload new version, wait for completed parse, then delete old ID",
                    )
                )
                continue

            upload_tasks.append(
                _PendingDocTask(
                    rel_path=rel_p,
                    folder_path=folder,
                    file_name=fname,
                    local_hash=l_hash,
                    raw_bytes=raw_bytes,
                    mode="replace_changed",
                    remote_hash=r_hash,
                    old_ids=[rid],
                )
            )
            continue

        # Case 1B: Unchanged root file needs moving into `folder_path`
        if not exact_cands and len(root_same_hash_cands) == 1:
            rdoc, r_hash = root_same_hash_cands[0]
            rid = rdoc["id"]
            matched_remote_ids.add(rid)
            if dry_run:
                report.moved_count += 1
                report.actions.append(
                    SyncActionRecord(
                        rel_path=rel_p,
                        action="move_folder",
                        local_hash=l_hash,
                        remote_hash=r_hash,
                        old_document_id=rid,
                        detail=f"dry-run: would move root doc into {folder!r}",
                    )
                )
                continue
            move_root_tasks.append((rel_p, folder, l_hash, r_hash, rid))
            continue

        # Case 2: Multiple duplicate remote docs at the exact same `rel_path` -> replace all safely after new succeeds
        if len(exact_cands) > 1:
            old_ids = [d["id"] for d, _ in exact_cands]
            matched_remote_ids.update(old_ids)
            if dry_run:
                report.replaced_count += 1
                report.actions.append(
                    SyncActionRecord(
                        rel_path=rel_p,
                        action="replace_changed",
                        local_hash=l_hash,
                        old_document_id=",".join(old_ids),
                        detail="dry-run: would consolidate duplicate remote entries after verifying new upload",
                    )
                )
                continue
            upload_tasks.append(
                _PendingDocTask(
                    rel_path=rel_p,
                    folder_path=folder,
                    file_name=fname,
                    local_hash=l_hash,
                    raw_bytes=raw_bytes,
                    mode="replace_changed",
                    old_ids=old_ids,
                )
            )
            continue

        # Case 3: Missing remotely -> Upload new
        if dry_run:
            report.uploaded_new_count += 1
            report.actions.append(
                SyncActionRecord(
                    rel_path=rel_p,
                    action="upload_new",
                    local_hash=l_hash,
                    detail="dry-run: would upload new document",
                )
            )
            continue

        upload_tasks.append(
            _PendingDocTask(
                rel_path=rel_p,
                folder_path=folder,
                file_name=fname,
                local_hash=l_hash,
                raw_bytes=raw_bytes,
                mode="upload_new",
            )
        )

    # Batch-move unchanged root files grouped by target folder
    if move_root_tasks:
        by_folder: Dict[str, List[Tuple[str, str, str, str, str]]] = defaultdict(list)
        for mt in move_root_tasks:
            by_folder[mt[1]].append(mt)
        for folder, items in sorted(by_folder.items()):
            try:
                move_documents_to_folder(client, kb_id, [it[4] for it in items], folder)
                for rel_p, _, l_hash, r_hash, rid in items:
                    report.moved_count += 1
                    report.actions.append(
                        SyncActionRecord(
                            rel_path=rel_p,
                            action="move_folder",
                            local_hash=l_hash,
                            remote_hash=r_hash,
                            old_document_id=rid,
                        )
                    )
            except Exception as exc:
                for rel_p, _, l_hash, _, rid in items:
                    report.failed_count += 1
                    report.actions.append(
                        SyncActionRecord(
                            rel_path=rel_p,
                            action="error",
                            local_hash=l_hash,
                            old_document_id=rid,
                            detail=f"Folder move failed: {exc}",
                        )
                    )

    # Phase 1 & 2: Sliding-window bounded concurrent upload + parse polling
    # Keeps <= `max_workers * 2` documents in WeKnora's parse queue simultaneously, with transient retries & 409 safe takeover.
    verified_tasks: List[_PendingDocTask] = []
    failed_parse_tasks: List[Tuple[_PendingDocTask, str]] = []
    workers = max(1, max_workers)
    window_limit = max(workers * 2, 1)

    if upload_tasks or await_existing_tasks:
        total_upload = len(upload_tasks)
        total_verify = total_upload + len(await_existing_tasks)
        if total_upload:
            _emit_progress(
                show_progress,
                f"Syncing {total_upload} document(s) to '{resolved_label}' "
                f"(in_sync={report.in_sync_count}, concurrency={workers}, max_in_flight={window_limit})...",
            )

        def _submit_one(task: _PendingDocTask) -> Tuple[_PendingDocTask, Optional[str], Optional[str]]:
            meta: Dict[str, Any] = {"document_sync_path": task.rel_path}
            if task.mode == "replace_changed" and len(task.old_ids) == 1:
                meta["document_sync_replaces"] = task.old_ids[0]
            local_md5 = hashlib.md5(task.raw_bytes).hexdigest()

            def _do_upload() -> Dict[str, Any]:
                return _upload_markdown(
                    client,
                    kb_id,
                    task.file_name,
                    task.raw_bytes,
                    task.local_hash,
                    remote_file_name=task.rel_path if task.folder_path else None,
                    metadata=meta,
                )

            try:
                uploaded = _call_with_retry(_do_upload)
                new_id = uploaded["id"]
                if new_id in task.old_ids:
                    return task, None, "Server returned old document ID on replacement upload"
                return task, new_id, None
            except RemoteHTTPError as exc:
                # Safe 409 duplicate_file takeover (e.g., network drop after server saved upload on retry)
                payload = exc.payload if isinstance(exc.payload, dict) else {}
                dup_data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
                dup_id = dup_data.get("id") if isinstance(dup_data.get("id"), str) else ""
                if exc.status == 409 and payload.get("code") == "duplicate_file" and dup_id.strip():
                    try:
                        dup_doc = _call_with_retry(lambda: client.document(dup_id, kb_id))
                        dup_hash = _existing_document_hash(dup_doc) or ""
                        dup_md5 = str(dup_doc.get("file_hash") or "").strip().lower()
                        if (
                            dup_id not in task.old_ids
                            and (dup_hash == task.local_hash or dup_md5 == local_md5)
                        ):
                            return task, dup_id, None
                    except Exception:
                        pass
                return task, None, str(exc)
            except Exception as exc:
                return task, None, str(exc)

        def _check_one(doc_id: str) -> Tuple[str, Optional[Dict[str, Any]], Optional[str]]:
            try:
                return doc_id, _call_with_retry(lambda: client.document(doc_id, kb_id)), None
            except Exception as exc:
                return doc_id, None, str(exc)

        upload_queue: deque[_PendingDocTask] = deque(upload_tasks)
        in_flight: Dict[str, _PendingDocTask] = {t.new_id: t for t in await_existing_tasks}
        submitted_cnt = 0
        upload_failed_cnt = 0
        timeout_window = max(0.1, poll_timeout_sec)
        deadline = time.monotonic() + timeout_window
        last_reported_done = -1

        while upload_queue or in_flight:
            # 1) Refill sliding window up to `window_limit`
            slots = max(0, window_limit - len(in_flight))
            if upload_queue and slots > 0:
                wave: List[_PendingDocTask] = []
                while upload_queue and len(wave) < slots:
                    wave.append(upload_queue.popleft())

                if workers == 1 or len(wave) == 1:
                    wave_res = [_submit_one(t) for t in wave]
                else:
                    wave_res = []
                    with ThreadPoolExecutor(max_workers=min(workers, len(wave))) as pool:
                        futs = [pool.submit(_submit_one, t) for t in wave]
                        for fut in as_completed(futs):
                            wave_res.append(fut.result())

                for task, new_id, err in wave_res:
                    submitted_cnt += 1
                    if err is not None or not new_id:
                        upload_failed_cnt += 1
                        report.failed_count += 1
                        old_ref = ",".join(task.old_ids)
                        prefix = f"Old document {old_ref} preserved! Replacement aborted: " if task.old_ids else "Upload failed: "
                        report.actions.append(
                            SyncActionRecord(
                                rel_path=task.rel_path,
                                action="error",
                                local_hash=task.local_hash,
                                remote_hash=task.remote_hash,
                                old_document_id=old_ref,
                                detail=f"{prefix}{err}",
                            )
                        )
                    else:
                        task.new_id = new_id
                        matched_remote_ids.add(new_id)
                        in_flight[new_id] = task

                if timeout_window >= 5.0:
                    deadline = time.monotonic() + timeout_window
                if show_progress and total_upload:
                    _emit_progress(
                        show_progress,
                        f"Uploaded {submitted_cnt}/{total_upload} files (in_flight={len(in_flight)}, upload_errors={upload_failed_cnt})",
                    )

            if not in_flight:
                continue

            # 2) Poll all currently in-flight documents once concurrently
            batch_ids = list(in_flight.keys())
            if workers == 1 or len(batch_ids) == 1:
                poll_results = [_check_one(did) for did in batch_ids]
            else:
                poll_results = []
                with ThreadPoolExecutor(max_workers=min(workers, len(batch_ids))) as pool:
                    futs = [pool.submit(_check_one, did) for did in batch_ids]
                    for fut in as_completed(futs):
                        poll_results.append(fut.result())

            progressed = False
            for doc_id, doc, err in poll_results:
                task = in_flight.get(doc_id)
                if task is None:
                    continue
                if err is not None:
                    in_flight.pop(doc_id, None)
                    failed_parse_tasks.append((task, f"Status check failed: {err}"))
                    progressed = True
                    continue
                assert doc is not None
                pstatus = doc.get("parse_status")
                if pstatus == "completed":
                    in_flight.pop(doc_id, None)
                    if doc.get("enable_status") not in (None, "enabled"):
                        failed_parse_tasks.append(
                            (
                                task,
                                f"Document {doc_id} completed parsing but enable_status={doc.get('enable_status')!r}",
                            )
                        )
                    else:
                        task.verified_doc = doc
                        verified_tasks.append(task)
                    progressed = True
                elif pstatus == "failed":
                    in_flight.pop(doc_id, None)
                    err_msg = str(doc.get("error_message") or "WeKnora parse_status == failed")[:300]
                    failed_parse_tasks.append((task, err_msg))
                    progressed = True

            done_cnt = len(verified_tasks) + len(failed_parse_tasks)
            if progressed:
                if timeout_window >= 5.0:
                    deadline = time.monotonic() + timeout_window
                if done_cnt != last_reported_done:
                    last_reported_done = done_cnt
                    _emit_progress(
                        show_progress,
                        f"Parsed {len(verified_tasks)}/{total_verify - upload_failed_cnt} completed "
                        f"({len(in_flight)} processing, {len(upload_queue)} queued, {len(failed_parse_tasks)} failed)",
                    )

            if not upload_queue and not in_flight:
                break
            if upload_queue and len(in_flight) < window_limit:
                # Slots freed up — immediately submit next wave without waiting
                continue
            if time.monotonic() >= deadline:
                for doc_id, task in list(in_flight.items()):
                    failed_parse_tasks.append(
                        (task, f"Timed out waiting for document {doc_id} to complete parsing")
                    )
                in_flight.clear()
                # Abort any remaining unsubmitted queue items cleanly
                while upload_queue:
                    qtask = upload_queue.popleft()
                    failed_parse_tasks.append((qtask, "Aborted due to earlier parse verification timeout"))
                break
            time.sleep(max(0.01, poll_interval_sec))

    for task, err_msg in failed_parse_tasks:
        report.failed_count += 1
        old_ref = ",".join(task.old_ids)
        prefix = f"Old document {old_ref} preserved! Replacement aborted: " if task.old_ids else "Parse verification failed: "
        report.actions.append(
            SyncActionRecord(
                rel_path=task.rel_path,
                action="error",
                local_hash=task.local_hash,
                remote_hash=task.remote_hash,
                old_document_id=old_ref,
                new_document_id=task.new_id if task.mode != "await_existing" else "",
                detail=f"{prefix}{err_msg}",
            )
        )

    # Phase 3A: Batch-move verified documents whose folder_path differs from target folder
    folder_move_groups: Dict[str, List[_PendingDocTask]] = defaultdict(list)
    ready_to_finalize: List[_PendingDocTask] = []
    for task in verified_tasks:
        expected = task.folder_path.strip("/")
        actual = str((task.verified_doc or {}).get("folder_path") or "").strip("/")
        if expected and actual != expected:
            folder_move_groups[expected].append(task)
        else:
            ready_to_finalize.append(task)

    for folder, group_tasks in sorted(folder_move_groups.items()):
        try:
            _call_with_retry(lambda f=folder, ids=[t.new_id for t in group_tasks]: move_documents_to_folder(client, kb_id, ids, f))
            ready_to_finalize.extend(group_tasks)
        except Exception as exc:
            for task in group_tasks:
                report.failed_count += 1
                old_ref = ",".join(task.old_ids)
                prefix = f"Old document {old_ref} preserved! Folder move failed: " if task.old_ids else "Folder move failed: "
                report.actions.append(
                    SyncActionRecord(
                        rel_path=task.rel_path,
                        action="error",
                        local_hash=task.local_hash,
                        remote_hash=task.remote_hash,
                        old_document_id=old_ref,
                        new_document_id=task.new_id,
                        detail=f"{prefix}{exc}",
                    )
                )

    # Phase 3B: Finalize verified uploads & safely delete old IDs ONLY after replacement is verified & placed
    for task in sorted(ready_to_finalize, key=lambda t: t.rel_path):
        if task.mode == "await_existing":
            report.in_sync_count += 1
            report.actions.append(
                SyncActionRecord(
                    rel_path=task.rel_path,
                    action="in_sync",
                    local_hash=task.local_hash,
                    remote_hash=task.remote_hash,
                    old_document_id=task.new_id,
                )
            )
            continue

        if task.mode == "upload_new":
            report.uploaded_new_count += 1
            report.actions.append(
                SyncActionRecord(
                    rel_path=task.rel_path,
                    action="upload_new",
                    local_hash=task.local_hash,
                    new_document_id=task.new_id,
                )
            )
            continue

        # `replace_changed`: Guard passed (new version is completed & placed) -> now safe to delete old ID(s)
        try:
            op_name = "delete_replaced_old" if len(task.old_ids) == 1 else "delete_duplicate_old"
            for oid in task.old_ids:
                if oid != task.new_id:
                    _call_with_retry(
                        lambda target_oid=oid, op=op_name: client.request(
                            f"/knowledge/{quote_id(target_oid)}", method="DELETE", operation=op
                        )
                    )
            report.replaced_count += 1
            report.actions.append(
                SyncActionRecord(
                    rel_path=task.rel_path,
                    action="replace_changed",
                    local_hash=task.local_hash,
                    remote_hash=task.remote_hash,
                    old_document_id=",".join(task.old_ids),
                    new_document_id=task.new_id,
                )
            )
        except Exception as exc:
            report.failed_count += 1
            report.actions.append(
                SyncActionRecord(
                    rel_path=task.rel_path,
                    action="error",
                    local_hash=task.local_hash,
                    remote_hash=task.remote_hash,
                    old_document_id=",".join(task.old_ids),
                    new_document_id=task.new_id,
                    detail=f"New document {task.new_id} verified, but deleting old ID(s) failed: {exc}",
                )
            )

    # Case 4: Remote orphans (present in WeKnora, absent in local `dist/kb_docs/`)
    for r_rel, pairs in sorted(remote_by_rel.items()):
        for rdoc, r_hash in pairs:
            rid = rdoc["id"]
            if rid in matched_remote_ids:
                continue
            report.remote_orphan_count += 1
            if not prune_removed:
                continue
            if report.failed_count > 0:
                report.actions.append(
                    SyncActionRecord(
                        rel_path=r_rel,
                        action="prune_orphan",
                        remote_hash=r_hash,
                        old_document_id=rid,
                        detail="Skipped orphan prune because earlier upload/replace had errors.",
                    )
                )
                continue
            if dry_run:
                report.pruned_count += 1
                report.actions.append(
                    SyncActionRecord(
                        rel_path=r_rel,
                        action="prune_orphan",
                        remote_hash=r_hash,
                        old_document_id=rid,
                        detail="dry-run: would delete orphan remote document",
                    )
                )
                continue
            try:
                client.request(f"/knowledge/{quote_id(rid)}", method="DELETE", operation="prune_orphan")
                report.pruned_count += 1
                report.actions.append(
                    SyncActionRecord(
                        rel_path=r_rel,
                        action="prune_orphan",
                        remote_hash=r_hash,
                        old_document_id=rid,
                    )
                )
            except Exception as exc:
                report.failed_count += 1
                report.actions.append(
                    SyncActionRecord(
                        rel_path=r_rel,
                        action="error",
                        remote_hash=r_hash,
                        old_document_id=rid,
                        detail=f"Orphan deletion failed: {exc}",
                    )
                )

    report.actions.sort(key=lambda a: a.rel_path)
    return report
