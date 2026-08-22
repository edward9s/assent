# Configuration

English is the canonical technical contract. See the
[Traditional Chinese reader translation](zh-TW/CONFIGURATION.md).

Assent reads built-in defaults, `~/.assent/assent.toml`, and optional project
`.assent/assent.toml`, in that order. Tables merge by key. Scalars and arrays
replace the inherited value.

## Models and adapters

Task files use only the portable tiers `prime`, `core`, and `lite`. Each adapter
maps a tier to one complete `model/effort` selection:

```toml
[adapter.codex.models]
prime = "gpt-5.6-sol/high"
core = "gpt-5.6-luna/max"
lite = "gpt-5.6-luna/medium"
```

The first `/` separates model and effort. A selection without `/` passes no
effort flag and uses the vendor CLI default. A project mapping replaces that
adapter's built-in mapping whole, so all three tiers must be present.

A role may state a tier or a vendor selection directly. A vendor selection is
valid only when the workflow step resolves to exactly one adapter.

## Abilities

An ability has a prompt and a write capability:

```toml
[abilities.plan_review]
prompt = "Inspect the cumulative candidate for correctness and simplicity."
writes = false

[abilities.plan_fix]
prompt = "Use the requirements to determine whether the defect is in tests or implementation, then correct whichever is wrong without weakening correct tests."
writes = true
```

`prompt` describes responsibility. `writes` decides whether the role may edit
ordinary candidate files. Ability names have no engine meaning. Prompts never
grant Git, task-contract, journal, receipt, scheduler-action, or acceptance
authority.

## Roles

A role composes one or more abilities and may choose a model:

```toml
[roles.plan_repairer]
ability = ["plan_review", "plan_fix"]
model = "core"
```

If any composed ability has `writes = true`, the role is writable. Adapter
selection belongs to the workflow entry: `adapter = "codex"` selects one, and
`adapter = [...]` is an ordered availability list. Omitting it uses the global
adapter rotation.

## Workflows

`[workflow]` contains three arbitrary finite step arrays:

```toml
[workflow]
task = [
  { role = "implementer" },
  { action = "focused_test" },
  { role = "task_repairer" },
  { action = "focused_test" },
]
plan = [
  { role = "plan_quality_repairer" },
  { action = "focused_sweep" },
  { role = "plan_repairer" },
  { action = "focused_sweep" },
]
integration = [
  { action = "full_verify" },
  { role = "integration_repairer" },
  { action = "full_verify" },
]
```

Each entry contains exactly one `role` or `action`. Legal actions are:

| Array | Action | Meaning |
| --- | --- | --- |
| `task` | `focused_test` | Run the current task's `verify` command. |
| `plan` | `focused_sweep` | Run each distinct task command on the cumulative candidate. |
| `integration` | `full_verify` | Reconstruct and verify the exact selection. |

A role success advances one position. A passing action completes the layer and
skips later positions. A failing action advances. Exhaustion without a pass is
`REVIEW UNRESOLVED, HUMAN DECISION` with exit zero and preserved evidence.

There is no structured verdict setting. Reviewer, fixer, or combined behavior
comes entirely from the configured abilities. Sessions are sequential and do
not converse; bounded output and action evidence flow to the next step. Adjacent
reviewer and fixer roles therefore both run after the reviewer exits successfully.
The scheduler stores role output in `.assent/<PLAN>/_workflow.toml` and injects
it into the next role's prompt; it never branches on that prose.

The effective `task` array is required and nonempty. A project override may omit
it only to inherit a lower configuration layer. An action-only task array runs
mechanical verification without an AI session. Omitted or empty `plan` adds no
plan session. Omitted or empty `integration` adds no integration role session;
the scheduler still performs `full_verify` for a completed run selection. When
a nonempty layer ends in a role, Assent adds that layer's action once at the end.

Writable task and plan roles may edit any ordinary candidate source, test,
configuration, or documentation file needed by the requirements. There is no
task path scope or finding ownership. A single-plan integration repair uses
that plan's source worktree. A failing multi-plan candidate is handed to a
human because there is no unique source branch to mutate.

## Task workflow overrides

A task may replace the project task array with task-local entries containing
only `role` or `action`:

```toml
workflow = [
  { role = "tests_writer" },
  { role = "source_implementer" },
  { action = "focused_test" },
]
```

An omitted field inherits `[workflow].task`. An empty task workflow is invalid;
use `workflow = [{ action = "focused_test" }]` when no AI session is wanted.

## Usage limits

Provider usage is recorded as derived observability evidence. It never decides
task correctness. Adapter quota and authentication handling preserve WIP
checkpoints and rotate through the configured candidates. The provider-neutral
immediate-continuation record is:

```json
{"type":"assent.checkpoint_resume"}
```

## Safety boundary

Default adapters may run with broad OS permissions. Assent uses worktrees,
prompts, control-file snapshots, primary-worktree comparison, and Git-HEAD
checks as cooperative detection, not as a preventive sandbox. Run unattended
workflows only in trusted repositories and environments.
