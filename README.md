# assent — an AI plan format plus an automatic scheduler

*[Traditional Chinese reader edition](README.zh-TW.md)*

A file-based system that lets an AI work correctly on long-running projects
with minimal context, plus a scheduler that understands that system and runs
it unattended.

- **Planning**: a human and an AI hold a meeting session; consensus is
  immediately fixed into task files under `.assent/`, and adjournment =
  `assent check` passes.
- **Execution**: `assent run` finishes every task unattended — picking a
  task, opening a headless AI session, objectively reviewing the result,
  committing a git checkpoint, waiting out quota exhaustion and resuming —
  the scheduler loop itself burns zero tokens.
- **Review**: a human reads the program-generated `.assent/<work
  folder>/_report.md` (zero tokens), and opens a session only for the tasks
  that need a decision.

## Design principles

1. **Minimize token consumption while keeping output quality trustworthy.**
   Scheduling, review, and reporting are all local, pure-Python work; every AI
   session's required reading is only the project `AGENTS.md` + the assent
   working instructions + its own task file.
2. **Stay flexible, less is more.** Zero third-party dependencies (standard
   library only); the task file itself is the state — no database, no hidden
   state.
3. **Automate everything an AI can handle; humans only review and decide.**
   Humans never hand-edit files; when a review fails, they issue instructions
   for an AI to make the change.
4. **Token-burned output from the executing AI is never discarded.**
   A quota interruption is collected into a `wip` checkpoint and resumed;
   a failed review is not reverted, and retried on top of the existing
   results; once retries are exhausted, the results are committed into a
   `BLOCKED` checkpoint for human adjudication.

## How it works

```text
              ┌────────────────────────────────────────────┐
              │           main loop (zero tokens)           │
 .assent/     │  1. Scan work folders, pick a task: resume  │
 work folder ─▶     WIP first, otherwise the first TODO      │
 (tNNN_name   │     whose upstreams are all DONE/SKIP        │──▶ executing AI
  .e.toml)    │  2. Read that task's tier/effort, open a     │
              │     headless session                         │
              │  3. Objective review after the session ends: │◀── updates the task
              │     status → structural diff (tamper guard)  │     file + the
              │     → scope → verify                         │     matching
              │  4a. Pass → auto(work-folder/tNNN) checkpoint │     .r.toml log
              │      → back to 1                              │
              │  4b. Fail → keep results, retry with reason   │
              │      → still failing → mark BLOCKED, commit   │
              │      results together → back to 1             │
              │  4c. Quota exhausted → wip checkpoint →       │
              │      countdown until reset → resume with a    │
              │      "continue" prompt                        │
              └────────────────────────────────────────────┘
```

- **The task file is the state**: each task is one `tNNN_name.e.toml` file
  (status, dependencies, tier, scope, verify, acceptance conditions); its log
  is the same-stem `tNNN_name.r.toml` (append-only, not read by default).
  After a handled interruption records WIP, running `assent run` resumes that
  task. An abrupt process or host failure can instead leave a dirty worktree;
  the scheduler then refuses to guess until you review and checkpoint it.
- **Format contract**: `.assent/format.md` (installed by `assent init`) is
  what a planning AI reads to produce task files, and what the scheduler's
  parser is aligned with byte-for-byte.
- **The session is visible live**: what the AI says (`AI|`), the tools it
  uses (`Tool|`), and token usage (`--|`) print to the terminal in real time
  and are kept in `.assent/<work folder>/_assent.log`.

## Installation

Python 3.11+, git, and a logged-in Claude Code CLI (`claude`) or Codex CLI
(`codex`).

```
cd <your assent project directory>
pip install -e .
```

Verify: run `assent --help` from any directory. Zero third-party
dependencies — nothing else gets downloaded.

## Quick start

