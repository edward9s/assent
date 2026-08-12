# assent working instructions

> This file lives at `~/.assent/instructions.md`, the per-user assent home
> shared by every project on this machine, and defines only the behavior of an
> assent session. The contract for the plan format is `format.md` in that
> same directory, and its companion `workflow.md` is the CLI, report, and
> receipt contract. All three are installed and refreshed by `assent init`; a
> project's own `.assent/` never carries a copy of any of them. A scheduled
> session is handed the ones it needs as absolute paths rather than deriving
> them.

## Cross-project common rules

- git commit messages must not contain any AI attribution or advertising text
  (`Co-Authored-By`, `Generated with`, and the like); not a single line of it
  is allowed.

## Contract ownership

Each normative rule has one canonical document owner:

- repository-specific development constraints belong in `AGENTS.md`;
- scheduled-session procedure belongs in `instructions.md`;
- persisted artifact schemas, filename rules, and state meanings belong in
  `format.md`; and
- CLI, report, and receipt contracts belong in `workflow.md`.

Other documents may reference an owned rule, but must not duplicate it as a
competing normative contract.

## Default reading scope

A **meeting / interactive session** reads only, to get started:

1. The project root `AGENTS.md` (if present)
2. This file, at `~/.assent/instructions.md`
3. `~/.assent/format.md` (required before creating or modifying a task file)
4. `~/.assent/workflow.md` (during a review or acceptance meeting, to judge
   whether a worktree's implementation matches its task file's spec, or
   whenever CLI/scheduler mechanics matter directly, including any change to
   `[workflow]`, `[roles]`, or `[abilities]` — not required merely to draft a
   task file; that contract remains the canonical owner of those settings)
5. The current work folder's task files and `_report.md` (during a review
   meeting)
6. The source and tests the task directly touches

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
- Never launch the full project suite or `.assent/verify.py`, in any session and
  for any reason: not because files changed, not to be thorough, and not because
  a human asked for it during interactive work — say it is theirs to start and
  name the command instead of running it. Complete verification belongs outside
  every AI session: a human runs it, or the scheduler runs it once at the end of
  a whole run (`assent run --verify`) or on an explicit `assent verify`. Run the
  smallest check that decides the question in front of you. In a human-driven
  `reconcile` -> `verify` flow, resolve the requested conflict and leave
  `assent verify` for the human to start after `assent reconcile --continue`.
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
- While any command runs — the focused verify command, a build, or any other
  long-running command you are allowed to start — stay in the session until it
  finishes and report its outcome once. A command that outlives the foreground
  tool limit may be started in the background and polled until it returns; the
  turn must not end while it is still running. Keep every interim line to one
  short status of the form `<command>: running, <elapsed>` — no restated plan,
  no narrated transcript, no reasoning about what the result might be.
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

`assent-integration/<folder>/<suffix>` and `assent-reconcile/<folder>` are
Assent-owned temporary branches; a human must never check one out or build on
it. Each is removed by the transaction that created it once that transaction
completes, so a surviving branch is orphaned only when its owning transaction
died before finishing. The repository-wide integration lock being held while
the branch still exists is the entire proof that it is an orphan -- never the
branch's content, and never whether its tree is published or superseded, which
is reporting information only. `assent clean --all` sweeps every such orphan
once per invocation, `assent archive --all` inherits that same sweep, and a
single named `assent clean FOLDER` deliberately does not sweep it. `assent
doctor` reports any it finds and offers a confirmed `[y/N]` removal as the
recovery path.

Folder `after` controls scheduler readiness only; it never supplies a worktree
base. A declared `base` is the only lineage declaration, and stacking occurs
only through it, so at most one unaccepted upstream can be in a downstream
stack. Without a declared `base`, the downstream starts from the current
integration target; the number or acceptance state of `after` members does not
create base ambiguity or a refusal. If an upstream advances, preserve the
downstream result but treat its stack as stale and use rework/reject or a new
folder. Verify the combined candidate before accepting upstream then
dependent; matching receipts can be reused, and accept does not rerun the
complete suite. Conflicts enter the configured integration review-and-repair
action sequence; bounded exhaustion retains them for human adjudication, and
final approval still belongs only to `accept`. Cleanup is upstream-first and
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
still starts no verification. A successful `assent run` automatically follows
the configured integration workflow for the same selection. With `--once` or
`--task`, integration is deferred when the limited run leaves the selected
folder incomplete.

The worktree is a change-isolation, conflict-management, audit, and recovery
boundary, not a security sandbox. With `danger-full-access` or
`bypassPermissions`, an AI can still access resources available to its OS
identity, including external Git writers, network services, credentials, and
files outside the worktree. Use unattended execution only in trusted projects
and account environments; Assent does not create a container or VM sandbox.

## Automatic folder review and bounded repair

