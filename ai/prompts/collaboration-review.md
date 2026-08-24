# Collaboration Review

Analyze how the user and coding agents collaborate in this repository. Produce
evidence-grounded rule candidates that could make future bug fixes, feature
work, reviews, and validation more accurate and efficient.

This is a review workflow, not a rule mutation workflow. Do not modify source
code, instructions, prompts, context, user memory, Git state, or existing
knowledge. The only permitted artifact write is one new review JSON file under
`Bdd/ai/knowledge/collaboration-reviews/`.

This workflow is manual and reserved for framework maintainers. Never invoke
it from SessionStart, session counts, elapsed time, project activity, or a
proactive suggestion. The repository cannot authenticate organizational roles;
framework-maintainer authorization is an external governance responsibility.

## Input

Accept an optional review window or explicit session IDs. Default to the last
30 days for the current repository. Never widen to another repository unless
the user explicitly asks for a cross-repository review.

## Sources

1. Read the current collaboration rules before proposing changes:
   - `.github/copilot-instructions.md`
   - applicable `.github/instructions/*.instructions.md`
   - `ai/context/project.md`
   - `ai/context/continuity.md`
   - relevant canonical prompts under `ai/prompts/`
2. Use the Copilot session store read-only. Match the current repository by
   normalized `repository`, `cwd`, and `session_files`; do not rely on only one
   field. First inspect the repository's `agent_name` mix.
3. Include user-facing VS Code conversations such as `GitHub Copilot Chat`,
   `VS Code Chat`, and `panel/editAgent`. Exclude stateless exploration,
   summarization, and other subagent sessions unless the user explicitly asks
   to review subagent behavior.
4. Read actual user messages and assistant responses for candidate sessions.
   Session summaries alone are insufficient. Use checkpoints and file/ref data
   only as supporting evidence.
5. Read prior collaboration review and promotion metadata when present. Do not
  emit an unchanged candidate that was already promoted, rejected, superseded,
  or contradicted; reference the prior candidate in `supersedes` when a newer
  candidate materially revises it.
6. If the session index is unavailable or contains too little repository data,
   report that limitation. Never fabricate history or infer repeated behavior
   from Git commits alone.

## Candidate Standard

Look for collaboration behavior, not product facts. Useful categories include:

- repeated user corrections or redirections;
- implementation approaches that repeatedly required rework;
- validation steps that repeatedly caught real defects;
- successful investigation/edit/test patterns worth standardizing;
- communication, status, scope, or Git-hygiene preferences stated by the user;
- stale, duplicated, conflicting, or overly broad existing instructions.

Do not propose a new rule when the current instructions already express it
adequately. Instead omit it, or identify a precise conflict/update if the
existing wording caused repeated friction.

Evidence thresholds:

- High confidence: consistent evidence from at least 3 independent sessions,
  with no material counterexample.
- Medium confidence: evidence from at least 2 independent sessions, or an
  explicit repeated user preference plus a verified successful outcome.
- Low confidence: a single session, one-off correction, or ambiguous outcome.
  Keep it as a candidate with `target: "none"`; it is not promotion-ready.
- Architecture candidates require current code/test corroboration. Conversation
  history alone cannot establish an architecture fact or weaken a safety gate.

Treat two turns in one long session as repeated occurrences, not independent
sessions. Record counterevidence and scope limitations. Prefer revising one
existing rule over adding overlapping rules.

## Privacy And Evidence

- Store only concise evidence summaries, session IDs, and turn indexes.
- Do not copy full messages, assistant responses, source code, secrets,
  credentials, personal/product data, absolute private paths, or media content.
- Do not turn temporary task instructions, emotional wording, or a one-time
  workaround into a persistent preference.
- Do not propose weakening Request, evidence, transaction, scope, PIC,
  Plan-to-Code, test, or review validation merely to reduce friction.
- Set `repository.root` to `.`. Set `repository.remote` to `null` by default;
  when repository identity is required for a cross-repository review, store only
  a credential-free `host/owner/repository` slug with no scheme or query.

## Output

Create exactly one JSON report conforming to
`ai/context/collaboration-review.schema.json` at:

```text
Bdd/ai/knowledge/collaboration-reviews/<review-id>.json
```

Before writing, resolve the destination and prove it is a direct child of
`Bdd/ai/knowledge/collaboration-reviews/`. Capture `git status --porcelain` as a
baseline. Do not write if the path escapes, already exists, or is not covered by
the repository's ignore rules.

Build `review_id` as
`collaboration-review-<UTC YYYYMMDDTHHMMSSZ>-<8 hex>`, where the suffix is the
first 8 characters of SHA-256 over the sorted source session IDs and canonical
candidate rules. Sort candidates by confidence, independent session count,
then candidate ID. Limit the report to the 12 highest-signal candidates.

The report is advisory. Every candidate remains `status: "proposed"`. This
workflow must not create or update an instruction, prompt, context file, or
user memory.

After writing, run the deterministic validator:

```powershell
python -m autowork_core.utils.debug_tools.collaboration_review `
  validate-review <review-path> --project-root .
```

Schema validation, when available, is additional. Always run deterministic
cross-field checks for duplicate candidate IDs, evidence references, confidence
thresholds, scope/target/path mapping, deterministic review ID, path
containment, ignore status, and privacy. Re-read the saved file and confirm it
contains no raw transcript excerpts or secrets. Compare Git status to the
baseline; it must be identical because the report is ignored. Never stage,
commit, or push. If any check fails, delete only the newly created report,
report failure, and do not claim that a review was produced.

Return a short summary containing:

- report path and analyzed window;
- number of sessions and user turns analyzed;
- for each promotion-ready candidate, its ID, confidence, target, one-line
  rationale, expected benefit, possible side effects or tradeoffs, and the
  scope and persistence of the target;
- limitations and candidates withheld for insufficient evidence;
- the exact next step: run `/collaboration-promote` with the report path and
  explicitly approved candidate IDs. Do not imply that review equals approval.

Before asking whether to promote, turn each candidate's `expected_benefit` and
`risk` into a concise, decision-ready explanation. Keep it neutral and
specific: distinguish likely effects from possibilities, state meaningful
uncertainty, and mention target-specific consequences such as a user-memory
rule affecting other repositories or a repository instruction adding ongoing
context. Do not use generic benefits, exaggerate risks, or imply that promotion
is necessary.