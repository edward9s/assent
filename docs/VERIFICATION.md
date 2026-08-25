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
assent verify <PLAN>     # one plan receipt
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
digest, and reviewed ignored-directory input digest. Any relevant source, candidate,
verifier, or ignored-directory input change makes it stale.

Complete plan verification refreshes `_report.md` once after the receipt
operation and all verification locks settle. The refresh is best-effort and
never changes the verification result. Focused and batch verification do not
refresh individual plan reports.

Direct and selected acceptance require a fresh matching PASS receipt, except
for an ancestry-proven already-integrated no-op. `accept --all` may replay one
fresh batch receipt atomically; without usable batch evidence, it verifies and
accepts eligible plans one at a time until a failure. A malformed batch
receipt is refused rather than ignored.

## Automatic repair

Mechanical PASS ends its workflow layer without opening an AI reviewer. Failure
may enter the next configured repair role; the following action rechecks its
work. The default repair roles combine review and repair in one session, while
custom workflows may separate them. The configured arrays are finite.

Task roles handle one task's failed check. Plan roles handle a failed cumulative
focused sweep. Integration roles handle exact-selection evidence. Conflict
evidence already names the conflicting plan and paths, so it does not require
guessed ownership. A multi-plan verifier failure without mechanical source
attribution remains a human decision.

If the finite positions end without a pass, Assent keeps all edits
and evidence and reports `REVIEW UNRESOLVED, HUMAN DECISION`. Failed mechanical
evidence still blocks acceptance.

## Conflicts and reconcile

Candidate conflicts happen before the verifier and produce no PASS receipt.
During `run`, the configured integration workflow receives the exact conflict
evidence. For a target-only conflict, Assent prepares a managed source-first
reconcile worktree. For a peer-only conflict, it identifies the conflicting
plan's persistent source worktree and supplies the compatible-prefix evidence.
The AI role edits content; Assent owns Git staging, commits, source transitions,
candidate reconstruction, and the next `full_verify` action.

The explicit manual alternative is:

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

A manual single-plan reconcile handles conflict with the current target. The
integration workflow can also repair peer-only conflicts without accepting a
prefix or changing the exact selection.

## Ignored-directory inputs

A fresh Git worktree has no ignored directories, but a project may need a large
local directory such as `assets/` or `pkg/` to compile or test. `ignored-dirs`
records which ignored directories are required source inputs. Assent links only
those required directories instead of copying every ignored tree. Ordinary
ignored leaf files beside tracked source are handled automatically.

The locations have separate responsibilities:

- The primary worktree contains the real directories and the untracked
  `.assent/_ignored-dirs.toml` decision cache.
- A managed source worktree receives same-relative Windows junctions or POSIX
  directory symlinks to those primary targets.
- The AI-only `ignored-dirs declare` operation uses its managed source worktree
  as both the declared snapshot and the destination whose links are reconciled.

Normally this is automatic. If a matching reviewed profile exists, `run`
creates the links before starting the AI session. For an `UNKNOWN` or `STALE`
decision, the session reviews the complete inventory and runs `declare` inside
its managed source worktree. That one operation validates and records the
declaration in the primary manifest and creates the links in the source
worktree, so the following focused action can start.
Running `declare` in the primary worktree is also valid, but only caches a
profile for that primary snapshot; it creates no link to itself.
Verification and reconcile apply the same profile to their managed workspaces;
they never depend on a link left behind by an earlier `run`.

Inspect either worktree without changing it:

```text
assent ignored-dirs status
```

The output identifies both worktrees, the manifest, state, matching profile,
required directories, watch files, and link agreement. In the primary worktree, links are
reported as not applicable because its ordinary directories are the targets.
It never repairs anything; an unreadable contract or a broken settled link
returns a nonzero status.

After reviewing the listed inventory, the active source role submits the
declaration from its managed worktree and names the tracked dependency or build
files whose changes should invalidate the decision:

```text
assent ignored-dirs declare --required assets --required pkg --not-required build "generated output" --watch package.lock
```

Every listed ordinary ignored directory must be covered once by `--required`
or `--not-required DIR REASON`; either may cover a subtree. Use
`--none-required` when none is a source input. Only `--required` directories
receive worktree links. Ignored leaf files remain automatic verifier inputs and
do not require classification. This command is not a general junction manager
and never copies a directory. It is not a human recovery command: use
`assent rework` and let the next `run` return the decision to the AI workflow.

The decision is cached in the primary worktree's untracked
`.assent/_ignored-dirs.toml`. A changed watch file, directory inventory, or target
makes it stale. A successful query that finds no ordinary ignored directory is
reported as `NO-IGNORED-DIRECTORY-CANDIDATE`; that describes current filesystem
evidence, not a semantic promise that the project will never need ignored-directory input.
The leading underscore marks this as Assent-owned local state; edit it only
through `ignored-dirs declare`.

Assent cannot safely link everything ignored by Git: ignore rules also cover
writable build output, caches, virtual environments, editor state, and
credentials. Linking them all would share mutable state, expose unrelated local
data, and make verification depend on stale artifacts. Never create a
source-worktree link by hand or copy an ignored directory into it. An undeclared
link invalidates verification, reporting, reconcile, and acceptance. Cleanup
detaches each provisioned link object without traversing or deleting its target.

If verifier output names a missing path inside an existing ignored directory,
Assent appends an `Ignored input diagnosis:` note pointing to the
`ignored-dirs declare` remedy without changing the original exit code.

See [Commands](COMMANDS.md) for selection syntax and
[Operations](OPERATIONS.md) for recovery safety.
