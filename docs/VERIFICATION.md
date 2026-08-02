# Verification

*[README](../README.md) · [Traditional Chinese reader guide](zh-TW/VERIFICATION.md)*

This English canonical guide covers focused and complete verification, receipts,
shared ignored inputs, reconciliation, and acceptance evidence. The
[Traditional Chinese translation](zh-TW/VERIFICATION.md) follows the same
boundaries. Use [COMMANDS](COMMANDS.md) for selection syntax,
[WORKFLOW](WORKFLOW.md) for human decisions, and [OPERATIONS](OPERATIONS.md)
for worktree and cleanup recovery.

## Two verification layers

### Focused task verification

Every task's `verify` field is the command run in its source worktree after the
AI edits it. The scheduler prints the literal command as `verify: <command>`
followed by `verify passed (exit 0)` or `verify failed (exit N)`. You can rerun
that exact command from `<project>.worktrees/<FOLDER>/`.

`assent verify <FOLDER> --focus` reruns distinct `DONE`-task verification
commands in that source worktree. It creates no integration candidate and no
receipt; a pass cannot authorize acceptance. Focused verification still
classifies shared ignored inputs and reconciles their reviewed links before any
check starts.

### Folder auto-fix review gate

When `[auto_fix.review]` is configured and the invocation states
`run --auto-fix`, the completed folder performs a folder-level read-only review
after the final focused gate. The scheduler runs each distinct `DONE`-task
`verify` command once more first; a failure writes the focused finding evidence
and starts no reviewer. An incomplete `--once` or `--task` run defers the loop
and spends no review token. A folder containing only `SKIP` tasks needs no
implementation review. An ordinary `run` without `--auto-fix` starts neither
this final sweep/review nor repair, even when the policy is configured.

`run --auto-fix` is the per-invocation authorization to repair a failed review.
It is orthogonal to run selection and remains compatible with explicit folders,
`...`, `--all`, `--once`, `--task`, and `--verify`. The flag does not run a full
candidate verifier or publish a ref. A failed review without the flag is
human-adjudication evidence; with it, the scheduler validates every finding to
one existing task and declared scope, records code-preserving reason-bearing
rework, and selects a finite fixer-profile assignment for each repair round.
The round's assignments are persisted before its first repair session, so
multi-task findings and dependency cascades do not escalate one task at a time.

The review follows changed and directly interacting code and may report
pre-existing technical debt only when repair is local to an existing task's
scope and reliably tested by its focused gate. It is not a repository-wide debt
audit. Unknown, ambiguous, or out-of-scope findings stop for a human. Automatic
repair never creates tasks, changes task requirements or scope, reverts source,
deletes source, accepts work, or treats `_auto_fix.toml` as a task status.
The reviewer must not write project or management files; Assent's before/after
surface snapshot refuses any detected write and preserves the exact edits.
This is cooperative prompt-plus-detection behavior under the documented
`danger-full-access` default, not a security sandbox.

### Complete candidate verification

`assent verify <FOLDER>` creates a temporary integration candidate containing
the source result, runs the main-tree `.assent/verify.py` against that candidate,
and writes or refreshes a derived folder receipt. It consumes no AI tokens,
does not change the target ref, and does not accept anything. The verifier runs
once after candidate construction; candidate setup conflicts refuse before the
verifier and before any `PASSED` evidence exists.

`assent verify A B` is one exact selected batch: A and B are normalized to
dependency order, one candidate is built, and one batch receipt records exactly
that set. It never changes the target. `assent verify --batch` dynamically
considers finished, not-yet-integrated folders as one batch. Selected and
dynamic batch verification writes a batch receipt, not a folder receipt, and
does not refresh a folder report merely as a side effect.

The complete verifier also checks the candidate tree and the committed delta
from `HEAD` to its first parent for leftover conflict markers. Whitespace-only
differences, including line endings, trailing spaces/tabs, and blank lines at
EOF, do not block verification unless the project adds an explicit formatter
check. The generated verifier runs the real project test selected by
`assent init`, not an empty placeholder.

## Receipts and reports

A receipt is disposable derived evidence, never an independent source of truth.
It must be reproducible from the source commit identities, reconstructed
integration tree, verifier-script digest, and shared-input identity. Any source,
candidate, verifier, or reviewed-input drift makes it stale. A malformed receipt
refuses; it is not silently replaced during acceptance.

Production folder-level complete verification, including
`verify_folder_if_needed`, refreshes that folder's `_report.md` exactly once
after the receipt operation settles and all verification locks are released.
The best-effort report refresh observes `PASSED`, `FAILED`, stale replacement,
fresh reuse, malformed refusal, incomplete-folder no-op, and interrupt outcomes
without changing or masking the verification result.

