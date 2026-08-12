# Assent workflow contract

> `~/.assent/workflow.md` defines scheduler, CLI, report, receipt, and
> acceptance behavior. Read only the sections relevant to the current review or
> configuration question. `format.md` separately owns plan-file schemas.

## Invariants

- Git is mandatory. Work folders are explicit or derived unambiguously; there
  is no current-folder pointer or Git-less mode.
- Preserve every token-burned edit and diagnostic. Failure, interruption, and
  repair never revert the workspace automatically.
- Scope checks fail closed. AI roles never own task-contract, receipt,
  acceptance, target-ref, or Git mutations; the scheduler performs validated
  state transitions.
- Verification is evidence. Only the explicit human `assent accept` command
  publishes work. No `run`, reviewer, fixer, or receipt accepts anything.
- Questions automation cannot settle become durable human-decision outcomes
  with exit zero. Infrastructure, refused preconditions, and broken safety gates
  remain nonzero failures.

## Settings and initialization

Settings resolve from lowest to highest priority:

1. built-in defaults;
2. `~/.assent/assent.toml`;
3. optional project `.assent/assent.toml`;
4. an applicable CLI override.

Tables merge by key; scalars and arrays replace whole values. Omission inherits.
An empty table adds no override; an accepted empty array is an explicit value;
empty required strings and invalid TOML are refused with their source path.

`assent init` refreshes `instructions.md`, `format.md`, and `workflow.md` in the
user home; adds only missing shared settings keys; creates the project verifier
once; and maintains the `AGENTS.md` bridge and `.gitignore` entry. It preserves
an existing project override and verifier. Every read and validation completes
before the first write. Before any AI session, all three user-home contracts
must be readable and match the installed version; otherwise run `assent init`.
CRLF and LF compare as equivalent text.

## AI session inputs and boundaries

Planning and acceptance meetings reach this file through `AGENTS.md` and
`instructions.md`. During `assent run`, the scheduler supplies each AI role the
minimum startup contract plus its responsibility, allowed-write policy,
relevant task files, and current evidence. Roles inspect source and tests from
the worktree as needed; they do not read raw `assent.toml` to infer their job.

Ordinary task and plan-worker sessions read project `AGENTS.md` and
`instructions.md`. Reviewer/fixer prompts point to the applicable project and
session rules, then provide the ability prompts, trusted task requirements, and
failure evidence needed for that decision. Do not infer permission or
responsibility from a role name.

The worktree is an isolation, conflict-management, audit, and recovery boundary,
not a security sandbox. Default adapters may use broad OS permissions because a
task must update its main-tree status/journal and tests may use system temp.
Prompt rules plus before/after checks detect unauthorized writes but cannot
prevent access to external files, credentials, network services, or Git writers.
Use unattended execution only in trusted environments.

One work folder permits one `run`, enforced by an OS lock on its persistent
`assent.lock`; file existence alone never means a live or stale lock. Different
folders may run concurrently in dedicated `<project>.worktrees/<folder>/`
worktrees.

## Workflow configuration

`[workflow]` has exactly three ordered arrays:

| Key | AI context | Scheduler action |
| --- | --- | --- |
| `task` | one task | `focused_test` |
| `plan` | completed plan, or plan-wide execution when `task = []` | `focused_sweep` |
| `integration` | exact reconstructed one-or-more-plan candidate | `full_verify` |

Each entry is a tagged union containing exactly one `role` or `action`. The
selected role's `[abilities]` carry what that session does through `prompt`,
`writes`, and optional `produces_verdict`; `[roles]` compose abilities and may
select model/effort. The engine never infers behavior from a role or ability
name. Task, plan, and integration reviewers should use different ability
prompts: they resolve one task's failure, cumulative plan conformance, and an
exact reconstructed selection respectively.

Actions are scheduler-owned and accept no role, adapter, model, effort, ability,
prompt, or arbitrary command. `focused_test` is legal only at task positions;
`focused_sweep` is legal only at plan positions; `full_verify` is legal only at
integration positions. AI roles never run these actions or the full suite.

