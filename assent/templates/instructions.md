# assent working instructions

> This file lives at `.assent/instructions.md` in the project's main worktree
> and defines only the behavior of an assent session. The single contract for
> the plan format is `format.md` in the same directory.

## Cross-project common rules

- git commit messages must not contain any AI attribution or advertising text
  (`Co-Authored-By`, `Generated with`, and the like); not a single line of it
  is allowed.

## Default reading scope

A **meeting / interactive session** reads only, to get started:

1. The project root `AGENTS.md` (if present)
2. This file
3. `.assent/format.md` (required before creating or modifying a task file)
4. The current work folder's task files and `_report.md` (during a review
   meeting)
5. The source and tests the task directly touches

An **assent-scheduled task session** reads only:

1. The `AGENTS.md` path the scheduler provides (the branch version first; when
   untracked, the main-tree absolute path; skip it if absent)
2. The absolute path to this file the scheduler provides
3. The absolute path to the one assigned task file the scheduler provides
4. The source and tests the task directly touches

A worktree does not contain `.assent/`; a task session must not guess the
location of management files from relative paths, and always uses the main
worktree absolute path the scheduler provides. Do not read by default: old work
folders, r files (logs; read only when debugging or explicitly referenced), and
the `_assent.log` inside a work folder.

## Working rules

- Write boundary (task session): all file edits must land under the session
  cwd (the isolated worktree); before editing, confirm the target's absolute
  path is prefixed by cwd. The scheduler-provided main-worktree absolute path
  has exactly two writable exceptions: your own task file's status line and
  your own r file; everything else, including the main tree's source and
  tests, is read-only. Writing into the main worktree fails that task run.
- Do not modify files unrelated to the current task.
- Reference shared specifications; do not copy them into each task file.
- Keep conjecture, changed, verified, and unverified separately recorded.
- Do not declare scheduled-task completion without passing the task's focused
  `verify` command; pending must not be dressed up as completed. Complete
  candidate verification is a separate receipt-refresh stage governed by
  configuration: `"auto"` runs it at folder closeout, while `"manual"` defers it
  until a human explicitly invokes `assent verify`.
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

The worktree is a change-isolation, conflict-management, audit, and recovery
boundary, not a security sandbox. With `danger-full-access` or
`bypassPermissions`, an AI can still access resources available to its OS
identity, including external Git writers, network services, credentials, and
files outside the worktree. Use unattended execution only in trusted projects
and account environments; Assent does not create a container or VM sandbox.

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
   conversation. Format follows `.assent/format.md`.
2. Run `assent check` — passing is what adjourns the meeting; not passing means
   the plan is not finished.
3. Decisions that stay valid across plans go into the project `AGENTS.md`
   Permanent constraints.