The ordered per-plan review-and-repair policy comes from
`[workflow].plan` and runs automatically after task execution. Each role's
agent resolves its adapter, abstract model, and effort through the configured
adapter mappings. An omitted or empty plan configures no plan review. Folder
selection, `...`, `--all`, `--once`, and `--task` retain their ordinary rules.

The plan workflow is considered only after every task is `DONE` or `SKIP`. On
that complete path, each distinct
`DONE`-task `verify` command runs once more, and only passing checks plus clean
source start the completed-folder reviewer. A limited `--once`/`--task` run that
remains runnable defers review. A task role that self-marks `BLOCKED` advances
within `workflow.task` to its next verdict role; a writable verdict role may
repair a task-local exact scope omission in that same session. A final task
`focused_test` failure or exhausted task workflow leaves the task `BLOCKED`
without consuming plan repair positions. A folder
containing only `SKIP` tasks needs no implementation review. Focused failure
writes the scheduler's finding evidence and starts no completed-folder reviewer.

Each explicit position in that configured review sequence contributes to the
finite bound on the loop. Every plan or selection role may name an adapter; an
omitted adapter resolves to the first configured adapter. Verdict-producing
roles open that adapter session; a write-capable non-verdict role authorizes
bounded repair with that adapter for the nearest earlier durable verdict.

A completed-folder role with `writes = true` is a merged review-and-repair
session, not a strictly read-only gate: when it finds a genuine blocking problem
it repairs it directly inside the declared scope of the one existing task its
finding names, or inside one exact scope addition returned with `FIXED`. The
scheduler validates the pre-session path state and complete write set, then alone
persists that task-contract addition at closeout. `PASS` means nothing
blocking remains and the round wrote nothing at all. Assent snapshots
the protected source and management surfaces before and after the reviewer
interval; a write to a management-plane file, a task file, another task's
scope, Git state, or the primary worktree makes the verdict unusable,
preserves the exact edits, and refuses closeout. This prompt-plus-detection
rule is cooperative detection, not a security sandbox: the configured
`danger-full-access` default remains in force and cannot intercept external
effects.

`_auto_fix.toml` is derived, deletable folder runtime memory, never a task
file, status, source-of-truth, or acceptance record. The version-7 record
contains `source_tree`, `task_plan_sha256`, `review_prompt_sha256`, the
resolved `reviewer_role`, `reviewer_adapter`, `reviewer_model`, and
`reviewer_effort`, the required recovery `phase`, a
`PASS`/`FIXED`/`FAIL` `verdict`, `review_context`, `review_stage`, and
`failure_trigger`, the `workflow_step_index` and `reviewer_step_index` cursors,
`current_finding_fingerprints`, the cumulative `findings` ledger,
`reviewer_recommendations`, `approved_scope_additions`, `scope_amendments`,
`worker_dispositions`, `repair_briefs`,
`plan_digest_transitions`, `review_transitions`, `observed_states`, and the
at-most-one `self_fixed_unreviewed` settled outcome. Its finding fingerprints
are scheduler identities. The phases are `NEEDS_REPAIR`, `REPAIRING`,
`AWAITING_REVIEW`, and `COMPLETE`; restart resumes the durable repair or
review boundary, and a reviewer identity that is no longer one of the
configured rounds refuses repair and closeout. A malformed record refuses, a
record written under an earlier version refuses rather than being migrated,
and a cached `PASS` is reusable only when the source tree, task contracts,
review prompt, and resolved reviewer identity all match exactly. The ledger
survives later observations. Scheduler-owned
status-only transitions during rework, interruption, repair closeout, or finite
exhaustion are normal lifecycle evidence and do not by themselves make a report
stale; edits to task requirements, scope, verification, or other contract bytes
remain structural drift.

An invalid reviewer record is retried only within the configured adapter retry
bound. The next prompt carries the exact validator error, a bounded diagnostic
of the rejected output, and a schema-complete non-PASS example; the review
context, workflow position, identity, source evidence, and durable cursor do
not move. If the final invalid response left source edits, Assent creates a WIP
checkpoint only when every path is mechanically contained by exactly one
existing task's declared scope, journals scheduler recovery evidence, and
keeps that task's already-proven status. Ambiguous, out-of-scope, protected, or
uncheckpointable writes are retained for explicit human recovery and are never
attributed by guesswork.

A `FAIL` record may be repaired only by a later configured workflow position. Every
finding must resolve to one existing task and that task's declared scope;
unknown or ambiguous findings stop for a human. A read-only verdict role may
return one exact mechanically valid scope addition for its separately configured
fixer, but only the scheduler may append it to a task contract; worker and
reviewer task-file edits remain forbidden. Automatic repair invokes the normal task session with the durable finding
ledger and repair brief, reopens only the implicated existing tasks, and
records the reason-bearing rework reason `Automatic repair of durable
folder-review findings` plus `authorization: configured workflow repair`. It keeps code by
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