Omission and empty arrays mean:

- omitted `task`: one implicit session per task using its model and effort;
- nonempty `task`: task-scoped roles/actions; if it contains `focused_test`, it
  ends with that action;
- `task = []`: no per-task sessions; the plan workflow makes the whole plan one
  unit using the union of task scope and focused gates. Every `plan` role step
  is an ordinary worker session evaluated according to the plan's focused gate;
  an empty plan in this mode is refused;
- omitted or empty `plan`: no plan review;
- omitted or empty `integration`: automatic integration repair is disabled;
- nonempty `integration`: starts and ends with `full_verify`; one action alone
  is valid.

A passing action completes its layer and skips every later role. A failing
non-final action advances to a verdict role. A writable verdict role reviews and
repairs either failure in one session, returning `FIXED`; a read-only verdict
role may be followed by one writable non-verdict fixer. The next action rechecks.
Neither form repeats a successful complete verification. Configuration validates
this structure before any session or verifier begins.

### Task layer

The task layer implements one `.e.toml` task. If its first `focused_test` passes,
the layer completes and skips repair. If it fails, the next configured task
verdict role reviews that evidence and may repair before the trailing test.

A role that self-marks `BLOCKED` advances to the next task verdict role and
skips the pending first test; its existing evidence is the input. A writable
verdict role must diagnose and repair a small task-local planning omission in
that same session. If no handler remains or the final test fails, the task stays
`BLOCKED` for human decision. Task failure never consumes a plan review position.

For one exact omitted scope path, a writable verdict role repairs that path in
the same session and returns a `scope_amendment`; a read-only verdict role may
return it for its configured fixer. The scheduler validates the path's
pre-session state and the complete session write set, then alone appends the
task scope. No AI edits the `.e.toml` task file.

### Plan layer

The plan workflow is considered only after every task is `DONE` or `SKIP`.
`focused_sweep` runs each distinct `DONE`-task command against the cumulative
worktree without a receipt. PASS completes the layer without opening a reviewer.
Failure opens the next configured plan reviewer/fixer, whose responsibility is
to decide whether the cumulative implementation conforms to the plan, including
cross-task interactions.

Findings must name one existing task and cite an existing requirement or a
concrete repair regression. They may not invent acceptance criteria, create
tasks, widen the plan, perform a repository-wide debt search, revert/delete
source, or accept. A writable role repairs only the implicated task's declared
scope or one validated exact addition; a read-only role writes nothing. Any
management-plane, task-file, other-task, primary-worktree, or Git write makes
the verdict unusable while preserving edits.

Only the first completed-plan review may record eligible technical debt. It must
be concrete, encountered in changed or directly interacting code, local to an
existing task, and testable by that task's focused gate. Rechecks may retain or
resolve it but cannot add debt. `_report.md` then flags `TECHNICAL DEBT REVIEW
REQUIRED` for the human meeting.

### Integration layer

After every selected plan boundary is complete, `full_verify` reconstructs the
same exact snapshotted selection and runs complete verification. PASS writes the
matching receipt and completes the layer without a reviewer. It never accepts.

On verifier failure, the next configured integration verdict role diagnoses and
repairs only that durable failure. On candidate conflict, the scheduler first
collects one complete typed conflict wave and runs zero full tests. Evidence
distinguishes:

- `target_alone`: one folder conflicts with the current target. Repair uses an
  Assent-managed source-first reconcile worktree and exact conflict paths.
- `peer_only`: selected folders conflict only when combined. Repair reopens the
  implicated existing task with compatible-prefix and three-way evidence; it
  never merges a speculative peer directly.

A writable verdict role repairs the complete assigned wave in one session; a
read-only verdict may hand it to the next fixer. Neither form may run Git,
Assent, focused tests, or the full suite. The scheduler owns prepare, staging,
commit, source fast-forward, focused checks, cleanup, candidate rebuild, and the
next `full_verify`.