```
# 0. cd into the target project root (must be a git repo)

# 1. Generate the .assent skeleton and AGENTS.md
#    (an existing AGENTS.md only gets one bridge line appended; nothing
#    else in it is overwritten)
assent init

# 2. Fill in AGENTS.md's project description/hard constraints and
#    .assent/verify.py's actual check commands
#    Whether AGENTS.md is committed is up to the project;
#    the whole .assent/ stays in the main worktree and is not committed

# 3. Hold an AI meeting to produce task files (an interactive session; see
#    "Usage loop" below)

# 4. Validate the plan and environment (zero tokens; passing = the meeting
#    can adjourn)
assent check

# 5. Try one task, confirm it's correct, then run everything unattended
#    (can run overnight)
assent run --once
assent run

# The work folder can also be given as a positional argument
# (orthogonal to --config)
assent run <FOLDER>

# Run every incomplete folder in dependency order, at most 2 folders at once
assent run --all --jobs 2

# 6. Check in any time (a separate terminal, zero tokens)
assent status
assent report
# After human review, accept one completed folder into the current target branch
assent accept <FOLDER>
# After acceptance, optionally sync with ordinary Git (or your own AI workflow)
git push
# Once acceptance and any desired sync are complete, remove redundant artifacts
assent clean <FOLDER>

# When a review meeting orders a single task redone (keeps code by default;
# does not run automatically)
assent rework <FOLDER> <TASK> [--cascade] [--reason TEXT]

# When a review meeting rejects an entire folder's implementation
# (archives it, force-deletes it, resets tasks to TODO)
assent reject <FOLDER>
```

Human review after a run finishes:

```
git log --oneline <folder name>/<run-id>   # one commit per task, review one by one
git diff main...<folder name>/<run-id>     # or look at the overall diff
# The human decides; Assent performs the guarded local integration
assent accept <folder>
# Then choose your own ordinary Git sync, such as `git push`, or an AI you delegate to
# Reject a single task → assent rework <folder> <task>
# There is downstream work already started → add --cascade
# Confirmed you want the code reverted → add --revert-code
# Reject the whole folder's implementation → assent reject <folder>
```

`rework` immediately updates `_report.md` on success, but does not print the
full report or start an AI; only after the human confirms the reopened TODO
and its blast radius are correct should they explicitly run
`assent run <FOLDER>`.

`DONE` is the executing AI's completion claim, not human approval. A human
must first read `_report.md`, inspect the report and checkpoint evidence, and
then make the acceptance decision by calling `assent accept <FOLDER>`.
`FOLDER` is required: `accept` has no `--all`, `--push`, or `push` subcommand,
and it does not connect to remote hosting, pull, rebase, or delete source
worktrees. After a successful local acceptance, use ordinary Git commands—or
an AI workflow you operate—to synchronize as a separate decision; Assent does
not provide that workflow as a built-in feature. Run `assent clean <FOLDER>`
only when the accepted source is no longer needed and the cleanup proof is
available.

Acceptance requires the main worktree to be on its target branch and the
folder source to be complete, clean, uniquely identified, and dependency-safe.
Assent verifies the source and the integrated result, records a `--no-ff`
merge as auditable evidence, and makes a repeat acceptance idempotent. It
refuses when completion, lock, cleanliness, branch, dependency, or ambiguity
proof is insufficient. Verification failure or a merge conflict leaves the
target unadvanced; Assent never resolves conflicts, pulls, rebases, force
pushes, or claims that its integration lock can stop unrelated external Git
writers. Do not run Git commands that write the same main worktree during
`accept`; the lock only serializes Assent accept operations.

## Parallel execution

You can point N terminals at N different work folders, e.g. `assent run
parallel01`, `assent run parallel02`; or let the scheduler arrange parallel
execution across folder dependencies with `assent run --all --jobs N`. `run
--all` stays a single foreground terminal and prefixes each subprocess's
messages live as `[work folder] message`; during parallel execution the
prefix identifies each line's source.

