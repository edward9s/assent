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

Reliability by construction is the highest architectural principle: use the
smallest architecture and fewest states that make invalid behavior difficult
to form. Prefer removing a failure mode over detecting, tracking, routing, or
recovering from it. Semantic precision shares that priority: one term names one
actual mechanism, and no name implies a capability the system does not provide.

Implementation uses the smallest coherent structure that satisfies the stated
behavior. Review treats avoidable state, branching, indirection, compatibility,
and recovery machinery as substantive defects. Repair removes the failure
mechanism instead of surrounding it with another guard when a direct invariant
can make the failure impossible. Repair uses the authoritative requirements to
decide whether a defect is in the tests or the implementation and corrects
whichever is wrong. Preserve correct tests; never weaken, narrow, delete,
rewrite, bypass, or mock them merely to make a check pass.

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
5. the current plan's task files, `_report.md`, and directly relevant source
   and tests as needed.

An **assent-scheduled task session** reads only:

1. the scheduler-provided `AGENTS.md` path, if any;
2. the scheduler-provided absolute path to this file;
3. the scheduler-provided absolute path to its one task file; and
4. directly relevant source and tests.

Use scheduler-provided absolute paths. A worktree has no `.assent/`; task and
journal files live in the main tree, while contracts live under `~/.assent`.
Do not read old plans, journals, or `_assent.log` unless debugging or asked.

## Verification boundary

An AI session never initiates the full suite or `.assent/verify.py`. An
interactive session may run complete verification only when the human
explicitly requests it. A scheduled task or review/repair session runs only the
smallest relevant check assigned by its runtime prompt. When that prompt says a
`focused_test` action is scheduler-owned, the session does not run the focused
command; the scheduler owns any workflow `full_verify` action and runs it
outside the AI session. After manual `assent reconcile --continue`, leave that
full verification for the human.

## Scheduled-session rules

- Edit project source only below the session cwd and any additional absolute
  conflict workspaces explicitly named by the runtime prompt. Confirm paths
  before writing. The main tree, task contracts, journals, scheduler state,
  and `~/.assent` are always read-only; the scheduler owns their transitions.
- Command side effects count as writes. A check, compiler, importer, formatter,
  generator, or test must leave no non-ignored generated artifact in the
  project worktree; use a non-writing check or project-approved temporary output
  when the runtime prompt reserves the focused command for the scheduler.
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
retry policy. A successful role session advances to the next configured step;
only a scheduler action changes task completion state.

## Shared ignored directories

If an injected clause says `UNKNOWN` or `STALE`, inspect the complete listed
inventory and submit the decision with `assent shared-paths declare` from the
source worktree before closeout. Assent validates, records, and applies the
declaration.
Cover every listed ordinary ignored directory once
as shared or non-shared, and watch only the tracked dependency or build files
that invalidate the decision.

Share only a demonstrably required directory whose same-relative primary target
is an ordinary Git-ignored directory. Never copy an ignored tree. Never
hand-create a source-worktree link, provision unrelated caches or credentials,
or modify a linked target.

## Review and acceptance meetings

Read `_report.md` first, then inspect the relevant requirements, source diff,
and verification evidence. Verification is evidence; only the human's explicit
`assent accept` publishes work.

## Task-session closeout

Complete closeout synchronously in this turn. Check every acceptance item and
run a focused command only when the runtime prompt assigns it to this session;
never run a scheduler-owned action or the full suite. Return a concise result
for the next configured step. Do not edit task contracts or journals and do not
commit; the scheduler owns status, evidence, and checkpoints.

## Meeting closeout

1. Persist each decision in its canonical file; do not leave it only in chat.
2. Ensure every tracked project file required by a scheduled session is in the
   Git snapshot from which its worktree will start. Uncommitted primary-tree
   changes to files such as `AGENTS.md` and `.gitignore` are not inherited; if
   committing is outside this meeting's authority, state that precondition and
   do not call the plan runnable.
3. Run `assent check`; the meeting ends only when it passes. This mechanical
   gate does not replace the planning audit in `format.md`.
4. Put decisions that remain valid across plans in `AGENTS.md`.
