# assent — an AI plan format plus an automatic scheduler

*[Traditional Chinese reader edition](README.zh-TW.md)*

A file-based system that lets an AI work correctly on long-running projects
with minimal context, plus a scheduler that understands that system and runs
it unattended.

- **Planning**: a human and an AI hold a meeting session; consensus is
  immediately fixed into task files under `.assent/`, and adjournment =
  `assent check` passes.
- **Execution**: `assent run` finishes every task unattended — picking a
  task, opening a headless AI session, running its focused verification,
  committing a git checkpoint, waiting out quota exhaustion and resuming. Once
  a folder is complete, the scheduler runs the full candidate verification
  outside the AI session; the scheduler loop itself burns zero tokens.
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

# 6. Under the default [verification] receipt_refresh = "manual", run closeout
#    leaves no receipt behind, and accepting directly is refused with a
#    prompt to verify first. Refresh it explicitly while away (zero tokens):
#    verifying several finished folders as one candidate costs one full
#    verification instead of one per folder
assent verify --batch
# Or refresh just one folder's receipt
assent verify <FOLDER>
# Set receipt_refresh = "auto" instead if you want run closeout to refresh
# the receipt itself

# 7. Check in any time (a separate terminal, zero tokens), then review
assent status
assent report
# After human review, accept every finished folder in dependency order
assent accept --all
# Or accept just one completed folder into the current target branch
assent accept <FOLDER>
# After acceptance, optionally sync with ordinary Git (or your own AI workflow)
git push
# Once acceptance and any desired sync are complete, remove redundant artifacts
assent clean <FOLDER>
# Once a folder is no longer needed, retire its plan into _archive/
assent archive --all

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
The receipt is the scheduler's complete-verification evidence; `accept` is the
human approval. `assent verify <FOLDER>` is the zero-token way to refresh a
missing or stale receipt without changing the target or opening an AI session.
`FOLDER` is required: `accept` has no `--all`, `--push`, or `push` subcommand,
and it does not connect to remote hosting, pull, rebase, or delete source
worktrees. After a successful local acceptance, use ordinary Git commands—or
an AI workflow you operate—to synchronize as a separate decision; Assent does
not provide that workflow as a built-in feature. Run `assent clean <FOLDER>`
only when the accepted source is no longer needed and the cleanup proof is
available. Do not clean an upstream source branch while a dependent remains
unaccepted; if its source worktree or branch disappears, a later `accept`
refuses rather than using passive merge metadata as a state database.

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

### Bounded optimistic stacking

Set a downstream folder's `_folder.toml` to `after = ["A"]` to declare A as an
ordering prerequisite. `base = "A"` declares that the downstream files are
built on A's commit and makes its worktree a complete checkout of that commit;
a non-`base` `after` upstream guarantees order only, not file content or
same-file conflict protection. Without a declared `base`, a folder starts from
the current integration target; the number and acceptance state of `after`
members do not affect that base selection, and multiple unaccepted upstreams
do not create a base ambiguity or refusal.

For example: `run A` -> `run B` stacked on A -> combined verification ->
human `accept A` -> human `accept B`. B's receipt may be created before A is
accepted and reused after A enters the target when source tip, integration
tree, and verifier digest still match; `accept` does not rerun the complete
suite. If A advances, B is stale but its work is retained: rework or reject B,
or open a new folder and replan it. Assent never rewrites stack history.

The same rule applies when A and B edit the same file. Git may merge changes
automatically and exact-tree verification proves the result; a conflict leaves
the target unchanged for human resolution. Assent does not automatically
rebase, resolve conflicts, or push.

Cleanup is upstream-first and evidence-based. Source evidence is retained
while a direct dependent is unfinished, unaccepted, dirty, missing, or not
provably integrated; `assent clean A` refuses and explains why. After every
dependent is accepted and provably integrated and clean, clean upstream and
then dependent with `assent clean`; never manually delete worktrees or branches.

### Interactive conflict skipping in `verify --batch`

