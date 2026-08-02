# Workflow

*[README](../README.md) · [Traditional Chinese reader guide](zh-TW/WORKFLOW.md)*

This guide explains the planning, execution, review, rework, and prompt
workflow. It is the English canonical page; the [Traditional Chinese
translation](zh-TW/WORKFLOW.md) is for readers. See
[COMMANDS](COMMANDS.md) for syntax, [VERIFICATION](VERIFICATION.md) for
receipts and acceptance evidence, [CONFIGURATION](CONFIGURATION.md) for
settings, and [OPERATIONS](OPERATIONS.md) for worktree safety.

## The three-act workflow

### Act 1: planning meeting

An interactive planning session reads the project `AGENTS.md`,
`~/.assent/instructions.md`, and `~/.assent/format.md`. Discuss the goal with
the human and inspect only the source and tests that the task directly touches.
Report source bugs, bad structure, and documentation/runtime mismatches rather
than silently designing around them. Do not use subagents or overengineer.

Consensus is written into `.assent/<work folder>/` as it is reached. Each task
is a formal `tNNN_name.e.toml` file with a matching append-only
`tNNN_name.r.toml` journal. The exact fields and filename contract belong to
`~/.assent/format.md`; do not copy that contract into a project.

Before the meeting adjourns, run:

```text
assent check
```

Passing `assent check` is the adjournment condition. It validates task shape,
dependency integrity, environment, and configuration without opening an AI
session. A plan that has not passed it is not finished.

### Act 2: unattended execution

`assent run` selects a ready task, opens a headless adapter session, and gives
the session its project rules, shared session instructions, and assigned task.
The session runs the task's focused `verify` command. After it exits, Assent
checks the task-file structural diff, scope, and focused result before changing
status and recording the matching journal entry.

Successful work receives one terminal `auto(work-folder/task)` checkpoint. A
failed attempt keeps its edits and retries with the reason; exhausted retries
become a `BLOCKED` checkpoint with the work preserved. A handled quota
interruption becomes a progress-bearing WIP checkpoint and resumes with a
continue prompt after waiting or adapter rotation; the task status is set to
`WIP` before that wait unless the session explicitly wrote `BLOCKED`. The exact
provider-neutral checkpoint-resume control record is
`{"type":"assent.checkpoint_resume"}`; it requests immediate continuation
without quota or account semantics and follows the same WIP rule. When a
resumed task later passes every gate, Assent writes exactly one terminal auto
checkpoint even if the tree is clean because the WIP checkpoint already holds
the changes: that marker is an intentional empty ownership-only commit. A
normal dirty success still gets one content-bearing auto checkpoint. A clean
legacy `DONE` task backed only by an older WIP checkpoint is left untouched; the
new rule does not retroactively synthesize historical evidence and never
rewrites it. See
[Configuration](CONFIGURATION.md) for adapter behavior.

The session's focused verification is not the scheduler's full candidate
verification. Folder receipts are controlled by `[verification]
receipt_refresh`: the default `"manual"` waits for an explicit
`assent verify`, while `"auto"` refreshes a complete folder receipt at folder
closeout. `assent run --verify` is an invocation-level request and chains the
matching complete verification only after a successful run. The candidate,
receipt, and report rules are in [Verification](VERIFICATION.md).

#### Optional bounded auto-fix

If `[auto_fix.review]` is configured and the invocation states
`--auto-fix`, the completed folder enters a bounded folder-level review-and-
repair loop. The final gate order is ordinary task-focused verification, one
final run of each distinct `DONE`-task `verify` command, then the read-only
reviewer; an incomplete `--once`/`--task` run defers the loop and spends no
review token. A focused failure prevents the reviewer from starting. An
ordinary `run` without `--auto-fix` stops after its ordinary task execution and
does not run this final sweep, review, or repair.

`assent run --auto-fix` is an invocation-level authorization for the repair
half of that loop. It is orthogonal to selection and works with the implicit
folder, explicit folders, `...`, `--all`, `--once`, `--task`, and `--verify`
forms. With `--all`, every launched folder receives the same policy. With
`--verify`, complete verification still happens only after the run and bounded
loop succeed; auto-fix itself never verifies a full candidate or accepts.

