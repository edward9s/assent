# assent working instructions

> `~/.assent/instructions.md` governs Assent sessions. `format.md` owns
> persisted artifact schemas; `workflow.md` owns CLI, report, receipt, and
> scheduler mechanics. `assent init` installs all three in the per-user Assent
> home; projects do not carry copies.

## Contract ownership

- repository-specific development constraints belong in `AGENTS.md`;
- scheduled-session procedure belongs in `instructions.md`;
- persisted artifact schemas, filename rules, and state meanings belong in
  `format.md`; and
- CLI, report, and receipt contracts belong in `workflow.md`.

Other documents may reference an owned rule, but must not duplicate it as a
competing normative contract.

Contract and reader documentation must be concise, present-tense, and
reader-oriented. Keep only text needed to act or understand; do not add
development chronology, meeting narrative, or changelog entries unless the
file explicitly owns historical records.

Git commit messages must contain no AI attribution or advertising text such as
`Co-Authored-By` or `Generated with`.

## Read only what the session needs

A **meeting / interactive session** starts with:

1. project-root `AGENTS.md`, when present;
2. this file;
3. `~/.assent/format.md` before creating or changing task files;
4. `~/.assent/workflow.md` when CLI or scheduler mechanics matter, including
   `[workflow]`, `[roles]`, or `[abilities]`, or when reviewing implementation
   against a plan; and
5. the current folder's task files, `_report.md`, and directly relevant source
   and tests as needed.

An **assent-scheduled task session** reads only:

1. the scheduler-provided `AGENTS.md` path, if any;
2. the scheduler-provided absolute path to this file;
3. the scheduler-provided absolute path to its one task file; and
4. directly relevant source and tests.

Use scheduler-provided absolute paths. A worktree has no `.assent/`; task and
journal files live in the main tree, while contracts live under `~/.assent`.
Do not read old folders, journals, or `_assent.log` unless debugging or asked.

## Verification boundary

An AI session never initiates the full suite or `.assent/verify.py`. An
interactive session may run complete verification only when the human
explicitly requests it. A scheduled task or review/repair session runs only the
smallest relevant check and its scheduler-provided focused command; the
scheduler owns any workflow `full_verify` action and runs it outside the AI
session. After manual `assent reconcile --continue`, leave that full
verification for the human.

## Scheduled-session rules

- Edit source only below the session cwd (the isolated worktree) and only
  within the scheduler-authorized scope. Confirm paths before writing. The main
  tree is read-only except that an ordinary task session may change its own
  task status line and append its own journal. `~/.assent` is always read-only.
- Touch nothing unrelated. Reuse existing patterns; add no unrequested
  abstraction, configuration, or scaffolding.
- Keep conjecture, changes, verified facts, and unverified facts distinct.
- Code, Git facts, and test results are authoritative.
- Do not run `git commit`; Assent owns checkpoints and Git transitions.
- Do not kill a process the session did not start. On timeout, extend or batch
  the command; do not launch a possibly still-running command again in
  parallel or mark a task `BLOCKED` solely because an outer tool timed out.
- Stay until every command started by the session finishes. A long command may
  run in the background only if this turn polls it to completion. Interim
  updates use `<command>: running, <elapsed>`; report the outcome once.
- Keep output economical: state facts once and quote only the decisive failure
  unless more detail is requested.
- A worktree is an isolation, conflict-management, audit, and recovery
  boundary, not a security sandbox. The AI retains whatever filesystem,
  credential, network, and external-Git access its OS identity permits.

The adapter result defines the session boundary. A nonzero adapter exit or
watchdog stall is an adapter failure: Assent preserves work and applies its
retry policy. A terminal task result is usable only after tamper and fail-closed
scope checks pass. A task's `BLOCKED` result then stays inside
`[workflow].task`, where a later configured verdict/repair role may resolve it;
the role-specific prompt defines that responsibility.

## Shared ignored directories

Assent records the reviewed shared-directory decision in the primary
worktree's untracked `.assent/manifest.toml` and provisions matching links
before a session.

- A plan-review prompt that requires a `shared_paths` verdict field owns the
  semantic decision in that same review session. Return its exact `paths` and
  `watch` lists in the terminal review record and do not run the CLI command;
  account for every existing same-primary directory link the prompt names. The
  scheduler validates the complete link agreement before writing the manifest.
  An omitted link returns a correction to this bounded reviewer rather than
  accepting a partial decision.
- In any other session, if the prompt says `UNKNOWN` or `STALE`, settle it
  before closeout with
  `assent shared-paths review --path DIR --watch FILE` (repeat either option as
  needed), or `assent shared-paths review --none --watch FILE`. Run it from the
  session cwd: the managed source worktree whose snapshot is being reviewed and
  whose links the command reconciles. The primary worktree supplies the targets
  and holds the manifest; running the command there only caches that primary
  snapshot and creates no link. A watch path is the exact tracked dependency or
  build file whose change should invalidate the decision. Judge only from
  Git-ignore rules, those declarations, and this task's evidence. This command
  is the manifest's only writer.
- If it says `NO-IGNORED-DIRECTORY-CANDIDATE`, a successful Git query found no
  ordinary ignored directory to declare. There is nothing to review; do not run
  the command just in case. This is not a semantic claim that shared input will
  never be needed.
- If the task demonstrably needs an ignored directory, confirm that the same
  relative path is an ordinary, Git-ignored directory in the primary worktree,
  then record it with
  `assent shared-paths review --path DIR --watch FILE`. Assent provisions the
  junction or directory symlink. Never hand-create a source-worktree link.
  Never copy the ignored directory tree in, provision unrelated caches or
  credentials, or modify anything inside the linked target. Ordinary ignored
  leaf files beside tracked source need no action.

## Review and acceptance meetings

Read `_report.md` first. If it says `TECHNICAL DEBT REVIEW REQUIRED`, read
`_technical_debt.md`, tell the human before recommending `accept`, and obtain
one disposition per finding: the local repair is sufficient; append or rework
a task for concrete follow-up; or promote a durable rule to `AGENTS.md`. Write
requested changes to their canonical owner. No-change dispositions remain part
of the existing human `accept` decision, not a second approval state.

## Task-session closeout

Complete closeout synchronously in this turn:

1. Check every acceptance item and run the scheduler-provided focused command
   to completion. Do not run the full suite.
2. Change only this task's status line to `DONE` or `BLOCKED`.
3. Append one `[[entry]]` to the scheduler-provided journal path with `time`,
   prompt-specified `by`, actual `requested_model`, actual
   `requested_effort` when the prompt states one, `event`, a one-sentence
   verifiable `summary`, and
   optional process `detail`.
4. Do not commit; the scheduler owns the checkpoint.

## Meeting closeout

1. Persist each decision in its canonical file; do not leave it only in chat.
2. Run `assent check`; the meeting ends only when it passes.
3. Put decisions that remain valid across plans in `AGENTS.md`.