A conflict-free `assent verify --batch` stays fully unattended. Building the
batch candidate is where a source conflict is discovered, and it is never
treated as a verification failure: every queued folder's merge is still
attempted, so one folder conflicting does not stop a later, independent
folder from being tried too. When one or more folders conflict, `verify
--batch` reports every conflicting folder with its conflicting path(s),
reports every folder queued `after` a conflicting one as excluded with it
(transitively, rather than verified without the upstream it depends on), and
then asks a single `[Y/n]` question offering to skip that whole excluded set
and verify only the remaining, still-mergeable folders.

- **Yes** (an empty answer or `y`/`yes`): runs one full verification over the
  smaller subset and records only those verified folders in the batch
  receipt; every skipped folder is left out entirely, not attempted.
- **No, an unrecognized answer, or EOF** (a non-interactive caller with no one
  to ask): `verify --batch` stops before running the full verifier and writes
  no receipt, same as any other refusal.
- **Every queued folder conflicts**: there is nothing independent left to
  offer, so the batch refuses outright without asking.

Skipping is not resolving, rebasing, accepting, or deleting anything — the
target and every source folder, skipped or merged, are left exactly as they
were. The conflicting folder's own source still needs a human decision
through `assent rework` or `assent reject` before it can rejoin a batch.

`assent accept --all` publishes exactly the receipt's own folders in one
atomic ref update, then reports — in that same run — every other finished
folder the receipt does not cover (one that finished too late to be built
into the batch, or that the batch was built without) without verifying or
accepting them: there is no second prompt and no same-run fallback to the
folder-by-folder path for that leftover set. Run `assent verify --batch`
again to build the next batch over what remains.

`assent archive --all` only archives a folder that is independently eligible
(complete, and either its source is already gone or `clean`'s own mechanical
proof can remove it); it retains the source evidence, and skips archiving,
for any folder whose source an unaccepted dependent still needs, the same
upstream-first rule `clean` enforces.

### Resolving one folder's conflict with `assent reconcile`

`assent verify --batch` can only skip a conflicting folder; it cannot resolve
it. `assent reconcile FOLDER` is the single-folder counterpart that lets a
human resolve that conflict by editing files only, while Assent owns every Git
operation around those edits. The whole sequence is:

```text
assent reconcile parallel01              # prepare the conflict in a worktree
                                         # (edit the reported files by hand)
assent reconcile --continue parallel01   # stage, commit, advance the source
assent verify parallel01                 # optional, explicit, expensive
assent accept parallel01                 # explicit human approval
```

**Start** requires a finished folder (every task `DONE` or `SKIP`), a clean
main worktree, and a source folder with its own branch and worktree. It
captures the integration target's current tip, creates the worktree
`<project>.reconcile/<FOLDER>` next to the main worktree on the temporary
branch `assent-reconcile/<FOLDER>` starting at the exact source tip, and merges
the captured target tip into it without committing. Because the merge is built
source-first, its first parent is the original source, so the source branch can
later be fast-forwarded onto it — the source is never rewritten and **the
integration target is never changed**. The main worktree and the folder's own
source worktree stay clean throughout. If the two sides in fact merge without
conflict, start says so, undoes the merge, removes what it created, and leaves
the source untouched. If the source is already contained in the target, there
is nothing to reconcile.

**You edit, and run no Git commands.** Start prints the worktree path, the
branch, both tips, and every conflicting file; resolve those files in that
worktree only.

**`--continue`** stages exactly the paths Git still reports as unmerged,
validates the result (no remaining unmerged path, no leftover conflict marker
or whitespace error per `git diff --cached --check`, and no edit outside the
conflict-resolution scene), creates the merge commit, fast-forwards the source
branch inside its own worktree, and then removes the temporary worktree and
branch. It proves ownership of each managed resource again before deleting it —
worktree of this repository, attached to the managed branch, `HEAD` at the
proven commit, clean — so it can never widen the deletion. Because the source
has really advanced, `--continue` deletes the receipts that were written
against the old source identity: the folder receipt, and the batch receipt if
any source identity it records is no longer current (a batch receipt is
all-or-nothing). A batch receipt it cannot even parse is left in place for
inspection rather than erased.

