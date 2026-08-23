# Assent workflow contract

> `~/.assent/workflow.md` defines scheduler, CLI, verification, and acceptance
> behavior. `format.md` separately defines plan-file schemas.

## Invariants

- Reliability comes from architecture simple enough that failure is difficult
  to form. Prefer one direct invariant over tracking, routing, compatibility,
  recovery, or inferred state.
- Names describe real mechanisms exactly. A workflow is an ordered step array,
  a step is one configured role or scheduler action, and a session is one role
  invocation. Assent does not call a sequence of sessions a meeting.
- Git is mandatory. Every plan is explicit or derived without ambiguity.
- Preserve every token-burned edit and diagnostic. Failure and interruption do
  not revert the candidate automatically.
- Writable AI roles may edit ordinary candidate source, tests, configuration,
  and documentation. They never edit task contracts, journals, scheduler
  state, receipts, Git state, or acceptance state.
- Scheduler actions, not AI text, decide whether a layer passed.
- Only the explicit human `assent accept` action publishes work.
- An unresolved engineering decision is exit zero plus durable evidence for a
  human. Infrastructure failure, a refused precondition, or a broken safety
  gate is nonzero.

## Settings and initialization

Settings resolve from built-in defaults, `~/.assent/assent.toml`, optional
project `.assent/assent.toml`, then an applicable CLI override. Tables merge by
key; scalars and arrays replace whole values. The effective `workflow.task`
array is required and nonempty. Empty `plan` and `integration` arrays disable
their optional role layers.

Before an AI session, `instructions.md`, `format.md`, and `workflow.md` in the
user home must match the installed version. Run `assent init` to refresh them.

## Sessions and worktrees

Each role step starts one independent AI session. Sessions do not talk to each
other. The scheduler supplies prior bounded session output and the latest
action evidence to the next step.

Role and ability names have no engine meaning. Abilities provide prompt text
and a `writes` flag; roles compose abilities and may choose a model. A workflow
entry chooses its adapter candidates and its position determines task, plan,
or integration context.

A writable session may change any ordinary candidate file needed to satisfy
the stated requirements. Predicted paths and task ownership are not write
boundaries. A read-only session may change no project file. All sessions are
forbidden from Git, Assent commands, scheduler-owned actions, task contracts,
journals, receipts, and `.git` or `.assent` state. For unknown or stale local
input evidence, the bounded `assent shared-paths declare` operation is the only
Assent-command exception. The session supplies the reviewed declaration;
Assent validates, records, and applies it.

Every writable repair session treats the authoritative requirements as the
source of truth, determines whether each defect is in the tests or the
implementation, and corrects whichever is wrong. It preserves correct tests
and never weakens, narrows, deletes, rewrites, bypasses, or mocks them merely
to make a check pass.

The worktree is an isolation and recovery boundary, not a security sandbox.
Prompt rules and before/after checks detect control-boundary violations; broad
adapter permissions do not prevent access outside the project. Use unattended
execution only in trusted environments.

One plan folder permits one live `run`, enforced by an OS lock. Different plans
may run concurrently in dedicated `<project>.worktrees/<plan>/` worktrees.

## Workflow configuration

`[workflow]` has three ordered arrays:

| Array | Role context | Legal scheduler action |
| --- | --- | --- |
| `task` | one task | `focused_test` |
| `plan` | the cumulative plan candidate | `focused_sweep` |
| `integration` | the exact selection and scheduler-named repair workspace(s) | `full_verify` |

Each entry contains exactly one `role` or `action`. Arrays may contain any
finite ordering of configured roles and the layer's legal action. Assent does
not require reviewer/fixer pairs, structured verdicts, named owners, or a
special repair grammar.

A role may use the global adapter rotation, one adapter, or an ordered adapter
list. Authentication or availability failure preserves progress and tries the
next candidate. Quota preserves progress and rotates or waits. A role process
failure is an infrastructure failure; review findings are ordinary session
evidence for later writable roles.

For a task role session, a workflow entry's `model` overrides the role's
`model`, which overrides the task file's `model`. Plan and integration sessions
have no task fallback, so the role or workflow entry must state a model. When a
workflow entry omits `adapter`, it uses the global adapter rotation. A portable
tier is mapped independently by each candidate adapter; a vendor
`model/effort` selection is valid only when that rotation contains exactly one
adapter.

Actions accept no role, adapter, model, prompt, or arbitrary command. AI
sessions never run them.

Task configuration is explicit:

- a project override may omit `task` only to inherit a lower layer's array;
- the effective `task` array is required and nonempty;
- a task file that omits `workflow` inherits that effective array;
- an empty task-local workflow is invalid;
- an action-only task array explicitly requests mechanical verification without
  an AI session; and
- a trailing `focused_test` is added when
  the final entry is a role;
- omitted or empty `plan`: no cumulative plan layer;
- omitted or empty `integration`: no integration role session; `full_verify`
  remains the mechanical integration action;
- a trailing plan or integration action is added when a nonempty array ends in
  a role.

## One interpreter

Task and plan workflows use one finite linear interpreter:

1. A role session completes successfully, its bounded output is persisted as
   evidence, and the cursor advances once.
2. A passing action completes the layer immediately; later roles are skipped.
3. A failing non-final action records its exact command/output and advances to
   the next configured step.
