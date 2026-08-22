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

`assent run` executes three finite workflow arrays:

- `task` works on one task; `focused_test` runs that task's command.
- `plan` works on the cumulative candidate; `focused_sweep` runs the distinct
  task commands.
- `integration` reconstructs the exact selection; `full_verify` runs the full
  project verifier outside AI sessions.

A role session that exits successfully advances one step. A passing action
completes its layer and skips later roles. A failing action records evidence and
advances. The configured arrays are the entire automation budget; Assent never
invents another review or repair round.

Role and ability names have no scheduler meaning. Abilities supply prompt text
and write authority. A writable role may repair any ordinary candidate file
needed by the stated requirements. Task contracts, journals, scheduler state,
Git, receipts, and acceptance remain scheduler-owned.

Sessions run sequentially and do not converse. The scheduler gives each session
bounded output from earlier roles and exact mechanical action evidence. There
is no structured verdict, finding ledger, owner routing, path-scope amendment,
or second repair engine.

Unknown or stale shared ignored-directory evidence adds one bounded declaration
instruction to a source role. The session reviews the complete inventory and
submits its decision through `assent shared-paths declare`; Assent validates,
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