**Reconciliation is not evidence and not approval.** `--continue` runs no
focused task tests and no complete verification, and writes no receipt. Proving
the resolved source is a later, explicitly human-started `assent verify FOLDER`
— the expensive step, run against the then-current target — and approving it is
a later `assent accept FOLDER`, which still requires a fresh, reproducible
`PASSED` complete-verification receipt. If the target advanced after start, the
captured merge is not rewritten; the drift is reported and that later `verify`
stays authoritative.

**Interruption and refusal** are recoverable and never destructive. There is no
state file: a later run reads the worktree, the temporary branch, `HEAD`,
`MERGE_HEAD`, and the merge parents to see how far the previous run got, so
`--continue` can resume a merge an interrupted run already committed, or finish
a fast-forward that was all that remained. When something does not match — the
source branch moved independently, the managed path is not a worktree or is on
another branch, a validation problem in the staged resolution — the run refuses
and preserves the worktree, the branch, and every edit; nothing is committed and
nothing is deleted.

**`--abort`** discards the attempt: it removes only the managed worktree and
temporary branch, and only after proving each is the resource it manages,
refusing while the worktree still holds uncommitted changes rather than throwing
away work. The source and the integration target are left unchanged.

Reconcile is deliberately not an integration engine. It handles exactly one
folder against the current integration target; it never resolves file content
for you, never combines speculative peer folders, never runs an AI adapter, and
never edits a task status. A conflict that appears only between two unaccepted
sources while building a batch candidate is outside this command — that set
still goes through `verify --batch`'s skip decision and then `assent rework` or
`assent reject`.

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

**Act 2: unattended execution**: `assent run`, then go to sleep. Each task
session runs only its focused `verify`. Whether folder completion also builds
a temporary integration candidate and runs the full `.assent/verify.py`
outside the AI session depends on `assent.toml`'s `[verification]`
`receipt_refresh`: the default `"manual"` leaves that to an explicit
`assent verify [--batch]` afterward; `"auto"` runs it at closeout as soon as
every task in the folder is done.

`assent verify <FOLDER>` refreshes that complete verification receipt with zero
tokens and no AI session; `assent verify --batch` does the same for every
finished, not-yet-integrated folder as one candidate. Either command's
`PASSED`/`FAILED` and `fresh`/`stale` state is shown in the report, so a stale
receipt can be refreshed unattended, and `assent accept` refuses without a
fresh `PASSED` receipt -- prompting you to verify first.

The packaged `.assent/verify.py` checks both the candidate working tree and the
committed delta from `HEAD` to its first parent. This catches committed
trailing whitespace that plain `git diff --check` cannot see. `assent init`
never overwrites an existing verifier; copy the template's checks into that
project manually when synchronizing. A verifier digest change makes old
receipts stale, so refresh them with `assent verify <FOLDER>` during unattended
verification before asking a human to accept.

**Rerunning verification yourself**: a task's focused `verify` command is
recorded in its `tNNN_name.e.toml` `verify` field, and you can run that exact
command yourself from inside that work folder's isolated worktree at
`<project>.worktrees/<folder>/`. During `assent run`, the run output echoes
the same text as a `verify: <command>` line, immediately followed by `verify
passed (exit 0)` or `verify failed (exit N)`, so that printed line is the
literal command to rerun by hand. The complete stage runs `assent verify
<FOLDER>` in a temporary integration candidate at
`<project>.integration/target-<uuid>`, a sibling of `<project>.worktrees/`, on
branch `assent-integration/<folder>/<uuid>`. This is the merged candidate tree
that the complete `.assent/verify.py` verifies and the receipt certifies; it
exists throughout the entire test run and is removed after the tests finish.
To reproduce or watch that stage manually, use the candidate as the command's
cwd while it exists and run the verifier script from the main worktree, for
example `python <main-worktree>/.assent/verify.py`; do not run it from the
source worktree as if that were the integration candidate. Cleanup runs in a
`finally` block, so normal completion, a Python exception, and Ctrl-C clean it
up. Only a hard kill (such as `taskkill /F`) or power loss can leave residue;
assent has no automatic stale-candidate recovery. Remove residue manually with
`git worktree remove --force <path>` and `git branch -D <branch>`.

