# assent workflow reference (the CLI, report, and receipt contract)

> This file lives at `~/.assent/workflow.md`, installed and refreshed by
> `assent init` alongside `instructions.md` and `format.md`. It holds the
> exact CLI-flag mechanics, receipt/report internals, and scheduler-internal
> plumbing that `~/.assent/format.md` deliberately excludes: `format.md`
> owns persisted artifact schemas, filename rules, and state meanings; this
> file owns CLI, report, and receipt contracts — the same split
> `instructions.md`'s "Contract ownership" section states.
> Planning AI and Executing AI do not need this file to write or execute a
> task file. A Review AI adjudicating an accept/rework meeting, and any
> human operating the `assent` CLI directly, are this file's audience.
> This file is extended reading beyond `format.md`/`instructions.md`, not a
> replacement for either.

## Settings precedence

Every setting resolves through one fixed order, lowest priority first:

1. assent's built-in defaults
2. the user settings in `~/.assent/assent.toml`
3. the optional project override in `.assent/assent.toml`
4. an explicit CLI selection, where a command supports one (`--config PATH`
   selects which project-level file plays role 3, and `--jobs` and similar
   flags override the corresponding setting for that invocation)

Tables merge by key; scalars and arrays are replaced whole. A project override
therefore shadows later edits to the shared user settings for exactly the keys
it states, and `assent init` never migrates such a file into the user home or
rewrites it — it is preserved byte for byte and reported as an override.

Omitting a key is the only way to inherit the layer below:

- `key =` is not "no value"; it is invalid TOML and the file fails to load.
- An empty table (`[adapter.claude.efforts]`) states no leaf override at all,
  so every key inside it still resolves from the lower layer.
- An empty array is an explicit replacement wherever that field accepts one,
  not a request to fall back to the lower layer's list.
- An empty or whitespace-only string is refused for any setting that requires
  useful text (a command, an adapter name, an effort value). The error names
  the dotted key and the file that stated it, rather than silently reinstating
  a lower layer.

## Initialization and contract freshness

`assent init` is both the installer and the upgrader, and it is safe to repeat:

- In the user home it rewrites `instructions.md`, `format.md`, and
  `workflow.md` to this installation's packaged text, and adds only the
  packaged settings keys that `assent.toml` does not already state, never
  replacing a stated value.
- In the project it creates `.assent/verify.py` once, refuses `--test` when
  that verifier already exists, adds the single bridge line to `AGENTS.md`, and
  adds the `.assent/` entry to `.gitignore`.
- An older project copy of `instructions.md`, `format.md`, or `workflow.md` is
  removed only when it matches the packaged text exactly. One that differs is
  kept and reported as a warning: sessions read the user-home contracts, so
  move anything worth keeping out of the local copy and then delete it
  yourself.
- An existing `.assent/assent.toml` is likewise kept byte for byte and reported
  as a compatibility override that outranks the user settings.
- Every read, parse, and merge happens before the first write, so invalid TOML
  or an invalid `--test` choice refuses without leaving the user home or the
  project half-upgraded.

Before opening any session, assent fails closed unless all global contracts
are present, readable, and byte-identical to this installation's packaged
text. A missing, unreadable, or stale contract names the offending path and
points at `assent init`; it is never patched, merged, or silently regenerated
mid-run. Because the comparison reads text with universal newlines, a file an
editor rewrote with CRLF still counts as the same contract.

## Execution permissions and the worktree boundary

The executing AI must be able to write to the main-tree `.assent/`, because
changing its own task file's status and appending to the r file are part of its
job. If a project whose entire `.assent/` is gitignored uses codex
`workspace-write`, the main-tree `.assent/` will be read-only, which does not
meet the task closeout requirement. The executing AI must also be able to write
to the system temp directory — tempfile-style tests write there, and
`workspace-write` refuses that too. These two are why the default configuration
uses `danger-full-access` rather than `workspace-write`; both must be permitted
when tightening the sandbox. The worktree isolates changes, supports conflict
management, and gives Git auditable recovery boundaries; it is not a security
sandbox. The main-tree escape detection described under "Lifecycle and review
(the objective gate)" below is a mechanical, after-the-fact defense, not a
preventive one, and does not change this non-sandbox positioning. With
`danger-full-access` or `bypassPermissions`, the AI can still reach resources
available to its OS identity, including external Git writers, network,
credentials, and files outside the worktree. Use unattended execution only in
trusted project and account environments. Assent does not create a container
or VM sandbox, and it must not claim to intercept those external effects. The
executing AI must also stay tidy: delete temporary probes or shims once used,
and above all leave no embedded git repo behind.

## Run locking and parallel execution

A single work folder allows only one `run` at a time, locked by the
`assent.lock` inside that folder; different folders can run in parallel in
different terminals. Git is always enabled and always uses a dedicated
worktree at `<project name>.worktrees/<folder>/`; the positional argument
`assent run <folder>` can state the work folder explicitly, and is orthogonal
to `--config`. Ownership is an OS-level lock tied to the open file handle, not
the file's existence: `assent.lock` is left behind on purpose as a diagnostics
record of the last run, and the lock itself is released by normal exit,
Ctrl+C, a crash, and forced termination alike. Do not read an existing
`assent.lock` as an active run and do not delete it as a recovery step; there
is no stale-lock procedure.

## CLI and task-selection rules (scheduler execution semantics)

1. First check whether the work folder still holds a deactivated
   `tNNN_name.toml`; if so, require it to be moved and fail closed; otherwise
   scan `tNNN_name.e.toml` only in **lexicographic filename order**, explicitly
   excluding `*.r.toml` (the three-digit number guarantees the order).
`assent run [FOLDER]` runs only that folder when the folder is stated
explicitly. When `FOLDER` is omitted, it derives from current task state and
folder `after` upstreams the single folder that "has a `TODO`/`WIP` and all
upstreams complete"; with no candidate or more than one, or if any folder fails
to resolve, it refuses to guess and requires an explicit folder.
`assent run --all` runs all incomplete folders in folder-dependency order;
`--jobs N` limits how many folders run at once, but may not be combined with
`FOLDER`, `--once`, or `--task`.

`assent run A B` is an explicit ordered selection: it runs exactly `A`, then
exactly `B`, in the order written. Each folder still checks its own task and
folder prerequisites; the explicit list is not silently reordered. The command
stops at the first configuration or run failure. `assent run A B --all` first
runs that explicit sequence and, only if it succeeds, runs the remaining
incomplete folders in folder-dependency order through the `--all` scheduler.
Neither form verifies or accepts a folder implicitly.

Before either explicit sequence starts, Assent audits every stated name as a
live work-folder identity. A name resolves only when the existing folder
discovery finds its directory and at least one formal `tNNN_name.e.toml` task
file. If one or more names are unresolved, all of them are reported together
and the command returns nonzero before the first selected folder is dispatched;
this audit includes an explicit prefix before `...`. It does not check task
status, dependency readiness, locks, receipts, or Git state, which remain the
operation's own gates. The same boundary applies to explicit `status`, `check`,
`report`, `verify`, `clean`, `accept`, `reconcile`, `reject`, `rework`, and live
`archive` selections; omitted and dynamic discovery modes retain their existing
contracts. Archive restore and recognized archive crash-resume states may use a
missing live directory.

The literal token `...`, given once as the last positional argument, is the
remainder selector shared by `run`, `verify`, `accept`, `clean`, and `archive`:
`assent run A B ...` means A, then B, then every other work folder, in
folder-dependency order. It is a remainder operator, not an alias for `--all`.
The whole selection is snapshotted before anything is mutated, so a folder that
appears during the operation cannot join it, and the expanded set is printed
first. Each command expands by its own discovery rule -- `verify` and `accept`
add only finished folders, `run`, `clean`, and `archive` add every work folder
and decide per folder afterwards -- and then applies its own ordering: `run`
keeps the stated prefix order and takes the remainder in folder-dependency
order, `verify` and `accept` normalize the whole selection to dependency order,
and `clean` normalizes it upstream-first. A folder name can never contain `..`, so the token cannot
collide with one. `...` more than once, or anywhere but last, is a usage error,
as is combining it with `--all`, `verify --batch`, `verify --focus`,
`run --once`, `run --task`, or the one-folder `archive --restore`. An expansion
that selects no folder is refused rather than treated as a no-op.

