# assent working instructions

> This file lives at `~/.assent/instructions.md`, the per-user assent home
> shared by every project on this machine, and defines only the behavior of an
> assent session. The single contract for the plan format is `format.md` in
> that same directory. Both are installed and refreshed by `assent init`; a
> project's own `.assent/` never carries a copy of either one. A scheduled
> session is handed both as absolute paths rather than deriving them.

## Cross-project common rules

- git commit messages must not contain any AI attribution or advertising text
  (`Co-Authored-By`, `Generated with`, and the like); not a single line of it
  is allowed.

## Contract ownership

Each normative rule has one canonical document owner:

- repository-specific development constraints belong in `AGENTS.md`;
- scheduled-session procedure belongs in `instructions.md`; and
- persisted artifact schemas, filename rules, state meanings, and CLI/report/
  receipt contracts belong in `format.md`.

Other documents may reference an owned rule, but must not duplicate it as a
competing normative contract.

## Default reading scope

A **meeting / interactive session** reads only, to get started:

1. The project root `AGENTS.md` (if present)
2. This file, at `~/.assent/instructions.md`
3. `~/.assent/format.md` (required before creating or modifying a task file)
4. The current work folder's task files and `_report.md` (during a review
   meeting)
5. The source and tests the task directly touches

An **assent-scheduled task session** reads only:

1. The `AGENTS.md` path the scheduler provides (the branch version first; when
   untracked, the main-tree absolute path; skip it if absent)
2. The absolute path to this file the scheduler provides
3. The absolute path to the one assigned task file the scheduler provides
4. The source and tests the task directly touches

The two contracts live in the user home, and a project's `.assent/` holds only
that project's own material: its `verify.py`, its work folders, the runtime
artifacts inside them, and at most a deliberate legacy `assent.toml` override.
A worktree contains neither directory, so a task session must not guess the
location of any management file from relative paths; it uses the absolute paths
the scheduler provides — the user home for the contracts, the main worktree for
this project's task and r files. Do not read by default: old work folders, r
files (logs; read only when debugging or explicitly referenced), and the
`_assent.log` inside a work folder.

## Working rules

- Write boundary (task session): all file edits must land under the session
  cwd (the isolated worktree); before editing, confirm the target's absolute
  path is prefixed by cwd. The scheduler-provided main-worktree absolute path
  has exactly two writable exceptions: your own task file's status line and
  your own r file; everything else, including the main tree's source and
  tests, is read-only. Writing into the main worktree fails that task run.
  The user home `~/.assent` is read-only to a session in every case: its
  contracts and shared settings belong to `assent init`, not to a task.
- Do not modify files unrelated to the current task.
- Reference shared specifications; do not copy them into each task file.
- Keep conjecture, changed, verified, and unverified separately recorded.
- Do not declare scheduled-task completion without passing the task's focused
  `verify` command; pending must not be dressed up as completed. Complete
  candidate verification is a separate receipt-refresh stage governed by
  configuration: `"auto"` runs it at folder closeout, while `"manual"` defers it
  until a human explicitly invokes `assent verify`.
- Shared ignored directories (task session): which ignored directories this
  project really shares is a reviewed decision Assent caches in the primary
  worktree's untracked `.assent/manifest.toml`. When a matching profile exists,
  Assent has already provisioned every declared directory as a link before your
  session started and you do nothing. When your prompt says the contract is
  `UNKNOWN` or `STALE`, you must settle it before closeout by running, in this
  worktree, `assent shared-paths review --path DIR --watch FILE` (repeat both as
  needed) or `assent shared-paths review --none --watch FILE` when no shared
  directory is required. `--watch` names the exact tracked dependency or build
  files whose change would make the decision worth reconsidering. Decide from the
  Git-ignore rules, the dependency/build declarations, and this task's verifier
  evidence alone — do not audit the whole repository. That command is the only
  way you may write the manifest; the scheduler refuses the task's completion
  while the contract is still unreviewed. When your prompt instead says
  `NO-IGNORED-DIRECTORY-CANDIDATE`, a successful Git query found no ordinary
  ignored directory in the primary worktree to declare at all: there is
  nothing to review and you must not run the command "just in case". It is not
  a claim that this project needs no shared input — if such a directory later
  appears, or a verifier proves one is required, the contract becomes
  `UNKNOWN` and you will be asked then.