**Parallel test execution**: the packaged `.assent/verify.py` template
provides `run_unittest_parallel()`, commented out by default, which runs each
`tests/test_*.py` module in its own subprocess concurrently instead of one
process running the whole suite serially, so total wall time is roughly the
slowest module's time rather than the sum of all of them. Process isolation
is deliberate: unittest modules mutate process-global state (`os.chdir`,
`os.environ`), so sharing one interpreter across modules would let them
corrupt each other. Concurrency defaults to `min(module count, CPU count)`;
set `ASSENT_VERIFY_JOBS` to override it. Opting in by editing
`.assent/verify.py` changes the verifier digest, so it expires existing
receipts once; rerun `assent verify <FOLDER>` to reissue them.

A worktree is a change-isolation, conflict-management, audit, and recovery
boundary, not a security sandbox. `danger-full-access` or `bypassPermissions`
still permits an AI to reach resources available to its OS identity, including
network, credentials, external Git writers, and files outside the worktree.
Use unattended runs only with trusted projects and accounts; Assent does not
provide a container or VM sandbox or intercept those external effects.

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
reviews the report. The verification receipt is scheduler evidence, not
approval. `assent accept <FOLDER>` quickly rebuilds the candidate and publishes
only when its source tip, integration tree, and verifier digest exactly
reproduce a fresh `PASSED` receipt; it does not run the full tests. Missing or
stale receipts require `assent verify <FOLDER>` first. Remote synchronization
remains a separate ordinary Git decision, and `assent clean <FOLDER>` is the
final optional cleanup. A new round
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

`assent verify <FOLDER>` is a zero-token, single-folder receipt refresh; it
never changes the target or opens an AI session. `assent accept <FOLDER>` is the
explicit human approval for one completed folder. It quickly rebuilds a
candidate and compares source tip, integration tree, and verifier digest with
a fresh `PASSED` receipt, then records a guarded `--no-ff` merge; it does not
run the full tests. A receipt is disposable derived evidence, and content
changes make it stale. It refuses when the source worktree or branch is gone.
There is no `review` task field. It never connects to a remote, uses `--all` or
`--push`, pulls, rebases, force pushes, resolves conflicts, deletes source, or
offers automatic conflict resolution. The integration lock cannot stop
external Git writers; do not run writing Git commands in the same main
worktree during acceptance. Re-running after success is idempotent.

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
| `assent verify <FOLDER>`<br>`assent verify parallel01` | Runs the complete verifier once for one folder's temporary integration candidate and refreshes the derived receipt; no target change and no AI session. Report status is `PASSED`/`FAILED`, `fresh`/`stale`. No `--all`. | **Zero** |
| `assent accept <FOLDER>`<br>`assent accept parallel01` | Human approval for exactly one completed folder. Quickly rebuilds the candidate and publishes only an exact fresh `PASSED` receipt match; it does not run complete verification. Missing/stale receipt requires `assent verify`. No `--all`, `--push`, remote, pull, rebase, force, conflict resolution, or source deletion. | **Zero** |
| `assent reconcile <FOLDER>`<br>`assent reconcile --continue parallel01` | Prepares one finished folder's source-versus-target conflict in the isolated worktree `<project>.reconcile/<FOLDER>` so a human can resolve the reported files by hand; `--continue` stages and validates that resolution, commits the merge, and fast-forwards the source branch; `--abort` discards only the proven managed worktree and branch. Never changes the target, resolves content, runs focused or complete verification, writes a receipt, or accepts. `FOLDER` is required; no `--all`. | **Zero** |
| `assent clean [FOLDER]`<br>`assent clean parallel01` | Cleans up only worktrees and same-folder-prefix branches that are fully merged and clean; skips anything it cannot prove, never touches `.assent/`, and has no force option. Acts on all work folders when `FOLDER` is omitted. | **Zero** |
| `assent reject <FOLDER>`<br>`assent reject parallel01` | Human-adjudicated rejection: archives uncommitted changes, then force-deletes that folder's worktree and same-prefix branches (recording full tip hashes before deletion), and resets DONE/WIP/BLOCKED tasks to TODO with Git evidence kept in the r file. `FOLDER` is required; refuses while a run is in progress. | **Zero** |
| `assent rework <FOLDER> <TASK>`<br>`assent rework parallel01 t003 --cascade --reason "review rejected"` | Non-destructively reopens a single task; keeps code by default, `--cascade` states downstream propagation explicitly. `--revert-code` creates a new reverse commit only when checkpoints form a contiguous tail. Updates the report on success, does not run automatically. Accepts `--config PATH`. | **Zero** |
| `assent init`<br>`assent init --path C:\work\my-project` | Generates the `.assent` skeleton and `AGENTS.md` in the target project; `--path DIR` defaults to the current directory. Does not accept `FOLDER` or `--config`. | **Zero** |
| `assent doctor`<br>`assent doctor` | Diagnoses the machine environment (Python version, git, adapter CLIs, temp directory writability); needs no `FOLDER` or `--config`, and runs without an existing `.assent/` project. | **Zero** |