`assent run --verify` runs one complete verification after the run, and only
when the run exited zero: a failing run is returned as it is, because there is
no finished plan to certify. The verification matches the selection -- one
folder (named or auto-selected) as a folder receipt, an exact multi-folder
selection, including a `...`-expanded one, as that selected batch, and `--all`
or a bare `...` as the whole-project dynamic batch, which is why a bare `...`
happens to verify like `--all` while an explicit prefix plus `...` certifies
exactly the folders it ran. The verification's exit code becomes the command's
exit code. `--verify` may also be combined with `--once` or `--task`: those
selectors stop after a single task, so the request verifies only when that
limited run left the single selected folder complete, and an incomplete folder
fails the request without writing a receipt. The refusal names the incomplete
task ids and statuses and happens before any integration candidate is created
or any full verifier starts; it is a failure, not a silent skip. As an
invocation-level request `--verify` verifies regardless of the configured
`receipt_refresh` policy.

Under the default manual receipt-refresh policy, successful run closeout defers
the per-folder receipt. If `--verify` was requested, that closeout identifies
the run-level verification that follows in the same invocation instead of
telling the user to start that verification command again.

`run --auto-fix` does not select folders, reorder them, widen scope, or change
the meaning of `--once`, `--task`, `--all`, `...`, or `--verify`. It is passed to
every explicitly selected folder and every folder launched by `--all`; with
`--once` or `--task` the review/repair gate runs only if that limited invocation
leaves the folder complete. A successful bounded loop then lets `--verify`
continue to its separately requested complete verification. A nonzero run or
auto-fix loop does not start that full verification, and no form of auto-fix
publishes a Git ref.

Every production folder-level complete verification operation uses the same
closeout boundary: after `verify_folder(cfg)` or
`verify_folder_if_needed(cfg)` settles and releases its verification locks, it
refreshes `_report.md` after the receipt operation and lock release exactly once
on a best-effort basis. This includes PASSED and
FAILED results, stale-receipt replacement, fresh-receipt reuse,
malformed-receipt refusal, incomplete-folder no-op, and interrupt outcomes; the
refresh observes the resulting receipt state and never changes or masks the
verification result or interrupt. This invariant covers direct folder verify,
single-folder `run --verify`, automatic run closeout, and the sequential
per-folder fallback of `accept --all`. Selected or dynamic batch verification
and `--focus` write no folder receipt and therefore do not refresh a folder
report merely for symmetry.

`assent verify FOLDER --focus` is the distinct focused mode and requires one
folder. It runs each distinct `verify` command belonging to a `DONE` task in
that folder's source worktree. It creates no integration candidate and writes
no receipt; even a pass cannot authorize `accept`, because complete candidate
verification has not run.

`assent verify A B` is an explicit selected full-verification batch. It requires
at least two distinct folder names, normalizes them to dependency order, and
verifies exactly that set in one temporary integration candidate with one full
verifier run. A selected conflict refuses the whole request instead of asking
to skip or silently shrinking it. A successful PASSED batch receipt records the
exact selected folders, source identities, intermediate trees, final tree, and
verifier digest; a failed request may leave a localized PASSED prefix but
still returns failure and cannot authorize the original selection. Verification
changes no target ref and performs no acceptance. `assent verify A ...` is the
same selected batch, expanded over the finished folders; cardinality still
chooses the path, so an expansion down to one folder is the single-folder
receipt refresh and rejects `--no-bisect` exactly as `assent verify FOLDER`
does.

The exact selected path is labeled `verify selected`, not `verify --batch`;
`verify --batch` is reserved for dynamic discovery and its interactive
conflict-skip policy. If exact candidate construction conflicts, the diagnostic
first states that the full verifier did not run, identifies the conflicting
folder and paths, and states that no receipt was written and the target and
selected source refs were left unchanged. A folder that conflicts with the
target on its own is directed to `assent reconcile <FOLDER>`. A peer-only
conflict names the compatible selected prefix ahead of the conflicting folder
and recommends verifying and accepting that prefix before reconciling the
conflicting folder against the advanced target; `assent rework <FOLDER> <TASK>`
and `assent reject <FOLDER>` remain explicit alternatives. The exact request
never asks to skip or silently shrinks its set.

The only automatic exception is the combined `assent run ... --verify
--auto-fix` `[workflow].selection` sequence. Its `full_verify` action first performs a
candidate-only scan and starts zero full-test runs when conflicts exist. One
typed conflict wave records every independently discoverable conflicting
folder and path, target/source/compatible-prefix identities, excluded selected
dependents, and `target_alone` versus `peer_only`. The next explicit read-only
selection reviewer must assign every folder/path to exactly one existing task;
the following explicit write-capable selection fixer position authorizes the
whole wave, and a later `full_verify` action is the only way to rebuild and run
the real full verifier. The selection remains exact throughout: Assent never
accepts a prefix, asks to skip, changes the target, or publishes anything.

A target-alone assignment reuses the source-first `assent-reconcile/<folder>`
transaction. The scheduler supplies the configured fixer every task contract
and bounded base/ours/theirs evidence and permits writes only to Git's exact
conflict paths; the AI may not stage, commit, change refs, invoke Assent, or run
focused or complete tests. Assent then applies the ordinary continue checks,
creates the merge, fast-forwards only the source, invalidates source-bound
receipts, and cleans managed resources. A peer-only assignment never merges a
speculative peer: it reopens the exact reviewed existing task set and supplies
the compatible-prefix and three-way evidence as read-only repair context.
Normal scope gates, task focused checks, final focused sweep, checkpoints, and
source-transition evidence apply before the exact candidate is rebuilt.

The selection cursor and reconcile Git facts recover preparation, AI editing,
continue, source fast-forward, cleanup, and rebuild by content identity. A
matching completed merge or PASSED receipt is reused instead of duplicated;
target/source drift, malformed evidence, ambiguous ownership, out-of-scene
writes, remaining markers, and exhausted reviewer/fixer/action positions fail
closed while retaining edits. A conflict wave consumes no `full_verify`; a
real verifier starts only after a complete rebuild is conflict-free, and final
success requires one fresh PASSED receipt for the entire snapshotted set.

When `status`, `check`, and `report` state `FOLDER` explicitly they act only on
that folder, and act on all folders when it is omitted. `check` additionally
validates the full dependency graph and cycles; a read-only command that hits an
error in any folder finishes as a failure.

`assent clean [FOLDER ...]` removes provably redundant worktrees and same-folder
prefix branches at fixed locations; it takes one folder, several
(`assent clean A B`, or `assent clean A ...`), and acts on all work folders when
none is named. Several folders are cleaned in one upstream-first pass with every
folder's own evidence rule unchanged. Each folder must be able to acquire the existing `assent.lock`, have a
fully clean worktree, and have all same-prefix branches and detached HEADs
merged into the main tree's current HEAD, before it first removes the worktree
with ordinary protection and then deletes branches with `git branch -d`. Before
that recursive worktree removal it inventories every directory link and
directory reparse point without entering it, detaches each recognized link
object, and refuses an unsupported reparse point; a failed proof keeps the
worktree and target intact. Any insufficient proof keeps it and states the reason; there is no
force-delete option, and it never touches `.assent/`.