- Ignored inputs a check needs (task session): the isolated worktree is built
  from tracked content, so a directory Git ignores — a private package tree, a
  large asset directory — is missing there even though the main worktree has
  it. When your assigned task or its focused verify command demonstrably needs
  one, confirm an ordinary target exists at the same relative path in the main
  worktree and Git ignores it, then record that path with
  `assent shared-paths review --path DIR --watch FILE`; Assent provisions the
  exact junction or directory symlink. Never hand-create a source-worktree link:
  a link outside the active profile is unreviewed evidence and closeout refuses.
  Never copy the ignored directory tree in: a copy passes the focused check and
  then disappears, because the integration candidate mirrors provisioned
  directory links and ignored leaf files and never a physical ignored tree.
  Provision nothing else — not caches, credentials, editor state, build output,
  or ignored directories the task does not require — and never modify anything
  inside the linked target. An ordinary ignored file generated beside its
  tracked source, such as a `*.g.dart`, needs no action.
- During interactive work, run the smallest relevant checks. Do not launch the
  full project suite merely because files changed. Launch it only when the human
  explicitly asks, when a scheduler-provided focused verify command itself
  requires it, or when no narrower check can responsibly validate the requested
  change and no later standard verification stage will do so; state that
  necessity before starting it. In a human-driven `reconcile` -> `verify` flow,
  resolve the requested conflict and leave `assent verify` for the human to
  start after `assent reconcile --continue`.
- Code, git, and test results are the final source of truth.
- Never kill / Stop-Process any process the session did not itself start — your
  parent process chain leads straight to the scheduler, and killing the wrong
  one makes the whole run die silently.
- The correct response to a command timeout is to raise the timeout or rerun in
  batches, not to hunt down a process that "looks stuck". If an outer tool
  timeout may have left children running, do not run the command in parallel
  again and do not mark the task BLOCKED solely because of that timeout; the
  recorded adapter result and any configured or explicitly invoked
  post-session verification are authoritative.
- Keep session output economical: state the fact once, skip narrated
  tool-call preambles and restated plans, and quote a command or test
  failure as its shortest decisive line rather than a full transcript,
  unless the task or a human explicitly asks for more.
- Prefer the smallest change that satisfies the task: reuse a helper,
  type, or pattern already in the touched files before adding a new one,
  and do not add abstractions, config, or scaffolding the task does not
  ask for.

The adapter result is authoritative for the session boundary: a nonzero exit
code or watchdog stall is an adapter failure, not permission to claim DONE or
BLOCKED. The scheduler records the adapter event, keeps the work, and retries.
Only after its tamper guard and fail-closed scope check pass may the scheduler
accept a terminal task result; a self-marked BLOCKED then goes directly to its
checkpoint without focused verification. Full candidate verification remains
separate from the AI task session: `"auto"` runs it unattended at folder
closeout, while `"manual"` waits for a human to invoke `assent verify`.

Cleanup is a separate guarded operation: `assent clean` must retain an
upstream source while any dependent folder remains unaccepted, and skips when
its merged-and-clean proof is insufficient.

Folder `after` controls scheduler readiness only; it never supplies a worktree
base. A declared `base` is the only lineage declaration, and stacking occurs
only through it, so at most one unaccepted upstream can be in a downstream
stack. Without a declared `base`, the downstream starts from the current
integration target; the number or acceptance state of `after` members does not
create base ambiguity or a refusal. If an upstream advances, preserve the
downstream result but treat its stack as stale and use rework/reject or a new
folder. Verify the combined candidate before accepting upstream then
dependent; matching receipts can be reused, and accept does not rerun the
complete suite. Conflicts are human decisions. Cleanup is upstream-first and
must retain source evidence until direct dependents are accepted and proven
integrated.

Acceptance is a human decision, not an implicit side effect of verification.
Direct `assent accept FOLDER` and selected `assent accept A B` never run the
complete verifier; except for an ancestry-proven already-integrated no-op, they
require matching fresh receipt evidence and replay that exact evidence. The
intentional exception is `assent accept --all`: a fresh PASSED batch receipt is
replayed and released atomically without new verification, while missing or
expired batch evidence selects the sequential path that runs
`verify_folder_if_needed` before each not-already-integrated folder accept. A
malformed batch receipt refuses rather than falling back; the sequential path
stops at its first real failure and preserves earlier publications.

The literal token `...`, written once as the last positional argument, adds
every remaining folder a folder-taking command would discover. It is a
remainder operator, not an alias for `--all`, and cannot be combined with it;
a `...`-expanded selection is an ordinary exact selection, so
`assent accept A ...` still requires evidence for exactly the expanded set and
still starts no verification. `assent run --verify` chains complete
verification onto a run that exited zero, matching that same selection; a
failing run verifies nothing. With `--once` or `--task` it verifies only when
that limited run left the single selected folder complete, and an incomplete
folder fails the request without writing a receipt.

The worktree is a change-isolation, conflict-management, audit, and recovery
boundary, not a security sandbox. With `danger-full-access` or
`bypassPermissions`, an AI can still access resources available to its OS
identity, including external Git writers, network services, credentials, and
files outside the worktree. Use unattended execution only in trusted projects
and account environments; Assent does not create a container or VM sandbox.