Each subcommand's `-h`/`--help` shows that layer's actual syntax; there is
no top-level `--config` or other global option that applies to every
subcommand.

## Adapters, model tiers, and effort levels

Assent works with different AI CLI tools via pluggable adapters. Each task file
specifies an abstract **tier** (`prime`, `core`, or `lite`) instead of a concrete
model name; the adapter's configuration table translates that tier into the actual
CLI model for this run. Similarly, a task can request an abstract **effort** level
(`heavy`, `normal`, or `slight`), which the adapter translates to the vendor's
concrete CLI value (if any).

### Supported adapters

**Claude** (`adapter.name = "claude"`)

```toml
[adapter.claude]
command = "claude"
extra_args = ["--permission-mode", "bypassPermissions"]

[adapter.claude.models]
prime = "fable"      # Fable 5 – fastest tier
core  = "opus"       # Opus 4.8 – balanced tier
lite  = "sonnet"     # Sonnet 5 – efficient tier
```

**Codex** (`adapter.name = "codex"`)

```toml
[adapter.codex]
command = "codex"
extra_args = ["--sandbox", "danger-full-access"]

[adapter.codex.models]
prime = "gpt-5.6-sol"    # largest model
core  = "gpt-5.6-terra"  # balanced model
lite  = "gpt-5.6-luna"   # efficient model
```

**Antigravity** (`adapter.name = "antigravity"`)

The Antigravity adapter runs Google's Gemini models via `agy` (Antigravity CLI),
a free locally-installed CLI that requires interactive login once per machine.
This adapter communicates headlessly using print mode (plain-text output, no JSON
events) and includes preflight validation of model/effort combinations before
opening a session.

```toml
[adapter.antigravity]
command = "agy"
extra_args = ["--dangerously-skip-permissions"]

[adapter.antigravity.models]
prime = "gemini-3.1-pro"   # Gemini 3.1 Pro – highest quality
core  = "gemini-3.6-flash" # Gemini 3.6 Flash – balanced (new)
lite  = "gemini-3.5-flash" # Gemini 3.5 Flash – efficient

# Antigravity effort translations per tier. The notes below explain each.
[adapter.antigravity.default_effort]
prime = "heavy"
core  = "heavy"
lite  = "heavy"

# Gemini 3.1 Pro supports only low and high efforts, not medium. For quality,
# the abstract normal effort is translated up to vendor high (never silently downgraded).
[adapter.antigravity.efforts.prime]
normal = "high"

# Gemini 3.5 Flash supports only low and medium, not high. The lite tier's
# abstract heavy effort is translated to vendor medium (the family's ceiling),
# visible here in the config table where it can be inspected and overridden if needed.
[adapter.antigravity.efforts.lite]
heavy = "medium"
```

### Model/effort matrix

Task files specify an abstract tier and optional effort. The adapter translates
this into the concrete CLI invocation. The full 9-cell grid below shows what
each task-file (tier, effort) pair resolves to in each adapter:

#### Claude adapter

| Effort | prime<br/>(Fable) | core<br/>(Opus) | lite<br/>(Sonnet) |
|--------|---|---|---|
| slight | `--model fable --effort low` | `--model opus --effort low` | `--model sonnet --effort low` |
| normal | `--model fable --effort medium` | `--model opus --effort medium` | `--model sonnet --effort medium` |
| heavy | `--model fable --effort high` | `--model opus --effort high` | `--model sonnet --effort high` |

#### Codex adapter

| Effort | prime<br/>(gpt-5.6-sol) | core<br/>(gpt-5.6-terra) | lite<br/>(gpt-5.6-luna) |
|--------|---|---|---|
| slight | `--model gpt-5.6-sol --effort low` | `--model gpt-5.6-terra --effort low` | `--model gpt-5.6-luna --effort low` |
| normal | `--model gpt-5.6-sol --effort medium` | `--model gpt-5.6-terra --effort medium` | `--model gpt-5.6-luna --effort medium` |
| heavy | `--model gpt-5.6-sol --effort high` | `--model gpt-5.6-terra --effort high` | `--model gpt-5.6-luna --effort high` |

#### Antigravity adapter (1.1.5+)

| Effort | prime<br/>(3.1 Pro) | core<br/>(3.6 Flash) | lite<br/>(3.5 Flash) |
|--------|---|---|---|
| slight | `--model gemini-3.1-pro --effort low` | `--model gemini-3.6-flash --effort low` | `--model gemini-3.5-flash --effort low` |
| normal | `--model gemini-3.1-pro --effort high` | `--model gemini-3.6-flash --effort medium` | `--model gemini-3.5-flash --effort medium` |
| heavy | `--model gemini-3.1-pro --effort high` | `--model gemini-3.6-flash --effort high` | `--model gemini-3.5-flash --effort medium` |

Notes:
- **Antigravity prime/normal**: Gemini 3.1 Pro does not support `medium`, so
  assent chooses `high` instead (quality-first mapping). This is not a silent
  fallback—the configuration table makes it visible and auditable.
- **Antigravity lite/heavy**: Gemini 3.5 Flash has no `high` effort level, so
  `high` is translated to `medium`, the family's maximum available.
- **Antigravity 1.1.5 minimum**: This is the version that supports `--effort`,
  stable model slugs, and the headless fixes required for unattended execution.
  Earlier versions are rejected before opening a session.

### Using Antigravity adapter

**First-time setup**

1. Install `agy` (Antigravity CLI) on your machine if not already present.
2. Run `agy auth login` to interactively sign in once per machine.
3. Verify your installation with `agy --version` (must be 1.1.5 or later) and
   `agy models` (shows available models).

Assent will **not** modify your `~/.gemini/antigravity-cli/settings.json`, run
the login browser, or interact with credentials. Your login credentials and
workspace trust remain under your control.

**Example task file using Antigravity**

```toml
title = "Analyze code with high-quality reasoning"
model = "prime"
effort = "heavy"
status = "TODO"
scope = ["src/", "tests/"]
verify = "python -m pytest"

goal = "Use Gemini 3.1 Pro (highest quality) to review the codebase."
```

When `assent run` executes this task, it will:
1. Validate that Antigravity 1.1.5+ is installed and can reach `gemini-3.1-pro
   --effort high`.
2. Open a headless session with `agy --print --model gemini-3.1-pro --effort
   high --mode accept-edits ...`.
3. Run the verification command and record the result.

**Switching adapters in an existing project**

Changing `[adapter]` name is a one-line config change. Existing task files do
not need to change; they still use `model = "prime"` and `effort = "heavy"`, and
the new adapter's configuration table translates those the same way. Once you
have switched adapters, the next `assent check` will validate the new adapter
before any session starts.

### Configuring model and effort translations

The config template in `.assent/assent.toml` shows how to customize the
tier-to-model mapping and the abstract-to-CLI effort translations. The lookup
order is always:

1. Task file's explicit `effort` annotation (if present)
2. Adapter's `default_effort` for this tier (if present)
3. No effort flag (some adapters/tiers may not support one)

And for effort translation:

1. Tier-specific section: `[adapter.<name>.efforts.<tier>]`
2. Flat section: `[adapter.<name>.efforts]`
3. Built-in baseline: `heavy` → `high`, `normal` → `medium`, `slight` → `low`
   (each abstract key falls back independently when a higher-priority table
   lacks that key).

Example: if your Antigravity setup has a newer 3.1 Pro that supports medium,
you can remove the quality-first mapping:

```toml
# Remove this line:
# [adapter.antigravity.efforts.prime]
# normal = "high"

# Or set it to the actual value:
[adapter.antigravity.efforts.prime]
normal = "medium"
```

### Configuring Antigravity print timeout

Antigravity's `--print-timeout` is independent of Assent's watchdog timeout.
The print timeout limits how long the CLI will wait for a single print
invocation to complete; the watchdog limits how long Assent will wait for any
output before killing the session.

In `.assent/assent.toml`:

```toml
[adapter.antigravity]
print_timeout_minutes = 120  # AGY will wait up to 2 hours for an answer
```

Do not set this lower than your longest expected task; `assent check` will
validate that the print timeout is positive.

### Troubleshooting Antigravity configuration

**Problem: `preflight failed: invalid model selection`**

Antigravity rejected the model/effort combination during preflight. Check:

```bash
agy models                         # See what models are available
agy --print --model <MODEL> ...    # Test your model/effort choice
```

Common causes:
- **Unmapped model tier**: add the model to `[adapter.antigravity.models]`.
- **Unsupported effort**: the model does not support that effort level. For
  example, Gemini 3.1 Pro does not support `medium`. Fix the mapping in
  `[adapter.antigravity.efforts.prime]`.

**Problem: `authentication required` or `permission denied`**

You must have logged in once on this machine:

```bash
agy auth login          # Opens a browser for Google sign-in
```

If you are running `assent run` unattended (e.g., at night), your login must
complete before the run starts. Assent cannot open a browser, log you in, or
detect when you are away; it only uses your existing login credentials.

**Problem: `command not found: agy`**

Antigravity CLI is not installed or not on your PATH. Visit the [Antigravity
CLI installation docs](https://google-antigravity.github.io/install) and verify
with `agy --version`.

**Problem: Quota exhausted mid-task**

When Antigravity reaches quota limits, `assent run` records a `WIP` checkpoint
with the partial results. When your quota resets (Google typically resets daily
or hourly depending on your plan), you can resume the same task:

```bash
assent run <FOLDER>  # Resumes from WIP automatically
```

The task journal records the exact quota-reset time (if available) and the
scheduler will poll until then before retrying. If you need to run a different
folder in the meantime, you can run it in a second terminal as long as it does
not depend on the quota-limited folder.
When `[adapter].name` is a list, quota exhaustion rotates to the next adapter
in order; the scheduler waits for the rotation poll only after every adapter
in the rotation is exhausted.

**Fixing configuration after a preflight error**

Do not modify the task file's abstract tier or effort. Instead, update only the
adapter configuration. For example, if prime/normal is mapped to high but you
want to change it:

```toml
# Before
[adapter.antigravity.efforts.prime]
normal = "high"

# After (if normal is now supported)
[adapter.antigravity.efforts.prime]
normal = "medium"
```

After fixing the config, no changes to the `.assent/` management files are
needed; `assent check` will re-validate and `assent run` will retry.

## Plan format and config files

- Full format contract: [assent/templates/format.md](assent/templates/format.md)
  (copied into a project's `.assent/format.md` by `assent init`).
- Working-instructions template: [assent/templates/instructions.md](assent/templates/instructions.md)
  — assent session behavior and cross-project common rules; project rules
  stay in `AGENTS.md`.
- Config template: [assent/templates/assent.toml](assent/templates/assent.toml)
  — adapter selection, the abstract tier (prime/core/lite) mapping table,
  abstract effort (heavy/normal/slight) defaults and CLI-value translation,
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
