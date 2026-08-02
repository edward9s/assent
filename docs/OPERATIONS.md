# Operations

*[README](../README.md) · [Traditional Chinese reader guide](zh-TW/OPERATIONS.md)*

This English canonical guide covers worktrees, locks, concurrency, recovery,
cleanup, archive, and operational safety. The
[Traditional Chinese translation](zh-TW/OPERATIONS.md) follows the same
boundaries. See [WORKFLOW](WORKFLOW.md) for planning and review,
[COMMANDS](COMMANDS.md) for syntax, and [VERIFICATION](VERIFICATION.md) for
candidate and receipt details.

## Worktrees and branches

Git is always required and every work folder gets its own worktree at:

```text
<project name>.worktrees/<FOLDER>/
```

This is the isolation, conflict-management, audit, and recovery boundary. A
folder's source branch and task files remain available for review until human
acceptance and proven integration. The whole `.assent/` management plane is
ignored and stays in the primary worktree; it is never treated as a second
source of truth in a worktree. Assent refuses before a session if a management
file has entered Git.

The scheduler supplies the main-tree absolute paths for task/journal files and
the verifier. It reads a tracked `AGENTS.md` from the branch, or the supplied
main-tree path when that file is untracked. The verifier is loaded from the
main tree but executed with the candidate or worktree as its current directory.
The shared contracts are always the absolute user-home paths
`~/.assent/instructions.md` and `~/.assent/format.md`.

AI meetings occur in the primary worktree. Review every worktree without
entering it using `git worktree list`, `git log <branch>`, and
`git diff main...<branch>` (replace `main` with the actual target branch).

## Parallel execution

Separate terminals may run separate folders, for example:

```text
assent run parallel01
assent run parallel02
```

Or let `assent run --all --jobs N` schedule independent folders. The parent
process stays in the foreground and prefixes live child output as
`[work-folder] message`. The root `.assent/_assent.log` keeps only startup and
per-folder scheduling summaries; each folder's `_assent.log` appends the rendered
terminal session output without the parent scheduler's `[work-folder]` prefix.

Parallelism shares adapter quota, and integrating branches back to the main
line remains a human decision. A declared `base`, not `after`, determines
speculative content; see [Workflow](WORKFLOW.md).

## Locks and live diagnostics

Each work folder has an `assent.lock`. The file is a diagnostic record containing
the last run's PID, start time, and folder name; its existence never means a
run is currently active. The real ownership is an OS-level exclusive lock on
the open handle: `msvcrt` on Windows and `fcntl` on POSIX. Normal exit,
Ctrl+C, crashes, and forced termination release it when the handle closes.

- Do not infer activity from the file's presence; it remains after runs.
- Do not delete it to recover. Deletion creates a race and fixes nothing;
  Assent reuses it, and archive can recreate a missing diagnostic file.
- If a folder is busy, the next run refuses when it cannot acquire the real
  lock. That refusal is the signal.

`run --all` waits for and reaps every child it owns on every exit path,
including refusal and scheduling errors. A recorded PID that is still alive
can therefore identify a genuinely running process. The lock guarantee is
intended for local filesystems; `flock` and `msvcrt.locking` are unreliable on
some network filesystems.

## Interrupted execution and recovery

If the scheduler handled an interruption, it records a `WIP` checkpoint and
`assent run` resumes the task with a continue prompt. At run startup, if every
uncommitted change is provably attributable to the task that will resume (or to
one uncheckpointed `DONE` task), Assent marks that task `WIP`, records the
scope-verified recovery, gathers the edits into a `WIP` checkpoint, and
continues the recovery path without opening an AI session. If ownership is
ambiguous or any dirt is outside the task's scope, Assent keeps the dirty
worktree for human inspection and refuses to guess; inspect and checkpoint the
edits before rerunning. The lock file is not part of this recovery and should
be left alone.

The scheduler never reverts the workspace on failure. A failed review keeps
the code and retries on top of it; exhausted results are committed into a
`BLOCKED` checkpoint for human adjudication. A task's journal carries structured
events plus bounded summaries and adapter classifications, not the full raw
adapter stream. The per-folder `_assent.log` carries the rendered terminal
session output, without a parent scheduler prefix.

### Auto-fix recovery and write boundary

When `[auto_fix.review]` is configured, it supplies the policy for the bounded
loop, but only an invocation of `run --auto-fix` starts the final folder review
and authorizes repair. The review is read-only; an ordinary `run` without the
flag starts neither review nor repair. A failed review in an authorized run may
reopen existing in-scope tasks with the reason-bearing automatic rework; it
does not create tasks, revert source, delete source, or accept a folder. The
fixer-profile assignments for a repair round are written to `_auto_fix.toml`
before that round's first write-capable session, so a process failure cannot
silently make a consumed profile available again and a sibling task cannot
escalate merely because an earlier task ran.
The finding ledger, consumed profiles, WIP checkpoints, and edits survive
interruption, quota, adapter failure, and failed focused gates.

