# Design consensus

*[Traditional Chinese reader edition](zh-TW/CONSENSUS.md)*

> Distilled from three rounds of discussion (Claude Fable × GPT-5.6), updated
> as the architecture evolves. Goal: the most robust balance between
> "trustworthy, reliable output" and "aggressive token savings". The single
> contract for the current format is `.assent/format.md` (source lives at
> `assent/templates/format.md`); this document records the design principles
> behind the format. This document is normative design rationale for the
> project, not the executable task-format contract itself — that contract is
> `assent/templates/format.md` alone.

## Core idea

The product namespace is `assent`, and its management plane is `.assent/`.

Rather than have an AI cleverly pick relevant content out of thousands of
lines, layer the context so each task only needs to load "the minimal
context sufficient to start work unambiguously".

```text
Project rules    → AGENTS.md (root; the entry point tools auto-load;
                    whether it is versioned is the project's call)
Working instructions → .assent/instructions.md (assent session behavior and
                    cross-project common rules)
This task         → .assent/<work folder>/tNNN_name.toml (task file,
                    self-contained for execution)
Current state     → the task file's status + git (the task file is the
                    state; there is no separate state file)
Historical evidence → rNNN_name.toml (one log file per task, append-only,
                    not read by default)
Proof of correctness → the task file's verify command
                    (defaults to .assent/verify.py)
```

## Four core principles

1. **Layering**
   Rules, tasks, state, and history are never mixed into the same file. An
   execution session's required reading is only the project AGENTS.md +
   instructions.md + the one assigned task file; a meeting session adds
   format.md, and a review meeting adds the work folder's `_report.md`.

2. **Generated, not a snapshot**
   Early designs had a hand-written `CURRENT.md` navigation snapshot, and "a
   stale authoritative snapshot is more dangerous than no snapshot at all".
   The current architecture drops the hand-written snapshot entirely: state
   lives in the task files and git, and the human-readable `_report.md` is
   mechanically assembled by the program and fully rewritten every time, so
   it can never diverge from fact. Priority of facts: program behavior and
   test results → source code and Git → task files → r-file logs.

3. **Rewrite, not append**
   A task file states the present tense: the scheduler writes `status` back
   precisely, leaving every other byte untouched. Process detail is appended
   to the r file and never edits existing entries. A task file is
   "self-contained for execution" (goal, scope, and acceptance are written
   directly in it), but shared knowledge is referenced rather than copied to
   avoid version drift; project-specific decisions that stay valid across
   plans settle into AGENTS.md.

4. **Tests prove correctness**
   Prose states intent; `verify` proves correctness. "The executing AI
   claims DONE" is only a claim — completion is objectively reviewed by the
   scheduler: status → structural diff (tamper guard) → scope → the task's
   focused verify exit 0, and only committed as a checkpoint once every step
   passes. A summary
   states only verifiable facts; pending must not be dressed up as
   completed.

## Verification, receipts, and human acceptance

The scheduler separates focused task verification from complete candidate
verification. During an AI task session, and in `assent verify FOLDER --focus`,
the distinct task-level `verify` commands run in the folder's source worktree.
Focused verification writes no receipt, creates no integration candidate, and
cannot authorize acceptance. After a folder is complete, unattended full
verification builds a temporary integration candidate and runs the complete
`.assent/verify.py`; the result is a derived `_verification.toml` receipt.
`assent verify <FOLDER>` is the zero-token single-folder refresh.

Explicit selection is exact. `assent run A B` runs exactly A then B in the
written order and stops on the first failure; `assent run A B --all` runs that
sequence first, then the remaining incomplete folders in dependency order.
`assent verify A B` normalizes exactly A and B to dependency order, merges them
into one integration candidate, and runs the full verifier once. A selected
conflict refuses instead of shrinking the set. A successful PASSED batch
receipt records exactly the selected source identities, intermediate trees,
final tree, and verifier digest; a failed request may leave a localized PASSED
prefix but still returns failure and cannot authorize the original selection.
None of these verification or run commands changes a target ref or accepts a
folder.

`DONE` is the executing AI's claim, a receipt is the scheduler's complete-
verification evidence, and `accept` is explicit human approval. Direct
`assent accept <FOLDER>` never runs the verifier: an ancestry-proven
already-integrated source is an idempotent no-op, otherwise the command
requires a fresh exact per-folder PASSED receipt and replays its reconstructed
candidate. Selected `assent accept A B` likewise never verifies, and requires
a fresh batch receipt for exactly the dependency-ordered set A and B; it
replays every recorded merge and publishes all selected folders atomically or
none. Neither form expands its set or silently accepts leftovers.

