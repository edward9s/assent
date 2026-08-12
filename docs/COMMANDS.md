# Commands

*[README](../README.md) · [Traditional Chinese](zh-TW/COMMANDS.md)*

Use `assent <command> --help` for every option. This page explains which command
to choose and how folder selection behaves.

## Folder selection

An explicitly named folder must exist under `.assent/` and contain at least one
formal `.e.toml` task. Assent checks every stated name before starting any
selected operation and reports the whole unresolved set at once.

Most folder-taking commands also accept a final `...`:

```text
assent run urgent01 ...
```

This means “`urgent01`, then every remaining folder this command would normally
discover.” It is not an alias for `--all`. Expansion is snapshotted before any
change begins. `run` keeps the explicit prefix order; `verify` and `accept`
dependency-order the complete selection; `clean` works upstream-first.

One selected folder uses the single-folder path. Two or more form one exact
batch. `...` does not weaken that rule: an expanded acceptance still needs
evidence for exactly the expanded set and never starts verification.

`run`, `status`, `check`, `report`, `verify`, `clean`, `archive`, `accept`,
`reconcile`, `reject`, and `rework` accept `--config PATH` as an option on that
subcommand. It selects the project override and locates the project; it is not a
top-level global option. `init`, `doctor`, and `shared-paths` have their own
project-location rules.

## Command guide

| Command | Purpose |
| --- | --- |
| `init` | Install shared contracts/settings and create the project skeleton. |
| `check` | Validate plan files, configuration, and dependencies without AI. |
| `run` | Execute task, plan, and integration workflows. |
| `status` | Show concise state for one or all plans. |
| `report` | Regenerate the human review agenda. |
| `verify` | Run focused or complete verification without accepting. |
| `accept` | Human publication decision using matching evidence. |
| `reconcile` | Prepare and finish a human-edited Git conflict resolution. |
| `rework` | Reopen existing tasks, preserving code by default. |
| `reject` | Confirm a destructive reset after recording recoverable Git evidence. |
| `clean` | Remove only worktrees/branches proven redundant. |
| `archive` | Retire completed management records after safe cleanup. |
| `doctor` | Diagnose installation and recover orphaned temporary branches. |
| `shared-paths review` | Record reviewed ignored directories needed by tests. |

## Common choices

Run one unambiguous ready plan:

```text
assent run
```

Run a named plan, one task only, or every incomplete plan:

```text
assent run <PLAN>
assent run <PLAN> --once
assent run --all --jobs 2
```

`--once` and `--task` defer integration if the limited run leaves the plan
incomplete. A normal successful run continues through configured plan and
integration verification; it still never accepts.

Refresh one complete receipt or verify an exact selection:

```text
assent verify <PLAN>
assent verify A B
```

Run only task-focused checks, with no receipt:

```text
assent verify <PLAN> --focus
```

Dynamically verify every currently eligible plan:

```text
assent verify --batch
```

Selected verification is exact and refuses conflicts. Dynamic batch may ask
whether to verify the independent remainder after reporting conflicts.

Review and decide:

```text
assent report <PLAN>
assent accept <PLAN>
assent rework <PLAN> <TASK>
assent reject <PLAN>
```

Direct and selected `accept` never verify. `accept --all` may either replay one
fresh batch receipt or, without usable batch evidence, verify and accept
eligible folders sequentially until the first failure.

Clean or archive only when wanted:

```text
assent clean <PLAN>
assent archive <PLAN>
assent archive --all
```

A named archive request treats an ineligible plan as an error; `--all` skips
ineligible plans. Neither cleanup nor archive has a force-delete path.

See [Workflow](WORKFLOW.md) for the three stages,
[Verification](VERIFICATION.md) for receipts and conflicts, and
[Operations](OPERATIONS.md) for recovery and cleanup safety.
