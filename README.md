# assent — an AI plan format + an automatic scheduler

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
- **Format contract**: `~/.assent/format.md` (installed by `assent init` into
  the per-user assent home, once per machine) is what a planning AI reads to
  produce task files, and what the scheduler's parser is aligned with
  byte-for-byte. Its sibling `~/.assent/instructions.md` holds the session
  rules. Neither is copied into a project.
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

Verify: run `assent --version` from any directory; it prints the installed
distribution version. `assent --help` shows the top-level CLI help. Zero
third-party dependencies — nothing else gets downloaded.

## Where files live

What describes assent itself lives once per machine; what describes your
project lives in your project.

```text
~/.assent/                  # the per-user assent home, shared by every project
├── assent.toml             # your settings: adapter, tier tables, watchdog, retries
├── instructions.md         # session rules contract   (assent owns this file)
└── format.md               # plan format contract     (assent owns this file)

<project>/
├── AGENTS.md               # your project rules + one assent bridge line
└── .assent/                # git-ignored, main worktree only
    ├── verify.py           # your project's verification script
    ├── assent.toml         # optional: a legacy or deliberate project override
    ├── <work folder>/      # task files, r files, _report.md, _assent.log,
    │                       #   assent.lock, and that folder's verification receipt
    ├── _batch_verification.toml   # the batch verification receipt (derived)
    ├── _archived.toml      # roster of retired work folders
    └── _archive/           # those folders, compressed one zip each
```

`instructions.md` and `format.md` describe the tool, so there is exactly one
copy of each on the machine and a project never receives one. `AGENTS.md` and
`.assent/verify.py` stay yours: `assent init` refreshes only the single bridge
line in the first and never overwrites the second.

### Settings precedence

Lowest priority first:

1. assent's built-in defaults
2. your user settings in `~/.assent/assent.toml`
3. the optional project override in `.assent/assent.toml`
4. an explicit CLI selection where the command offers one (`--config PATH`
   chooses which project-level file plays role 3; `--jobs` and similar flags
   override that setting for one invocation)

Tables merge by key; scalars and arrays are replaced whole. A project override
therefore shadows later edits to your shared settings for exactly the keys it
states, and `assent init` never migrates it into the user home or edits it —
it is preserved byte for byte and reported as an override.

Omitting a key is the only way to inherit the layer below:

- `key =` is not "no value" — it is invalid TOML, and the file fails to load.
- An empty table states no leaf override, so every key inside still resolves
  from the layer below.
- An empty array is an explicit replacement wherever that field permits one,
  not a request to fall back.
- An empty or whitespace-only string is refused for any setting that needs
  useful text (a command, an adapter name, an effort value). The error names
  the dotted key and the file that stated it instead of quietly reinstating a
  lower layer.

### What `assent init` does on a repeat run

It refreshes both user-home contracts to this installation's packaged text and
adds only the packaged settings keys your `assent.toml` does not already state,
never replacing a value you wrote. In the project it preserves an existing
`verify.py` (and refuses `--test` when one exists), keeps the bridge line in
`AGENTS.md` current, and keeps `.assent/` in `.gitignore`. Every read, parse,
and merge finishes before the first write, so invalid TOML or an invalid
`--test` choice refuses without leaving anything half-upgraded.

Migrating an older project prints warnings rather than making the decision for
you. A project copy of `instructions.md` or `format.md` is removed only when it
matches the packaged text exactly; one that differs is kept and reported, since
sessions read the user-home contract either way — move anything you still want
out of the local copy, then delete it. An existing `.assent/assent.toml` is
kept and reported as a compatibility override that outranks your user settings.

Before opening any session, assent fails closed unless both user-home contracts
are present, readable, and byte-identical to this installation's packaged text.
A missing, unreadable, or stale contract names the path and points at `assent
init`; it is never patched or silently regenerated mid-run. The comparison
reads text with universal newlines, so a file an editor rewrote with CRLF still
counts as the same contract.

## Quick start