A failed review is written to the folder's derived `_auto_fix.toml` state and
report. With `--auto-fix`, only findings resolved to existing tasks and their
declared scopes may trigger automatic, code-preserving rework. The scheduler
records the reason `Automatic repair of durable folder-review findings` and
`authorization: run --auto-fix`, then selects and persists the finite
fixer-profile assignments for the whole repair round before its first
write-capable session. Multi-task findings and dependency cascades therefore do
not consume the normal profile once per task. A pre-existing technical-debt
finding is eligible when it is encountered in changed or directly interacting
code, local to an existing task scope, and reliably testable; the review is not
a repository-wide debt audit. Unknown, ambiguous, or out-of-scope findings stop
for a human. No automatic task creation, source reversion, source deletion, or
acceptance occurs. Exhaustion, interruption, quota, and failed repair gates
preserve state and edits for a later `run --auto-fix` recovery or human
adjudication. Recovery refuses repair and closeout if the current
`[auto_fix.review]` is missing or its resolved reviewer identity drifted.

### Act 3: human review and decision

Start with the generated `.assent/<work folder>/_report.md`; it is the zero-token
agenda containing progress, blockers, checkpoint hashes, and verification
status. Then inspect the relevant task and journal files, the checkpoint commit
and diff, the implementation, and focused/full verification evidence.

The report's `Folder auto-fix` line is zero-token derived evidence: `NOT RUN`
means no state file, `PASSED (fresh)` and `FAILED (fresh)` show the current
review verdict, and `STALE` means malformed state or a changed source/task
binding. A `FAIL` report lists current findings, but neither that state nor a
review `PASS` is acceptance evidence.

`DONE` means that the executing AI claims the task is complete. It is not a
second review state and it is not human approval. Human approval is the
explicit `assent accept` action plus the guarded Git integration. Direct and
selected acceptance replay fresh matching receipts and do not start the full
verifier; see [Verification](VERIFICATION.md).

The review decision can be:

- accept a completed folder with `assent accept <FOLDER>` or an exact selected
  batch;
- reopen one task with `assent rework <FOLDER> <TASK>`; or
- reject the whole folder with `assent reject <FOLDER>`.

Remote synchronization, such as `git push`, is a separate human Git decision.
Cleanup with `assent clean` happens only after the source is no longer needed
and Assent can prove it is safe; see [Operations](OPERATIONS.md).

## Planning prompt

This prompt preserves the planning boundary and the required concise answer:

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

The supplied Traditional Chinese wording is kept verbatim in the reader
README and [translation](zh-TW/WORKFLOW.md). The human may replace the
numbered placeholders with the actual requirements; they are not a new task
schema field.

## Independent acceptance-review prompt

Use this prompt when asking another model or person for an independent review:

```text
Act as an independent acceptance reviewer. Answer concisely and do not use
subagents. Before changing anything, inspect the work folder's _report.md, the
relevant task and journal files, the checkpoint commit and diff, the
implementation, and the focused and full verification evidence. Report
evidence-based findings first: bugs, structural problems, overengineering,
missing tests, and documentation/runtime drift. Recommend a high-capability
model from a different vendor than the implementer for an independent
cross-review, but do not require or encode a second model or automatic gate.
This ordinary acceptance review remains human-driven: do not accept or rework
as part of the review. Wait for the human decision; only after the human agrees
should you write any Assent-format rework tasks or explain the acceptance
action. An explicit `run --auto-fix` is a separate bounded repair authorization
and still never accepts a folder.
```

The recommendation is workflow guidance only. It does not add a model field,
adapter capability, scheduler state, or mandatory multi-model mechanism.

## Folder dependencies and stacking

In a work folder's `_folder.toml`, `after = ["A"]` declares that A is an
ordering prerequisite. It does not make A's files part of B and it does not
provide same-file conflict protection. `base = "A"` is the only lineage
declaration: it makes B's source worktree start from A's commit. A folder with
no `base` starts from the current integration target, even when it has several
`after` members or several unaccepted upstreams.

