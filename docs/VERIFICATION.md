# Verification

*[README](../README.md) · [Traditional Chinese](zh-TW/VERIFICATION.md)*

Assent separates fast task checks from complete candidate verification. This
keeps execution economical without letting a focused pass stand in for the
result a human will publish.

## Focused checks

Every task has a narrow `verify` command. `focused_test` runs it for one task;
`focused_sweep` runs each distinct command for a completed plan. Neither writes
a receipt.

You can run one task's command or repeat the completed plan's distinct commands:

```text
assent verify <PLAN> --focus t003  # focused_test
assent verify <PLAN> --focus       # focused_sweep
```

A named task is run regardless of its current status. The sweep includes only
`DONE` tasks and de-duplicates identical commands.

A focused pass proves only the tested source worktree. It cannot authorize
`accept`.

## Complete verification

`full_verify` and `assent verify` build a temporary integration candidate from
the selected source commits, then run the project's `.assent/verify.py` there.
They do not change the target ref or accept anything.

Every explicit `assent verify` form is mechanical: it does not enter configured
workflow roles, start an AI session, or automatically repair a failure.

```text
assent verify <PLAN>     # one folder receipt
assent verify A B        # one exact batch receipt
assent verify --batch    # dynamically discovered batch
```

An exact selection succeeds only as the full set. If `A B C` was requested,
Assent cannot verify `A C` and claim success for `A B C`. A passing prefix may
help diagnose a failure but cannot authorize the original request.

Dynamic `--batch` is different because the command itself discovers eligible
plans. When candidate construction finds conflicts, it reports every conflict
and affected dependent, then may ask whether to verify only the independent
remainder. Any resulting receipt names only what was actually verified.

## Receipts

A receipt is deletable evidence, not source truth. It records enough identity
to reproduce the result: selected source commits, reconstructed trees, verifier
digest, and reviewed shared-input digest. Any relevant source, candidate,
verifier, or shared-input change makes it stale.

Complete folder verification refreshes `_report.md` once after the receipt
operation and all verification locks settle. The refresh is best-effort and
never changes the verification result. Focused and batch verification do not
refresh individual folder reports.

Direct and selected acceptance require a fresh matching PASS receipt, except
for an ancestry-proven already-integrated no-op. `accept --all` may replay one
fresh batch receipt atomically; without usable batch evidence, it verifies and
accepts eligible folders one at a time until a failure. A malformed batch
receipt is refused rather than ignored.

## Automatic repair

Mechanical PASS ends its workflow layer without opening an AI reviewer. Failure
may enter the next configured reviewer/fixer; the following action rechecks its
work. The configured arrays are finite.

Task repair handles one task's failed check or `BLOCKED` evidence. Plan repair
handles a failed cumulative focused sweep. Integration repair handles a failed
complete verifier or candidate conflict. These responsibilities do not borrow
one another's budget.

If the finite positions end with an unresolved finding, Assent keeps all edits
and evidence and reports `REVIEW UNRESOLVED, HUMAN DECISION`. Failed mechanical
evidence still blocks acceptance.

## Conflicts and reconcile

Candidate conflicts happen before the verifier and produce no PASS receipt.
Automatic integration can repair them inside the configured finite workflow,
then rebuild and verify the same exact selection.

For manual repair:

```text
assent reconcile <PLAN>
# edit only the reported conflict paths
assent reconcile <PLAN> --continue
assent verify <PLAN>     # or repeat the original exact/batch verification
```

Reconcile creates a managed source-first worktree. You edit file content;
Assent owns staging, commits, ref updates, validation, and cleanup. `--continue`
refuses unresolved paths, conflict markers, whitespace errors, or unrelated
edits. It advances the source, not the target, so fresh verification is still
required. `--abort` removes only clean, re-proven resources.

A single-plan reconcile handles conflict with the current target. A peer-only
conflict between selected plans needs the compatible predecessors accepted
first, an explicit rework/reject decision, or the automatic integration repair
path.

## Shared ignored inputs

A fresh Git worktree has no ignored directories, but a project may need a large
local directory such as `assets/` or `pkg/` to compile or test. Assent exposes
only reviewed directories instead of copying every ignored tree. Ordinary
ignored leaf files beside tracked source are handled automatically.

The locations have separate responsibilities:

- The primary worktree contains the real directories and the untracked
  `.assent/manifest.toml` review cache.
- A managed source worktree receives same-relative Windows junctions or POSIX
  directory symlinks to those primary targets.
- `shared-paths review` uses the worktree where it is run as the source snapshot
  being reviewed and as the destination whose links are reconciled.

Normally this is automatic. If a matching reviewed profile exists, `run`
creates the links before starting the AI session. For an `UNKNOWN` or `STALE`
decision, the session runs `review` inside its managed source worktree. That one
operation records the profile in the primary manifest and creates the links in
the source worktree, so its focused test can continue.
Running `review` in the primary worktree is also valid, but only caches a
profile for that primary snapshot; it creates no link to itself.
Verification and reconcile apply the same profile to their managed workspaces;
they never depend on a link left behind by an earlier `run`.

Inspect either worktree without changing it:

```text
assent shared-paths status
```

The output identifies both worktrees, the manifest, state, matching profile,
paths, watch files, and link agreement. In the primary worktree, links are
reported as not applicable because its ordinary directories are the targets.
It never repairs anything; an unreadable contract or a broken settled link
returns a nonzero status.

When review is required, run it in the managed source worktree and name the
tracked dependency or build files whose changes should invalidate the decision:

```text
assent shared-paths review --path assets --path pkg --watch package.lock
```

Use `--none` when review concludes that no ignored directory is required. The
decision is cached in the primary worktree's untracked
`.assent/manifest.toml`. A changed watch file or target makes it stale. A
successful query that finds no candidate directory is reported as
`NO-IGNORED-DIRECTORY-CANDIDATE`; that describes current filesystem evidence,
not a semantic promise that the project will never need shared input.

Assent cannot safely link everything ignored by Git: ignore rules also cover
writable build output, caches, virtual environments, editor state, and
credentials. Linking them all would share mutable state, expose unrelated local
data, and make verification depend on stale artifacts. Never create a
source-worktree link by hand or copy an ignored directory into it. An undeclared
link invalidates verification, reporting, reconcile, and acceptance. Cleanup
detaches each provisioned link object without traversing or deleting its target.

If verifier output names a missing path inside an existing ignored directory,
Assent appends an `Ignored input diagnosis:` note pointing to the
`shared-paths review` remedy without changing the original exit code.

See [Commands](COMMANDS.md) for selection syntax and
[Operations](OPERATIONS.md) for recovery safety.