The automatic path keeps the original exact selection. It never asks to skip,
silently removes a folder, accepts a compatible prefix, changes the target, or
publishes. Resume reuses content-identical source, target, prefix, merge,
focused-gate, and receipt evidence. Drift, ambiguous ownership, out-of-scene
writes, remaining conflict markers, or exhausted positions fail closed and
retain edits.

### Finite outcomes and recovery

Each configured position advances a durable `workflow_step_index` once. The
array itself is the only repair budget; prompts state current, total, and
remaining positions. There is no subjective no-progress or diff-oscillation
extension.

- PASS after repair completes the layer without another reviewer.
- If the array ends on `FIXED`, a settling gate re-runs the implicated focused
  command through a de-duplicating ledger. PASS settles `SELF-FIXED,
  UNREVIEWED`, preserving task statuses and exiting zero. Failure is distinct:
  the folder does not settle, no task becomes `BLOCKED`, edits remain, and the
  run exits nonzero.
- If it ends with an unrepaired blocker, it settles `REVIEW UNRESOLVED, HUMAN
  DECISION`, preserves all statuses, findings, journals, and edits, and exits
  zero so unrelated queued folders continue. Failed mechanical evidence still
  blocks acceptance.

`_auto_fix.toml` is version-7, untracked, deletable runtime memory, never task
status, source truth, receipt, or acceptance. It binds source tree, task-plan and
prompt digests, exact reviewer role/adapter/model/effort and workflow positions;
keeps cumulative findings, scope decisions, repair briefs/dispositions and
transitions; and records the recovery phase `NEEDS_REPAIR`, `REPAIRING`,
`AWAITING_REVIEW`, or `COMPLETE`. A restart resumes `REPAIRING` or
`AWAITING_REVIEW`; malformed state or missing or drifted workflow configuration
refuses repair and closeout. A fresh cached `PASS` requires every binding to
match.

Interrupted writes remain. When durable evidence proves exactly one task owns
them and all paths fit its scope, startup gathers them into a `wip` checkpoint
without opening AI; otherwise it refuses fail-closed for human recovery. A
repair worker acknowledges every current finding in its journal detail with:

```text
ASSENT_REPAIR_DISPOSITION {"fingerprint":"<64 lowercase hex>","disposition":"fixed|not_reproducible|still_blocked","detail":"concrete bounded evidence"}
```

The scheduler validates the exact fingerprint set; `still_blocked` requires a
`BLOCKED` closeout. Workflow repair never creates tasks, accepts, or deletes
source.

## Selection and command rules

Every explicitly named live folder is audited before dispatch. Each name,
including a prefix before `...`, must resolve to an existing `.assent/` folder
with a formal task file. Any unresolved set is reported in full and prevents all
selected work from starting. Restore/recovery may intentionally resume an absent
live folder.

The literal final token `...` appends every remaining folder the command itself
would discover. It is not `--all`, may appear only once and last, and cannot be
combined with `--all` or a mode that has incompatible cardinality. Expansion is
snapshotted before mutation. `verify` and `accept` add finished folders; `run`,
`clean`, and `archive` add all work folders. Native ordering then applies:
`run` preserves its explicit prefix and dependency-orders the remainder;
`verify` and `accept` dependency-order the whole set; `clean` is upstream-first.

Cardinality selects behavior: one folder uses the single-folder path; two or
more form one exact batch. Expansion never weakens exact receipt matching or
causes acceptance to verify.

`run [FOLDER ...]` executes the named folders in stated order; omitted selection
requires exactly one runnable folder; `--all` runs incomplete folders in
dependency order and `--jobs N` limits concurrency. It stops dispatching after
a genuine nonzero failure. A successful run follows plan and integration
workflows for the same selection. `--once` and `--task` defer automatic
integration when they leave the selected folder incomplete. No run accepts.

`status`, `check`, and `report` act on one explicitly named folder or all when
omitted. `check` also validates every task and the dependency graph.

For full option syntax, use `assent <command> --help`. The decision-relevant
behavior of verification, reconcile, acceptance, cleanup, and archive follows.