## Opt-in folder review and bounded repair

When `[auto_fix.review]` is configured, it supplies the policy for the bounded
folder review-and-repair loop. The entire loop is invocation-level opt-in:
only `run --auto-fix` starts its folder-level, read-only review after the final
focused checks and authorizes repair. An ordinary `run` without the flag starts
neither the review nor repair. The reviewer is configured by a registered
adapter plus the abstract `prime`/`core`/`lite` model and
`heavy`/`normal`/`slight` effort values; the adapter mapping resolves the actual
CLI identity and may name a vendor outside the worker rotation. If the optional
table is absent, the first effective worker adapter at `prime`/`heavy` is the
resolved reviewer policy; `assent init` need not be rerun and
`~/.assent/assent.toml` need not be edited. The flag is orthogonal to selection
and may accompany an implicit folder, explicit folders, `...`, `--all`,
`--once`, `--task`, or `--verify`; the ordinary selection and verification rules
still apply.

The order is fixed: task sessions run their ordinary focused gates; when the
folder is complete, each distinct `DONE`-task `verify` command runs once more;
only if all of those checks pass and the source remains clean does the completed-
folder reviewer start. A limited `--once`/`--task` run that remains incomplete
defers that review and spends no review token. A quiescent blocked dependency
with durable worker `BLOCKED` or task-focused-gate evidence enters the separate
blocked-adjudication reviewer path; it does not run a new focused command just
to create evidence. A folder containing only `SKIP` tasks needs no
implementation review. Focused failure writes the scheduler's finding evidence
and starts no completed-folder reviewer.

The reviewer receives a read-only prompt and must not edit, create, delete,
rename, or format project or management files. Assent snapshots the protected
source and management surfaces before and after the reviewer interval; any
detected write makes the verdict unusable, preserves the exact edits, and
refuses closeout. This prompt-plus-detection rule is cooperative detection,
not a security sandbox: the configured `danger-full-access` default remains in
force and cannot intercept external effects.

`_auto_fix.toml` is derived, deletable folder runtime memory, never a task
file, status, source-of-truth, or acceptance record. The version-5 record
contains `source_tree`, `task_plan_sha256`, `review_prompt_sha256`, the
resolved `reviewer_adapter`, `reviewer_model`, and `reviewer_effort`, the
required recovery `phase`, a `PASS`/`FAIL` `verdict`,
`review_context`, `review_stage`, and `failure_trigger`,
`current_finding_fingerprints`, the cumulative `findings` ledger,
`reviewer_recommendations`, `approved_scope_additions`, `scope_amendments`,
`worker_dispositions`, `repair_briefs`, `repair_round_assignments`,
`plan_digest_transitions`, `review_transitions`, `observed_states`, and
`consumed_fixer_profiles`. Its finding fingerprints are
scheduler identities; its consumed fixer profiles are ordered abstract
adapter/model/effort triples. The phases are `NEEDS_REPAIR`, `REPAIRING`,
`AWAITING_REVIEW`, and `COMPLETE`; restart resumes the durable repair or
review boundary, and a missing or drifted reviewer configuration refuses
repair and closeout. A malformed record refuses, and a cached `PASS` is
reusable only when the source tree, task contracts, review prompt, and resolved
reviewer identity all match exactly. The ledger and consumed profiles survive
later observations so recovery cannot repeat a profile silently. Scheduler-owned
status-only transitions during rework, interruption, repair closeout, or finite
exhaustion are normal lifecycle evidence and do not by themselves make a report
stale; edits to task requirements, scope, verification, or other contract bytes
remain structural drift.

A `FAIL` record may be repaired only when `--auto-fix` was stated. Every
finding must resolve to one existing task and that task's declared scope;
unknown or ambiguous findings stop for a human. The reviewer may return one
exact mechanically valid scope addition, but only the scheduler may append it
to a task contract; worker and reviewer task-file edits remain forbidden.
Automatic repair invokes the normal task session with the durable finding
ledger and prior profile list, reopens only the implicated existing tasks, and
records the reason-bearing rework reason `Automatic repair of durable
folder-review findings` plus `authorization: run --auto-fix`. It keeps code by
default and never creates a task, changes task requirements, reverts source,
accepts work, deletes source, or performs an unbounded repository-wide debt
audit. A pre-existing technical-debt finding is eligible only when it is first
introduced in `COMPLETED_FOLDER + INITIAL`, is in changed or directly
interacting code, local to an existing task scope, and reliably tested by that
task's focused gate. Blocked adjudication and `RECHECK` may retain or resolve
that ledger entry but cannot add another.

