# Operations

*[README](../README.md) · [Traditional Chinese](zh-TW/OPERATIONS.md)*

Assent uses Git worktrees to isolate changes and keep failed work reviewable.
The worktree is an audit and recovery boundary, not a security sandbox.

## Worktrees and locks

Each plan uses a worktree at:

```text
<project>.worktrees/<PLAN>/
```

The project's ignored `.assent/` directory stays in the primary worktree. A
tracked branch version of `AGENTS.md` applies when present; otherwise the
scheduler supplies the primary file's absolute path.

Only one `run` may own a plan at a time. The persistent `assent.lock` is a
diagnostic file; the live OS lock, not file existence, proves ownership. Do not
delete the file to “unlock” a plan. Different plans may run concurrently when
their dependencies allow it.

Do not write the primary Git worktree while `accept` is running. Assent's
integration lock serializes its own publication operations but cannot stop an
external Git process.

## Interruption and recovery

Assent preserves work after adapter failure, quota interruption, Ctrl+C, or a
crash. Before a role or scheduler action starts, it checkpoints dirty candidate
work. A later run gathers a dirty managed plan worktree into a `WIP` checkpoint
and resumes the persisted workflow cursor without a recovery AI session.

If an AI writes into the primary worktree, the before/after boundary check
refuses the role. Assent preserves both trees for human recovery and never
guesses how to transfer or discard those edits.

Journals retain structured events and bounded summaries, not the full raw
adapter stream. The terminal log keeps rendered session output without adding a
second scheduler prefix to every line.

Never kill a process you do not own and never manually remove a managed
worktree or temporary branch. Retain the exact paths and diagnostics, then use
the owning Assent command again or run `assent doctor`.

## Link-safe cleanup

Before recursive Git or filesystem removal, Assent inventories each directory
junction, directory symlink, and other directory reparse point, then detaches
the link object itself. The remover never traverses the link's resolved target.
External targets survive success, refusal, failure, interruption, and retry.

If inventory, ownership, or detachment cannot be proven, cleanup stops and
retains the managed path.

## Clean

```text
assent clean <PLAN>
assent clean              # all plans
```

`clean` removes only worktrees and branches proven clean, owned, and integrated.
It keeps an upstream while a direct dependent remains unfinished, unaccepted,
dirty, missing, or unproven. Multiple plans are handled upstream-first. There
is no force-delete option, and `.assent/<PLAN>/` is not removed.

## Archive

```text
assent archive <PLAN>
assent archive --all
assent archive <PLAN> --restore
```

Archive first requires the same safe-cleanup proof, then stores the management
directory in `.assent/_archive/`, records it in the archive roster, and removes the
live plan directory. A named ineligible plan is an error; `--all` skips ineligible plans.
Restore takes one plan, validates the archive, and never overwrites an existing
live plan directory.

## Temporary branches and doctor

`assent-integration/<PLAN>/<suffix>` and `assent-reconcile/<PLAN>` belong to
the transaction that created them. A surviving branch is considered orphaned
only after the repository-wide integration lock proves no transaction owns it.
Whether its tree is already published or superseded is just reporting
information.

`clean` with no plan sweeps these orphaned namespaces once per invocation;
`archive --all` uses the same sweep. A named `clean <PLAN>` deliberately does
not touch repository-wide temporary branches. `assent doctor` reports them and
offers a confirmed `[y/N]` removal after checking ownership again.

## Security boundary

With broad adapter permissions, an AI may reach anything available to its OS
identity: credentials, network services, external Git writers, and files beyond
the worktree. Assent detects project writes after the fact; it does not provide
a container or VM. Use unattended execution only with trusted repositories,
instructions, adapters, and accounts.

See [Verification](VERIFICATION.md) for candidate links and receipts, and
[Workflow](WORKFLOW.md) for acceptance and rework decisions.
