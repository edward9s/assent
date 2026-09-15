# Workflow

*[README](../README.md) · [Traditional Chinese](zh-TW/WORKFLOW.md)*

The ordinary human path is: initialize once, hold a planning meeting with any AI
you choose, run the resulting plan, run its explicit runtime test when required,
accept it, then archive it. The planning AI owns the final `assent check`; the
human does not normally run that validation step separately.

Each execution AI session reads only the material needed for its stage.

## 1. Planning meeting

Start in the primary worktree. The planning AI reads `AGENTS.md`,
`~/.assent/instructions.md`, and `~/.assent/format.md`. It reads
`~/.assent/workflow.md` only when changing workflow settings or checking exact
scheduler behavior, and inspects relevant source and tests as needed.

Confirm requirements before writing plan files. After explicit human agreement,
the planning AI creates `.assent/<PLAN>/tNNN_name.e.toml` tasks. Each task states
behavior and a focused verification command; it does not predict a write scope.
Before the meeting ends, the AI configures `.assent/verify.py` for the agreed
completed project, using the test runner's greatest safe parallelism, and creates
the plan's `_runtime_test.toml`. The commands may name tests or probes that the
plan will create; planning does not run them. Use `disabled` only when the plan
needs no runtime gate, never because its command is unknown.

Planning prompt:

```text
Let's plan this change together. Read AGENTS.md,
~/.assent/instructions.md, and ~/.assent/format.md. Answer concisely and do
not use subagents. Inspect relevant source and tests. Report source bugs, bad
structure, and documentation/runtime mismatches. Do not overengineer. Confirm
the requirements first; create no files before I explicitly agree. After I
agree, turn the consensus above into an Assent-format plan under
.assent/<PLAN>/, configure its complete verification and runtime decisions,
and run assent check until it passes before ending this meeting.
```

The plan is runnable only after `assent check` passes. In the normal workflow,
this is the planning AI's final validation gate. The command remains available
to humans for diagnostics, but it is not an extra human step after the meeting.

## 2. Unattended execution

After planning, the human normally starts execution with `assent run` or an
explicit `assent run <PLAN>` selection.

`assent run` first executes the plan's preflight array, then the task, plan, and
integration arrays, with the independent runtime-test array inserted where a
plan requires it:

- `preflight` runs the complete read-only `check`; failure may enter a
  declarative repair role and then recheck.
- `task` works on one task; `focused_test` runs that task's command.
- `plan` works on the cumulative candidate; `focused_sweep` runs the distinct
  task commands.
- `integration` reconstructs the exact selection; `full_verify` runs the full
  project verifier outside AI sessions.

A role session that exits successfully advances one step. A passing action
completes its layer and skips later roles. A failing action records evidence and
advances. The configured arrays are the entire automation budget; Assent never
invents another review or repair round.

### Preflight repair workflow

The installed `~/.assent/assent.toml` strictly alternates `check`,
`preflight_repairer`, and `check`. A passing first action starts no AI. Failure
supplies the complete diagnostic to the repairer, whose declarative edits are
accepted only when the next read-only check passes. The repairer cannot change
task status, workflow cursors, evidence, receipts, Git, candidate source, or
acceptance. Successful repair evidence appears in the plan journal and report.

Explicit `assent check` remains a read-only command and never enters the
workflow. A configuration failure that prevents Assent from resolving the
preflight role or any sendable adapter remains a manual bootstrap failure.

### Independent runtime-test workflow

The installed `~/.assent/workflow.md` owns this runtime-test contract; this
guide summarizes how to use it.

`assent test [PLAN]` is separate from task, plan, integration, `full_verify`, and
`accept`.

Main discovery starts with the file created by `assent init`:

```toml
# .assent/_runtime_test.toml
execution = "pending"
```

Running `assent test` then follows this finite sequence:

| Step | Observable result |
| --- | --- |
| `runtime_test` action | Does not start and is not recorded as `FAILED`. |
| writable runtime role | Inspects the implemented primary tree and may create a small probe. It proposes only `explicit` plus a command. |
| scheduler | Restores the role's direct control-file edit, validates it, then installs the proposal. |
| next `runtime_test` action | Runs the installed command from the beginning. |

A successful proposal looks like:

```toml
# .assent/_runtime_test.toml
execution = "explicit"
command = "python tools/runtime_probe.py"
```

An invalid proposal is refused. If no role supplies one, the finite workflow
ends unresolved. `assent check` remains read-only and never starts discovery.

Plan contracts use the same filename inside the plan directory but have three
different modes:

| `.assent/<PLAN>/_runtime_test.toml` | Effect |
| --- | --- |
| `execution = "disabled"` | No `command` and no runtime gate. |
| `execution = "explicit"` plus `command` | Run with `assent test <PLAN>`. |
| `execution = "after_plan"` plus `command` | `assent run` runs it after the plan workflow and before integration `full_verify`. |

For example, an explicit plan follows:

```text
assent run demo
assent test demo
assent accept demo
```

`after_plan` omits the middle command. `accept` never runs runtime testing; it
rechecks required source-bound runtime evidence before publication.

The default repair loop is visible directly in the shared configuration:

```toml
runtime_test = [
  { action = "runtime_test" },
  { role = "runtime_repairer" },
  { action = "runtime_test" },
  { role = "runtime_repairer" },
  { action = "runtime_test" },
  { role = "runtime_repairer" },
  { action = "runtime_test" },
]
```

If a command array fails, the trace is:

```text
runtime_test       first nonzero command -> FAILED; later commands not run
runtime_repairer   edits ordinary source, tests, fixtures, config, or docs
runtime_test       restarts the command array from its first command
```

Only the action records `PASSED`, `FAILED`, or `STALE`; role prose cannot pass a
test. After a real command failure, a role that changes no source ends
unresolved. The pending main-contract proposal and a completed ignored-input
decision are the two cases that may advance without a tracked-source change.
Per-step adapter examples are in [Configuration](CONFIGURATION.md).

| Target | Contract | Workflow state | Working tree |
| --- | --- | --- | --- |
| current main | `.assent/_runtime_test.toml` | `.assent/_runtime_test_workflow.toml` | primary tree |
| live plan | `.assent/<PLAN>/_runtime_test.toml` | `.assent/<PLAN>/_runtime_test_workflow.toml` | plan candidate |

Runtime roles never run commands or change Git, journals, receipts, task
contracts, scheduler state, or acceptance state. Quota interruption preserves
their edits and resumes the saved workflow position. Exhaustion reports
`REVIEW UNRESOLVED, HUMAN DECISION`: standalone `assent test [PLAN]` returns 1,
while unattended `run` returns 0 so unrelated plans continue. Runtime evidence
is not a verification receipt; `full_verify` remains separate evidence.

Role and ability names have no scheduler meaning. Abilities supply prompt text
and write authority. A writable role may repair any ordinary candidate file
needed by the stated requirements. Task contracts, journals, scheduler state,
Git, receipts, and acceptance remain scheduler-owned.

Sessions run sequentially and do not converse. The scheduler gives each session
bounded output from earlier roles and exact mechanical action evidence. There
is no structured verdict, finding ledger, owner routing, path-scope amendment,
or second repair engine.

Unknown or stale ignored-input evidence adds one bounded declaration
instruction to a source role. The session reviews the complete inventory and
submits its decision through `assent ignored-inputs declare`; Assent validates,
records, and applies it. This operation is the only writer of the local manifest.
The following action does not start until the decision is settled; no directory
is copied or linked by hand.

Integration failures may advance to a configured integration role. Typed Git
conflict evidence names the conflicting plan and paths; Assent supplies a
managed reconcile worktree for a target-only conflict or that plan's persistent
source worktree for a peer-only conflict, then rebuilds the exact candidate. A
multi-plan verifier failure without mechanical source attribution remains a
human decision.

If a finite array ends without a pass, all edits and evidence remain. The
result is `REVIEW UNRESOLVED, HUMAN DECISION` with exit zero, so unrelated queued
plans may continue. Infrastructure failure, a refused precondition, or a broken
safety gate remains nonzero.

Interruptions and quota waits checkpoint dirty candidate work. A later run
resumes the persisted cursor and worktree; it does not discard token-burned
output.

## 3. Human acceptance

Review the implementation, diff, verification evidence, and any required
runtime-test result. `assent report <PLAN>` is an optional helper that
regenerates a structured review agenda; it is not a mandatory step before
acceptance.

When a structured review is useful:

```text
assent report <PLAN>
```

Inspect `_report.md`, task requirements, relevant journals, the source diff,
and verification evidence. Use an independent AI when a second opinion helps,
but keep the decision human-owned.

Acceptance review prompt:

```text
Act as an independent acceptance reviewer. Answer concisely and do not use
subagents. Read AGENTS.md and the Assent contracts, then inspect this plan's
_report.md, relevant task and journal files, source diff, implementation, and
verification evidence. Report evidence-based bugs, unmet requirements, missing
tests, harmful complexity, and documentation/runtime mismatches first. This is
human-driven: do not accept, rework, or edit anything. Wait for the human
decision.
```

The ordinary success path ends with:

```text
assent accept <PLAN>
assent archive <PLAN>
```

`accept` publishes receipt-backed work into the current target branch and never
runs verification or runtime testing itself. `archive` is the normal final
maintenance action after accepted work is no longer needed as a live plan.

When the result needs intervention instead, the human may choose:

- `assent rework <PLAN> <TASK>` reopens an existing task while preserving code.
- `assent reject <PLAN>` is a confirmed destructive reset: it checkpoints dirty
  edits, records the worktree HEAD and every branch tip in the plan-level
  `_reject.toml`, removes managed worktrees and same-prefix branches, then resets
  started tasks to `TODO`. The hashes support manual recovery only while Git
  retains the commit objects; rerunning `reject` neither reads this journal nor
  reconstructs deleted branches.
- `assent reconcile <PLAN>` handles a Git conflict that requires human-edited
  reconciliation.

No workflow step accepts a plan. Verification supplies evidence; `accept` is
the human publication decision.

## 4. Archive finished work

`assent archive <PLAN>` strictly contains the safe cleanup performed by
`assent clean`: it first proves and removes any redundant managed source
worktree/branch, then compresses `.assent/<PLAN>/`, records the archive, and
retires the live plan directory.

Therefore the ordinary workflow does not run `clean` before `archive`.
Use `assent clean` separately only when you want to remove proven-redundant
source worktrees/branches while deliberately keeping the live plan record.

## Dependencies and stacked work

`after` controls readiness. Only `base` allows one unaccepted upstream tip in a
downstream stack. Without `base`, the plan starts from the current integration
target. If an upstream changes, preserve downstream work and use rework,
rejection, or a new plan instead of rewriting history.

See [Commands](COMMANDS.md), [Configuration](CONFIGURATION.md),
[Verification](VERIFICATION.md), and [Operations](OPERATIONS.md).
