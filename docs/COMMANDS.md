# Commands

*[README](../README.md) · [Traditional Chinese](zh-TW/COMMANDS.md)*

Use `assent <command> --help` for every option. This page explains which command
to choose and how plan selection behaves.

## Plan selection

`PLAN` is a directory name directly under the project's `.assent/`, not a path:
for example, `demo` means `.assent/demo/`. It must contain at least one formal
`.e.toml` task. Assent checks every stated plan name before starting any selected
operation and reports the whole unresolved set at once.

One selected plan uses the single-plan path. Two or more form one exact batch.
Selected acceptance needs evidence for exactly the stated set and never starts
verification.

`run`, `status`, `check`, `report`, `verify`, `clean`, `archive`, `accept`,
`reconcile`, `reject`, and `rework` accept `--config PATH` as an option on that
subcommand. It selects the project override and locates the project; it is not a
top-level global option. `init`, `doctor`, and `ignored-dirs` have their own
project-location rules.

## Command guide

| Command | Purpose |
| --- | --- |
| `init` | Install shared contracts/settings and create the project skeleton. |
| `check` | Validate plan files, configuration, and dependencies without AI. |
| `run` | Execute task, plan, and integration workflows. |
| `test` | Run a plan's declared runtime command, or the project command against the current main candidate. |
| `status` | Show concise state for one or all plans. |
| `report` | Regenerate the human review agenda. |
| `verify` | Run a requested mechanical verification without AI review, repair, or acceptance. |
| `accept` | Human publication decision using matching evidence. |
| `reconcile` | Prepare and finish a human-edited Git conflict resolution. |
| `rework` | Reopen existing tasks, preserving code by default. |
| `reject` | Confirm a destructive reset after recording recoverable Git evidence. |
| `clean` | Remove only worktrees/branches proven redundant. |
| `archive` | Retire completed management records after safe cleanup. |
| `doctor` | Diagnose installation and recover orphaned temporary branches. |
| `ignored-dirs status` | Inspect the current worktree's ignored-directory decision and links without changing them. |
| `ignored-dirs declare` | AI source-role operation that records the reviewed decision and links only required directories. |

## Initialize a project

`assent init` installs the shared contracts and settings and creates a
fail-closed `.assent/verify.py` skeleton without asking for verification or
runtime commands. It preserves an existing project-owned verifier command block
when refreshing the framework and preserves `.assent/assent.toml` unchanged.
The planning meeting configures the verifier and plan runtime decision before
its final `assent check`.

## Common choices

Schedule every discovered plan:

```text
assent run
assent run --jobs 2
```

With no `PLAN`, `run` uses the whole-project dependency scheduler. `--jobs`
sets its concurrency cap and is valid only for this whole-project form.

Run an exact named selection:

```text
assent run <PLAN>
assent run A B
```

Named plans run in the stated order. Every successful run continues through
the configured plan and integration workflows for its completed selection; it
never accepts.

## Runtime test

Run the independent runtime-test workflow for one live plan:

```text
assent test <PLAN>
```

The plan form reads `.assent/<PLAN>/_runtime_test.toml` and runs its declared
`command`, which may be one string or an ordered string array, in the plan
candidate worktree. An array stops at its first failed command and records the
remaining entries as not run. `execution = "disabled"` refuses this
plan command. The no-`PLAN` form runs the project-layer
`[runtime_test].command` from `.assent/assent.toml` directly in the current
primary working tree:

```text
assent test
```

`test` starts only the independent `runtime_test` workflow. It does not run
task, plan, integration, `full_verify`, or `accept`. The complete mode, state,
repair, quota, and source-bound evidence rules are in [Workflow](WORKFLOW.md);
the settings needed for the main command and repair role are in
[Configuration](CONFIGURATION.md).

Refresh one complete receipt or verify an exact selection:

```text
assent verify <PLAN>
assent verify A B
```

Run one task check or the plan's `DONE`-task focused sweep, with no receipt:

```text
assent verify <PLAN> --focus t003
assent verify <PLAN> --focus
```

Explicit `verify` commands do not enter configured workflow roles or automatic
repair. A failure returns directly to the caller.

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
eligible plans sequentially until the first failure.

Clean or archive only when wanted:

```text
assent clean
assent clean <PLAN>
assent archive <PLAN>
assent archive --all
```

A named archive request treats an ineligible plan as an error; `--all` skips
ineligible plans. Neither cleanup nor archive has a force-delete path.

See [Workflow](WORKFLOW.md) for the three stages,
[Verification](VERIFICATION.md) for receipts and conflicts, and
[Operations](OPERATIONS.md) for recovery and cleanup safety.