`assent accept --all` deliberately has two modes. A fresh PASSED batch receipt
is replayed and released atomically without new verification, publishing only
the folders it records; finished folders outside it are only reported. A
missing or expired/non-PASSED batch receipt selects the sequential path, which
calls `verify_folder_if_needed` before each not-already-integrated folder and
then performs the ordinary receipt-backed accept in dependency order. An
already-integrated folder is an ancestry no-op; a finished folder whose source
branch and worktree were both cleaned after proven integration is skipped only
on this path. A malformed batch receipt refuses rather than falling back. The
first real verification or acceptance failure stops the sequential chain while
earlier publications remain.

Receipts are disposable derived artifacts and never outrank Git. A target tip
change is acceptable when the rebuilt integration tree is identical; a content
change makes the receipt stale. Direct and selected acceptance refuse missing,
malformed, stale, or mismatched evidence instead of starting verification.
Passive merge metadata is for human audit only, not a post-clean state
database; retain upstream sources while dependents remain unaccepted. All
acceptance paths keep local human approval, refuse conflicts without advancing
the target, and provide no remote, pull, rebase, force, automatic conflict
resolution, or source-deletion behavior.

The lifecycle is `run` -> focused checks -> explicit full `verify` (single,
selected, or dynamic batch) -> human review -> `accept` -> ordinary Git
synchronization, if desired -> `clean`. A verification receipt alone never
publishes anything.

The packaged verifier checks the working tree and the candidate's committed
delta against its first parent, safely skipping that second check for a root
commit. This catches committed trailing whitespace without reading a project's
private verifier or invoking a shell. `assent init` never replaces an existing
`.assent/verify.py`; projects synchronize it manually. Changing the verifier
digest makes prior receipts stale, so unattended `assent verify <FOLDER>` must
refresh the evidence before acceptance.

A worktree is the boundary for change isolation, conflict management, audit,
and Git recovery, not a security sandbox. Full-permission modes such as
`danger-full-access` and `bypassPermissions` still expose resources available
to the AI's OS identity, including network, credentials, external Git writers,
and files outside the worktree. Users must choose trusted project and account
environments; the product does not add a container or VM sandbox.

## Location conventions

- `AGENTS.md` must stay at the project root — agent tools automatically look
  for the instructions file at root, and the location itself is a feature.
  It holds only project rules and one assent bridge line; when versioned,
  the worktree's branch version is used, and when not versioned, the
  scheduler's prompt supplies the main-tree absolute path.
- assent session behavior and cross-project common rules live in
  `.assent/instructions.md`, not mixed into the project's `AGENTS.md`. Every
  other management file is likewise kept under `.assent/`, leaving root
  clean.
- The entire `.assent/` is excluded by `.gitignore` and kept only in the
  main worktree; the scheduler hands instructions, t/r files, and the
  default verification script to a worktree session as absolute paths,
  never producing a second source of truth.
- The verification script defaults to `.assent/verify.py` in the main tree,
  holding the project's own check commands; it is loaded from the main tree
  but reviews the isolated result with the worktree as cwd.
- Git is always enabled and every folder always uses a worktree; this is not
  to be replaced by a toggle or a git-less degraded mode — it is what makes
  running multiple work folders in parallel safe. Any tracked `.assent/`
  file fails closed, to prevent a second source of truth.
- A work folder's `assent.lock` guarantees one run per folder; a worktree
  path is `<project name>.worktrees/<folder>/`, and the work folder can be
  stated via a positional argument.
- Work-folder names use the portable Windows/Git-ref contract: non-empty, no
  whitespace, path separators, control characters, Git-ref-forbidden
  characters (`~`, `^`, `:`, `?`, `*`, `[`), or Windows-forbidden characters
  (`<`, `>`, `"`, `|`); no leading `-` or `.`, `..`, `@{`, trailing `.` or
  `.lock`, or reserved Windows device name. The name is also the Git branch
  prefix, so this validation is applied before any worktree or branch is made.

## Folder-dependency consensus

