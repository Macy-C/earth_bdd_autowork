---
name: knowledge-assist
description: "Use when the user asks to export, analyze, polish, verify, upload, check status, review or accept Wiki content, or explicitly retire or clean a BDD runtime knowledge case. Local tasks do not require WeKnora."
argument-hint: "A case file or capture selector, the requested action, and a target knowledge base only for uploading"
---

# Knowledge Assist

Choose the requested action and use its direct call below. Do not load the entire
workflow to route a routine operation. Consult the relevant section of
[ai/prompts/knowledge-assist.md](../../../ai/prompts/knowledge-assist.md)
only for unresolved evidence, Wiki review, retention, or failure handling.

## Direct Calls

Use the existing confirmed Python executable; do not reread core implementations,
project secrets, or session history for routine operations.

### Local Selection, Export, And Analysis

```text
PYTHON -B -m autowork_core.knowledge scan --query "SELECTOR"
PYTHON -B -m autowork_core.knowledge build --capture-dir "SELECTED_CAPTURE" --compact
PYTHON -B -m autowork_core.knowledge verify --markdown "SELECTED.md"
PYTHON -B -m autowork_core.knowledge inspect --capture-dir "SELECTED_CAPTURE" --compact --read-only
PYTHON -B -m ai.tools.knowledge.artifact_polisher inspect --markdown "SELECTED.md" --compact --read-only
```

Only scan when selection is unresolved; an explicit file is never replaced with a
capture search or rebuild. Reuse existing exported Markdown when suitable. Add
known Feature/Scenario selectors rather than inspecting unrelated cases. Analysis
uses read-only context, not a generated polish spec; read named source/image proof
only when needed for the requested reasoning. Prose extraction is not a substitute
for execution evidence. Return the analysis or verification without uploading.

### Upload And Processing Status

```text
PYTHON -B -m autowork_core.knowledge publish --markdown "SELECTED.md" --kb "TARGET_KB"
PYTHON -B -m autowork_core.knowledge.document_sync status "KNOWN_DOCUMENT_ID" --kb "TARGET_KB"
PYTHON -B -m autowork_core.knowledge processing-status --markdown "SELECTED.md" --kb "TARGET_KB"
PYTHON -B -m autowork_core.knowledge processing-status --receipt "PUBLICATION_RECEIPT.json"
PYTHON -B -m autowork_core.knowledge status --markdown "SELECTED.md" --kb "TARGET_KB"
```

Publish already validates the artifact, checks the connection, uploads and verifies
the remote bytes. Do not run separate preflight/verify commands first unless the
user requested those independent checks. Report the returned result without an
extra status call. Tags remain a separate post-upload outcome.

Publish records progress before submission and saves a received document ID before
verification. Report `upload_outcome`, `verification_status`, `tagging_status` and
`processing` separately. For `upload_outcome_unknown` or `verification_pending`,
use the full `status` call to inspect the recorded submission without uploading,
tagging or rewriting its record. Explicitly resuming `publish` continues that
same submission, never reposts an unknown one, and retries pending tags only after
the original document is verified. Do not rebuild, rename or switch artifact bytes
to bypass an interrupted submission.

New publications use the knowledge-base root and the required lowercase
`feature:<Feature filename stem>` tag. Do not invent archive directories or
replace it with a path-based tag. When `WEKNORA_SETTING.KNOWLEDGE_BASES.ENGINEERING_CASES`
is configured in `Bdd/config.yaml`, `--kb` defaults to `ENGINEERING_CASES` and
rejects `FRAMEWORK_DOCS` or `PRODUCT_DOCS` targets. Existing receipts retain their bound document
and destination; follow the canonical workflow for legacy input and manual upload.

The second command only reads processing state; reuse a document ID already bound
to this upload and knowledge base. It is not file verification or Wiki acceptance.
The existing `autowork_core.knowledge status --markdown ...` remains the full
verification/Wiki path, with unchanged behavior. If the ID is unknown, use
`processing-status` with the selected file or receipt; it resolves the exact
publication binding itself. Do not read receipt folders or compose lookup scripts.
A receipt supplies its bound KB unless the user selects one explicitly; the
configured server must still match. Missing receipts, changed artifact bytes and
scope mismatches block without uploading or falling back to full verification.
Never guess a document ID from a similar title.

A record without a document ID reports not-submitted, rejected or unknown; a
processing-only query does not perform full reconciliation or advance the record.

### Protected File Polishing

```text
PYTHON -B -m ai.tools.knowledge.artifact_polisher inspect --markdown "SELECTED.md" --compact
PYTHON -B -m ai.tools.knowledge.artifact_polisher apply --markdown "SELECTED.md" --polish-spec "RETURNED_SPEC.json" --source-sha256 "RETURNED_HASH" --polished-by "vscode_agent" --replace "BLOCK_ID" "REVIEWED_REPLACEMENT"
```

Inspect returns source prose once; use it instead of rereading the specification
or the embedded-image Markdown. AI actually polishes the title and chapters 3/4
prose, preserving action IDs, controls, tokens, code, tables, images and provenance.
Repeat `--replace` only for changed blocks; omitted blocks keep their source text.
The program merges protected metadata, checks the inspected hash and verifies the
new copy without changing source/spec. Report the copy directly; it never uploads.
The old full-spec apply mode remains available. Capture polishing uses compact
capture context and the workflow's evidence rules, not this explicit-file adapter.

### Wiki Review And Maintenance

```text
PYTHON -B -m autowork_core.knowledge status --markdown "SELECTED.md" --kb "TARGET_KB" --review-output "NEW_REVIEW.json" --compact
PYTHON -B -m autowork_core.knowledge accept --markdown "SELECTED.md" --kb "TARGET_KB" --review "RETURNED_REVIEW.json" --review-sha256 "RETURNED_REVIEW_HASH" --check action_order --check control_attribution --check images --check inference_boundaries --notes "ACTUAL_REVIEW_FINDINGS" --compact
PYTHON -B -m autowork_core.knowledge retire --markdown "SELECTED.md" --kb "TARGET_KB" --compact
PYTHON -B -m autowork_core.knowledge clean --capture-dir "SELECTED_CAPTURE" --kb "TARGET_KB"
```

Only full verification/review uses full status. Read the saved review content once
and the selected evidence needed to assess it; compact output is not acceptance.
Pass each `--check` only after actual review, with nonempty findings. The program
constructs the bound acceptance record and rechecks current Wiki versions before
writing. Never rewrite its pages/hashes/IDs or mark checks true just to proceed.
Review, acceptance, retirement and cleanup are separate user intents. Load their
workflow rules before acceptance/retention writes; acceptance deletes nothing.
Retirement/cleanup require explicit scope and original gates. Report processing,
pending retirements and errors truthfully, without polling or blind retries.

This Skill is an optional natural-language adapter, not the owner of capture,
Markdown export, verification, publication, or retention. Those deterministic
capabilities live in [autowork_core/knowledge](../../../autowork_core/knowledge/).
Core functions and `python -B -m autowork_core.knowledge` are usable without this Skill.

Select only the actions requested by the user. Local analysis, verification, and
polishing do not require a knowledge base or network. A polish-only task ends at
the verified copy. Uploading does not imply polishing or Wiki review. Explicit
requests take precedence over filename conventions.

AI-specific protected prose editing is provided by
[artifact_polisher.py](../../../ai/tools/knowledge/artifact_polisher.py); local input
resolution is provided by [case_workflow.py](../../../ai/tools/knowledge/case_workflow.py).
Use an existing confirmed interpreter, preserve selected source files, and do
not perform environment setup, automatic installation, or unrelated maintenance.