## Verification and receipts

Focused verification runs task commands in source worktrees, writes no receipt,
and cannot authorize acceptance. Complete verification builds a temporary
integration candidate and runs `.assent/verify.py` outside every AI session.
Its folder or exact-batch receipt is derived, deletable evidence bound to source
commits, reconstructed trees, verifier digest, and shared-input digest.

- `assent verify FOLDER --focus`: distinct `DONE`-task focused commands only.
- `assent verify FOLDER`: one folder candidate and folder receipt.
- `assent verify A B`: exactly that dependency-ordered selection, one candidate,
  one full run, one exact receipt. Conflict refuses the whole request; it never
  silently shrinks. A localized passing prefix cannot authorize the request.
- `assent verify --batch`: dynamically discovers finished, unintegrated folders.
  It tries the whole merge wave, reports every conflict and excluded dependent,
  and may ask once whether to verify the remaining independent subset. A reduced
  receipt names only that subset.

Verification changes no target or source ref and never accepts. A candidate
conflict is not a verifier failure. Complete folder verification refreshes that
folder's `_report.md` exactly once, best-effort, after its receipt operation and
all verification locks settle; focused and batch operations do not.

### Shared ignored inputs

Candidates start from tracked Git trees. Complete verification mirrors only:

1. reviewed-profile ignored directory links provisioned by Assent; and
2. ordinary ignored leaf files inside otherwise tracked directories.

Nothing is copied. Whole ignored trees, build output, caches, credentials,
editor state, `.git`, `.assent`, and link-target contents are never enumerated.
Destinations must be absent and ignored. Conflicting targets/content/kinds,
ancestor overlap, dangling or unsupported links, occupied destinations, or
unsafe parents refuse before verification or PASS evidence.

The primary worktree's untracked `.assent/manifest.toml` stores reviewed shared
directory profiles keyed by watched Git-ignore and dependency/build inputs:

- `UNKNOWN`: a real candidate has no matching decision;
- `REVIEWED-NONE`: matching review chose no paths;
- `REVIEWED-PATHS`: matching review chose exact same-relative primary targets;
- `STALE`: watched evidence or a declared target changed;
- `NO-IGNORED-DIRECTORY-CANDIDATE`: a successful current Git query found no
  ordinary ignored directory. This is not a semantic claim that none is needed.

Only `assent shared-paths review` writes the manifest. Every contributing source
link must match its active profile and exact primary target; undeclared manual
links refuse verification, reconcile, receipt freshness, reporting, and
acceptance. Receipts bind `shared_inputs_sha256` before and after verification;
acceptance rechecks it without provisioning or repair.

All candidate cleanup detaches every junction, directory symlink, or directory
reparse-point object before recursive Git or filesystem removal and never
traverses its resolved target. External targets survive success, refusal,
failure, interruption, and retry. If ownership, inventory, or detachment cannot
be proven, retain the managed path for Assent recovery.

## Manual reconcile

`assent reconcile FOLDER` handles a finished folder that conflicts with the
current target. It creates `<project>.reconcile/<folder>` on
`assent-reconcile/<folder>`, merges target into the exact source tip, and lets a
human edit only conflicted paths while Assent owns Git actions.

`--continue` refuses unresolved paths, conflict markers, whitespace errors, and
out-of-scene edits; then commits the merge, fast-forwards only the source,
invalidates source-bound receipts, and performs link-safe cleanup. `--abort`
requires clean state and removes only re-proven resources. No separate state
file exists: worktree, branch, `HEAD`, `MERGE_HEAD`, and parents provide
idempotent recovery evidence.

Reconcile runs no AI, focused check, or full verification and creates no
approval. After `--continue`, run verification again. It handles one source
against the current target, not peer-only selected-source conflicts.

## Reports and acceptance

`_report.md` is a mechanically regenerated, untracked meeting agenda: task
status/checkpoints, relevant journal summaries, verification freshness, and
review outcomes. It is informational, not authorization. `_technical_debt.md`
is likewise derived meeting evidence.