Dependencies follow the work folder: a folder may declare direct upstreams
via `_folder.toml`'s `after`; no such file means no upstreams. A folder's
completion is not tracked by hand; it is derived on the spot from every
formal task file in it, complete only when all are `DONE` or `SKIP`. Both
`run`'s upstream gate and `check`'s full dependency-graph validation are
fail-closed: an incomplete upstream, a reference to something nonexistent, a
parse failure, or a cycle all refuse to continue.

The `after` declaration controls readiness, while only an explicit `base`
selects reproducible stacked file content. A downstream may stack on zero or
exactly one not-yet-accepted upstream through `base`; additional `after`
upstreams provide ordering only, and a missing `base` starts from the current
integration target rather than becoming an implicit integration engine. The
operational sequence is `run A`, `run B` stacked on A, combined verification,
human `accept A`, then human `accept B`. A matching receipt can be reused when
its source tip, integration tree, and verifier digest still match, so direct
and selected accept remain fast evidence checks rather than full-suite reruns.
If A advances, B is stale but its work is retained; rework/reject B or open a
new folder instead of rewriting stack history. Same-file edits use ordinary
Git integration: exact-tree verification covers an automatic merge, while a
conflict leaves the target unchanged for human resolution. Assent never
rebases, resolves conflicts, or pushes. Cleanup is upstream-first: direct
dependents retain source evidence until accepted and mechanically proven
integrated and clean, after which clean may remove redundant artifacts without
a separate state database.

## Batch conflict-skip consensus (2026-07-26)

`verify --batch` never resolves a source conflict itself; it only decides,
once, whether to certify a smaller batch instead of none. Building the batch
candidate merges every queued folder in turn regardless of an earlier
conflict, so one conflicting folder does not stop a later, independent one
from being attempted. A conflict-free batch stays fully unattended. When one
or more folders conflict, every conflicting folder and its transitively
queued-`after` downstream are collected and reported together, then exactly
one `[Y/n]` question offers to skip that whole set and verify the remaining,
still-mergeable folders. A clear yes runs one full verification over that
smaller subset and records only it in the receipt; no, an unrecognized
answer, or EOF is fail-closed and certifies nothing. A batch with nothing
independent left to offer refuses outright without asking.

Skipping is deliberately not a form of resolution: it changes nothing about
the target or any source, conflicting or merged, and the conflicting
folder's own source still requires an explicit human `rework` or `reject`
before it can rejoin a future batch. `accept --all` has two distinct modes:
with a fresh PASSED batch receipt its release path publishes only the exact
receipt folders in one atomic ref update and only reports every other finished
folder left out; it does not verify or accept those leftovers in the same run.
With no receipt, or with expired/non-PASSED evidence, its intentional
sequential path verifies and accepts folders one by one, stopping on the first
real failure while preserving earlier publications. A malformed receipt
refuses instead of selecting that fallback. `archive --all` extends the same
upstream-first rule `clean` already enforces: it archives only a folder that
is independently eligible and continues to retain the source evidence an
unaccepted dependent still needs.

## Model and reasoning-investment consensus

`model` and `effort` are orthogonal abstract tiers. A task's model is fixed
to `prime` / `core` / `lite`; the optional effort is fixed to `heavy` /
`normal` / `slight`, usually omitted and written explicitly only when
deliberately deviating from the adapter's default for that model. The three
effort values describe a portable relative investment, not a precise
budget; `heavy` does not claim to equal any vendor's native maximum tier.

Effort resolves in two steps, selection then translation. Selection is
deterministic and has three ordered sources: the task's explicit value, the
configured `default_effort` override for that tier, and the built-in per-tier
default. A stated `default_effort` table overrides per tier rather than
replacing the built-in one, so an absent, empty, or partial table still leaves
every known tier with a value. The consequence is the decision this settles:
every supported invocation passes a concrete requested effort, and assent never
omits the flag to inherit a vendor CLI's own default. After the abstract value is
selected, the engine looks up the `efforts` config in the order "tier
subsection > flat > built-in baseline". The built-in baseline maps `heavy` to
`high`, `normal` to `medium`, and `slight` to `low`; each abstract key falls
back independently from the tier subsection to the flat table and then to the
baseline. Abstract and vendor effort names intentionally differ, so an
abstract value cannot be sent through unchanged. The flat layer expresses the
adapter's general rule; a model-tier subsection only needs to hold the few
exceptions. Vendor-specific effort values are configuration data at the same
level as the models mapping table, and must not enter the task format,
`default_effort`, or adapter code; the adapter interface only receives the
already-translated actual value.