At most one not-yet-accepted upstream may be in a speculative stack. A typical
sequence is:

```text
assent run A
assent run B                 # B has base = "A"
assent verify A B
assent accept A
assent accept B
```

B may be verified before A is accepted. Its receipt can be reused after A
enters the target only if the source tip, reconstructed integration tree, and
verifier digest still match. If A advances, B is stale but its work remains;
use rework/reject or open a new folder and replan it. Assent never rewrites
stack history, automatically rebases, resolves conflicts, or pushes.

If Git can merge two folders, exact-tree verification proves the result. A
conflict leaves the target unchanged for a human decision. For a source-versus-
target conflict, use `assent reconcile <FOLDER>` as described in
[Verification](VERIFICATION.md); a peer-only batch conflict follows the
`verify --batch` skip decision.

## Explicit selections

`assent run A B` runs exactly A then B in the written order. Each folder still
checks its own prerequisites, and the command stops on the first configuration
or run failure. `assent run A B --all` completes that prefix and then hands
remaining incomplete folders to the normal dependency-ordered scheduler.
Neither path verifies a full candidate or accepts anything implicitly.

Every explicitly named folder is audited before dispatch. Each name, including
the prefix before `...`, must resolve to an existing `.assent/` directory with
at least one formal task file. Any unresolved set is reported in full and
prevents every selected operation from starting. Task readiness, locks,
receipts, Git state, and other eligibility remain each command's own gates.

The literal final positional `...` is a shared remainder selector for `run`,
`verify`, `accept`, `clean`, and `archive`. It appends every remaining folder
that that command would discover, after the explicit prefix, and snapshots the
selection before mutation. It is not `--all`; it cannot be combined with
`--all`, repeated, or placed anywhere but last. `verify` and `accept` discover
finished folders, while `run`, `clean`, and `archive` discover every work
folder and apply their normal per-folder eligibility afterward. `run` retains
prefix order and dependency-orders the remainder; `verify` and `accept`
dependency-order the whole selection; `clean` uses upstream-first order.

Selection cardinality chooses the path: one folder is a folder receipt, direct
accept, or one-folder archive; two or more is an exact selected batch. A
remainder-expanded selection is still exact: selected acceptance needs evidence
for exactly that set and still never verifies. `...` is incompatible with
`verify --batch`, `verify --focus`, `run --once`, `run --task`, and
`archive --restore`. The complete command table is in [COMMANDS](COMMANDS.md).

## Rework and rejection

`assent rework <FOLDER> <TASK>` is the normal non-destructive review response.
It resets only the named task to `TODO` and keeps the code by default. If
downstream tasks have started or completed, `--cascade` is required to reopen
them too. `--reason TEXT` records the human reason. `--revert-code` is
fail-closed and creates a new reverse commit only when the target checkpoints
are a contiguous branch tail; it never rewrites history. Rework updates the
report but does not run AI automatically.

`assent reject <FOLDER>` is a separate human decision for discarding a folder's
implementation. It records branch tips, archives uncommitted changes as a WIP
commit, removes the managed worktree through the link-safe cleanup boundary,
force-deletes same-prefix branches, resets `DONE`/`WIP`/`BLOCKED` tasks to
`TODO`, and leaves full Git evidence in the journal. `SKIP` is not overturned.
It refuses while a run is in progress. See [Operations](OPERATIONS.md) before
using destructive rejection.

If review or verification finds a missing piece of the same live folder's own
objective, append a newly numbered task rather than rewriting or renumbering an
earlier task. Open a new folder for a genuinely distinct objective, an already
accepted/archived/rejected folder, or a separate dependency/base lineage.

## Language and contracts

English is canonical for tracked technical documentation and generated
scheduler text. `README.zh-TW.md` and `docs/zh-TW/` are Taiwan Traditional
Chinese reader translations; commands, paths, task IDs, JSON, configuration,
and user data stay literal. The shared contracts live only at
`~/.assent/instructions.md` and `~/.assent/format.md`. See
[TRANSLATING](TRANSLATING.md) for translation process and
[CONSENSUS](CONSENSUS.md) for design rationale.
