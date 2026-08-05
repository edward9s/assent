# assent — an AI plan format + an automatic scheduler

*[Traditional Chinese reader edition](README.zh-TW.md)*

Assent is a file-based planning format and scheduler for long-running
projects. A human and an AI agree on a plan, the scheduler runs its task files
in isolated Git worktrees, and a human reviews the evidence before acceptance.
The management plane is `.assent/`; the source project remains ordinary Git.

## The shortest useful tour

| Stage | What happens | Main commands |
| --- | --- | --- |
| Plan | Discuss the goal, write Assent-format task files, and validate the plan. | `assent check` |
| Execute | Run focused task checks, keep WIP/checkpoint evidence, and optionally authorize a bounded folder review-and-repair loop. | `assent run`, `assent run --auto-fix` |
| Review | Read `_report.md`, inspect the diff and verification evidence, then decide whether to accept, rework, or reject. | `assent report`, `assent verify`, `assent accept` |

`DONE` is an executing AI's completion claim, not human approval. A complete
verification receipt is evidence; `accept` is the explicit human decision.
Assent never discards token-burned work: handled interruptions become WIP
checkpoints, and failed attempts remain available for retry or adjudication.

## Prerequisites and installation

You need Python 3.11+, Git, and a logged-in supported AI CLI such as `claude`
or `codex`. Assent uses only the Python standard library.

Install the published distribution:

```text
python -m pip install assent
```

Remove the published distribution when you choose:

```text
python -m pip uninstall assent
```

Uninstalling removes the Python package and the `assent` CLI entry point only.
It does not delete `~/.assent`, any project's `.assent/`, worktrees, archives,
or Git branches; data cleanup remains an explicit human choice. For an editable
source checkout, see [Configuration](docs/CONFIGURATION.md).

Check the installation with `assent --version` or `assent doctor`.

## Quick start

From the root of an existing Git project:

```text
# Install the per-user contracts/settings and the project's .assent skeleton.
assent init --test unittest

# Review ~/.assent/assent.toml, AGENTS.md, and .assent/verify.py.
# Hold a planning meeting and write tasks under .assent/<folder>/.
assent check

# Try one task, then run the remaining work unattended.
assent run --once
assent run

# Refresh complete verification evidence without an AI session.
assent verify <FOLDER>

# Read the report and inspect the checkpoint diff before this human decision.
assent report <FOLDER>
assent accept <FOLDER>

# Cleanup and retirement are separate explicit choices.
assent clean <FOLDER>
assent archive --all
```

`assent init` asks for the real project verifier on a fresh project. It can
activate parallel unittest, pytest, npm test, Flutter test, or a custom argv
command. A repeat init preserves an existing verifier, refreshes the three
user-home contracts, and adds only missing settings keys. The user-home
contracts are `~/.assent/instructions.md`, `~/.assent/format.md`, and
`~/.assent/workflow.md`; a project does not receive copies. See
[Configuration](docs/CONFIGURATION.md).

For a second terminal, use `assent status`, `assent report`, or `git log` and
`git diff` on the worktree branch. `assent run --all --jobs 2` schedules
independent folders in dependency order. A folder's `after` entries control
readiness; only an explicit `base` makes a downstream worktree start from an
upstream commit. See [Workflow](docs/WORKFLOW.md) and
[Operations](docs/OPERATIONS.md).

## Optional bounded auto-fix

Configure `[auto_fix.review]` when a project wants to override a final,
folder-level AI review-and-repair loop. With no table, `assent run --auto-fix`
resolves the first effective worker adapter at `prime`/`heavy`; no `assent init`
rerun or `~/.assent/assent.toml` edit is needed. Only an explicit
`assent run --auto-fix` starts the completed-folder review after all
task-focused checks and the final distinct focused sweep, or enters the
quiescent read-only blocked-adjudication path with durable blocker evidence, and
authorizes bounded repair for that invocation. An ordinary `assent run` without
the flag starts neither review nor repair. The flag is compatible with the
normal run selectors, including explicit folders, `...`, `--all`, `--once`,
`--task`, and `--verify`. A limited run that leaves work incomplete defers the
completed-folder loop.