`assent archive <FOLDER>` (or `--all`) one-way retires a finished folder: it
strictly contains `clean` -- reusing its mechanical proof and removal for any
still-present source branch/worktree -- then acquires the folder's
`assent.lock`, creating it if missing: unlike `clean`'s `probe_lock`, which
refuses to touch `.assent/` and so cannot tell a missing lock file from an
unprovable one, archive already writes into `.assent/<FOLDER>/`, so a missing
lock file is proof nobody holds the folder rather than a reason to skip.
Holding that lock across the rest of the operation blocks a concurrent
run/reject/rework and closes the probe-then-act window; it then compresses
`.assent/<FOLDER>/` (excluding `assent.lock` itself, a runtime artifact that
never enters the zip) into `.assent/_archive/<FOLDER>.zip`, registers the
folder in the `.assent/_archived.toml` roster, and deletes the live
directory -- which also removes the lock file, so there is no separate
release step; `clean` never archives in return. `FOLDER` and `--all` are
mutually exclusive, and bare
`archive` naming neither is a parser error so a missed folder name cannot
archive everything by accident; `--all` archives every eligible folder in
lexicographic order, prints a skip reason for each ineligible one, and exits 0
unless a real error occurred. Two or more names (`assent archive A B`, or
`assent archive A ...`) archive an explicit selection and keep single-folder
`archive`'s contract rather than `--all`'s: the human named those folders, so a
folder that is merely ineligible is a refused request. Every named folder is
attempted so one refusal does not hide the rest, a summary line reports how many
were archived, and the command exits nonzero if any was not. Every step is crash-resumable: a re-run resolves
the current on-disk state against the roster and finishes only the steps still
missing. `archive --restore FOLDER` (incompatible with `--all`) reverses one
archive by extracting the zip back to the live directory, deregistering the
folder, and deleting the zip; it refuses when the live directory already
exists or no archive exists. The two archived file bodies carry separate
responsibilities: the zip under `.assent/_archive/` exists only to serve
`--restore` and may be deleted or moved elsewhere at any time, losing only the
ability to restore, while `.assent/_archived.toml` is the sole basis for
dependency resolution and must be kept. The source removal inherits the
non-traversing cleanup boundary: Assent detaches each directory-link object
before any recursive removal and never deletes through its resolved target, so
external targets survive archive success, refusal, failure, interruption, and
retry.

`assent reject <FOLDER>` lets a review meeting reject a whole folder's
implementation: after acquiring the same `assent.lock`, it first fully parses
the task files, then resolves the folder dependency graph for direct
dependents. A dependent whose own run lock is busy always refuses the whole
command without prompting; otherwise any dependent not yet provably accepted
defaults to refusing and is listed, and proceeding past it requires an
explicit human confirmation to accept stranding it. It then stashes
uncommitted changes as a wip commit, records each branch's full tip hash as
evidence, then removes the worktree through the non-traversing link boundary,
deletes same-prefix branches with `git branch -D`, and finally reverts
DONE/WIP/BLOCKED tasks back to TODO and appends a `rejected` record with full
Git evidence, plus the confirmed
would-be-stranded dependent list when there was one, to the r file. The
status reset is intrinsic to rejection, not an exception to routine cleanup;
if link inventory/detachment or any other Git step fails it does not enter the
task-file reset, and rerunning the same command uses the guarded boundary again
without touching an external target.

`assent rework <FOLDER> <TASK>` lets a review meeting reopen a single task; both
positional arguments are required, with no omission-derivation, `--all`, or
`--once`. By default it keeps the code and reopens only the target; started or
completed downstream blocks the operation unless `--cascade` is stated to revert
them to TODO as well. `--reason TEXT` preserves the adjudication reason.
`--revert-code` reverses the code with a new commit only when all related
checkpoints form a contiguous tail of the current branch; if that cannot be
proven it fails closed, and never rewrites history. On success it regenerates
`_report.md` with the updated plan, does not print the full report, does not
start an AI, and does not run `run` automatically.

Within any folder: a `WIP` task -> prefer it, carrying a "resume" prompt to
continue; otherwise take the first `TODO` whose every `dep` is `DONE` / `SKIP`.
When a `TODO` task's r file last entry is `rework_requested`, the scheduler
likewise attaches to the prompt the rejection reason plus a reminder that any
remaining old implementation and old tests must be re-evaluated from scratch,
not treated as spec.
A `BLOCKED` task only blocks tasks that have it as an upstream; other tasks run
as usual. When every task is `DONE` / `BLOCKED` / `SKIP` it finishes, prints a
summary, and updates the `_report.md` inside the work folder.

Every completed task, plan, or exact-selection adapter invocation gets one
best-effort record in the repository-level `_usage.jsonl`, including distinct
retry, quota, and checkpoint-resume invocations. One selection record names its
exact folder set and is presented in every contributing folder's report without
being duplicated in the evidence. Missing, malformed, interrupted, unsupported,
or unwritable usage evidence never changes adapter handling, retries, task
status, verification, receipts, exit codes, or acceptance.

`_report.md` includes `AI usage (provider-reported)`. It groups by adapter and
provider-reported actual model; a missing actual model is visibly labeled
`requested:<requested_model>`, or `unknown` when neither identity exists. Each
token category is summed separately and shows available-record coverage, so a
missing counter is never estimated or reinterpreted as zero and no misleading
cross-category total is shown. The section states unavailable or partial
coverage, ignores malformed derived records, and does not reconstruct sessions
from before collection existed. Usage is observability only: there is no price,
budget, quota-control, receipt-freshness, verification, or acceptance meaning.

## Review, acceptance, and cleanup lifecycle

`assent accept <FOLDER>` is a human's explicit acceptance decision, made after
reading the folder's `_report.md` and inspecting task results and checkpoint
evidence — `DONE` is only the executing AI's completion claim, never a human
approval by itself.

`FOLDER` and `--all` are mutually exclusive alternatives. Two or more explicit
folder names select an exact batch, for example `assent accept A B`, and
`assent accept A ...` expands that selection over the remaining finished
folders. A remainder-expanded selection is an ordinary exact selection: it
takes the single-folder path when it expands to one folder and the selected
batch path otherwise, it requires evidence for exactly the expanded set, and it
never starts the verifier. `...` and `--all` cannot be combined.

`assent accept FOLDER` is explicit human approval for one finished folder. It
never runs the full verifier. If the current source tip is already an ancestor
of the target, it is an ancestry-proven idempotent no-op and needs no receipt.
Otherwise it requires a fresh PASSED per-folder receipt, reconstructs the
integration candidate, and publishes only when the source tip, reconstructed
tree, and verifier digest exactly reproduce that receipt. Missing, malformed,
stale, or mismatched evidence refuses and points to `assent verify FOLDER`;
accept never starts that verifier itself. A missing source is a refusal for a
directly named folder, not permission to infer acceptance from old metadata.

A single-folder accept whose derived auto-fix state is SELF-FIXED, UNREVIEWED
adds one interactive confirmation as the last gate before the merge. Every
receipt-based check has already passed, so this is not a refusal: the only
missing thing is the independent confirmation the finite round list never
produced, and only a human can supply it. Assent names the self-fixed round,
its adapter/model/effort, and the `_report.md` holding the repaired findings,
then asks `Publish it anyway? [y/N]`. Only an exact `y`/`Y` publishes; anything
else, including EOF from a closed or non-interactive stdin, declines, merges
nothing, and changes no Git state. `_auto_fix.toml` is deletable derived
memory, never acceptance evidence, so a malformed record cannot manufacture
this gate on a folder whose receipt evidence is complete.

A single-folder accept whose derived auto-fix state is REVIEW UNRESOLVED,
HUMAN DECISION gates the same way, after the same receipt-based checks
already pass: Assent names the round position and identity that produced the
unresolved findings and, for each one, its task, path, and summary -- not
merely a count -- then asks `Publish it anyway? [y/N]`. The same decline rule
applies, with no Git side effect. A folder carrying both the self-fixed gate
condition and this one is asked exactly once, naming both reasons; two
prompts for one decision would train a human to answer without reading.

`assent accept A B` is the selected exact-batch human approval path. The names
are normalized to dependency order, and the command requires a fresh PASSED
batch receipt produced for exactly that set by `assent verify A B`. It never
verifies, skips a conflicting folder, broadens the set, or falls back to
per-folder verification. It replays each recorded `--no-ff` merge in a
temporary candidate and publishes all selected folders with one target-ref
update, or publishes none. A malformed, stale, wrong-set, drifted, or
conflicting receipt-backed chain refuses with every source and the target
unchanged. Folders outside the selected set are neither verified nor accepted.

`assent accept --all` is the only intentional exception to the no-implicit-
verification rule, and it has two distinct modes:

1. With a fresh PASSED batch receipt, it replays the exact recorded dependency-
   ordered chain, compares every intermediate tree, and releases it atomically
   without a new verifier run. A malformed receipt is unsafe evidence and
   refuses; it does not fall back. The release publishes exactly the receipt's
   folders, consumes the receipt after publication, and only reports finished
   folders it did not cover -- it does not verify or accept those leftovers in
   the same run.
