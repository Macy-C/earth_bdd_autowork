---
name: document-sync
description: "Use when the user asks to refresh framework document hashes, check or synchronize docs/ with a WeKnora knowledge base preserving directory hierarchy (说明文档/ and 维护文档/), upload selected Markdown documents, confirm a directory sync or background document update, or inspect, resume or cancel an update task. Refresh, check, sync-plan, upload and update all refresh local hashes first. Ordinary upload never deletes. Confirmed directory sync moves unchanged root files into folders, uploads missing files, updates changed files (keeping old versions until verified), and optionally deletes extra remote files."
argument-hint: "检查并同步 docs/ 目录层级（缺补多删）；只检查；刷新哈希；上传；查询、恢复或取消更新任务"
---

# Document Sync

For `docs/` directory synchronization, version checks, explicit refresh, upload, or status
requests, use the fixed calls below with the confirmed interpreter. For unresolved
conflicts, recovery, or cancellation details, follow
[ai/prompts/document-sync.md](../../../ai/prompts/document-sync.md).

## Direct Calls

### Directory Sync Plan & Apply (Default Check / Sync Entry)

1. Identify the target knowledge base: if `WEKNORA_SETTING.KNOWLEDGE_BASES.FRAMEWORK_DOCS` is
   configured in `Bdd/config.yaml`, `--kb` may be omitted (it defaults to `FRAMEWORK_DOCS` and
   blocks accidental writes to `ENGINEERING_CASES` or `PRODUCT_DOCS`). Otherwise reuse the
   selected KB in this conversation, or call MCP `list_knowledge_bases` and ask if unknown.
2. Run `sync-plan` in one command. It refreshes the `文档哈希（SHA256）` header of all
   Markdown files under `docs/` (or the explicitly selected paths/subdirectories) first,
   preserving their relative directory hierarchy (`说明文档/某文档.md`, `维护文档/某文档.md`),
   and compares against the remote knowledge base without writing remote data:

```text
PYTHON -B -m autowork_core.knowledge.document_sync sync-plan --kb "TARGET_KB"
```

3. Report the structured summary and lists from `sync-plan`:
   - `in_sync`: path, folder, and hash match.
   - `to_move`: content matches (`document_sync_hash` or raw `file_hash`), only `folder_path`
     needs moving (e.g., `"" -> "说明文档"` via `POST /api/v1/knowledge/folder`).
   - `to_upload`: missing remotely; will be uploaded directly into `folder_path`.
   - `to_update`: content changed; will upload replacement into `target_folder_path`,
     mark old version with status tag, and delete the bound old ID after verification.
   - `to_delete`: extra remote documents not present under local `docs/` (only populated
     for full `docs/` sync without path filters).
   - `active_tasks` / `conflicts`: report any active background update tasks or same-name
     conflicts; conflicts block `sync-apply` until resolved.
4. All in sync: say so and stop. Explicit check-only requests stop at results.
   Otherwise ask the user once to confirm executing:
   - Full sync (`sync-apply --kb "TARGET_KB" --confirm`: move + upload + update + delete extra),
   - Sync without deleting extra remote files (`sync-apply --kb "TARGET_KB" --confirm --no-delete`), or
   - Cancel / no remote changes.
5. After explicit user confirmation, run:

```text
PYTHON -B -m autowork_core.knowledge.document_sync sync-apply --kb "TARGET_KB" --confirm
```

   (Add `--no-delete` if the user chose not to delete extra remote files.) Report moved
   documents, uploaded document IDs, started update `task_id`s, and submitted deletions.

### Other Actions

```text
PYTHON -B -m autowork_core.knowledge.document_sync refresh
PYTHON -B -m autowork_core.knowledge.document_sync refresh "SELECTED.md"
PYTHON -B -m autowork_core.knowledge.document_sync upload "SELECTED.md" --kb "TARGET_KB"
PYTHON -B -m autowork_core.knowledge.document_sync status "KNOWN_DOCUMENT_ID" --kb "TARGET_KB"
PYTHON -B -m autowork_core.knowledge.document_sync task-status "KNOWN_TASK_ID" --summary
```

Refresh without paths refreshes all Markdown files under `docs/` recursively; upload
always needs explicit scope and KB and preserves the relative folder under `docs/`
(`说明文档` or `维护文档`). `compare` and `compare-many` remain available for MCP
header-only comparisons when needed.
Task-status is read-only; do not poll, resume, or cancel as part of that query.
Summary omits history only, retaining current failures, warnings and worker state.
Omit `--summary` when the user requests history or a detailed task investigation.

Reuse [the document sync core](../../../autowork_core/knowledge/document_sync.py) and
[durable update tasks](../../../autowork_core/knowledge/document_update.py), which remain
usable as standalone CLI commands outside Copilot. Return task IDs without waiting for
parsing, and report every stage and exception truthfully (accepted uploads and accepted
deletions are not completed updates). Do not expose API keys, register persistent system
services, alter environments, or modify KB settings.