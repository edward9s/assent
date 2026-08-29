# Workflow

*[README](../README.md) · [Traditional Chinese](zh-TW/WORKFLOW.md)*

Assent has three human-facing stages: agree on a plan, run it unattended, and
review the result. Each AI session reads only the material needed for its stage.

## 1. Planning meeting

Start in the primary worktree. Read `AGENTS.md`,
`~/.assent/instructions.md`, and `~/.assent/format.md`. Read
`~/.assent/workflow.md` only when changing workflow settings or checking exact
scheduler behavior. Inspect relevant source and tests as needed.

Confirm requirements before writing plan files. After explicit human agreement,
create `.assent/<PLAN>/tNNN_name.e.toml` tasks. Each task states behavior and a
focused verification command; it does not predict a write scope.

Planning prompt:

```text
Let's plan this change together. Read AGENTS.md,
~/.assent/instructions.md, and ~/.assent/format.md. Answer concisely and do
not use subagents. Inspect relevant source and tests. Report source bugs, bad
structure, and documentation/runtime mismatches. Do not overengineer. Confirm
the requirements first; create no files before I explicitly agree. After I
agree, turn the consensus above into an Assent-format plan under
.assent/<PLAN>/ and run assent check.
```

The plan is runnable only after `assent check` passes.

## 2. Unattended execution

`assent run` executes the task, plan, and integration arrays, with the
independent runtime-test array inserted where a plan requires it:

- `task` works on one task; `focused_test` runs that task's command.
- `plan` works on the cumulative candidate; `focused_sweep` runs the distinct
  task commands.
- `integration` reconstructs the exact selection; `full_verify` runs the full
  project verifier outside AI sessions.

A role session that exits successfully advances one step. A passing action
completes its layer and skips later roles. A failing action records evidence and
advances. The configured arrays are the entire automation budget; Assent never
invents another review or repair round.

### Independent runtime-test workflow

The installed `~/.assent/workflow.md` owns this runtime-test contract; this
guide summarizes how to use it.

`assent test [PLAN]` is separate from the task, plan, and integration layers. A
plan argument reads that live plan's `_runtime_test.toml` and runs its command
or ordered command array
in the plan candidate worktree. Without a plan argument, `assent test` uses the
project-layer `[runtime_test].command` directly in the current primary working
tree. It does not dispatch `full_verify`, write a verification receipt, or
accept anything.

The plan contract selects one exact `execution` mode: `disabled` has no runtime
gate, `explicit` runs only when `assent test PLAN` is requested, and
`after_plan` runs automatically in `run` after the plan workflow and before the
selection's integration `full_verify`. Every `after_plan` source must pass its
own current runtime gate before that full verification starts. Acceptance
rechecks the same source-bound runtime evidence; `accept` never runs runtime
testing.

`[workflow].runtime_test` is a finite linear array of
`{ action = "runtime_test" }` steps and writable repair roles. The project
template strictly alternates action, `runtime_repairer`, and action. The
For an array, the scheduler stops at the first nonzero exit or launch failure
and records completed, failed, and not-run entries for the repair role. After a
repair, the next action restarts at the first entry because the source changed.
The runtime action is the authority: every entry exiting 0 records `PASSED`, a
nonzero exit records `FAILED`, and source or command-list drift records `STALE`. Role output cannot
declare a pass. A successful repair role that makes no working-tree source change
ends the workflow unresolved; no extra action is invented. This source-change
requirement applies only after a runtime command actually failed. A plan runtime
role that settles an injected ignored-directory precondition may advance without
changing tracked source, and the next action then evaluates the command. Main
runtime commands run directly in the primary working tree and do not use this
precondition.

Runtime role sessions may edit ordinary source, tests, fixtures, project
configuration, and documentation in the current working tree. They do not run commands or
change task contracts, journals, scheduler state, receipts, Git, or acceptance
state. Runtime state records the workflow cursor, bounded evidence, candidate
identity, and quota waits. Quota interruption checkpoints the candidate and
resumes that state on restart; it never reverts token-burned work. Exhaustion
reports `REVIEW UNRESOLVED, HUMAN DECISION` with preserved evidence. A standalone
`assent test [PLAN]` returns 1; an unattended `run` returns 0 for this human-
decision outcome so unrelated queued plans continue.

Plan runtime state is `.assent/<PLAN>/_runtime_test_workflow.toml` beside the
plan contract. Main runtime state is `.assent/_runtime_test_workflow.toml`; its
commands and repairs operate directly in the primary working tree, where edits
remain for ordinary Git review. Runtime evidence is not a verification receipt:
`full_verify` and its receipt remain separate evidence, and acceptance requires
both fresh receipt evidence and any required current runtime gate.

In a worktree-backed source workflow, an unsettled ignored-directory decision
means the action did not start; Assent records that gate evidence separately
from test results. A later configured action runs again after FAILED evidence.
Only matching PASSED evidence is reused to finish interruption recovery.

Role and ability names have no scheduler meaning. Abilities supply prompt text
and write authority. A writable role may repair any ordinary candidate file
needed by the stated requirements. Task contracts, journals, scheduler state,
Git, receipts, and acceptance remain scheduler-owned.

Sessions run sequentially and do not converse. The scheduler gives each session
bounded output from earlier roles and exact mechanical action evidence. There
is no structured verdict, finding ledger, owner routing, path-scope amendment,
or second repair engine.

Unknown or stale ignored-directory evidence adds one bounded declaration
instruction to a source role. The session reviews the complete inventory and
submits its decision through `assent ignored-dirs declare`; Assent validates,
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

## 3. Acceptance review

Start with:

```text
assent report <PLAN>
```

Inspect `_report.md`, task requirements, relevant journals, the source diff,
and verification evidence. Use an independent AI when a second opinion helps,
but keep the decision human-owned.

Acceptance-review prompt:

```text
Act as an independent acceptance reviewer. Answer concisely and do not use
subagents. Read AGENTS.md and the Assent contracts, then inspect this plan's
_report.md, relevant task and journal files, source diff, implementation, and
verification evidence. Report evidence-based bugs, unmet requirements, missing
tests, harmful complexity, and documentation/runtime mismatches first. This is
human-driven: do not accept, rework, or edit anything. Wait for the human
decision.
```

The human then chooses one explicit action:

- `assent accept <PLAN>` publishes receipt-backed work.
- `assent rework <PLAN> <TASK>` reopens an existing task while preserving code.
- `assent reject <PLAN>` is a confirmed destructive reset: it checkpoints dirty
  edits, records branch tips, removes managed worktrees and same-prefix branches,
  then resets started tasks to `TODO`.

No workflow step accepts a plan. Verification supplies evidence; `accept` is
the human publication decision.

## Dependencies and stacked work

`after` controls readiness. Only `base` allows one unaccepted upstream tip in a
downstream stack. Without `base`, the plan starts from the current integration
target. If an upstream changes, preserve downstream work and use rework,
rejection, or a new plan instead of rewriting history.

See [Commands](COMMANDS.md), [Configuration](CONFIGURATION.md),
[Verification](VERIFICATION.md), and [Operations](OPERATIONS.md).