2. With no batch receipt, or with absent/expired/non-PASSED batch evidence, it
   takes the sequential folder path. It walks finished folders in dependency
   order and calls `verify_folder_if_needed` before each folder that is not
   already integrated; that step runs or reuses the folder's complete
   verification receipt. It then calls the ordinary receipt-backed
   `accept FOLDER`. An already-integrated folder is an ancestry no-op without a
   new verifier run. A finished folder whose source branch and worktree were
   both removed after proven integration is skipped only on this `--all` path.
   The first real verification or acceptance failure stops the chain, while
   earlier publications remain published.

All acceptance modes still refuse incomplete, locked, dirty, detached,
ambiguous, or dependency-unsafe state; conflicts never get auto-resolved, and
the target does not advance on a failed gate. There is no remote, `--push`,
pull, rebase, force-push, source deletion, or automatic conflict-resolution
behavior. Human acceptance remains the explicit `accept` action; a verification
receipt alone never publishes anything.

The integration lock serializes Assent `accept` operations. It is not an
atomic barrier against external programs, so users must not run Git commands
that write the same main worktree during acceptance. After acceptance and any
separately chosen synchronization, `assent clean <FOLDER>` may remove the
source only when its independent merged-and-clean proof succeeds; cleanup
never deletes source before that proof.

## Temporary integration and reconcile branches

`assent-integration/<folder>/<suffix>` (including the folder-independent
`assent-integration/batch/<suffix>` a batch verification candidate uses) and
`assent-reconcile/<folder>` are the two Assent-owned temporary branch
namespaces. Both name branches a human must not check out, build on, or
delete by hand: each is removed by the transaction that created it -- a
verification candidate's cleanup, or `reconcile --continue`/`--abort` --
once that transaction completes. A branch in either namespace survives only
when its owning transaction died before finishing, which makes it orphaned.

A surviving branch is proven orphaned by the repository-wide integration lock
being held while nobody is otherwise integrating, never by inspecting the
branch itself: `assent clean --all` and `assent doctor` both re-read the
branch list inside one hold of that lock, so what they act on is exactly what
the lock proves has no live owner. `gitops.temporary_branches` additionally
reports each surviving branch as `published` (its tree is already reachable
from the target, so ancestry alone would find no leftover) or `superseded`
(its tree is not); that distinction is reporting information only and is
never the deletion criterion; a content- or reachability-based gate would
collect none of the branches this sweep exists to remove, because the
integration lock is the only thing that tells the difference between "still
mid-transaction" and "orphaned."

`assent clean --all` runs this sweep exactly once per invocation, after its
per-folder cleanup, because the two namespaces are folder-independent and no
per-folder path can otherwise ever see them; a single named `assent clean
FOLDER` deliberately does not sweep, so that naming a subset of folders never
deletes repository-global refs as a side effect. `assent archive --all`
inherits the identical sweep by delegating to the same implementation after
its own per-folder loop, rather than reimplementing it, and for the same
reason: every per-folder archive step already holds the integration lock the
sweep must take for itself. `assent doctor` reports any orphaned temporary
branch it finds and offers one confirmed `[y/N]` removal, re-checking the
branch list inside the same lock immediately before deleting so an answer of
"y" can never remove a branch that stopped being an orphan between the report
and the confirmation; doctor's offer is the recovery path, while `clean --all`
and `archive --all` are the routine, unattended one.

## Opt-in folder review and bounded repair

The `[workflow]` table has exactly two keys. `task` selects task-scoped context
and task-by-task accountability; `plan` selects plan-scoped context and plan
accountability. A key selects the scheduler-supplied execution layer and its
granularity, not permission. The selected role's `[abilities]` carry what that
session does (`prompt`, `writes`, and `gate`), and `[agents]` only compose those
abilities with optional model and effort choices. Ability prompts therefore do
not decide whether their context is a task, plan, or folder. The only special
behavior the engine infers from a role is an ability's `produces_verdict`; it
activates the provider-neutral folder-review verdict protocol.

With ordinary task execution, only `run --auto-fix` walks `plan` as the
folder-level review/repair workflow:

```text
assent run FOLDER --auto-fix
```

```toml
[workflow]
plan = [
  { role = "folder_reviewer", adapter = "codex" },
  { role = "bounded_fixer" },
  { role = "folder_reviewer", adapter = "antigravity" },
]
# task = [{ role = "task_worker" }]
```

Both keys are ordered arrays of role steps. A verdict-producing `plan` role
requires `adapter`; a role with `produces_verdict = false` omits it. In the
folder-review layer, a writable non-verdict step authorizes the bounded
task-profile repair described below, while a verdict-and-write role is a merged
reviewer-fixer and may report `FIXED`. Every verdict step resolves its role's
model and effort through its adapter mappings. Preflight is keyed by the full
`(adapter, requested_model, requested_effort)` identity, so two steps using the
same adapter with different models are both proven.

All omitted and empty boundaries are explicit. An absent `[workflow]` table is
identical to both keys being omitted. An omitted `plan` and `plan = []` both
configure no folder review; with no plan, `run --auto-fix` reports that the flag
had no effect and continues as an ordinary run. An omitted `task` keeps one
implicit session per task using that task's own model and effort. A non-empty
`task` runs its stated roles for each task with task-scoped context and keeps
each task as its own accountability unit. `task = []` is intentionally
different: it disables per-task sessions and makes the whole plan one unit,
executed by the `plan` steps with plan-wide context and the union of task scope
and focused gates. In that plan-execution mode, every `plan` step is an ordinary
worker session; a non-verdict step succeeds or retries according to the plan's
focused gate, not a reviewer verdict. An empty `plan` then leaves nothing able
to execute and is refused. The removed `[auto_fix.review]` table is never
recognized alongside this one: config loading fails closed and names the exact
settings-layer file that must be edited.

The folder-level workflow is considered when no task can make further progress:
the folder is complete, or it is quiescent-blocked with durable worker or
focused-gate evidence. The flag is selection-orthogonal: it is forwarded for automatic, explicit
single/multi-folder, prefix-plus-`...`, and `--all` selections and is compatible
with `--once`, `--task`, and `--verify`, whose own ordering and scope rules do
not change. A limited run defers the completed-folder loop when it leaves work
incomplete; a quiescent blocked dependency with durable worker or focused-gate
evidence may enter the separate blocked-adjudication review. Neither path
spends a workflow step before its own evidence gate. Without the flag, configured
review is inert. Complete verification requested by `--verify` follows only a
successful run and loop according to the existing receipt policy; auto-fix
neither performs that verification nor accepts. A missing receipt, an unrun
full suite, or the absence of complete verification is never a reviewer
failure.

The folder-level order is:

1. Run each selected task session and its ordinary focused gate.
2. Once every task is `DONE` or `SKIP`, run each distinct `DONE`-task `verify`
   command once as a final focused sweep. A folder with only `SKIP` tasks has
   no implementation review to run.
3. If every final focused command passes and the source remains clean, start
   the configured reviewer with the cumulative checkpoint diff, all task
   contracts and journals, directly interacting code, and the focused evidence.
4. For a quiescent blocked folder, use the durable BLOCKED worker or
   task-focused-gate evidence as the blocker input; this blocked-adjudication
   entry point does not run a new focused command merely to manufacture one.
5. Accept exactly one provider-neutral `PASS`, `FIXED`, or `FAIL`: PASS ends
   the loop; FIXED and FAIL are persisted and only this invocation's flag
   authorizes further repair.
6. Resolve every finding to one existing task and its declared scope. The
   reviewer may approve one exact mechanically valid scope addition, but the
   scheduler alone performs that one transaction; the worker and reviewer
   never edit task files. Reopen only existing implicated tasks, repair, then
   repeat the task gate, final sweep when applicable, and review.
7. Stop on PASS, adapter/infrastructure failure, an unresolved ownership or
   scope decision, or the end of the configured plan, always retaining
   edits and evidence. No runtime human adjudication step is inserted into
   the loop.