```
# 0. cd into the target project root (must be a git repo)

# 1. Install the user home (~/.assent: shared settings + the two contracts)
#    and the project's .assent skeleton and AGENTS.md, selecting the real
#    project test interactively (or use --test in scripts, for example):
assent init --test unittest
#    Choices are parallel unittest, pytest, npm test, Flutter test, or a
#    custom argv command such as: assent init --test "custom:python -m unittest"
#    Repeat init does not prompt: it preserves the project's verify.py,
#    refreshes the two user-home contracts, and adds only missing settings keys.

# 2. Review the shared settings in ~/.assent/assent.toml (every project on this
#    machine reads them), then fill in AGENTS.md's project description/hard
#    constraints and .assent/verify.py's actual check commands
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

# Run exactly A, then B, in the order written
assent run A B
# Run A and B in that order, then hand the rest to the --all scheduler
assent run A B --all
# The literal `...` is the remainder selector: run A, then B, then every other
# work folder, as one selection snapshotted before anything starts
assent run A B ...

# Run every incomplete folder in dependency order, at most 2 folders at once
assent run --all --jobs 2

# Chain complete verification onto a run that exited zero (a failing run
# verifies nothing); the verification's exit code becomes the exit code
assent run --all --verify

# 6. Under the default [verification] receipt_refresh = "manual", run closeout
#    leaves no receipt behind, and direct/selected acceptance is refused with a
#    prompt to verify first. Refresh it explicitly while away (zero tokens):
#    verifying several finished folders as one candidate costs one full
#    verification instead of one per folder
assent verify --batch
# Or refresh just one folder's receipt
assent verify <FOLDER>
# Run complete verification for exactly A and B as one dependency-ordered batch
assent verify A B
# Verify A plus every other finished folder as one exact selected batch
assent verify A ...
# Rerun DONE-task focused checks in FOLDER's source worktree (no receipt)
assent verify <FOLDER> --focus
# Set receipt_refresh = "auto" instead if you want run closeout to refresh
# the receipt itself

# 7. Check in any time (a separate terminal, zero tokens), then review
assent status
assent report
# After human review, accept every finished folder in dependency order
assent accept --all
# Or accept just one completed folder into the current target branch
assent accept <FOLDER>
# Accept exactly A and B from a matching verified batch receipt
assent accept A B
# Accept A plus every other finished folder; still an exact selection, so it
# needs a receipt for exactly that expanded set and never verifies
assent accept A ...
# After acceptance, optionally sync with ordinary Git (or your own AI workflow)
git push
# Once acceptance and any desired sync are complete, remove redundant artifacts
assent clean <FOLDER>
# Clean several folders, upstream-first (`assent clean` alone still means all)
assent clean A B
assent clean A ...
# Once a folder is no longer needed, retire its plan into _archive/
assent archive --all
# Or name the folders to retire; an ineligible named folder is a failure,
# unlike --all, which skips it
assent archive A B

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
then make the acceptance decision explicitly. The receipt is the scheduler's
complete-verification evidence; `accept` is the human approval.

Direct `assent accept <FOLDER>` and selected `assent accept A B` never run the
complete verifier. A direct folder that is already contained in the target is
an ancestry-proven idempotent no-op; otherwise the direct form requires a
fresh PASSED per-folder receipt whose source tip, reconstructed integration
tree, and verifier digest match exactly. The selected form requires a fresh
PASSED batch receipt for exactly the dependency-ordered set named in
`assent accept A B`. Missing, malformed, stale, mismatched, or drifted
evidence refuses and points to the corresponding `assent verify` command.
Neither form silently verifies or accepts folders outside its explicit target.

`assent accept --all` is the intentional exception and has two modes. A fresh
PASSED batch receipt is replayed and released atomically, without a new full
verification, for exactly the folders recorded in that receipt. A missing or
expired/non-PASSED batch receipt selects the sequential path: in dependency
order it runs `verify_folder_if_needed` before each not-already-integrated
folder, then performs the ordinary receipt-backed accept. A malformed batch
receipt refuses instead of falling back. The sequential path treats an
already-integrated folder as an ancestry no-op, skips a finished folder only
when its source branch and worktree were both cleaned after proven integration,
stops on the first real failure, and preserves earlier publications. The fresh
batch path only reports finished folders outside its receipt; it does not
verify or accept them in the same run.

All acceptance paths require explicit human action, a complete and
dependency-safe source, and a clean, uniquely identified Git state. They keep
the source for inspection and cleanup, never auto-resolve conflicts, and do
not connect to remote hosting, pull, rebase, force-push, delete source, or
write the target on a failed gate. The integration lock serializes Assent
accept operations but cannot stop unrelated external Git writers; do not run
writing Git commands in the same main worktree during acceptance. Run
`assent clean <FOLDER>` only when the accepted source is no longer needed and
the cleanup proof is available.

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

### Explicit selected workflows

`assent run A B` runs exactly the two named folders in the stated order. Each
folder still checks its own prerequisites, and the command stops on the first
configuration or run failure. `assent run A B --all` first completes that
explicit sequence, then hands every remaining incomplete folder to the normal
dependency-ordered `--all` scheduler. Neither command verifies a full
integration candidate or accepts anything as a hidden side effect.

#### The `...` remainder selector

The literal ASCII token `...` is a final positional argument shared by `run`,
`verify`, `accept`, `clean`, and `archive`. It means "and every remaining
folder this command would discover", so `assent run A B ...` is "A, then B,
then everything else". It is not a spelling of `--all`: `...` produces one
exact folder selection, snapshotted before anything is mutated, while `--all`
keeps its own dynamic whole-project mode. Combining the two is a usage error,
as is giving `...` more than once or anywhere but last. Folder names may never
contain `..`, so the token can never collide with a real folder.

Each command expands the remainder by its own discovery rule. `verify` and
`accept` add only finished folders, because that is the set their whole-project
modes work on. `run`, `clean`, and `archive` consider every work folder and
make their usual per-folder decision afterwards. The remainder is appended
after the explicit prefix, and each command then applies its own ordering:
`run` keeps the stated prefix order and takes the remainder in
folder-dependency order, `verify` and `accept` normalize the whole selection to
dependency order, and `clean` normalizes it upstream-first. The expanded
selection is printed before the work starts, and an expansion that selects no
folder is refused. `...` selects folders; it does not switch a command over to
another mode, so even a bare `assent run ...` walks the expanded selection
through the ordinary explicit-folder path rather than the `--all` scheduler,
and `--jobs` remains an `--all` option.

Cardinality, not the token, still picks the path: one folder is the
single-folder path (a folder receipt, a direct accept, one `archive`), and two
or more is the exact selected batch. So `assent verify A ...` writes a batch
receipt for exactly the expanded set, and `assent accept A ...` requires a
fresh receipt for exactly that same set and still never verifies. `...` is
rejected with `verify --batch`, `verify --focus`, `run --once`, `run --task`,
and `archive --restore`, which restores exactly one folder.

#### `run --verify`

`assent run --verify` chains one complete verification onto a run that exited
zero; a failing run is returned exactly as it is and verifies nothing, because
there is no finished plan to certify. The verification matches the selection:

| Invocation | What is verified |
| --- | --- |
| `assent run --verify` (auto-selected folder) | that folder's receipt |
| `assent run A --verify` | A's folder receipt |
| `assent run A B --verify` | A and B as one selected batch |
| `assent run A ... --verify` | the exact expanded selection as one batch |
| `assent run --all --verify` | the whole-project dynamic batch |
| `assent run ... --verify` | the whole-project dynamic batch |

A bare `...` is a whole-project request and therefore happens to land on the
same dynamic batch `--all` uses, rather than freezing a set the scheduler may
still extend; an explicit prefix plus `...` stays an exact expanded selection
and is verified as exactly the folders it ran. The verification's exit code
becomes the command's exit code. `--verify` is refused with `--once` and
`--task`, which stop before folder closeout on purpose, and it is an
invocation-level request that runs regardless of the configured
`receipt_refresh` policy.

#### Multi-folder `clean` and `archive`

`assent clean A B` and `assent clean A ...` clean several folders in one
upstream-first pass, with every folder's own evidence rule unchanged; bare
`assent clean` still means all folders. `assent archive A B` archives each
named folder and keeps single-folder `archive`'s contract rather than
`--all`'s: the human named those folders, so a folder that is merely
ineligible is a refused request. Every named folder is attempted, a summary
line reports how many were archived, and the command exits nonzero if any was
not — while `assent archive --all` skips an ineligible folder without failing.
`archive --restore FOLDER` reverses exactly one archive and takes neither
`--all` nor `...`.

#### Colored help

Where the standard library colorizes `--help` (Python 3.14 and later), assent
re-themes only the `usage:` prefix and the section headings, so they are not
the barely legible dark blue of the default theme; every other option and
label color is the standard one. The swap happens inside argparse's own color
decision, so `NO_COLOR`, `FORCE_COLOR`, `PYTHON_COLORS`, and a redirected or
unsupported stream still decide whether any escape sequence is emitted at all.
On Python 3.11-3.13, whose argparse has no color support, help is plain text.
Color is therefore never promised — help stays readable without it.

`assent verify A B` selects exactly A and B, normalizes them to dependency
order, builds one integration candidate, and runs the complete verifier once.
It writes one batch receipt for the selected source identities and intermediate
trees; a selected merge conflict refuses rather than skipping or shrinking the
set. It never changes the target ref and never accepts a folder. If a failed
request is bisected to a passing prefix, the command still returns failure and
that prefix cannot authorize the original selected acceptance.

`assent verify <FOLDER> --focus` is different: it runs the distinct DONE-task
verification commands in that folder's source worktree. It creates no
integration candidate or receipt, and even a passing result cannot authorize
acceptance. After a successful exact selected verification, human review may
run `assent accept A B`; that command requires the fresh receipt for exactly A
and B, replays it without running verification, and publishes all selected
folders atomically or none.

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

`assent accept --all` has two deliberate modes. With a fresh PASSED batch
receipt, it publishes exactly the receipt's own folders in one atomic ref
update, then reports — in that same run — every other finished folder the
receipt does not cover without verifying or accepting those leftovers. There
is no second prompt or hidden expansion of that receipt. With no batch receipt,
or with expired/non-PASSED batch evidence, it instead takes the sequential
folder path and runs `verify_folder_if_needed` before each not-already-
integrated accept. A malformed batch receipt refuses rather than falling back.
That sequential path skips folders whose source was already cleaned after
proven integration, stops at the first real failure, and keeps earlier
publications. Run `assent verify --batch` again to build the next explicit
batch when the receipt-release path is wanted.

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
assent verify parallel01                 # required before accept, explicit, expensive
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
branch through the same link-safe cleanup boundary. It proves ownership of each
managed resource again before deleting it —
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
temporary branch through the link-safe cleanup boundary, and only after proving
each is the resource it manages,
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

### The lock file is diagnostics, not the lock

`assent.lock` stays on disk after a run, on purpose. It is a diagnostics
record — the PID, the start time, and the folder name of the last run — and
nothing decides anything from its contents or its existence.

The actual ownership is an OS-level exclusive lock (msvcrt on Windows, fcntl
on POSIX) tied to the open file handle for the process's lifetime. Normal
exit, Ctrl+C, a crash, and a forced kill all release it automatically, because
the OS closes the handle either way. There is therefore no such thing as a
stale lock here, no PID-reuse hazard, and no cleanup procedure:

- Do not read the file's presence as "a run is in progress" — it is present
  after every run that has ever touched the folder.
- Do not delete the file to recover from anything. Deleting it introduces a
  race and fixes nothing; the next run reuses it, and `assent archive` even
  creates it when it is missing, because a missing lock file proves nobody
  holds the folder.
- If a folder really is busy, the next `assent run` says so when it fails to
  acquire the lock. That refusal, not the file, is the signal.

The one thing the recorded PID does tell you is about a *living* process:
`assent run --all` does not finish its own interrupt path until every work-
folder child it owns has exited and been reaped, on every exit path including
a refusal or a scheduling error. So if the recorded PID is still alive, that
is a genuinely running process to wait for — never a stale-file situation to
clean up by hand.

The limitation worth knowing is unrelated to staleness: `flock` and
`msvcrt.locking` semantics are unreliable on some network filesystems, so this
lock only guarantees mutual exclusion on a local filesystem.

The version-control boundary is deliberately simple: `AGENTS.md` is the
project rules; when tracked, the worktree's branch version is used, and when
not tracked, the prompt supplies the main-tree absolute path. The whole
`.assent/` is the assent management plane, excluded via `.gitignore` and
kept only in the main worktree. The scheduler likewise supplies t/r files and
the default verification script as main-tree absolute paths, and the two
contracts as their `~/.assent` absolute paths; the verification script is
loaded from the main tree but its execution cwd is still the worktree. Whenever any `.assent/` file has
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
Let's start planning. Please read AGENTS.md, ~/.assent/instructions.md, and
~/.assent/format.md, then discuss the following goal with me and progressively
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
receipt can be refreshed unattended. Direct `assent accept <FOLDER>` and
selected `assent accept A B` refuse without their matching fresh `PASSED`
receipt and never start the verifier; `assent accept --all` instead uses its
fresh-batch release mode or, when batch evidence is absent/expired, its
intentional sequential verify-then-accept mode.

The packaged `.assent/verify.py` checks both the candidate working tree and the
committed delta from `HEAD` to its first parent for leftover conflict markers.
Whitespace-only differences, including line endings, trailing spaces or tabs,
and blank lines at EOF, do not block verification; projects that require a
formatting policy can select or add an explicit formatter check. Fresh
`assent init` asks which real project test to enable: parallel unittest, pytest,
npm test, Flutter test, or a custom command rendered as argv. The packaged
template keeps all of those project-test examples commented, while the new
project copy activates exactly the selected command. A fresh verifier therefore
fails when the selected test is absent instead of reporting `verify: OK` from
an empty skeleton.

On repeat initialization, `assent init` never overwrites an existing verifier
and refuses `--test` when one is already present. It does replace
`~/.assent/format.md` and `~/.assent/instructions.md` with the packaged
contracts, and merges missing active settings into `~/.assent/assent.toml`
without changing existing or custom values. Invalid TOML or input is refused
before any managed file changes. A verifier digest change makes old
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
assent has no automatic stale-candidate recovery. Do not manually run a raw
Git worktree-removal command or recursive deletion against residue. Preserve
the exact candidate path and branch as recovery evidence and use the owning
Assent recovery/retry path, which re-proves ownership, inventories directory
links and other directory reparse points, and detaches each link object before
recursive removal. If that proof cannot be completed, the path, branch, and
external target remain in place.

**Linked-target cleanup warning:** Assent detaches each directory-link object
before any recursive Git or filesystem removal and never traverses its resolved
target. External link targets survive success, refusal, failure, interruption,
and retry. This applies to `clean`, `archive`, `reject`, reconciliation,
setup-failure cleanup, and temporary verification candidates; deleting a link
object is not deleting anything through its resolved path.

**Ignored inputs the candidate still needs**: `git worktree add` builds that
candidate from tracked content, so ignored paths are absent from it. Complete
verification therefore mirrors exactly two kinds of artifact from each source
worktree that enters the candidate, at the root or nested below tracked
parents: ignored directory links you provisioned yourself (Windows junctions
and directory symlinks, POSIX directory symlinks), such as a nested
`lib/l10n/arb`, and ordinary ignored leaf files sitting inside an otherwise
tracked directory, such as a generated `lib/models/task.g.dart` beside its
tracked source. A directory becomes a link to the same resolved target, a file
becomes a candidate-side link to the source file (a same-volume hard link on
Windows, a file symlink on POSIX). Nothing is copied, and you never prepare
hardlink twins or convert a generated file to a symlink by hand.

Everything else stays out. Whole ignored directory trees are pruned rather
than walked, so `.git`, `.assent`, build output, caches, credentials, editor
state, and every path inside a mirrored link's target are never enumerated, and
neither is a file whose parent chain is not part of the candidate's tracked
tree. A destination must be absent from the candidate and ignored there; a
mirrored artifact never replaces or shadows tracked content. When several
folders are verified as one candidate their artifacts are unioned — one path
with one directory target, or one file content digest, is a single artifact,
while conflicting targets, differing file contents, a kind mismatch, an
overlap, a dangling link, or an occupied destination refuses before the
verifier runs and before any `PASSED` receipt exists. The mirrors last only for
that verifier run and are removed before the temporary worktree is, so your
source worktree's links, generated files, and external targets survive a pass,
a failure, and a Ctrl-C alike. There is no force flag and no project setting
that widens this.

**Parallel test execution**: choosing `unittest` during `assent init` activates
the packaged helper `run_unittest_parallel()`, which runs each
`tests/test_*.py` module in its own subprocess concurrently instead of one
process running the whole suite serially. The packaged template leaves this
and the pytest, npm, and Flutter examples commented until one choice is made.
Process isolation is deliberate: unittest modules mutate process-global state
(`os.chdir`, `os.environ`), so sharing one interpreter across modules would let
them corrupt each other. Concurrency defaults to `min(module count, CPU count)`;
set `ASSENT_VERIFY_JOBS` to override it. Selecting a command changes the
generated verifier digest, so it expires existing receipts once; rerun
`assent verify <FOLDER>` to reissue them.

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
approval. Direct `assent accept <FOLDER>` quickly rebuilds the candidate and
publishes only when its source tip, integration tree, and verifier digest
exactly reproduce a fresh `PASSED` receipt; it does not run the full tests.
Selected `assent accept A B` applies the same no-verifier rule to exactly the
matching batch receipt. `assent accept --all` is the documented exception: a
fresh batch receipt is replayed atomically, while absent or expired batch
evidence invokes the sequential per-folder verification fallback. Remote
synchronization remains a separate ordinary Git decision, and
`assent clean <FOLDER>` is the final optional cleanup. When review or
verification of a live, not-yet-accepted folder turns up a correction or a
missing piece of that folder's own objective, prefer appending it to that
folder as a newly numbered task rather than opening a new one; earlier tasks
are never rewritten or renumbered to carry it. Open a new work folder when the
objective is genuinely distinct, when the relevant folder is already accepted,
archived, or rejected, or when dependency or `base` isolation needs a separate
source lineage; an old folder can keep taking
part in dependency resolution via `_folder.toml`'s `after`. A folder's
completion is derived from its task files — it is complete only once every
task is DONE/SKIP.

## Command reference

The full form of `run`, `status`, `check`, and `report` is
`assent <command> [options] [FOLDER]`. `FOLDER` may be stated explicitly;
when omitted, `run` derives the single runnable folder from current task
state and `_folder.toml`'s `after` upstreams, and refuses on ambiguity.
`status`, `check`, and `report` act on all folders when `FOLDER` is omitted.
`--config PATH` selects the project-level config file, defaulting to
`.assent/assent.toml`. That file is the optional override layer on top of
`~/.assent/assent.toml`, and it also locates the project (the project root is
the parent of the `.assent` directory the path lives in), so the path is
meaningful even when no such file exists. The config file no longer maintains
a work-folder pointer. `--config` and `FOLDER` are orthogonal — use either
alone or together, e.g.
`assent status --config configs/night.toml parallel01`.

Work-folder names are portable Windows/Git-ref names: non-empty, with no
whitespace, path separators, control characters, Git-ref-forbidden characters
(`~`, `^`, `:`, `?`, `*`, `[`), or Windows-forbidden characters (`<`, `>`,
`"`, `|`). They cannot start with `-` or `.`, contain `..` or `@{`, end with
`.` or `.lock`, or use a reserved Windows device name. The name becomes the
Git branch prefix, so this validation happens before a worktree or branch is
created.

`assent verify <FOLDER>` is a zero-token, single-folder full-verification
receipt refresh; it never changes the target or opens an AI session.
`assent verify A B` is the exact selected-batch form: it normalizes A and B to
dependency order, verifies one integration candidate once, and writes one
batch receipt for exactly that set. `assent verify <FOLDER> --focus` instead
runs distinct DONE-task checks in the source worktree, writes no receipt, and
cannot authorize acceptance.

`assent accept <FOLDER>` is explicit human approval for one completed folder.
It never runs the full tests: except for an ancestry-proven already-integrated
no-op, it requires a fresh matching `PASSED` receipt, rebuilds the candidate,
and records a guarded `--no-ff` merge. `assent accept A B` requires a fresh
batch receipt for exactly A and B, replays it without verification, and
publishes all selected folders atomically or none. `assent accept --all` has
the intentional two-mode exception: a fresh PASSED batch receipt is replayed
atomically, while absent or expired batch evidence runs the sequential
per-folder verify-then-accept path. Malformed batch evidence refuses rather
than falling back. Receipts are disposable derived evidence; content changes
make them stale. Direct and selected acceptance never silently expand their
set or start verification. None of these commands connects to a remote, uses
`--push`, pulls, rebases, force pushes, resolves conflicts, deletes source, or
offers automatic conflict resolution. The integration lock cannot stop
external Git writers; do not run writing Git commands in the same main
worktree during acceptance. Re-running after success is idempotent where the
source is already integrated.

`assent clean [FOLDER ...]` only deletes worktrees and branches that are fully
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
| `assent run A B`<br>`assent run A B --all`<br>`assent run A B ...` | Runs exactly A then B in the stated order and stops on the first failure. With `--all`, it then runs every remaining incomplete folder in dependency order; the literal `...` instead appends every remaining folder as one selection snapshotted before the run starts. `...` and `--all` cannot be combined, and neither form verifies or accepts implicitly. | Only spent while an AI session runs |
| `assent run --all --verify`<br>`assent run A B --verify` | Runs, then — only if the run exited zero — runs the complete verification that matches the selection: one folder as a folder receipt, an exact multi-folder selection as that selected batch, `--all` or a bare `...` as the whole-project batch. The verification's exit code becomes the command's; incompatible with `--once` and `--task`. | Only spent while an AI session runs; the verification itself is **zero** |
| `assent run --all`<br>`assent run --all --jobs 2` | Runs every incomplete folder in `_folder.toml` dependency order; `--jobs N` caps how many folders run at once (default 1), with the parent terminal live-tagging each subprocess's output as `[folder] message`. | Only spent while an AI session runs |
| `assent status [FOLDER]`<br>`assent status parallel01` | Shows progress statistics, the next task, the branch, and the last checkpoint. Accepts `--config PATH`. | **Zero** |
| `assent check [FOLDER]`<br>`assent check --config .assent/assent.toml parallel01` | Validates task-file format, dependency-cycle freedom, config, and environment; this is the planning meeting's adjournment condition. Accepts `--config PATH`. | **Zero** |
| `assent report [FOLDER]`<br>`assent report parallel01` | Generates and displays the work folder's human-readable report `_report.md`. Accepts `--config PATH`. | **Zero** |
| `assent verify <FOLDER>`<br>`assent verify parallel01` | Runs the complete verifier once for one folder's temporary integration candidate and refreshes the derived receipt; no target change and no AI session. Report status is `PASSED`/`FAILED`, `fresh`/`stale`. | **Zero** |
| `assent verify A B`<br>`assent verify A ...` | Verifies exactly A and B in dependency order with one integration candidate and one full verifier run, writing one batch receipt for that exact set; `--no-bisect` applies to a batch only. A selected conflict refuses rather than skipping. `A ...` expands the selection over the remaining finished folders and is still exact. | **Zero** |
| `assent verify <FOLDER> --focus`<br>`assent verify parallel01 --focus` | Repeats distinct DONE-task verify commands in the source worktree; writes no receipt and cannot authorize acceptance. | **Zero** |
| `assent accept <FOLDER>`<br>`assent accept parallel01` | Explicit human approval for one folder. Never runs complete verification; except for an ancestry no-op, it requires a fresh exact `PASSED` receipt and quickly rebuilds the candidate. | **Zero** |
| `assent accept A B`<br>`assent accept A ...` | Explicit human approval for exactly A and B from their matching fresh batch receipt; replays the dependency-ordered chain without verification and publishes all or none. It never expands the set or falls back. `A ...` selects the remaining finished folders too and still requires evidence for exactly that expanded set. | **Zero** |
| `assent accept --all` | Fresh PASSED batch receipt: atomic replay without new verification. Missing/expired evidence: sequential `verify_folder_if_needed` then accept in dependency order, stopping on failure while preserving earlier publications. Malformed evidence refuses; already-integrated folders no-op and cleaned sources skip. | **Zero** |
| `assent reconcile <FOLDER>`<br>`assent reconcile --continue parallel01` | Prepares one finished folder's source-versus-target conflict in the isolated worktree `<project>.reconcile/<FOLDER>` so a human can resolve the reported files by hand; `--continue` stages and validates that resolution, commits the merge, and fast-forwards the source branch; `--abort` discards only the proven managed worktree and branch. Never changes the target, resolves content, runs focused or complete verification, writes a receipt, or accepts. `FOLDER` is required; no `--all`. | **Zero** |
| `assent clean [FOLDER ...]`<br>`assent clean A B`<br>`assent clean A ...` | Cleans up only worktrees and same-folder-prefix branches that are fully merged and clean; skips anything it cannot prove, never touches `.assent/`, and has no force option. Takes one folder, several, or the literal `...` remainder, and acts on all work folders when none is named; several folders are cleaned in one upstream-first pass. | **Zero** |
| `assent archive <FOLDER ...>`<br>`assent archive --all`<br>`assent archive --restore FOLDER` | Retires finished folders: contains `clean`, then compresses the plan into `_archive/` and registers it in the roster. Named folders keep single-folder `archive`'s contract — every one is attempted and an ineligible one exits nonzero — while `--all` skips an ineligible folder without failing. `--restore` reverses exactly one archive and takes neither `--all` nor `...`. | **Zero** |
| `assent reject <FOLDER>`<br>`assent reject parallel01` | Human-adjudicated rejection: archives uncommitted changes, removes that folder's worktree through the link-safe cleanup boundary, then force-deletes same-prefix branches (recording full tip hashes before deletion), and resets DONE/WIP/BLOCKED tasks to TODO with Git evidence kept in the r file. `FOLDER` is required; refuses while a run is in progress. | **Zero** |
| `assent rework <FOLDER> <TASK>`<br>`assent rework parallel01 t003 --cascade --reason "review rejected"` | Non-destructively reopens a single task; keeps code by default, `--cascade` states downstream propagation explicitly. `--revert-code` creates a new reverse commit only when checkpoints form a contiguous tail. Updates the report on success, does not run automatically. Accepts `--config PATH`. | **Zero** |
| `assent init --test CHOICE`<br>`assent init --path C:\work\my-project --test pytest` | Installs the user home `~/.assent` (shared settings plus the `instructions.md` and `format.md` contracts) and the project's `.assent/verify.py`, `AGENTS.md` bridge line, and `.gitignore` entry, after selecting exactly one real project test: parallel unittest, pytest, npm test, Flutter test, or custom argv. Without `--test`, fresh init shows a numbered menu. Repeat init does not prompt, preserves an existing verifier, refreshes both user-home contracts, and merges only missing active config defaults. An old project copy of a contract is removed only when it matches the packaged text exactly; a project `assent.toml` is preserved as an override and reported. Invalid input/TOML refuses before any managed file changes. | **Zero** |
| `assent doctor`<br>`assent doctor` | Diagnoses the machine environment (Python version, git, adapter CLIs, temp directory writability); needs no `FOLDER` or `--config`, and runs without an existing `.assent/` project. | **Zero** |
| `assent --version` | Prints `assent` followed by the installed distribution version and exits; works without a project or subcommand. | **Zero** |

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

The settings in `~/.assent/assent.toml` show how to customize the
tier-to-model mapping and the abstract-to-CLI effort translations. The lookup
order is always:

1. Task file's explicit `effort` annotation (if present)
2. The configured `default_effort` override for this tier (if present)
3. The built-in default for this tier

A stated `[adapter.<name>.default_effort]` table overrides per tier; it does
not replace the built-in table. An absent, empty, or partial table therefore
still leaves every tier with a value — write only `lite`, and `prime`/`core` keep
their built-in defaults. The result is that every supported invocation passes a
concrete effort to the CLI; assent never omits the flag and inherits the
vendor's own default.

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

### Reading the session line

When `run` opens a session it prints one compact line stating the whole
resolved identity:

```
  Session: codex | core->gpt-5.6-terra | heavy->high
```

Read it as adapter, then two mappings. Each arrow points from the portable
abstract value the task file states, on the left, to the actual argument sent
to that adapter's CLI, on the right — so `core->gpt-5.6-terra` is the `--model`
value and `heavy->high` is the `--effort` value for this run. All four audit
facts (adapter, tier, model, effort) are on the line; it stays a single line
and is not expanded back into verbose labels.

### Configuring Antigravity print timeout

Antigravity's `--print-timeout` is independent of Assent's watchdog timeout.
The print timeout limits how long the CLI will wait for a single print
invocation to complete; the watchdog limits how long Assent will wait for any
output before killing the session.

In `~/.assent/assent.toml` (or a project override):

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
  — installed to `~/.assent/format.md` and refreshed there on every successful
  `assent init`.
- Working-instructions template: [assent/templates/instructions.md](assent/templates/instructions.md)
  — assent session behavior and cross-project common rules; installed to
  `~/.assent/instructions.md` and refreshed there on every successful `assent
  init`; project rules stay in `AGENTS.md`.
- Config template: [assent/templates/assent.toml](assent/templates/assent.toml)
  — adapter selection, the abstract tier (prime/core/lite) mapping table,
  abstract effort (heavy/normal/slight) defaults and CLI-value translation,
  watchdog, and retry parameters. It seeds `~/.assent/assent.toml` on first
  init; a later init adds only missing active table/key paths there and
  preserves existing and custom values.

These three are the tool's own files, so each exists once per machine. A
project holds only its `AGENTS.md`, its `.assent/verify.py`, its work folders,
and — only where an older layout or a deliberate decision put one there — a
project `.assent/assent.toml` override.

### Tasks that use project media

An image, PDF, audio file, or other media a task needs is ordinary project
context, so the plan schema stays unchanged — there is no `inputs`, image,
audio, or video field, and assent never attaches a file to an adapter or infers
what media a model can read.

- Name an existing media file by its project-relative path, with its purpose,
  in the task's `behavior` or `notes`. A read-only reference path does not need
  to enter `scope`.
- Every media file the task may create or modify must be covered by `scope`.
- Prefer versioned worktree files so the run is reproducible; do not put source
  media in the generated `.assent/` management plane.
- `verify` keeps the objective checks; visual or perceptual judgment stays a
  human call at `accept`, not a second review state.

The format contract carries a worked example.

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
changes before rerunning. The `assent.lock` file you find in the folder is not
part of that recovery: the OS released the real lock when the process died,
and the file is only the last run's diagnostics. Leave it alone.

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