A later `run --auto-fix` resumes the existing `FAIL` state and skips consumed
profiles only when the current `[auto_fix.review]` exists and its resolved
reviewer identity matches the state. Removing or changing that policy refuses
repair and closeout. Profile exhaustion is a deliberate finite handoff to
human adjudication, not an instruction to keep retrying or undo code. The
report shows `NOT RUN`, `PASSED`, `FAILED`, or `STALE` auto-fix evidence as
derived runtime information; none of these values changes task status or
acceptance.
The reviewer's prompt-plus-detection refusal for project writes is cooperative
and runs with the documented `danger-full-access` default; it is not a security
sandbox or a preventive OS permission boundary.

### Temporary integration candidates

Complete verification creates a sibling candidate such as:

```text
<project>.integration/target-<uuid>
branch assent-integration/<folder>/<uuid>
```

The candidate exists for the entire verifier run and is cleaned in a `finally`
path after success, Python exceptions, and Ctrl+C. To inspect it while it
exists, run the main-tree verifier with that candidate as cwd; do not run the
verifier from a source worktree and call it the integration candidate.

Only a hard kill such as `taskkill /F` or power loss can leave candidate
residue. Do not use raw Git worktree removal or recursive deletion on residue.
Preserve the exact path and branch and use Assent's owning recovery/retry path,
which inventories directory links and other directory reparse points, detaches
each managed link object, re-proves ownership, and only then removes the
managed resources. If proof fails, keep the path, branch, and external targets.

## Link-safe cleanup

Assent cleanup applies to `clean`, `archive`, `reject`, reconciliation, setup
failure, and temporary candidates. A directory junction, directory symlink, or
other directory reparse point is detached as a link object before any
recursive Git or filesystem removal. The remover never traverses the link's
resolved target. External targets survive success, refusal, failure,
interruption, and retry.

If inventory, ownership, or detachment cannot be proven, cleanup refuses and
retains the managed path for an Assent-owned retry. Never pass a tree that
contains a directory link to Git or to a recursive remover, and never hand
delete a source worktree or branch.

## `clean`

`assent clean` removes only worktrees and same-folder-prefix branches that are
fully merged and clean. It never touches `.assent/`, has no force option, and
is unrelated to `git clean`.

Cleanup is upstream-first and evidence-based. Source evidence is retained
while a direct dependent is unfinished, unaccepted, dirty, missing, or not
provably integrated. `assent clean A` refuses and explains why when the proof
is insufficient. After every dependent is accepted, provably integrated, and
clean, clean upstream and then dependent with Assent.

`assent clean A B` and `assent clean A ...` process a selected set in one
upstream-first pass; bare `assent clean` keeps its all-folder discovery. The
literal remainder selection is defined in [Commands](COMMANDS.md).

## `archive`

Archive is a retirement action, not ordinary cleanup. It contains the clean
contract, compresses an eligible work folder into `.assent/_archive/`, and
registers it in `.assent/_archived.toml` (or the current roster name). A named
multi-folder archive uses the single-folder contract: every named folder is
attempted, an ineligible one causes a nonzero result, and a summary reports
what succeeded. `archive --all` skips an ineligible folder without failing the
dynamic request.

`archive --restore FOLDER` restores exactly one archived folder and accepts
neither `--all` nor `...`. Archive recovery may intentionally begin without a
live directory; the normal explicit-selection audit does not turn that
recognized restore state into a false missing-folder error.

## `reject`

Rejection is the explicit human decision to discard a folder's implementation,
not a cleanup shortcut. It requires a named folder and refuses while a run is
in progress. Before deleting branches it records each full tip hash as
recoverable Git evidence, archives uncommitted changes as a WIP commit, and
then removes the folder worktree through the link-safe boundary. It force-
deletes same-prefix branches, resets `DONE`, `WIP`, and `BLOCKED` task statuses
to `TODO`, preserves `SKIP`, and appends a `rejected` record to the journal.
The hash is recoverable only while Git retains it through its normal garbage
collection grace period.

## Acceptance and external writers

Acceptance is an explicit human action. It uses the integration lock, but that
lock cannot stop unrelated Git writers. Do not run writing Git commands in the
same primary worktree during acceptance. Assent does not connect to remote
hosting, pull, rebase, force-push, push, auto-resolve conflicts, or delete
source as an acceptance side effect. Use ordinary Git synchronization only
after the local decision and evidence are complete.

Direct and selected acceptance do not verify; the `accept --all` exception and
receipt freshness rules are in [Verification](VERIFICATION.md). Keep accepted
source evidence until dependent folders are also accepted and clean proof is
available.

## Operational security boundary

A worktree is not a security sandbox. `danger-full-access` and
`bypassPermissions` still allow an AI to reach resources available to its OS
identity, including credentials, network services, external Git writers, and
files outside the worktree. Use unattended execution only in trusted projects
and account environments. Assent does not create a container or virtual
machine, or intercept external effects.

## Related guides

- [Workflow](WORKFLOW.md) — planning, execution, review, and decisions.
- [Commands](COMMANDS.md) — selection, `...`, and command syntax.
- [Configuration](CONFIGURATION.md) — initialization and adapter settings.
- [Verification](VERIFICATION.md) — candidates, receipts, ignored inputs,
  reconcile, and acceptance evidence.