A completed-folder round is a merged reviewer-fixer session, not a strictly
read-only gate. When it finds a genuine blocking problem it may repair it
directly, writing only inside the declared scope of the one existing task its
finding names, and reports that with the verdict `FIXED`. Every other write --
a management-plane file, a task file, another task's scope, a commit, or any
write in the primary worktree -- is refused by the same structural safety gate
an ordinary worker session faces, which makes the verdict unusable while
preserving the exact edits. Assent also captures the protected management
surfaces before and after the round interval; any detected management write
refuses the review. `PASS` is returned only when nothing blocking remains and
the round wrote nothing at all. `FAIL` remains the verdict for a blocker the
round may not repair itself, such as an exact scope omission, and for blocked
adjudication, which stays read-only and must write nothing. The configured
`danger-full-access` or `bypassPermissions` execution default remains in
force; this prompt-plus-detection rule is cooperative write detection, not a
security sandbox or a preventive permission boundary.

The loop terminates by walking `[workflow].plan` by position. Every step
advances the durable `workflow_step_index` exactly once, and reaching the end
ends automation finitely. A sequence that runs out on a `FIXED` verdict step does
not settle immediately: the scheduler first re-runs the implicated task's own
focused gate against the repaired source, reusing the same de-duplicating
ledger that skips a command already proven earlier in this invocation, so the
settled claim -- that every task passed its own focused gate -- is proven of
the final repair and not only of the state that preceded it. When that gate
passes, the folder settles as the distinct SELF-FIXED, UNREVIEWED outcome: the
durable state records the round position, the total rounds used, that round's
adapter/model/effort, the gate evidence that just proved the repair, and the
finding fingerprints nothing confirmed, and the run exits zero. Nothing is
reverted, reopened, or re-marked, so a repaired task keeps the `DONE` its own
focused gate proved rather than being turned `BLOCKED`. The scheduler writes
one journal entry per implicated task and refreshes the report. That outcome is
terminal rather than a resumable phase: a later `run --auto-fix` reports it
again and starts no further round, and only a human `rework` reopens the
folder. The one thing missing is independent review confirmation, which only
the human `accept` decision can supply.

When that settling gate instead fails, this is a distinct disposition,
separate from both SELF-FIXED, UNREVIEWED and an ordinary `BLOCKED` task: the
folder does not settle, no `self_fixed_unreviewed` record is written, the run
ends nonzero, no task is marked `BLOCKED`, and the repair, the findings, and
every edit stay on disk exactly as the round left them. This is the one case
where a `FIXED` round's exhaustion still ends nonzero.

A sequence that instead runs out on an unrepaired blocker preserves every
finding, edit, and journal without another round, but no longer exits nonzero:
it settles as the equally distinct REVIEW UNRESOLVED, HUMAN DECISION outcome
and the run exits zero. Every task keeps the status its own closeout gave it;
nothing is reverted, reopened, or marked `BLOCKED`. This replaces a prior
nonzero exit deliberately: an unresolved review finding is a question the
scheduler cannot decide, not an infrastructure failure, and because the folder
scheduler's `--all` launch loop only keeps starting folders while none has
failed, a nonzero exit here used to silently cancel every unrelated folder
still queued behind it in the same invocation. The unresolved question instead
becomes a durable terminal record and a distinctly named report state, and the
human acceptance meeting -- not the run -- is where it is decided. `assent
accept <FOLDER>` gates on it exactly as it gates on SELF-FIXED, UNREVIEWED:
after every receipt-based check already passes, it names the round position
and identity that produced the unresolved findings and each finding's task,
path, and summary, then asks one explicit `[y/N]` confirmation before
publishing; a folder carrying both outcomes is asked once, naming both
reasons.

### `_auto_fix.toml` derived-state contract

`<FOLDER>/_auto_fix.toml` is a folder-local, untracked, deletable runtime
artifact. It is included in the runtime exclusions with `_assent.log`,
`_report.md`, locks, and verification receipts; it is not a task file, a new
task status, acceptance evidence, or a source-of-truth database. It may be
rebuilt from the current folder run and must never be staged or committed.
Version 7 has exactly these scalar fields and ordered table collections. The
`phase` field is required; it makes crash recovery explicit rather than
inferring a repair boundary from task statuses alone:

```toml
version = 7
source_tree = "<40-or-64 lowercase hexadecimal tree id>"
task_plan_sha256 = "<64 lowercase hexadecimal digest>"
review_prompt_sha256 = "<64 lowercase hexadecimal digest>"
reviewer_role = "folder_reviewer"
reviewer_adapter = "<registered adapter>"
reviewer_model = "<resolved requested model>"
reviewer_effort = "<resolved requested effort>"
phase = "COMPLETE"              # NEEDS_REPAIR / REPAIRING / AWAITING_REVIEW / COMPLETE
verdict = "PASS"                 # PASS, FIXED or FAIL
review_context = "completed_folder" # completed_folder / blocked_adjudication
review_stage = "initial"          # initial / recheck
failure_trigger = ""              # worker_blocked / focused_gate_failure / empty
workflow_step_index = 0           # next [workflow].plan position to walk
reviewer_step_index = 0           # exact position that produced this verdict
current_finding_fingerprints = []

[[findings]]
fingerprint = "<64 lowercase hexadecimal digest>"
kind = "correctness"            # correctness | safety | unmet_requirement | focused_test_gap | eligible_technical_debt | blocked_recovery | scope_amendment
task_id = "t001"                 # empty string represents a null reviewer id
path = "project/relative/path"
summary = "concise blocker"
evidence = "specific evidence"
recommendation = "focused repair recommendation"
scope_addition_path = ""
scope_addition_path_state = ""

[[observed_states]]
source_tree = "<40-or-64 lowercase hexadecimal tree id>"
finding_fingerprints = []

[[reviewer_recommendations]]
fingerprint = "<64 lowercase hexadecimal digest>"
recommendation = "the current review recommendation"

[[approved_scope_additions]]
fingerprint = "<64 lowercase hexadecimal digest>"
task_id = "t001"
path = "project/relative/new-file.py"
path_state = "new_file"       # existing_file | new_file

[[scope_amendments]]
finding_fingerprints = ["<64 lowercase hexadecimal digest>"]
task_id = "t001"
paths = ["project/relative/new-file.py"]
path_states = ["new_file"]
task_before_sha256 = "<64 lowercase hexadecimal digest>"
task_after_sha256 = "<64 lowercase hexadecimal digest>"
plan_before_sha256 = "<64 lowercase hexadecimal digest>"
plan_after_sha256 = "<64 lowercase hexadecimal digest>"

[[worker_dispositions]]
task_id = "t001"
fingerprint = "<64 lowercase hexadecimal digest>"
disposition = "fixed"         # fixed | not_reproducible | still_blocked
detail = "bounded acknowledgement evidence"

[[repair_briefs]]
task_id = "t001"
finding_fingerprints = ["<64 lowercase hexadecimal digest>"]
brief = "scheduler-persisted reviewer-to-worker repair brief"

[[plan_digest_transitions]]
before_sha256 = "<64 lowercase hexadecimal digest>"
after_sha256 = "<64 lowercase hexadecimal digest>"

[[review_transitions]]
fingerprint = "<64 lowercase hexadecimal digest>"
transition = "initial"        # initial | still_present | repair_regression | newly_exposed
prior_fingerprint = ""
transition_evidence = ""

# At most one settled outcome, written only when the workflow plan ran out on a
# FIXED verdict step.
[self_fixed_unreviewed]
round_index = 1                  # zero-based position of the self-fixed round
rounds_used = 2
adapter = "codex"
model = "prime"                  # abstract tier
effort = "heavy"                 # abstract effort
finding_fingerprints = []
```

The actual file may use empty arrays instead of the example tables. The
serializer writes a null `task_id` as `""`; readers restore it to null. The
reviewer never supplies a fingerprint: Assent normalizes each finding and
computes its identity from `kind`, `task_id`, `path`, `summary`, `evidence`,
`recommendation`, and the optional `scope_addition` path and path state using
canonical JSON. A `PASS`
has no current findings; a `FAIL` or `FIXED` has at least one. The findings
ledger and `observed_states` retain prior evidence.