The folder report also renders the derived `_auto_fix.toml` memory without
opening an AI session. No file means `Folder auto-fix: NOT RUN (no review
state)`; malformed state or a changed source/task binding is `STALE`; a fresh
review `PASS` is `PASSED (fresh)`; and a fresh `FAIL` is `FAILED (fresh)` with
its current blocking findings. The version-2 state requires `phase` and binds
the source tree, task-plan digest, review-prompt digest, and resolved reviewer
adapter/model/effort; it retains the finding ledger, observed states, and
consumed fixer profiles. Its `NEEDS_REPAIR`, `REPAIRING`, `AWAITING_REVIEW`, and
`COMPLETE` phases make restart boundaries explicit. It is deletable derived
evidence, never a receipt, task status, or acceptance gate. Repair and closeout
refuse when a pending `FAIL` has no current reviewer policy or its resolved
reviewer identity has drifted.

The configured `[verification] receipt_refresh = "manual"` (the default)
defers a folder receipt after ordinary `run` closeout. `"auto"` refreshes it
when every task in a folder is complete. `assent run --verify` is independent of
that setting and verifies only after a successful run, with the scope shown in
the command guide. A failing run verifies nothing.

## Batch conflicts and reconciliation

`assent verify --batch` remains unattended when all queued sources merge. If a
source conflict appears while building the batch, Assent still attempts every
queued folder, reports every conflicting folder and path, and excludes every
folder transitively queued after a conflict. It then asks one `[Y/n]` question:

- empty answer or `y`/`yes` verifies the smaller remaining mergeable subset and
  records only that subset in the batch receipt;
- no, an unrecognized answer, or EOF refuses before the verifier and writes no
  receipt;
- if every queued folder conflicts, there is no independent subset and the
  batch refuses without asking.

Skipping does not resolve, rebase, accept, or delete anything. A selected
`assent verify A B` conflict refuses rather than shrinking the named set. If a
peer-only conflict leaves a compatible prefix, that prefix can be verified and
accepted first; then reconcile the conflicting folder against the advanced
target. `assent rework` and `assent reject` remain explicit alternatives.

### `assent reconcile`

`assent reconcile <FOLDER>` is the human-controlled source-versus-target
conflict path. It requires a finished folder, a clean main worktree, and the
folder's source branch/worktree. It captures the current target tip, creates
`<project>.reconcile/<FOLDER>` on temporary branch
`assent-reconcile/<FOLDER>`, starts at the exact source tip, and merges the
captured target without committing. The merge is source-first, so the first
parent is the original source and the source branch can later advance onto it;
the integration target and source worktree remain unchanged.

If the merge is actually conflict-free, start reports that fact, undoes the
merge, removes only what it created, and leaves the source untouched. If the
source is already contained in the target, there is nothing to reconcile.

The human edits only the reported conflict files in the printed worktree and
runs no Git commands. `assent reconcile --continue <FOLDER>` stages exactly
the paths Git still reports unmerged, then checks that there are no unmerged
paths, conflict markers, whitespace errors from `git diff --cached --check`, or
edits outside the conflict scene. It commits the merge, fast-forwards the source
branch in its own worktree, and removes the temporary worktree and branch after
re-proving ownership. Because the source tip changed, old folder evidence and
any batch receipt that names the old source are deleted; an unparseable batch
receipt is retained for inspection.

Reconciliation writes no receipt, runs no focused task tests or complete suite,
and never accepts. The human must later run `assent verify <FOLDER>` against
the current target and then make the separate `assent accept <FOLDER>` decision.
If the target advanced after reconcile started, the captured merge is not
rewritten; later verification is authoritative.

There is no reconcile state file. Recovery inspects the managed worktree,
temporary branch, `HEAD`, `MERGE_HEAD`, and merge parents. `--continue` may
resume an already committed merge or finish its remaining fast-forward. If the
source branch, managed path, branch, or staged resolution does not match, it
refuses and preserves every edit. `--abort` removes only the proven managed
worktree and branch, and refuses while uncommitted edits remain.

## Candidate construction

The candidate is built from tracked content in the source worktrees. Complete
verification mirrors exactly two additional artifact kinds, at the root or
below tracked parents:

1. reviewed ignored directory links provisioned by Assent — Windows junctions
   or directory symlinks, and POSIX directory symlinks;
2. ordinary ignored leaf files inside an otherwise tracked directory, such as
   a generated `*.g.dart` beside its tracked source.

A mirrored directory is a link to the same resolved target. A mirrored file is
a candidate-side link to the source file (same-volume hard link on Windows,
file symlink on POSIX). Nothing is copied, no ignored directory tree is
enumerated, and no hardlink twin is prepared by hand.

Git's ignore walk prunes whole ignored trees, `.git`, `.assent`, build output,
caches, editor state, credentials, everything under a discovered link target,
and files whose parent chain is not part of the candidate's tracked tree.
Every destination must be absent from the candidate and Git-ignored there; a
mirror never replaces or shadows tracked content.

Several sources contribute a union. One path resolving to one directory target,
or one file with one content digest, is deduplicated. Verification refuses
before the verifier when it finds conflicting targets, different file contents,
a kind mismatch, ancestor/descendant overlap, a dangling or unsupported link,
an occupied destination, an unsafe parent, or an uncreatable link.

Mirrors exist only for the verifier run. They are removed deepest-first before
the temporary candidate worktree, followed only by empty parents that
provisioning created. Candidate setup and cleanup never traverses, modifies, or
deletes a linked target. Source links, files, and external targets survive
success, failure, and interruption. There is no force flag, blanket ignore
overlay, copy fallback, or project `local_inputs` setting.