A round that writes a repair and is then interrupted before its verdict is
recorded -- an unclean exit during `REPAIRING` or `AWAITING_REVIEW` -- keeps
the edit and does not advance `workflow_step_index`. The next run's startup
recovery gate attributes that dirt to the task the durable `_auto_fix.toml`'s
current findings implicate, proven by the same scope-containment machinery its
other recovery owners use, and gathers it into a `wip` checkpoint with no AI
session opened. Ownership that cannot be proven this way -- dirt outside that
task's declared scope, more than one plausible owner, an unreadable state file,
or no in-flight round recorded at all -- still refuses fail-closed at
`ensure_clean` rather than guessing.

Each configured position advances the durable `workflow_step_index` exactly
once, and reaching the end of the configured plan ends the loop; that plan
length, not a worker-identity escalation ladder, is what makes the loop finite. Each reopened
task is repaired under its own ordinary task profile and nothing is consumed,
so an interrupted round resumes on exactly the same identity and multi-task
findings and dependency cascades never escalate one task at a time. Repair runs
the ordinary focused gate before reviewing again. When the list ends on a round
that repaired what it found, the scheduler re-runs the implicated task's own
focused gate against the repaired source -- reusing the same de-duplicating
ledger that skips a command already proven earlier in this invocation -- before
anything settles, so the claim `accept` and the report make, that every task
passed its own focused gate, is proven of the final repair itself and not only
of the state that preceded it. When that gate passes, the folder settles as
`SELF-FIXED, UNREVIEWED`: every task keeps the status its own focused gate
proved, nothing is marked `BLOCKED`, the run succeeds, and `accept` asks for
one explicit human confirmation before publishing. When that gate instead
fails, this is a distinct disposition from both `SELF-FIXED, UNREVIEWED` and an
ordinary `BLOCKED` task: the folder does not settle, no
`self_fixed_unreviewed` record is written, the run ends nonzero, no task is
marked `BLOCKED`, and every edit and finding stays exactly as the round left
it. When the list ends on an unrepaired blocker instead, the folder settles as
the equally distinct `REVIEW UNRESOLVED, HUMAN DECISION` outcome: every task
keeps the status its own closeout gave it, nothing is marked `BLOCKED`, the
unresolved finding ledger and all edits remain for later human review, and the
run succeeds -- exiting zero rather than failing, precisely so that the rest of
an `--all` invocation's queued folders still start behind it. An unresolved
review finding is a question the scheduler cannot decide, not an
infrastructure failure, so it belongs at the human acceptance meeting rather
than in a run failure that cancels unrelated queued work. `assent accept
<FOLDER>` gates on that outcome exactly as it gates on `SELF-FIXED,
UNREVIEWED`: after every receipt-based check already passes, it names the
unresolved findings' task, path, and summary and asks one explicit `[y/N]`
confirmation naming what is being overruled before publishing; a folder
carrying both outcomes is asked once, naming both reasons.

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
all edits and state. A later `run` reads the existing pending state,
resumes WIP work, and continues from the durable workflow position, but only
while the current configured per-plan review sequence still contains the exact
role and identity
that decided that state. Removing or changing that policy refuses
repair and closeout; a settled `SELF-FIXED, UNREVIEWED` or `REVIEW UNRESOLVED,
HUMAN DECISION` folder is terminal and resumes nothing. An omitted or empty
workflow layer starts no role from that layer. A human may inspect the report and use explicit
`rework`, `reject`, `verify`, or `accept` actions; a review `PASS`, an auto-fix
state, or a full verification receipt never accepts a folder.

Complete verification follows the configured integration `full_verify` action
after a successful run. Its absence, a missing receipt, or an
unrun full suite is never a reviewer failure; only a concrete local focused-test
gap tied to an existing task requirement may be considered by review.

For an exact selection, `full_verify` owns candidate construction and complete
verification; task and plan sessions never run either. In the configured
integration workflow, a candidate conflict is collected
as one complete typed wave before any full verifier. A writable verdict role
may assign every conflict path and repair it in the same session, using only
Assent's listed source or managed reconcile worktrees. A read-only verdict role
may instead hand its findings to the next explicit write-capable fixer. Neither
form may run Git, Assent, focused tests, or the full suite; the scheduler owns
every Git mutation and every gate. Each explicit role/action position is
finite, content-identical completed work is reused on resume, and exhaustion
retains all edits for human adjudication.

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
   scheduler-provided focused verify command, run to completion inside this
   turn; it never runs the full suite, which happens outside every AI session.
   When that command outlives the foreground tool limit, the background-and-poll
   rule in "Working rules" applies: poll it to completion in this turn, then
   close out. Polling is not deferral.
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