Version 7 retains every applicable version-6 ledger and recovery field. It
adds `reviewer_role`, `workflow_step_index`, and `reviewer_step_index` so
freshness and restart bind the exact configured role, resolved identity, and
step position. Version 6 had added the
at-most-one `self_fixed_unreviewed` outcome and removed version 5's
`repair_round_assignments` and `consumed_fixer_profiles`: there is no
escalation ladder to record, because each reopened task is repaired under its
own ordinary task profile and nothing is consumed, so an interrupted round
resumes on exactly the same identity. These records are evidence, not worker or
reviewer permission to edit task files.

The recovery phases have fixed meanings. `NEEDS_REPAIR` is a durable `FAIL`
awaiting an authorized rework round; `REPAIRING` means the current bounded
repair round is under way; `AWAITING_REVIEW` means that round's task work
completed and the next configured round must run; and `COMPLETE` is valid only
for a `PASS` with no current findings. `review_context` distinguishes a completed-folder review
from blocked adjudication, and `review_stage` distinguishes the first review
from a recheck. A restart resumes `REPAIRING` or `AWAITING_REVIEW` from the
stored evidence, while a missing or drifted workflow configuration
refuses repair and closeout rather than treating the state as a cache miss.

An unclean exit that interrupts a round after it has already written a repair
-- during `REPAIRING` or `AWAITING_REVIEW`, before the verdict that would
advance `workflow_step_index` is recorded -- preserves the edit and leaves the
round index unmoved. The next run's startup recovery gate attributes that dirt
to the task this durable state's current findings implicate, using the same
scope-containment proof its other recovery owners use, and gathers it into a
`wip` checkpoint with no AI session opened. When ownership cannot be proven
this way -- dirt outside that task's declared scope, more than one plausible
owner, an unreadable state file, or no `REPAIRING`/`AWAITING_REVIEW` phase
recorded -- recovery still refuses fail-closed at `ensure_clean` rather than
guessing.

A schema-invalid reviewer record writes no auto-fix state and advances no
workflow cursor. While an adapter retry remains, the scheduler feeds the exact
validation error, a bounded rejected-output diagnostic, and one complete
non-PASS JSON example into the next otherwise unchanged reviewer prompt. If the
final invalid response wrote source, the scheduler gathers it into a
`wip(<folder>/<task>)` checkpoint only when all uncommitted paths fit exactly
one existing task's declared scope, then records recovery evidence without
changing the task's already-proven status. Ambiguous ownership, out-of-scope or
protected writes, and checkpoint failure retain the edits and refuse with an
explicit human-recovery diagnostic; Assent never widens scope, reverts, or
guesses an owner.

An exact fresh `PASS` requires all of `source_tree`, `task_plan_sha256`,
`review_prompt_sha256`, `reviewer_adapter`, `reviewer_model`, and
`reviewer_effort` to match the current invocation. A source or task-contract
change, or any change to the prompt inputs or resolved reviewer identity,
therefore makes old review evidence unusable. A malformed state refuses closed
rather than being ignored. Reports use the derived state as zero-token
evidence only:

- no file: `Folder auto-fix: NOT RUN (no review state)`;
- malformed file: `STALE (malformed review state: ...)`;
- source tree or task-contract drift: `STALE` with the changed binding;
- fresh `PASS`: `PASSED (fresh)`;
- fresh non-`PASS` verdict: `FAILED (fresh)` plus the current blocking
  findings;
- settled self-fixed folder: `SELF-FIXED, UNREVIEWED (fresh)` naming the
  self-fixed round position, the rounds used, and that round's
  adapter/model/effort, including the settling-gate evidence that proved the
  final repair;
- settled unresolved-review folder: `REVIEW UNRESOLVED, HUMAN DECISION (fresh)`
  naming the round position, the rounds used, that round's
  adapter/model/effort, and the findings no round resolved -- a distinct
  outcome from both `SELF-FIXED, UNREVIEWED` and an ordinary `BLOCKED` task.

A `FIXED` round's settling gate that fails is not one of these named report
states: it writes no settled outcome at all, so the report still shows the
pending `FAILED (fresh)` non-`PASS` verdict together with the failing gate's
command and evidence, and the run that produced it ends nonzero.

The report is informational and never authorizes acceptance. It renders the
phase, original blocker, current findings and recommendations, approved scope
additions, worker acknowledgements, repair briefs, the review round index
against the number of configured rounds, and the terminal `PASS` or nonzero
round-exhaustion/unresolved reason without starting an AI session or mutating
state.

### Review findings, repair, escalation, and recovery

Findings may cover correctness, safety, unmet requirements, missing tests, or
eligible technical debt in the cumulative diff and directly interacting code,
never a repository-wide search. A concrete local focused-test gap tied to an
existing task requirement is eligible; absent or unrun complete verification is
not. The review has two dimensions: `COMPLETED_FOLDER` or
`BLOCKED_ADJUDICATION` context, and `INITIAL` or `RECHECK` stage. Only
`COMPLETED_FOLDER + INITIAL` may introduce eligible technical debt. Blocked
adjudication and recheck may retain and resolve a debt entry but may not add
one. Unknown or ambiguous ownership, an out-of-scope path, or plan widening
requires a scheduler-owned exact decision, not a worker edit.

Re-review handles the prior current findings first. A blocker that remains
must retain its scheduler fingerprint and be recorded as `still_present`.
`repair_regression` is valid only when the repair delta evidences the blocker;
`newly_exposed` is valid only when it names an existing requirement exposed by
the repair. Once the prior set is cleared, the reviewer must return `PASS`.
Optional improvements, speculative concerns, and repeated debt discovery never
keep the loop open.

Automatic repair invokes ordinary code-preserving rework with reason
`Automatic repair of durable folder-review findings` and journal marker
`authorization: run --auto-fix`. It reopens only implicated existing tasks and
never creates tasks, edits requirements or scope, sweeps repository-wide debt,
reverts or deletes source, accepts, or creates a second task status. Only a
later explicit human `rework --revert-code` may remove the preserved code.

The durable repair brief is acknowledged in the worker task's journal detail
with one line per current finding, using exactly this provider-neutral syntax:

```text
ASSENT_REPAIR_DISPOSITION {"fingerprint":"<64 lowercase hex>","disposition":"fixed|not_reproducible|still_blocked","detail":"concrete bounded evidence"}
```

The scheduler validates every fingerprint and disposition before closeout;
`still_blocked` requires a `BLOCKED` task. This is acknowledgement evidence,
not permission for a worker or reviewer to edit a task file, change scope,
accept the folder, or start a human gate. The scheduler owns task status, one
reviewed exact-scope transaction, and Git state.

Each reopened task is repaired under its own ordinary task profile. There is no
escalation ladder and nothing is consumed, so an interrupted round resumes on
exactly the same identity and multi-task findings and dependency cascades never
escalate one task at a time. What bounds modification is the configured round
list: running out of rounds on an unrepaired blocker settles the folder as the
distinct REVIEW UNRESOLVED, HUMAN DECISION outcome, preserving the ledger,
journals, WIP/checkpoint edits, and unresolved statuses for the human
acceptance meeting without reverting code or inventing tasks, and exiting zero
so the rest of an `--all` invocation still starts; running out on a `FIXED`
round instead settles the folder as SELF-FIXED, UNREVIEWED once its repair
re-proves the implicated task's own focused gate, or, if that gate fails,
settles nothing and ends the run nonzero. There is no runtime human
adjudication prompt inside the loop.

Interruption, quota wait, adapter/focused failure retains edits and state. A
later `run --auto-fix` resumes the pending ledger, WIP, and workflow cursor only
when `[workflow].plan` still has the exact role, resolved identity, and step
position that decided the stored state; missing or drifted policy refuses repair and
closeout. A settled SELF-FIXED, UNREVIEWED or REVIEW UNRESOLVED, HUMAN
DECISION folder is terminal: it is reported again and no further round starts.
A run without the flag
may continue ordinary tasks but starts no review or repair. Successful flow
remains focused task gates, optional auto-fix review/repair/final focused sweep,
optional complete verification after a successful run or explicit `--verify`,
human review, accept, then clean. A reviewer never treats the missing receipt
or an unrun full suite as its own failure.

## Lifecycle and review (the objective gate)

