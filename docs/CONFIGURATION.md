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
prime = "gpt-5.6-sol/medium"
core = "gpt-5.6-luna/max"
lite = "gpt-5.6-luna/xhigh"
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
adapter rotation from `[adapter].name`. The shipped workflow omits `adapter` on
every role entry.

The same role can therefore be placed differently at each workflow position:

```toml
[workflow]
runtime_test = [
  { action = "runtime_test" },
  { role = "runtime_repairer" }, # Global rotation.
  { action = "runtime_test" },
  { role = "runtime_repairer", adapter = "codex" }, # Codex only.
  { action = "runtime_test" },
  # Ordered fallback with a per-step model override.
  { role = "runtime_repairer", adapter = ["claude", "codex"], model = "prime" },
  { action = "runtime_test" },
]
```

For a task session, model precedence is workflow entry model > role model > task file model. A task-local workflow entry contains only a role or action, so its
named role falls back directly to the task tier. Plan and integration sessions
have no task fallback; their role or workflow entry must state a model.

When a workflow entry states a portable tier but omits `adapter`, every adapter
in the global rotation maps that tier independently. A vendor `model/effort`
selection with no `adapter` is valid only when the global rotation contains
exactly one adapter; otherwise configuration loading refuses it as ambiguous.

## Workflows

`[workflow]` contains a preflight repair array, three core finite step arrays,
and an independent runtime-test array:

```toml
[workflow]
preflight = [
  { action = "check" },
  { role = "preflight_repairer" },
  { action = "check" },
  { role = "preflight_repairer" },
  { action = "check" },
  { role = "preflight_repairer" },
  { action = "check" },
]
task = [
  { role = "implementer" },
  { action = "focused_test" },
  { role = "task_repairer" },
  { action = "focused_test" },
  { role = "task_repairer" },
  { action = "focused_test" },
  { role = "task_repairer" },
  { action = "focused_test" },
  { role = "task_repairer" },
  { action = "focused_test" },
  { role = "task_repairer" },
  { action = "focused_test" },
  { role = "task_repairer" },
  { action = "focused_test" },
]
plan = [
  { role = "plan_quality_repairer" },
  { action = "focused_sweep" },
  { role = "plan_repairer" },
  { action = "focused_sweep" },
  { role = "plan_repairer" },
  { action = "focused_sweep" },
  { role = "plan_repairer" },
  { action = "focused_sweep" },
  { role = "plan_repairer" },
  { action = "focused_sweep" },
  { role = "plan_repairer" },
  { action = "focused_sweep" },
  { role = "plan_repairer" },
  { action = "focused_sweep" },
]
integration = [
  { action = "full_verify" },
  { role = "integration_repairer" },
  { action = "full_verify" },
  { role = "integration_repairer" },
  { action = "full_verify" },
  { role = "integration_repairer" },
  { action = "full_verify" },
  { role = "integration_repairer" },
  { action = "full_verify" },
  { role = "integration_repairer" },
  { action = "full_verify" },
  { role = "integration_repairer" },
  { action = "full_verify" },
]
runtime_test = [
  { action = "runtime_test" },
  { role = "runtime_repairer" },
  { action = "runtime_test" },
  { role = "runtime_repairer" },
  { action = "runtime_test" },
  { role = "runtime_repairer" },
  { action = "runtime_test" },
  { role = "runtime_repairer" },
  { action = "runtime_test" },
  { role = "runtime_repairer" },
  { action = "runtime_test" },
  { role = "runtime_repairer" },
  { action = "runtime_test" },
]
```

Each entry contains exactly one `role` or `action`. Legal actions are:

| Array | Action | Meaning |
| --- | --- | --- |
| `preflight` | `check` | Run the complete read-only plan and environment check. |
| `task` | `focused_test` | Run the current task's `verify` command. |
| `plan` | `focused_sweep` | Run each distinct task command on the cumulative candidate. |
| `integration` | `full_verify` | Reconstruct and verify the exact selection. |
| `runtime_test` | `runtime_test` | Run the declared runtime command in its candidate. |

A role success advances one position. A passing action completes the layer and
skips later positions. A failing action advances. When entered by `assent run`,
exhaustion becomes `REVIEW UNRESOLVED, HUMAN DECISION`, preserves evidence, and
returns zero so unrelated plans continue. Standalone `assent test [PLAN]`
returns 1 when its runtime workflow is exhausted. A final preflight failure is
a refused precondition and also returns nonzero.

`preflight` strictly alternates `check` actions with writable repair roles and
starts and ends with the action. `assent run` enters it before task execution;
the first passing check skips all repair roles. The repairer may change only
declarative Assent input named by check evidence and may not change status,
workflow cursors, scheduler evidence, receipts, Git, candidate source, or
acceptance. A final failure stops `run` nonzero. Explicit `assent check` remains
read-only and never enters this workflow.

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

## Runtime-test settings

Runtime testing has its own workflow layer and does not reuse task, plan, or
integration actions. The shared `~/.assent/assent.toml` defines the writable
`runtime_repairer` role and a finite default workflow. A
project may replace `[workflow].runtime_test`; its array must strictly
alternate actions and writable roles, begin and end with an action, and give
each custom role a model. Each role entry may independently select its adapter
as shown above.

`assent init` creates the main contract at `.assent/_runtime_test.toml` without
guessing the project command:

```toml
execution = "pending"
```

On the first no-argument `assent test`, a pending action records that it did not
start. The next configured writable role inspects the implemented project and
proposes the exact transition to `explicit` with the unambiguous, documented,
finite production operation:

```toml
execution = "explicit"
command = "python run.py sync"
```

or an ordered command array:

```toml
execution = "explicit"
command = ["python run.py migrate", "python run.py sync"]
```

One shell command is one complete string containing its executable and every
argument. The array above means two sequential shell commands; it is not an argv
array. Do not write `command = ["python", "run.py", "sync"]` for one command.

The operation uses normal persistent production configuration and data. A unit
test, test runner, mock, fixture, dedicated probe, temporary or in-memory
resource, sample invocation, or artificially bounded invocation is not a
production operation. Discovery changes no ordinary project file and leaves the
contract pending if the operation is missing or ambiguous. The role cannot
select `disabled`. Assent first restores its direct edit to the control file,
validates the proposal, displays the exact command, and asks once `[y/N]`.
Only `y` or `yes` installs it as scheduler-owned state; refusal or stdin EOF
runs nothing and retains `pending`. The following action runs the command from
the beginning. Once explicit, later `assent test` runs without asking again.
`assent check` remains read-only and never invokes this discovery workflow.

Each plan instead has its own `_runtime_test.toml` contract with `disabled`,
`explicit`, or `after_plan`; its exact rules are in `format.md`. A plan command
never falls back to the main contract or a task's `verify` command.

The runtime repair ability may edit ordinary candidate source, tests, fixtures,
project configuration, and documentation. It may not run a command or change
Assent/Git control state, apart from proposing the pending main contract's one
validated transition. Its text cannot declare a runtime pass. The full runtime
workflow and candidate lifecycle are in [Workflow](WORKFLOW.md).

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
