# assent — plan with AI, run unattended, approve as a human

*[Traditional Chinese](README.zh-TW.md)*

Assent turns an agreed AI plan into isolated, repeatable work. You first confirm
requirements with an AI, then ask it to turn the agreed consensus into an
Assent-format plan. Let `assent run` execute and verify the work, then review the
evidence before explicitly accepting it.

The source remains ordinary Git. Assent keeps its plans and runtime evidence in
the project's ignored `.assent/` directory.

## The workflow

| Stage | What you do | Main command |
| --- | --- | --- |
| Plan | Agree on requirements with an AI, then explicitly ask: “Turn the consensus above into an Assent-format plan under `.assent/<PLAN>/`.” | `assent check` |
| Run | Let task, plan, and integration workflows implement, test, and repair within finite limits. | `assent run` |
| Runtime test | Run a plan's declared runtime contract or test the current main candidate. | `assent test [PLAN]` |
| Review | Read the report and diff, then accept, rework, or reject. | `assent report`, `assent accept` |

`DONE` means the execution AI believes a task is finished. A passing receipt
means the reconstructed result passed complete verification. Neither is human
approval: only `assent accept` publishes the work.

## Install

Assent requires Python 3.11+, Git, and an installed and authenticated supported
AI CLI such as Claude or Codex. It uses only the Python standard library.

```text
python -m pip install assent
```

To uninstall:

```text
python -m pip uninstall assent
```

Uninstalling removes the package and CLI only. It does not delete
`~/.assent`, project `.assent/` directories, worktrees, archives, or Git
branches. Cleanup remains an explicit choice.

## Quick start

Run these commands from an existing Git project:

```text
assent init

# First confirm requirements with the AI. After agreement, ask it to turn the
# consensus into an Assent-format plan under .assent/<PLAN>/.
assent check

# Run every discovered plan unattended.
assent run

# Explicitly run a plan runtime test, or test the current main candidate.
assent test <PLAN>
assent test

# Review before making the human decision.
assent report <PLAN>
assent accept <PLAN>

# Remove redundant worktrees or retire completed plan records when wanted.
assent clean <PLAN>
assent archive --all
```

`assent init` installs shared settings and three AI contracts under
`~/.assent/` and creates the project skeleton without asking for commands. Its
`.assent/verify.py` starts fail-closed. Before finishing the first live plan,
the planning AI configures its complete project-test block, chooses that plan's
`_runtime_test.toml`, adds a project runtime-test workflow when needed, and runs
`assent check` until it passes.

## What happens during `run`

The configured `[workflow]` has a preflight repair layer, three core layers,
and an independent runtime-test workflow:

- `preflight` runs the complete read-only check and starts its AI repairer only
  when that action fails;
- `task` works on one task and uses `focused_test` as its mechanical gate;
- `plan` runs a `focused_sweep` over the completed plan and reviews cumulative
  behavior only after all tasks are done or skipped; and
- `integration` reconstructs the exact selected result and runs `full_verify`.

A passing action completes its layer immediately. A failure may open the next
configured repair role, whose work is checked by the following action. The
default repair roles combine review and repair in one session; custom workflows
may keep those abilities in separate roles. The arrays are the complete repair
budget: Assent never invents extra rounds.
Explicit `assent check` remains read-only; only `assent run` enters the
configured preflight repair workflow.
If automation cannot decide safely, it preserves all work and reports `REVIEW
UNRESOLVED, HUMAN DECISION` for the acceptance meeting.

A failed task action stays in the task layer and advances through the remaining
configured steps. Plan review has a different job: checking whether the
cumulative implementation matches the agreed plan.

`assent test [PLAN]` is an independent runtime-test workflow. With `PLAN`, it
uses that live plan's `_runtime_test.toml` command or ordered command array in the plan candidate. Without
`PLAN`, it uses the project-layer `[runtime_test].command` directly in the
current primary working tree. A plan using `execution = "after_plan"` runs this workflow
after its plan layer and before integration `full_verify`; `accept` never runs
runtime testing. An array stops at its first failed command; repair evidence
names that command, and the next runtime action restarts the array from the beginning.

Integration keeps the exact selected plans. Typed Git conflict evidence names
the conflicting plan and paths, so a configured integration role may repair it
in the scheduler-provided reconcile or source worktree before `full_verify`
rebuilds the candidate. A multi-plan verifier failure without mechanical source
attribution remains a human decision. Assent never drops a plan, accepts a
passing prefix, or calls `accept`.

## Documentation

- [Workflow](docs/WORKFLOW.md): planning, unattended execution, and acceptance
  review.
- [Commands](docs/COMMANDS.md): selection rules and command guide.
- [Configuration](docs/CONFIGURATION.md): initialization, adapters, models, and
  workflow settings.
- [Verification](docs/VERIFICATION.md): focused/full checks, receipts,
  conflicts, and ignored-directory inputs.
- [Operations](docs/OPERATIONS.md): worktrees, recovery, cleanup, and archive.

English documentation is canonical. Matching
[Traditional Chinese guides](docs/zh-TW/WORKFLOW.md) are provided for readers.
The installed AI contracts are deliberately separate from these human guides:
`instructions.md` gives session rules, `format.md` defines plan files, and
`workflow.md` defines scheduler and acceptance behavior.

## Safety boundaries

- Assent preserves failed and interrupted work instead of reverting it.
- AI roles cannot change task contracts, scheduler state, Git state, receipts,
  or acceptance state.
- Complete verification uses a temporary integration candidate and changes no
  target ref.
- Cleanup never traverses a junction or directory symlink target.
- A worktree isolates and records changes; it is not a security sandbox.
- `reject` is destructive and asks for confirmation; use `rework` when code
  should remain in place.
- Verification never implies acceptance. The final decision remains human.