4. If the array ends without a passing action, all edits and evidence remain and
   the outcome is `REVIEW UNRESOLVED, HUMAN DECISION`.

There is no finding ledger, ownership routing, cascade, repair phase, scope
amendment, disposition protocol, or second review engine. The finite array is
the only automation budget.

At task completion the scheduler alone sets `DONE`, appends the journal result,
and creates the checkpoint. If the final `focused_test` fails, it sets the task
`BLOCKED` and leaves evidence for human adjudication. Plan roles review the
cumulative result as one unit; they do not allocate findings to task owners.

Integration follows the same linear rule. `full_verify` reconstructs the exact
candidate and writes source-bound evidence. PASS completes without an AI
session. Failure may advance to a configured integration role. Conflict
evidence mechanically names each conflicting plan and path: the scheduler
prepares a managed reconcile worktree for a target-only conflict or identifies
the persistent source worktree for a peer-only conflict. The role edits file
content; the scheduler owns staging, commits, source transitions, exact
candidate reconstruction, and recheck. A multi-plan verifier failure without
mechanically identified source attribution remains a human decision instead of
inventing ownership.

## Selection and commands

Every explicitly named live plan is audited before dispatch. Each name must
resolve to an existing plan directory containing a formal `tNNN_name.e.toml`
task. Any unresolved name prevents the whole selected operation.

With no `PLAN`, `assent run` schedules every discovered plan in dependency
order; `--jobs N` sets the concurrency cap. One or more named plans form an
exact selection and run in the stated order. `--jobs` is valid only when no
plan is named.

`run` executes task and plan workflows, then the integration workflow for the
same completed selection. No run accepts.

`status`, `check`, and `report` are read-only. `check` validates configuration,
task files, dependencies, adapters, Git layering, and global contracts.

## Verification and receipts

`focused_test` runs one task's `verify` command. `focused_sweep` runs the
distinct task commands for the cumulative plan. They write no receipt.
`full_verify` builds a temporary integration candidate and runs
`.assent/verify.py` outside every AI session. Its receipt is deletable evidence
bound to source commits, the reconstructed tree, verifier digest, and shared
input digest.

An explicit `assent verify` performs only the requested mechanical check. It
never enters a workflow role or repairs a failure.

- `verify PLAN --focus TASK`: one focused command;
- `verify PLAN --focus`: distinct completed-task commands;
- `verify PLAN`: one plan candidate and receipt;
- `verify A B`: one exact dependency-ordered candidate and receipt;
- `verify --batch`: dynamically selected completed, unintegrated plans.

Verification changes no source or target ref and never accepts. A candidate
conflict is distinct from verifier failure.

## Shared ignored inputs

Complete verification mirrors only reviewed ignored-directory links and
ordinary ignored leaf files inside otherwise tracked directories. Nothing is
copied. Build trees, caches, credentials, `.git`, `.assent`, and link-target
contents are not enumerated.

The primary worktree's untracked `.assent/manifest.toml` stores reviewed shared
directory profiles. `assent shared-paths declare` is the only writer. A profile
may classify as `UNKNOWN`, `REVIEWED-NONE`, `REVIEWED-PATHS`, `STALE`, or
`NO-IGNORED-DIRECTORY-CANDIDATE`. Unknown or stale evidence adds one bounded
validated command to a source role. The following action refuses to start
until that operation settles the decision; sessions never edit the manifest or
create a link by hand.

Candidate cleanup detaches directory links before recursive cleanup and never
traverses their targets. If inventory or detachment cannot be proven, cleanup
retains the managed path for recovery.

## Reconcile, reports, and acceptance

`assent reconcile PLAN` is the explicit human alternative outside `run` for a
finished source that conflicts with the target. It uses the same managed
source-first reconcile boundary: file content is edited only at the conflict
paths, while Git actions stay scheduler-owned.

`_report.md` is a mechanically regenerated, untracked acceptance agenda. It
contains task status, journal summaries, usage, and receipt freshness; it is
not authorization.

Complete plan verification refreshes that plan's `_report.md` exactly once
after the receipt operation settles and all verification locks are released.

Direct `accept PLAN` and selected `accept A B` never start verification. Unless
already integrated by ancestry, they require a fresh exact receipt. `accept
--all` may replay a fresh batch receipt or verify plans sequentially according
to its documented fallback. Acceptance performs no pull, push, rebase,
force-push, source deletion, or automatic conflict resolution.

## Cleanup, archive, rejection, and rework

`clean` removes only proven Assent-owned, clean, integrated worktrees and
branches. `archive` requires finished, accepted state and stores the plan under
`.assent/_archive`. `reject` is an explicitly confirmed destructive reset;
`rework` reopens existing tasks and preserves code unless the human explicitly
chooses `--revert-code`.

Never manually remove managed worktrees or temporary branches. Git evidence,
not a hand-maintained current-plan pointer, determines recovery.

## Checkpoints and interruption

The scheduler checkpoints dirty candidate work before quota waits, adapter
rotation, immediate continuation, failure, and interruption. Startup gathers a
dirty managed plan worktree into a WIP checkpoint without inferring task
ownership.

The provider-neutral immediate-continuation record is:

```json
{"type":"assent.checkpoint_resume"}
```

When quota and this record both appear, quota handling wins. Ctrl+C exits 130
after preserving current progress. A control-boundary violation or unprovable
Git state is nonzero and leaves evidence intact for human recovery.