Direct `accept FOLDER` and selected `accept A B` never start verification.
Except for an ancestry-proven already-integrated no-op, they require a fresh
receipt matching exactly the source, reconstructed integration tree, verifier,
and shared inputs. Selected acceptance replays the recorded merge chain and
publishes all or none.

`accept --all` alone has two modes:

1. replay a fresh PASSED batch receipt atomically without verification; or
2. when batch evidence is absent or expired, sequentially run/reuse each
   folder's verification immediately before its ordinary accept, stopping at
   the first failure while preserving earlier publications.

A malformed batch receipt refuses instead of falling back. Every mode refuses
incomplete, locked, dirty, detached, ambiguous, dependency-unsafe, or
conflicting state. Acceptance performs no pull, push, rebase, force-push, source
deletion, or automatic conflict resolution. The integration lock serializes
Assent accept operations but cannot stop external Git writers.

## Cleanup, archive, rejection, and rework

`clean` removes a managed worktree and same-folder branches only after proving
cleanliness, expected ownership, and full integration. It retains upstream
source while any direct dependent remains unfinished, unaccepted, dirty,
missing, or unproven. There is no force mode. Multiple selections clean
upstream-first.

`archive FOLDER` requires finished, accepted, proven-integrated state; performs
the same guarded cleanup; then stores the live management folder in
`.assent/_archive/<folder>.zip`, updates the archive roster, and removes the
live directory. Named ineligible folders make a multi-folder request nonzero;
`--all` skips ineligible folders. Restore takes exactly one folder and validates
the archive before replacing nothing.

`reject` is an explicitly confirmed destructive reset. It checkpoints dirty
work, records Git tips in journals, link-safely removes the managed worktree,
force-deletes only same-prefix branches, and resets `DONE`, `WIP`, and `BLOCKED`
tasks to `TODO`; branch content is then recoverable by recorded hash only while
Git retains it. `rework` reopens existing tasks while preserving code unless
the human states `--revert-code`; reversal is allowed only for a mechanically
proven checkpoint tail. Neither command invents a current folder or implicitly
runs or accepts work.

`assent-integration/<folder>/<suffix>` and `assent-reconcile/<folder>` are
temporary Assent-owned branches. A survivor is proven orphaned only when the
repository-wide integration lock is held and no transaction owns it. Whether
its tree is `published` or `superseded` is reporting information only, never
the deletion criterion. `clean` with no folder sweeps both namespaces once per
invocation; `archive --all` inherits that sweep; named `clean FOLDER`
deliberately does not. `doctor` reports survivors and offers confirmed `[y/N]`
recovery.

## Checkpoints and interruption

The scheduler validates status, protected task structure, fail-closed scope,
and focused evidence before a terminal checkpoint. A resumed task may receive
an empty terminal `auto(<folder>/tNNN)` commit because earlier WIP history owns
the changes. A clean legacy `DONE` task does not retroactively synthesize one.

Quota, checkpoint-resume control, Ctrl+C, adapter failure, and unclean exit keep
progress. Quota rotates immediately to the next configured adapter and waits
only after all are exhausted. The exact provider-neutral immediate-continuation
record is:

```json
{"type":"assent.checkpoint_resume"}
```

A wrapper may replace a provider quota result with it only after arranging an
immediate continuation; if it forwards provider quota, Assent performs the
normal wait or rotation. When quota evidence and this record are both present,
the ordinary quota path wins.

On startup, provably single-task, in-scope dirt becomes a `wip` checkpoint
without an AI session; ambiguous or out-of-scope dirt stays for human recovery.
Writes that escaped into the primary worktree are ported back only when all
paths fit the task and the transfer is unambiguous; otherwise both trees remain
untouched and the run refuses.

Never manually remove managed worktrees or temporary branches. History rewrites
must preserve `auto(<folder>/tNNN):`, `wip(`, `rework(`, and `accept(` subjects,
must not occur during rework, and make all verification receipts stale.
