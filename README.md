# assent — plan with AI, run unattended, approve as a human

*[Traditional Chinese](README.zh-TW.md)*

Assent turns an agreed AI plan into isolated, repeatable work. You can use any
AI or other process for the planning conversation; Assent takes over once that
conversation has produced an Assent-format plan. Let `assent run` execute and
verify the work, run the declared runtime test when required, then make the
human acceptance decision.

The source remains ordinary Git. Assent keeps its plans and runtime evidence in
the project's ignored `.assent/` directory.

## Human workflow

The ordinary human-facing path is deliberately small:

| Stage | What happens | Main command |
| --- | --- | --- |
| Initialize | Install Assent's shared contracts/settings and create the project skeleton. | `assent init` |
| Plan | Discuss requirements with any AI you choose. The planning AI creates `.assent/<PLAN>/` and must run `assent check` until it passes before ending the meeting. | AI-owned `assent check` |
| Run | Let Assent implement, test, repair, and verify the plan within finite limits. | `assent run` |
| Runtime test | Run the plan's declared runtime workflow when it is `explicit`; `after_plan` runs automatically during `run`. | `assent test <PLAN>` |
| Accept | Make the human publication decision using matching evidence. | `assent accept <PLAN>` |
| Archive | Retire the finished plan. Archive performs the same safe cleanup as `clean` before compressing the plan record. | `assent archive <PLAN>` |

You normally do **not** need to run `assent check` yourself; it is primarily the
planning contract's validation gate. You also do not need a separate
`assent clean` before `archive`.

`DONE` means the execution AI believes a task is finished. A passing receipt
means the reconstructed result passed complete verification. Neither is human
approval. `assent accept` records that approval by publishing the verified
result into the current target branch; it does not push anything to GitHub.

## Install

Assent requires Python 3.11+, Git, and an installed and authenticated supported
AI CLI for unattended execution, such as Claude or Codex. Planning itself is
not tied to that choice: use whichever AI or workflow you want to reach the
requirements consensus. Assent uses only the Python standard library.

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

Run `assent init` once from the root of an existing Git project:

```text
assent init
```

Then hold the planning meeting with the AI of your choice. Assent does not
start or choose that planning AI. After you explicitly agree on the
requirements, the AI creates the Assent plan and validates it before ending the
meeting. For example:

```text
Help me plan this change. Read AGENTS.md, ~/.assent/instructions.md and
~/.assent/format.md. Do not create plan files until I explicitly agree. After I
approve the requirements, turn our consensus into an Assent-format plan under
.assent/, configure its verification and runtime decisions, and run
assent check until it passes before ending this meeting.
```

Once the meeting is finished, the normal human path is:

```text
assent run my-plan
assent test my-plan
assent accept my-plan
assent archive my-plan
```

Replace `my-plan` with the plan directory name created under `.assent/`.
`assent test my-plan` is needed when the plan declares `execution = "explicit"`.
If it declares `after_plan`, runtime testing already runs inside `assent run`;
if it declares `disabled`, there is no runtime gate.

`assent archive my-plan` first performs the same mechanical safe-cleanup proof
and source worktree/branch removal as `assent clean`, then compresses and retires
the live plan record. A separate `clean` is only useful when you want that
cleanup without archiving the plan yet.

For whole-project scheduling, omit the plan names:

```text
assent run
assent run --jobs 2
```

`assent report`, `status`, `verify`, `rework`, `reject`, `reconcile`, `clean`,
and `ignored-dirs` remain available for inspection, manual verification,
recovery, and advanced workflows. They are not extra steps in the ordinary
happy path.

`assent init` installs shared settings and three AI contracts under
`~/.assent/` and creates the project skeleton without asking for commands. Its
`.assent/verify.py` starts fail-closed. Before finishing the first live plan,
the planning AI configures its complete project-test block, chooses that plan's
`_runtime_test.toml`, adds a project runtime-test workflow when needed, and runs
`assent check` until it passes.

## What happens during `run`

At a high level, `assent run` lets configured AI roles work on the selected
plans, uses mechanical checks between repair attempts, verifies the reconstructed
result, and stops for human review rather than accepting anything automatically.

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
UNRESOLVED, HUMAN DECISION` for human review.

A failed task action stays in the task layer and advances through the remaining
configured steps. Plan review has a different job: checking whether the
cumulative implementation matches the agreed plan.

`assent test [PLAN]` is an independent runtime-test workflow. With `PLAN`, it
uses that live plan's `_runtime_test.toml` command or ordered command array in
the plan candidate. Without `PLAN`, it uses the project-layer
`[runtime_test].command` directly in the current primary working tree. A plan
using `execution = "after_plan"` runs this workflow after its plan layer and
before integration `full_verify`; `accept` never runs runtime testing. An array
stops at its first failed command; repair evidence names that command, and the
next runtime action restarts the array from the beginning.

Integration keeps the exact selected plans. Typed Git conflict evidence names
the conflicting plan and paths, so a configured integration role may repair it
in the scheduler-provided reconcile or source worktree before `full_verify`
rebuilds the candidate. A multi-plan verifier failure without mechanical source
attribution remains a human decision. Assent never drops a plan, accepts a
passing prefix, or calls `accept`.

## Documentation

- [Workflow](docs/WORKFLOW.md): planning, unattended execution, runtime testing,
  acceptance, and archive.
- [Commands](docs/COMMANDS.md): the normal human path, command roles, and
  selection rules.
- [Configuration](docs/CONFIGURATION.md): initialization, adapters, models, and
  workflow settings.
- [Verification](docs/VERIFICATION.md): focused/full checks, receipts,
  conflicts, and ignored-directory inputs.
- [Operations](docs/OPERATIONS.md): worktrees, recovery, cleanup, and archive.

English documentation is canonical. Matching Traditional Chinese guides are
provided for readers. The installed AI contracts are deliberately separate
from these human guides: `instructions.md` gives session rules, `format.md`
defines plan files, and `workflow.md` defines scheduler and acceptance behavior.

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