## Shared ignored directories

An isolated worktree contains tracked content, so a complete check that truly
needs an ignored directory must use the reviewed shared-input handoff. The
scheduled session instructions tell the executing AI to run:

```text
assent shared-paths review --path DIR --watch FILE
```

Repeat it for additional directories/watch files, or use
`assent shared-paths review --none --watch FILE` when the review concludes that
no directory is required. This operation is the only writer of the primary
worktree's untracked `.assent/manifest.toml`. Never copy the ignored tree and
never hand-create a source link. Assent provisions the exact same-relative
primary target as a junction or directory symlink before the session.

The manifest stores whole `[shared_paths]` profiles keyed by a fingerprint of
normalized project-relative `paths`, exact tracked `watch` files, and tracked
Git-ignore rules. A source snapshot can be:

- `UNKNOWN`: no matching answer exists;
- `REVIEWED-NONE`: a matching profile has `paths = []`, which is a settled
  answer and never triggers another review;
- `REVIEWED-PATHS`: reviewed directories are provisioned to exact primary
  targets;
- `STALE`: a watched file or target changed, moved, changed type, stopped
  being ignored, or a verifier diagnosis names an undeclared directory; or
- `NO-IGNORED-DIRECTORY-CANDIDATE`: a successful primary-worktree Git query
  found no existing ordinary ignored directory outside `.git/` and `.assent/`.

The last state is a deterministic zero-token result, not a semantic claim that
the project never needs shared input. It needs no manifest profile, link, or AI
review and has a digest identity distinct from `REVIEWED-NONE`; it is
recomputed at every applicable gate. Ignored leaf files do not count, while
any ordinary ignored directory does, even if a later review answers
`paths = []`. A directory appearing later makes the next classification
`UNKNOWN` unless a matching profile already answers it. A Git ignored-entry
query error refuses rather than becoming an empty set.

If complete verifier evidence names a required directory, it is never settled
as `NO-IGNORED-DIRECTORY-CANDIDATE`: a valid existing primary target is
classified for review, and a missing or not-ignored target produces an
actionable refusal. A directory or ignore rule existing only on an unaccepted
source branch is not a provisionable primary target.

`UNKNOWN` and `STALE` append one bounded review clause to the next already
scheduled session and refuse closeout until settled. An unchanged fingerprint
uses no review tokens. A controlled review validates every value, takes one
project-local lock, and atomically replaces the manifest; interruption leaves
the previous or complete replacement profile.

Every verification entry point, including single-folder, selected, dynamic
batch, localization-prefix, `run --verify`, `--focus`, and `assent reconcile`,
classifies and reconciles shared inputs before a candidate, verifier, or managed
reconcile worktree exists. Every contributing source's ignored directory links
must equal its active profile and resolve to the exact primary targets. An
undeclared manual link is unreviewed evidence: verification, reconciliation,
receipt freshness, reporting, and acceptance refuse. Ordinary ignored leaf
files retain their separate automatic candidate-link behavior.

Folder and batch receipts bind one `shared_inputs_sha256`, snapshotted
immediately before and after the full verifier. Acceptance rechecks it before
publishing and never repairs links or invokes AI.

## Ignored-input diagnosis

If a full verifier fails on a path inside a physically present ordinary ignored
source directory, the failure keeps its verifier output and exit code and gains
one appended note:

```text
Ignored input diagnosis: <directory> is omitted from the candidate; place the
required ordinary Git-ignored target at the primary path and record it with
assent shared-paths review rather than copying it or hand-creating a link.
```

The diagnosis names only a directory the verifier output itself names and
normalizes separators; it never enumerates or traverses an ignored tree. It is
stored in whichever receipt records the failure summary and applies to
single-folder, exact selected, dynamic batch, and localization-prefix runs.

## Acceptance evidence

Direct `assent accept <FOLDER>` is explicit human approval. Unless the source is
already integrated by ancestry as an idempotent no-op, it requires fresh
`PASSED` folder evidence whose source tip, integration tree, verifier digest,
and shared-input digest reproduce exactly. `assent accept A B` likewise needs a
fresh `PASSED` batch receipt for exactly the dependency-ordered selected set.
Neither command starts complete verification, expands its selection, connects
to a remote, resolves conflicts, or writes the target after a failed gate.

`assent accept --all` is intentionally different:

1. A fresh `PASSED` batch receipt is replayed and released atomically without
   new verification, for exactly the folders in that receipt. Other finished
   folders are reported as outside it and are not silently included.
2. Missing or expired/non-`PASSED` batch evidence runs sequential
   `verify_folder_if_needed` before each not-already-integrated folder, then
   publishes successful folders in dependency order. It stops at the first
   real failure and preserves earlier publications.
3. A malformed batch receipt refuses rather than falling back.

Acceptance keeps source evidence for inspection and cleanup. It cannot stop
unrelated external Git writers, so do not run writing Git commands in the main
worktree during acceptance. Full verification is evidence; the human review
and explicit `accept` action remain separate.