`assent run` uses two verification stages. During each AI task session, the
scheduler runs only that task's focused `verify` command before creating its
checkpoint. `assent verify FOLDER --focus` repeats the distinct DONE-task
commands in the source worktree; it writes no receipt and cannot authorize
acceptance. The second stage builds one temporary integration candidate outside
any AI session and runs the complete `.assent/verify.py` once. The candidate is
at `<project>.integration/target-<uuid>`, a sibling of
`<project>.worktrees/`, on branch `assent-integration/<folder>/<uuid>`. It is
the merged tree being verified and remains present throughout the entire test
run, then is removed after the tests finish. Its result is a derived
`_verification.toml` receipt; the report shows whether the receipt is `PASSED`
or `FAILED` and `fresh` or `stale`. To reproduce the stage manually, use that
candidate as the verifier's cwd while it exists and run the verifier script
from the main worktree, such as `python <main-worktree>/.assent/verify.py`.
Cleanup runs in a `finally` block, covering normal completion, Python
exceptions, and Ctrl-C. Only a hard kill, such as `taskkill /F`, or power loss
can leave residue; Assent has no automatic stale-candidate recovery. Do not
manually run a raw Git worktree-removal command or recursive deletion against
that residue. Preserve the exact candidate path and branch as recovery
evidence, and have the owning Assent recovery/retry path re-prove their
identity, inventory directory links and other directory reparse points, and
detach each link object before it removes anything recursively. If that proof
cannot be completed, the path, branch, and external target remain in place.

That candidate is built by `git worktree add`, so untracked and ignored paths
are absent from it. Complete verification therefore mirrors, and mirrors only,
two kinds of artifact from the source worktrees that enter the candidate, at
the root or nested below tracked parents: the reviewed-profile ignored
directory links Assent provisioned — Windows junctions and directory symlinks, POSIX directory
symlinks — and ordinary ignored leaf files that sit inside an otherwise tracked
directory, such as a generated `lib/models/task.g.dart` beside its tracked
source. A directory is mirrored as a link to the same resolved target, a file
as a candidate-side link to the source file (a same-volume hard link on
Windows, a file symlink on POSIX); only missing parent directories are created,
and cleanup removes exactly those. Nothing is copied, and nobody has to prepare
hardlink twins or turn a generated file into a symlink by hand.

Arbitrary ignored content is never exposed. Whole ignored directory trees are
pruned rather than enumerated, so `.git`, `.assent`, build output, caches,
credentials, editor state, and every path inside a mirrored link's target stay
out, as does any file whose parent chain is not part of the candidate's tracked
tree. Each destination must be absent from the candidate and ignored there; a
provisioned artifact may add an ignored path, never replace or shadow tracked
content. Several sources contribute the union of their artifacts: the same
relative path resolving to the same directory target, or to a file with the
same content digest, is one artifact, while conflicting targets, differing file
contents, a path that is a directory in one worktree and a file in another, an
overlap between one artifact and another's subtree, a dangling or unsupported
link, an occupied destination, and a link that cannot be created all refuse
before the verifier runs and before any `PASSED` evidence exists. The mirrors
live only for the verifier run and are removed before the temporary worktree
is, deepest path first, so neither creating nor cleaning a candidate ever
traverses, modifies, or deletes a linked target, and the source worktree's own
links and files survive success, failure, and interruption. Assent detaches each
directory-link object before any recursive Git or filesystem removal and never
traverses its resolved target. External link targets survive success, refusal,
failure, interruption, and retry. Record a required private package or large
asset directory through the shared-path review so Assent provisions its
reviewed-profile link; there is no project setting, hand-created-link fallback,
or force flag that widens any of this.

The scheduled-task instructions tell a zero-memory worker to record a required
ignored directory through the review command, never copy it or hand-create a
source link (a copy can pass focus but is pruned from the candidate). When full
verification names a path inside a physically present ignored source directory,
its unchanged output and exit code gain one `Ignored input diagnosis:` note:
the named directory, its intentional omission, and the directory-link remedy.
Matching normalizes Windows/POSIX separators, reports only a directory named by
the verifier without traversing its tree, and applies to single-folder, exact
selected, dynamic batch, and localization-prefix receipts.

Hand-created source links are never a fallback. The primary worktree's untracked,
Assent-owned `.assent/manifest.toml` is local execution memory, never committed,
copied to a worktree, or treated as source. Its `[shared_paths]` stores whole
reviewed profiles: normalized project-relative paths, exact tracked `watch`
files, and a fingerprint of those files plus tracked Git-ignore rules. Profiles
are retained by fingerprint so differing branches do not oscillate the cache.

A source is `UNKNOWN` when no profile answers a real candidate;
`REVIEWED-NONE` when a matching `paths = []` profile conclusively requires no
link or repeated review; `REVIEWED-PATHS` when a matching nonempty profile makes
Assent provision same-relative primary-worktree directories as Windows
junctions or POSIX directory symlinks; and `STALE` when a watched file changed,
appeared, or disappeared, a declared target disappeared, changed kind,
collided, or stopped being ignored, or `Ignored input diagnosis:` names an
undeclared requirement.
Conflicting matching profiles fail closed. Controlled review replaces every
matching snapshot profile, including different watch sets, and retains others.

`NO-IGNORED-DIRECTORY-CANDIDATE` is the zero-token result of a successful Git
ignored-entry query finding no ordinary ignored primary-worktree directory
outside `.git/` and `.assent/`; it never claims the project semantically needs
no shared input. It uses no profile, link, or AI review, has a receipt identity
distinct from `REVIEWED-NONE`, and is recomputed at each gate. Discovery error
refuses; ignored leaf files do not count; every ordinary ignored directory does,
even if later review returns `paths = []`; a later candidate makes the state
`UNKNOWN` unless cached evidence answers it. Complete-verifier
`required_evidence` for a missing directory instead enters review when a valid
primary target exists, or reports the exact missing/not-ignored problem.

Classification queries the *primary* worktree because every allowed target must
already be an ordinary Git-ignored directory at the same relative path; fresh
source checkouts need not contain ignored inputs. A directory or ignore rule
found only on an unaccepted branch is not provisionable and produces an
actionable prerequisite refusal, never a semantic "none needed" answer.

Only `assent shared-paths review` writes the manifest, taking repeated `--path
DIR` or explicit `--none` plus exact `--watch FILE` values. It prevalidates all
input, holds one project lock, and atomically replaces the file; concurrency
refuses and interruption retains the prior file. There is no arbitrary target,
copy, glob, all-ignored mode, force, or staging. `UNKNOWN`/`STALE` adds one
bounded clause (prior paths plus changed evidence) to the next scheduled session
and blocks closeout until settled. `.gitignore` alone proves no semantic need.

All verify modes (single, exact/dynamic batch, localization prefix,
`run ... --verify`, and `--focus`) classify contributing live sources and
reconcile Assent-owned links before any candidate or verifier; focus provisions
the persistent source and writes no receipt. Reconcile classifies before its
worktree/merge, provisions declared links, revalidates rather than repairs on
`--continue`, and detaches only recorded links on every cleanup path.

Folder and batch receipts bind deterministic `shared_inputs_sha256` over ordered
profiles, paths, exact targets, and bounded target content immediately before
and after the full verifier; target drift prevents PASS. `REVIEWED-NONE` has an
explicit empty identity, unlike digest-less `UNKNOWN`; old receipts lacking the
field are stale, never upgraded. Acceptance rechecks the digest without creating
or repairing links. Every source link must match its active profile and exact
primary target; a manual undeclared link refuses verification and invalidates
folder/batch receipts and report freshness. Ignored leaf files remain separately
auto-mirrored and never become manifest paths.

`[verification].receipt_refresh` selects the second-stage starter. Default
`"manual"` closeout defers to explicit `assent verify [--batch]`, allowing one
batch run; `run --verify` instead proceeds directly to its requested run-level
verification without repeating that advice. `"auto"` refreshes after every task
completes. Focused task verification is unconditional. Direct/selected accept
never verifies and needs fresh matching PASS; only `accept --all`'s documented
sequential fallback may call `verify_folder_if_needed`. Missing receipt reports
`NOT RUN`.

