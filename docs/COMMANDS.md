# Commands

*[README](../README.md) · [Traditional Chinese](zh-TW/COMMANDS.md)*

Use `assent <command> --help` for every option. This page separates the ordinary
human workflow from planning-AI, inspection, and recovery commands, then
explains plan selection.

## Ordinary human workflow

For the common case, the human command surface is:

```text
assent init
# Hold a planning meeting. The planning AI creates the plan and runs
# assent check until it passes before ending the meeting.
assent run <PLAN>
assent test <PLAN>      # when runtime execution is explicit
assent accept <PLAN>
assent archive <PLAN>
```

`assent check` is primarily the planning contract's validation gate. Humans may
run it for diagnostics, but normally the planning AI owns it. `assent report`
is an optional review helper, not a required step. `assent archive` already
performs the same safe cleanup as `clean`, so a normal finished plan does not
need a separate `clean` first.

With whole-project scheduling, `assent run` may omit plan names.

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

| Command | Role |
| --- | --- |
| `init` | **Normal human path.** Install shared contracts/settings and create the project skeleton. |
| `run` | **Normal human path.** Execute task, plan, and integration workflows. |
| `test` | **Normal human path when required.** Run a plan's declared runtime command, or the project command against the current main candidate. |
| `accept` | **Normal human path.** Human publication decision using matching evidence. |
| `archive` | **Normal human path.** Safely clean and retire completed management records. |
| `check` | **Planning AI / diagnostics.** Validate plan files, configuration, and dependencies without AI. |
| `report` | **Optional inspection.** Regenerate the human review agenda. |
| `status` | **Optional inspection.** Show concise state for one or all plans. |
| `verify` | **Manual verification / recovery.** Run requested mechanical verification without AI review, repair, or acceptance. |
| `reconcile` | **Conflict resolution.** Prepare and finish a human-edited Git conflict resolution. |
| `rework` | **Task reopening.** Reopen existing tasks, preserving code by default. |
| `reject` | **Destructive reset.** Discard a plan's implementation after recording manual Git recovery evidence in `_reject.toml`. |
| `clean` | **Optional maintenance.** Remove proven-redundant worktrees/branches without archiving the live plan. |
| `doctor` | **Diagnostics.** Diagnose installation and recover orphaned temporary branches. |
| `ignored-dirs status` | **Diagnostics.** Inspect the current worktree's ignored-directory decision and links without changing them. |
| `ignored-dirs declare` | **AI source-role operation.** Record the reviewed decision and link only required directories. |

## Initialize a project

`assent init` installs the shared contracts and settings and creates a
fail-closed `.assent/verify.py` skeleton without asking for verification or
runtime commands. It preserves an existing project-owned verifier command block
when refreshing the framework and preserves `.assent/assent.toml` unchanged.
The planning meeting configures the verifier and plan runtime decision, and the
planning AI runs the final `assent check` until it passes before ending the
meeting.

## Run

Schedule every discovered ready plan:

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
remaining entries as not run. `execution = "disabled"` refuses this plan
command. `execution = "after_plan"` runs automatically during `assent run`, so
a separate human `test` is unnecessary in that mode. `execution = "explicit"`
is the normal reason to run `assent test <PLAN>` after `run`.

The no-`PLAN` form runs the project-layer `[runtime_test].command` from
`.assent/assent.toml` directly in the current primary working tree:

```text
assent test
```

`test` starts only the independent `runtime_test` workflow. It does not run
task, plan, integration, `full_verify`, or `accept`. The complete mode, state,
repair, quota, and source-bound evidence rules are in [Workflow](WORKFLOW.md);
the settings needed for the main command and repair role are in
[Configuration](CONFIGURATION.md).

## Optional inspection and manual verification

Regenerate the structured human review agenda when useful:

```text
assent report <PLAN>
```

Refresh one complete receipt or verify an exact selection manually:

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

## Accept, recover, and archive

The ordinary success path is:

```text
assent accept <PLAN>
assent archive <PLAN>
```

Direct and selected `accept` never verify. `accept --all` may either replay one
fresh batch receipt or, without usable batch evidence, verify and accept
eligible plans sequentially until the first failure.

If the result needs intervention instead:

```text
assent rework <PLAN> <TASK>
assent reject <PLAN>
assent reconcile <PLAN>
```

Use `rework` to keep and revise an implementation. `reject` discards the plan's
implementation after recording its worktree HEAD and branch tips in
`_reject.toml`. That journal supports only manual, best-effort Git recovery while
the commit objects remain; rerunning `reject` applies the command to the state
that remains and does not reconstruct deleted branches.

`archive` strictly contains safe cleanup: it reuses `clean`'s proof and removal
for any still-present source branch/worktree before compressing and retiring the
live plan directory. Therefore no separate `clean` is needed before archive.
Use these only when you want cleanup without archival:

```text
assent clean
assent clean <PLAN>
```

A named archive request treats an ineligible plan as an error; `--all` skips
ineligible plans. Neither cleanup nor archive has a force-delete path.

See [Workflow](WORKFLOW.md) for the full lifecycle,
[Verification](VERIFICATION.md) for receipts and conflicts, and
[Operations](OPERATIONS.md) for recovery and cleanup safety.
