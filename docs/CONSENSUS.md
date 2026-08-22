# Design principles

*[Traditional Chinese](zh-TW/CONSENSUS.md) · [README](../README.md)*

This page explains why Assent is structured as it is. It is not an executable
contract; current behavior belongs to the installed `format.md` and
`workflow.md` contracts and the source code.

## Minimal context, explicit control boundaries

Each AI should receive the smallest context that lets it act correctly:

- `AGENTS.md` contains durable project rules and points to Assent contracts;
- `instructions.md` contains short rules shared by AI sessions;
- `format.md` teaches a planning or review AI how plan files work;
- one `.e.toml` task contains the concrete execution contract;
- role prompts describe the current responsibility and permission; and
- reports, journals, diffs, and test output provide current evidence.

Human guides explain how to use the system but are not hidden dependencies of
AI execution.

## Mechanical gates before judgment

Task contracts, dependencies, protected control files, Git state, focused
tests, candidate construction, and complete verification are checked
mechanically. AI judgment is used only at configured role positions within a
finite workflow. A passing check never spends a reviewer session merely to
confirm the machine result.

## Preserve work, fail closed

Assent keeps edits and evidence across failure and interruption. When it cannot
prove a control boundary, Git identity, or safe state transition, it stops
instead of guessing or reverting. Questions that genuinely require human
judgment are reported as such and do not cancel unrelated queued work.

## Human acceptance

Task `DONE`, AI review, and a passing verification receipt are evidence, not
approval. Publication remains one explicit human action: `assent accept`.

## Plain Git underneath

Worktrees isolate concurrent plans and make recovery auditable, but do not form
a security sandbox. Assent does not hide Git lineage, invent a current-plan
pointer, infer speculative bases, or push to remotes. Cleanup is separately
guarded and link-safe.

## Maintenance rule

Keep one canonical owner for each rule. Change source, contract, tests, and
reader documentation together when behavior changes. Remove historical detail
once it no longer helps a current user or maintainer make a decision.
