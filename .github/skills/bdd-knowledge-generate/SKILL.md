---
name: bdd-knowledge-generate
description: "Use when asked to generate or complete BDD Autowork Step, Page/View or data implementations for an existing Feature or Scenario using knowledge-base cases and Wiki, reusing existing locators and keeping supported new selectors local to Steps. Not for Recorder Generation Jobs, case export, polishing, publishing, or debugging and maintenance-only requests."
argument-hint: "Feature path, target Scenario/Outline/Example scope, and any supplied specification or knowledge scope"
user-invocable: true
---

# BDD Knowledge Generate

Use the existing local Coding Agent and read the complete
[canonical generation workflow](../../../ai/prompts/bdd-knowledge-generate.md)
(`startLine=1, endLine=600` in one read) and the
[project source guidance](../../../Bdd/ai/README.md) before acting, running the
static context bundler `--prepare-generation` in the same opening pass for initial
generation rather than calling `list_dir` or reading target `<step_stem>.py` /
`<step_stem>.md` files first. That workflow file is the only workflow body; this
entry does not define another generation pipeline or a new Agent. When resuming
from an existing companion `<step_stem>.md`, read `<step_stem>.md` and
`<step_stem>.py` directly and follow the workflow's resume fast-path without
re-querying knowledge bases.

Do not load Recorder-only generation instructions: in particular,
`ai/instructions/bdd-generation.md` and `ai/prompts/recorder-generate.md` are not
ordinary script-writing references. Follow the canonical workflow's relevant
framework-guide and current-API references instead.

The input is an existing Feature and a target Scenario/Outline/Example scope.
Locator files are read-only; supported new selectors stay local to the Step.
An explanation or planning-only request remains read-only. Ordinary generation
does not claim or advance a Recorder Job and does not imply product execution,
environment changes, knowledge publication, or evidence rebuilding.

Explicit invocation is supported; relevance-based discovery is not a guarantee
that the host loads the Skill for every matching request. Entry-contract checks
do not prove new-session discovery, tool integration, or real generation quality.