The recheck has a soft-convergence rule: it reviews prior current findings
first, keeps the same fingerprint for a blocker that is still present, and
accepts a new blocker only for an evidenced repair regression or a newly
exposed existing requirement. When the prior set is cleared it must return
`PASS`; optional improvements, speculative concerns, and repeated debt
discovery do not keep the loop open.

Each repair round selects its profile assignments from the consumed-profile
history that existed when that round began, then persists every newly selected
assignment before the round's first write-capable session. The ordinary task
profile is tried first, followed by the finite configured worker rotation at
`prime`/`heavy`; one task in a multi-task finding or dependency cascade cannot
consume a sibling's normal slot and force it to escalate. A profile is never
silently reused. Repair runs the ordinary focused gate before reviewing again.
If profiles are exhausted, the unresolved finding ledger and all edits remain
for later human review; no automatic code reversion or task creation follows.

The repair brief requires one acknowledgement line for every current finding
in the task's closeout journal detail, using exactly this provider-neutral
syntax (the scheduler validates the JSON and fingerprint set):

```text
ASSENT_REPAIR_DISPOSITION {"fingerprint":"<64 lowercase hex>","disposition":"fixed|not_reproducible|still_blocked","detail":"concrete bounded evidence"}
```

Use one line per fingerprint, in the durable brief's order. `still_blocked`
requires the task to close `BLOCKED`; the line is acknowledgement evidence, not
permission to edit the task contract, change scope, accept the folder, or start
a human gate. The scheduler owns task status, the one reviewed exact-scope
amendment, and all Git state.

Interruption, quota exhaustion, adapter failure, and a failed repair gate keep
all edits and state. A later `run --auto-fix` reads the existing `FAIL` state,
resumes WIP work, and consumes only unused profiles, but only while the current
`[auto_fix.review]` exists and its resolved reviewer identity still matches the
state. Removing or changing that policy refuses repair and closeout. Running
without the flag continues ordinary task execution only; it neither starts this
review nor authorizes repair. A human may inspect the report and use explicit
`rework`, `reject`, `verify`, or `accept` actions; a review `PASS`, an auto-fix
state, or a full verification receipt never accepts a folder.

Complete verification still follows a successful run under the configured
receipt policy or an explicit `--verify`. Its absence, a missing receipt, or an
unrun full suite is never a reviewer failure; only a concrete local focused-test
gap tied to an existing task requirement may be considered by review.

## Review and acceptance meeting handoff

In a review or acceptance meeting, first inspect the folder's `_report.md`.
If it carries `TECHNICAL DEBT REVIEW REQUIRED`, read the sibling
`_technical_debt.md`, proactively tell the human about the flag before
recommending `accept`, enumerate every listed finding, and obtain an explicit
disposition for each: accept the completed local repair as sufficient, append or
rework a task for concrete follow-up, or promote a genuinely durable project
rule to `AGENTS.md`. Merely reading the agenda silently does not satisfy this
procedure. Persist a requested change in its canonical owner; a no-further-
change disposition remains part of the existing explicit human `accept`
decision, not a second debt-approval state.

## Task session closeout (when scheduled by assent)

1. Closeout must complete synchronously within this same turn: do not defer
   the status update or r-file entry until after any background work
   finishes, and do not use a scheduled wakeup or background notification to
   wait on tests — a headless session terminates the moment the turn ends, so
   a deferred closeout will never happen. Verification runs only the
   scheduler-provided focused verify command, in the foreground and
   synchronously; it does not run the full suite — full verification belongs
   to the scheduler's folder-closeout stage.
2. Self-check against the task file's acceptance item by item, and run the
   verification command the scheduler provides to confirm exit code 0.
3. Change the status of **your own task file** to DONE or BLOCKED — only this
   one line of the whole task file may be changed, and no other task file is
   touched.
4. Append one `[[entry]]` to the end of the r file at the absolute path the
   scheduler provides: time, the prompt-specified `by = "claude"`, `by = "codex"`,
   or `by = "antigravity"`, requested_model, requested_effort, event, summary (a
   verifiable fact, one sentence), detail (process notes). A supported invocation
   always states a requested_effort. requested_model and requested_effort are the
   values actually passed to the AI CLI this run, not the model or reasoning
   investment the service ultimately adopts or reports.
5. Do not run git commit — the checkpoint is the scheduler's job.

## Meeting session closeout (when interactive)

1. Settle consensus into task files on the spot; do not leave it in the
   conversation. Format follows `~/.assent/format.md`.
2. Run `assent check` — passing is what adjourns the meeting; not passing means
   the plan is not finished.
3. Decisions that stay valid across plans go into the project `AGENTS.md`
   Permanent constraints.