`assent verify <FOLDER>` is a zero-token, unattended receipt refresh: no AI or
target change. Its boundary refreshes `_report.md` after the receipt operation
and lock release exactly once—even on reuse, refusal, replacement, or incomplete
input—as a best-effort write that never masks result/interrupt.
`assent verify A B` dependency-orders exactly that set, builds one candidate,
runs once, and writes one exact batch receipt; conflict refuses. Batch/focused
verification does not refresh folder reports. All receipts are deletable Git-
subordinate evidence: identical rebuilt candidate trees survive target-commit
change, different trees are stale. Verification never accepts or moves a ref.

`assent verify --batch` dynamically selects every finished, unintegrated folder
in `accept --all` dependency order, performs one `--no-ff` merge step per source,
and runs one unattended full verification. Its
`.assent/_batch_verification.toml` is independent of every folder receipt:
neither reads, consumes, or requires the other. It records every intermediate
tree for exact replay, not just the final one. If every folder is unfinished,
integrated, or already ancestral, nothing is certified and no receipt is
written.

Candidate construction conflicts are not verifier failures. Dynamic batch tries
all queued merges, reports every conflicting folder/path and transitively
excludes its `after` dependents, then offers one `[Y/n]` to verify only the
remaining independent subset. Empty/`y`/`yes` accepts; `n`/`no`, unknown input,
or EOF refuses before verification and receipt. If nothing remains it refuses
without asking; conflict-free operation is unattended. A reduced receipt names
only verified folders. Skipping never resolves, rebases, accepts, deletes, or
changes target/source state. For peer-only conflict, compatible predecessors may
be verified/accepted first, then the target advances before reconcile;
`assent rework` and `assent reject` remain alternatives.

`assent reconcile <FOLDER>` lets a human edit only conflicted paths while Assent
owns every Git action. It requires a finished folder, clean main worktree, and
source branch/worktree; captures target/source tips; and merges target into the
exact source tip at `<project>.reconcile/<FOLDER>` on
`assent-reconcile/<FOLDER>`. Source-first merge enables source fast-forward
without rewriting source or changing/dirtying the target or ordinary worktrees.

`--continue` stages only unresolved paths; refuses remaining unmerged paths,
conflict markers, whitespace errors, or out-of-scene edits; commits, fast-
forwards source, then re-proves and removes its worktree/branch through the
non-traversing link boundary. `--abort` refuses dirty state and removes only
re-proven resources. Link targets survive every outcome. No state file exists:
worktree, temporary branch, `HEAD`, `MERGE_HEAD`, and parents drive idempotent
resume; mismatch preserves all evidence.

Reconcile creates no approval/evidence, runs no focused/complete verification,
AI, or task-status edit. Because source advances, continue deletes its folder
receipt and any now-mismatched readable batch receipt; an unreadable batch
receipt is retained for inspection. It handles one folder against current target, never peer-source
conflicts or file content. Verify then accept still require a fresh reproducible
PASS. For peer conflict, verify/accept compatible predecessors before reconcile;
rework/reject remain alternatives.

On verifier failure, dynamic batch defaults to at most `ceil(log2(N))` extra
runs locating the first red merge. The proven prefix retains a PASSED receipt,
but the requested batch still exits nonzero. Localization never changes the
guilty folder's status/tasks; only rework/reject reopen it. `--no-bisect` records
the whole-chain failure without localization. Rework/reject invalidate the batch
receipt, rebuilt by the next `verify --batch`.

When execution is isolated, the scheduler also detects writes that landed in
the main tree instead of the worktree: it diffs a main-tree dirty-path
snapshot taken just before the session against one taken just after: any new
dirt is what the session itself wrote there. When every escaped path falls
inside the task's scope, the scheduler ports it into the worktree, restores
the main tree, records the port-back in the r file, and this attempt's
evaluation is judged a failure and goes to retry like any other checkpoint
failure. An escaped path outside scope, or an in-scope path whose port-back
itself fails (for example the worktree copy already diverged), is
fail-closed: both trees are left untouched and the state is handed to a
human.

The four per-task checks (status/structural/scope/focused-verify), described
in `~/.assent/format.md`, additionally behave as follows on each outcome:

- **Status check failure alone**: when only this check fails and the structural
  comparison and scope checks both pass, the scheduler probes the task's
  focused verify once before retrying; a pass attaches a dedicated
  closeout-only retry prompt (close out the task without touching code or
  tests), and a retries-exhausted BLOCKED record notes that verify already
  passed and only closeout was missing.
- **Focused verification failure**: a nonzero adapter exit or watchdog stall is
  a scheduler failure and is recorded before retrying. Full candidate
  verification belongs to the post-folder scheduler stage, not to the AI tool.
- Failure -> **do not revert the workspace**, retry with the failure reason;
  retries exhausted -> the scheduler marks BLOCKED + a machine record in the r
  file + commits along with the results that did not pass. **Token-burned output
  is never discarded.**
- Quota exhausted -> not counted as a failure: the r file records `quota`,
  the task status is written back to `WIP` unless the task explicitly wrote `BLOCKED`,
  progress is gathered into a progress-bearing `wip(<work folder>/tNNN)` checkpoint, and the
  same task reruns carrying a "resume" prompt. The status write happens before the wait or
  adapter rotation, so a result that arrived after the AI wrote `DONE` cannot skip closeout.
  With one configured adapter, a
  countdown waits for the known reset (or the configured quota poll when no
  reset is known). When `[adapter].name` is a list, quota exhaustion switches
  immediately to the next adapter in order; the scheduler waits for the
  configured rotation poll only after every adapter in the rotation is
  exhausted, then continues with the next adapter.
- Adapter checkpoint-resume control -> not counted as a failure: a finished,
  non-stalled, nonzero adapter process whose complete final non-empty output
  line is exactly `{"type":"assent.checkpoint_resume"}` records
  `checkpoint_resume`, writes the task back to `WIP` unless it explicitly wrote `BLOCKED`,
  gathers progress into the same WIP checkpoint, refreshes the report, and immediately reopens
  the same adapter command with the resume prompt. It does not sleep, rotate adapters, or consume
  a retry. The terminal
  control line is suppressed from live rendering while the raw adapter output
  remains available as result evidence. The record adds no configuration key or
  capability probe. A wrapper may replace a provider quota result with it only
  after arranging an immediate continuation; if it forwards provider quota,
  Assent performs the normal wait or rotation. When quota evidence and this
  record are both present, the ordinary quota path wins.
- Unclean exit (power loss, a forced kill) never reaches the Ctrl+C/quota
  interrupt handlers, so a dirty worktree can survive to the next `run`
  startup. The startup gate checks whether every uncommitted change is
  provably inside the scope of the resumable candidate task: provable ->
  gathered into a `wip` checkpoint and the run continues, no AI session;
  otherwise -> fail-closed, `run` refuses and hands the state to a human.
  A clean legacy `DONE` task backed only by an older WIP checkpoint is not treated as dirty and
  does not receive a retroactive auto marker; existing history remains reviewable without being
  synthesized, amended, rebased, or renumbered.
- If setup fails after Assent creates a new worktree, only that exact,
  still-owned path and branch are cleanup candidates. Assent re-proves the
  clean checkout, expected `HEAD`, and branch ownership, detaches every
  directory-link object before recursive removal, and retains the path and
  ref as recovery evidence if any proof or detachment fails. The external
  target is never traversed or modified, and a later Assent retry repeats the
  guarded cleanup.
- The executing AI never runs git commit — the checkpoint is created by the
  scheduler.

### History rewrites

Any author/email/message rewrite must preserve three load-bearing facts:

1. Subject prefixes `auto(<folder>/tNNN):`, `wip(`, `rework(`, and `accept(`
   survive byte-for-byte because report attribution, rework-tail matching, and
   clean/reject branch judgment depend on them.
2. No rework may be in progress: its revert checkpoint embeds `original_head`
   and reverted hashes on the folder worktree. The project's rewrite-tool
   preconditions (clean tree, one worktree, only `main` and `origin/main` refs)
   enforce this; do not add another command.
3. Every verification receipt becomes stale and must be rebuilt by standard
   `verify`; receipts are disposable caches, never durable truth.

Legacy checkpoint boundary: a historical branch may contain a progress-bearing WIP checkpoint
without its later terminal auto marker. Assent does not retroactively synthesize that marker or
rewrite, amend, rebase, or renumber the branch; the empty terminal auto rule applies to a new
resumed run that passes its gates.