Because the identity is now fully resolved before a session opens, `run`
states it in one compact line, `Session: codex | core->gpt-5.6-terra |
heavy->high`: the adapter, then each abstract value on the left of an arrow
beside the actual CLI argument on its right. The four audit facts — adapter,
tier, model, effort — stay intact on one line and are not expanded back into
verbose labels.

## Media as ordinary project context

Media a task works with — an image, a PDF, an audio file, a video — is project
context referenced by the textual task contract, not a schema feature. The
fixed task fields therefore do not change: assent adds no `inputs`, image,
audio, or video field, no adapter attachment protocol, no inference about which
media a model can consume, and no second review state.

A task that uses a media file already in the project names its
project-relative path and its purpose in `behavior` or `notes`. A path that is
only read need not enter `scope`; every media file the task may create or
modify must be covered by `scope`, exactly like source. Media belongs in
versioned worktree files so a run is reproducible, and never in the generated
`.assent/` management plane. `verify` still carries the machine-checkable
requirements, and visual or perceptual judgment stays part of the explicit
human `accept`.

This holds until a concrete adapter attachment requirement proves a schema
change is necessary; the textual contract is the cheaper answer while it
suffices.

## Quality standard (replacing token-count KPIs)

**Cold-start test**: given only AGENTS.md + instructions.md + any one `TODO`
task file, can a fresh AI with zero memory correctly state the goal, the
changeable scope, the acceptance conditions, and the next step without
asking questions? Yes → the plan is final; no → the task file lacks
information. The machine-side equivalent: `assent check` passes — this is
also the planning meeting's adjournment condition.

What this architecture eliminates is the O(n) growth of "re-reading all
history every time": scheduling, review, and reporting are all local,
pure-Python work at zero tokens; the only remaining real cost is the code
and verification output that each task session needs to check.

## Maintenance discipline

- AI handoff most easily slips at the end of a session, when context is
  nearly full → the closeout protocol is fixed in
  `.assent/instructions.md` and does not rely on self-discipline: the
  scheduler's structural diff makes "relaxing your own review" an
  immediate failure, and the scope exemption covers only the task's own t
  file and r file.
- The human role is reduced to review and adjudication: read `_report.md`
  (zero tokens), open a session only for the tasks that need a decision and
  issue instructions; humans never hand-edit files — editing is always done
  by an AI following instructions.
- Token-burned output from the executing AI is never discarded: a quota
  interruption is collected into a wip checkpoint and resumed; a failed
  review is not reverted and is retried with the reason attached; once
  retries are exhausted, the results are committed into a BLOCKED
  checkpoint together, for human adjudication.
- Merged-worktree and branch cleanup is performed mechanically by `assent
  clean`; the safety condition must be proven by the machine, and a human
  never runs Git cleanup by hand. Rejecting an entire folder's
  implementation is likewise performed mechanically by `assent reject`
  (archive, force-delete, reset tasks to TODO, leave a trace in the r
  file), again without manual Git operations.
- Redoing a single task's review is performed mechanically by `assent
  rework <FOLDER> <TASK>`. Code is kept by default, downstream propagation
  must be stated explicitly; reverting code only accepts checkpoints
  provably forming a contiguous branch tail, and creates a new commit
  rather than rewriting history. The operation only updates state and the
  report, and does not start an AI automatically.

## Upgrade path (add structure only once there is a pain point)

| Pain point | Only then add |
|---|---|
| A new round of goals gets mixed with the current plan | Open a new work folder; the old folder continues participating in dependency resolution as an `after` upstream |
| The same decision keeps getting overturned, and an AI keeps re-adopting an already-rejected approach | Settle it into AGENTS.md's Permanent constraints |
| Multiple tasks repeat a large amount of shared explanation | Extract it into a reference (a file or an anchor); the task file keeps only a pointer |

Do not pre-build a document bureaucracy for problems that may never appear.

## The one-sentence summary

> AGENTS manages project rules, instructions manages assent behavior, task
> files manage the here and now, r files manage history, and verify manages
> truth; an execution session by default reads only AGENTS + instructions +
> its own task file, objectively reviews at the end, writes back precisely,
> and archives the detail.
>
> Documentation exists so an AI can take over quickly; Git and the
> scheduler's objective gates exist to guarantee facts; humans make the
> adjudications — and the precondition for saving tokens is always that
> output quality stays trustworthy and reliable.