The reviewer may identify a regression, an unmet requirement, or eligible
pre-existing technical debt encountered in the changed and directly
interacting code. Debt may be introduced only by `COMPLETED_FOLDER + INITIAL`,
when repair stays inside an existing task's scope and its focused tests can
verify the result; blocked adjudication and `RECHECK` may retain or resolve it
but cannot add another. This is not an unbounded repository-wide debt audit. A
failed review automatically reopens only implicated existing tasks and records a
reason-bearing rework. A completed-folder round is a merged reviewer-fixer
session: it may repair a genuine blocker directly, writing only inside the
declared scope of the one existing task its finding names, and reports that as
`FIXED`; `PASS` means nothing blocking remains and the round wrote nothing at
all. `[auto_fix.review].adapter` accepts one adapter or an ordered list of
them, and that list length is the finite round bound: each round advances a
durable round index by exactly one. Each reopened task is repaired under its
own ordinary task profile, with no escalation ladder and nothing consumed, so
an interrupted round resumes on exactly the same identity and a multi-task
finding or dependency cascade never escalates sibling tasks one at a time. A
reviewer may approve one exact scope addition, but only the scheduler edits the
task file; worker and reviewer task-file edits remain forbidden. It keeps code
by default and never creates tasks, reverts source, deletes source, or accepts
a folder. Re-review keeps still-present fingerprints, admits new findings only
for evidenced repair regression or newly exposed existing requirements, and
must PASS once the prior set clears. Optional improvements and speculation do
not keep the loop open. A round list that ends on a repair nothing confirmed
settles as `SELF-FIXED, UNREVIEWED`: every task keeps the status its own
focused gate proved, the run still succeeds, and `assent accept` asks for one
explicit human confirmation before publishing. A list that ends on an
unrepaired blocker preserves the findings and edits for
later human review; no runtime human adjudication step is inserted. A later
opted-in recovery requires that the identity which decided the pending state is
still one of the configured rounds. `_auto_fix.toml`
is derived runtime memory, not a task status or acceptance evidence; complete
verification remains a separate successful-run/receipt-policy or explicit
`--verify` stage, and its absence is never a reviewer failure. Report generation
also shows exact scope-amendment transactions and the durable review round
index. Scheduler-owned status-only transitions during rework, interruption,
repair closeout, or exhaustion do not by themselves make that evidence stale;
structural task-contract edits do. See the [Workflow](docs/WORKFLOW.md) and
[Verification](docs/VERIFICATION.md) guides.

## Planning-meeting prompt

Use this prompt as a starting point. It keeps the discussion human-led and
leaves the task schema to the installed format contract:

```text
Let's plan this project together. Please read AGENTS.md,
~/.assent/instructions.md, and ~/.assent/format.md first. Answer concisely and
do not use subagents. Discuss the goal with me before creating any plan files.
Report every source bug, bad structure, and documentation/runtime mismatch you
find. Do not overengineer. Wait for my explicit human agreement, then write
Assent-format task files under .assent/<work folder>/. Use these numbered
requirement placeholders during the discussion:
1. Requirement description.
2. Requirement description.
3. Requirement description.
Before we adjourn, run assent check.
```

Every agreed requirement should be fixed into task files as it is settled;
`assent check` passing is what allows the meeting to adjourn. The canonical
schema is the user-home `~/.assent/format.md`, not a copied project document.

## Independent acceptance-review prompt

Use this after execution when a second opinion is useful:

```text
Act as an independent acceptance reviewer. Answer concisely and do not use
subagents. Before changing anything, inspect the work folder's _report.md. If
it says TECHNICAL DEBT REVIEW REQUIRED, read _technical_debt.md, tell the human
about the flag before recommending accept, and enumerate every debt item with
an explicit sufficient-repair, follow-up-task/rework, or durable AGENTS.md-rule
disposition. Then inspect the relevant task and journal files, the checkpoint
commit and diff, the implementation, and the focused and full verification evidence. Report
evidence-based findings first: bugs, structural problems, overengineering,
missing tests, and documentation/runtime drift. Recommend a high-capability
model from a different vendor than the implementer for an independent
cross-review, but do not require or encode a second model or automatic gate.
This ordinary acceptance review remains human-driven: do not accept or rework
as part of the review. Wait for the human decision; only after the human agrees
should you write any Assent-format rework tasks or explain the acceptance
action. An explicit `run --auto-fix` is the separate, bounded review-and-repair
authorization; it still never accepts a folder.
```

The ordinary reviewer does not mutate the worktree while forming findings. The
configured auto-fix reviewer is also read-only, runs only for an explicit
`run --auto-fix`, and uses prompt-plus-detection write refusal before any repair
session. Human acceptance remains
`assent accept <FOLDER>` (or an explicitly selected batch), and human rework
remains `assent rework <FOLDER> <TASK>`.

## Topic map

The README is an entry point. Durable detail lives in five paired guides:

| Topic | English canonical guide | Traditional Chinese reader guide |
| --- | --- | --- |
| Planning, execution, review, prompts, rework | [WORKFLOW](docs/WORKFLOW.md) | [WORKFLOW — Traditional Chinese](docs/zh-TW/WORKFLOW.md) |
| Selection and CLI reference | [COMMANDS](docs/COMMANDS.md) | [COMMANDS — Traditional Chinese](docs/zh-TW/COMMANDS.md) |
| Init, settings, adapters, models, effort | [CONFIGURATION](docs/CONFIGURATION.md) | [CONFIGURATION — Traditional Chinese](docs/zh-TW/CONFIGURATION.md) |
| Focused/full verification, receipts, reconcile, acceptance evidence | [VERIFICATION](docs/VERIFICATION.md) | [VERIFICATION — Traditional Chinese](docs/zh-TW/VERIFICATION.md) |
| Worktrees, locks, concurrency, recovery, cleanup, archive | [OPERATIONS](docs/OPERATIONS.md) | [OPERATIONS — Traditional Chinese](docs/zh-TW/OPERATIONS.md) |

The English pages are canonical. The Traditional Chinese pages are reader
translations and identify English as the source of truth. The specialized
[design consensus](docs/CONSENSUS.md) and
[translation process](docs/TRANSLATING.md) keep their existing roles.

## Safety boundaries worth remembering

- Git is always required; there is no Git-less mode and no hand-maintained
  current-folder pointer. State the folder explicitly or let task facts derive
  an unambiguous selection.
- Direct and selected `accept` never start complete verification. They require
  fresh matching evidence, except for an ancestry-proven no-op. `accept --all`
  has its documented fresh-batch replay and sequential fallback modes.
- Complete verification uses a temporary integration candidate and disposable
  receipts. It mirrors tracked content plus only reviewed ignored directory
  links and ordinary ignored leaf files inside tracked directories; it never
  copies ignored trees.
- Cleanup detaches directory links before recursive removal and never traverses
  their external targets. Do not manually remove a managed worktree or branch.
- A worktree is an isolation and audit boundary, not a security sandbox:
  unattended AI still has the OS identity's access to credentials, network,
  external Git writers, and files outside the worktree.
- `[auto_fix.review]` supplies the bounded folder-review policy; only an
  explicit `run --auto-fix` starts its read-only review and finite,
  code-preserving repair loop. An ordinary `run` without the flag does neither.
  Its derived `_auto_fix.toml` state is never acceptance, and `accept` remains a
  human action.

For exact selection rules, receipt freshness, shared ignored-input review,
adapter mappings, recovery, and all command options, use the topic guides above.