The parent terminal shows the prefixed messages above; the root
`.assent/_assent.log` only keeps the startup header and per-folder start/
finish/failure scheduling summaries. Each work folder's own `_assent.log` is
kept by its subprocess with the full raw output, without the parent's
prefix, and is never written twice. Each folder's own tasks and logs
separately use the `tNNN_name.e.toml` and `tNNN_name.r.toml` filenames.
Every work folder has its own `assent.lock`, so only one run at a time is
allowed per folder; Git is always enabled, and every folder always gets its
own worktree at `<project name>.worktrees/<folder>/` — this is the
foundation of safe parallel processing.

The version-control boundary is deliberately simple: `AGENTS.md` is the
project rules; when tracked, the worktree's branch version is used, and when
not tracked, the prompt supplies the main-tree absolute path. The whole
`.assent/` is the assent management plane, excluded via `.gitignore` and
kept only in the main worktree. The scheduler likewise supplies
instructions, t/r files, and the default verification script as absolute
paths; the verification script is loaded from the main tree but its
execution cwd is still the worktree. Whenever any `.assent/` file has
entered Git, the scheduler fails closed before opening a session, to prevent
the worktree from ending up with a second source of truth.

AI meetings happen in the main tree. From the main tree you can review every
worktree's checkpoints directly with `git worktree list`, `git log
<branch>`, and `git diff main...<branch>`, with no need to enter the
worktree directory.

The inherent cost of parallel execution is shared quota, and merging
branches back into the main line is a human responsibility.

## The usage loop (three acts)

**Act 1: planning meeting** (interactive session)

```text
Let's start planning. Please read AGENTS.md, .assent/instructions.md, and
.assent/format.md, then discuss the following goal with me and progressively
write the consensus into task files under .assent/<work folder>/:
<your goal>
```

Every consensus reached during the meeting is immediately fixed into a task
file; before adjourning, run `assent check` — not passing means the meeting
isn't done.

**Act 2: unattended execution**: `assent run`, then go to sleep.

**Act 3: review meeting** (interactive session)

First read `_report.md` yourself (it is the agenda: progress, BLOCKED
sticking points, checkpoint hashes), then open a session only for the tasks
that need a decision:

```text
Please read .assent/<folder>/t003_xxx.e.toml, t003_xxx.r.toml, and the diff
of the commit auto(<folder>/t003) at <hash>, explain the sticking point, and
propose a fix.
```

Carrying out the decision means the AI edits the task file (status back to
TODO, added clarification, new tasks, marked SKIP); once `assent check`
passes, go back to Act 2. `DONE` remains an execution claim until a human
reviews the report and calls `assent accept <FOLDER>`. That command performs
the safe local `--no-ff` integration; remote synchronization remains a
separate ordinary Git decision, and `assent clean <FOLDER>` is the final
optional cleanup. A new round
of planning = just open a new work folder; an old folder can keep taking
part in dependency resolution via `_folder.toml`'s `after`. A folder's
completion is derived from its task files — it is complete only once every
task is DONE/SKIP.

## Command reference

The full form of `run`, `status`, `check`, and `report` is
`assent <command> [options] [FOLDER]`. `FOLDER` may be stated explicitly;
when omitted, `run` derives the single runnable folder from current task
state and `_folder.toml`'s `after` upstreams, and refuses on ambiguity.
`status`, `check`, and `report` act on all folders when `FOLDER` is omitted.
`--config PATH` selects the config file, defaulting to
`.assent/assent.toml`; the config file no longer maintains a work-folder
pointer. The two are orthogonal — use either alone or together, e.g.
`assent status --config configs/night.toml parallel01`.

`assent accept <FOLDER>` is the explicit human acceptance action for one
completed folder. It requires the current main worktree branch to be the
target, verifies both source and integrated result, and records a guarded
`--no-ff` merge with evidence. It refuses incomplete, locked, dirty,
ambiguous, or dependency-unsafe folders; verification failure or conflict
does not advance the target. It never connects to a remote, pulls, rebases,
force pushes, resolves conflicts, or deletes the source. The integration lock
serializes Assent accepts, but cannot atomically stop external Git writers;
do not run writing Git commands in the same main worktree during acceptance.
Re-running after success is idempotent.

`assent clean [FOLDER]` only deletes worktrees and branches that are fully
merged and clean; when it cannot prove that, it skips the folder. It never
touches `.assent/`, has no force option, and is unrelated to `git clean`.

`assent reject <FOLDER>` is the explicit human-adjudicated rejection action,
kept separate from routine cleanup: it first archives uncommitted changes as
a wip commit, prints each branch's full tip hash as evidence (recoverable by
hash only within git's gc grace period), then force-deletes that folder's
worktree and same-prefix branches, resets DONE/WIP/BLOCKED tasks back to
TODO, and leaves a `rejected` record with full Git evidence in the r file
(SKIP is not overturned). `FOLDER` is required and cannot act on all
folders; it refuses while a run is in progress.

`assent rework <FOLDER> <TASK>` is the non-destructive reopening of a single
task. By default it keeps all code and only resets the target status to
TODO; downstream tasks that have started or completed require an explicit
`--cascade` to be reverted along with it. `--reason TEXT` preserves the
adjudication reason. `--revert-code` is fail-closed: it creates a new
reverse commit only when the target's checkpoints form a contiguous tail of
the current branch, and it never rewrites Git history. On success it
regenerates the report but does not run `run` automatically; a failed
precheck, status update, or report regeneration all return failure.

Two old settings have been removed: the work folder is no longer maintained
by a hand-edited config pointer, and Git has no disable switch or git-less
degraded mode; the work folder is stated explicitly on the command line or
derived from task-file facts, and Git is always enabled.

| Command and a representative invocation | Options and effect | Token cost |
|---|---|---|
| `assent run [FOLDER]`<br>`assent run parallel01` | Runs a work folder until every task is DONE/BLOCKED/SKIP. Omitting `FOLDER` derives the single runnable folder; `--once` stops after the next task; `--task ID` runs a single task while still checking its upstreams, e.g. `assent run --task t003 parallel01`. | Only spent while an AI session runs; `--once` or `--task` run at most one task |
| `assent run --all`<br>`assent run --all --jobs 2` | Runs every incomplete folder in `_folder.toml` dependency order; `--jobs N` caps how many folders run at once (default 1), with the parent terminal live-tagging each subprocess's output as `[folder] message`. Cannot combine with `FOLDER`, `--once`, or `--task`. | Only spent while an AI session runs |
| `assent status [FOLDER]`<br>`assent status parallel01` | Shows progress statistics, the next task, the branch, and the last checkpoint. Accepts `--config PATH`. | **Zero** |
| `assent check [FOLDER]`<br>`assent check --config .assent/assent.toml parallel01` | Validates task-file format, dependency-cycle freedom, config, and environment; this is the planning meeting's adjournment condition. Accepts `--config PATH`. | **Zero** |
| `assent report [FOLDER]`<br>`assent report parallel01` | Generates and displays the work folder's human-readable report `_report.md`. Accepts `--config PATH`. | **Zero** |
| `assent accept <FOLDER>`<br>`assent accept parallel01` | Records the human's acceptance decision for exactly one completed folder. Requires a target branch in the current main worktree; verifies source and integrated result, creates an auditable `--no-ff` merge, and is idempotent. Refuses insufficient completion, lock, cleanliness, branch, dependency, or ambiguity proof; conflicts and verification failures do not advance the target. No `--all`, `--push`, remote connection, pull, rebase, force push, conflict resolution, or source deletion. | **Zero** |
| `assent clean [FOLDER]`<br>`assent clean parallel01` | Cleans up only worktrees and same-folder-prefix branches that are fully merged and clean; skips anything it cannot prove, never touches `.assent/`, and has no force option. Acts on all work folders when `FOLDER` is omitted. | **Zero** |
| `assent reject <FOLDER>`<br>`assent reject parallel01` | Human-adjudicated rejection: archives uncommitted changes, then force-deletes that folder's worktree and same-prefix branches (recording full tip hashes before deletion), and resets DONE/WIP/BLOCKED tasks to TODO with Git evidence kept in the r file. `FOLDER` is required; refuses while a run is in progress. | **Zero** |
| `assent rework <FOLDER> <TASK>`<br>`assent rework parallel01 t003 --cascade --reason "review rejected"` | Non-destructively reopens a single task; keeps code by default, `--cascade` states downstream propagation explicitly. `--revert-code` creates a new reverse commit only when checkpoints form a contiguous tail. Updates the report on success, does not run automatically. Accepts `--config PATH`. | **Zero** |
| `assent init`<br>`assent init --path C:\work\my-project` | Generates the `.assent` skeleton and `AGENTS.md` in the target project; `--path DIR` defaults to the current directory. Does not accept `FOLDER` or `--config`. | **Zero** |

Each subcommand's `-h`/`--help` shows that layer's actual syntax; there is
no top-level `--config` or other global option that applies to every
subcommand.

## Plan format and config files

- Full format contract: [assent/templates/format.md](assent/templates/format.md)
  (copied into a project's `.assent/format.md` by `assent init`).
- Working-instructions template: [assent/templates/instructions.md](assent/templates/instructions.md)
  — assent session behavior and cross-project common rules; project rules
  stay in `AGENTS.md`.
- Config template: [assent/templates/assent.toml](assent/templates/assent.toml)
  — adapter selection, the abstract tier (prime/core/lite) mapping table,
  abstract effort (low/medium/high) defaults and CLI-value translation,
  watchdog, and retry parameters.

## FAQ

**Q: Does status / check / report consume tokens?**
No. Only sessions that run an AI consume tokens; the scheduler never feeds
any file content to a model — the executing AI reads task files with its own
tools.

**Q: What if I lose power or crash midway?**
Inspect the isolated worktree first. If the interruption was handled and the
task was left at WIP, `assent run` resumes it with a "continue" prompt. An
abrupt failure can leave uncommitted changes; in that case the scheduler
refuses the dirty worktree instead of guessing, so review and checkpoint the
changes before rerunning.

**Q: What if the executing AI edits its task file to loosen its own review?**
Three layers of defense: the scope exemption covers only its own
`tNNN_name.e.toml` task file and `tNNN_name.r.toml` log; any field of the
task file other than status being changed fails the review (compared field
by field against the checkpoint version); `check` validates deps integrity
and cycle-freedom every round.

**Q: Does a BLOCKED task block all progress?**
Only tasks that depend on it as an upstream; other tasks continue as usual.
`_report.md` lists every sticking point and its last log entry.

**Q: How do I plug in an AI CLI other than Claude / Codex?**
Subclass `Adapter` and implement a two-step interface.
`resolve_model(model: str) -> str` first translates the task file's abstract
tier into this run's actual `--model` value, `requested_model`; the engine
then translates the abstract effort into `requested_effort` per the config
file, and calls the existing `run_task(prompt, requested_model,
requested_effort, cwd) -> TaskResult`. An adapter does not define a separate
effort-translation method — it only uses the actual CLI value it is handed.
`TaskResult` carries `exit_code`, `output`, `quota_exhausted`, and
`reset_at`; quota detection is encapsulated inside the adapter, and the main
loop is unaware of vendor differences.

## Project status

The core is complete: the TOML task/log format, eight subcommands, the
claude and codex adapters, and a full unittest suite (runs with no network
and no real CLI). Design consensus is recorded in
[docs/CONSENSUS.md](docs/CONSENSUS.md).
