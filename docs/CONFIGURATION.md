# Configuration

*[README](../README.md) · [Traditional Chinese](zh-TW/CONFIGURATION.md)*

Assent requires Python 3.11+, Git, and at least one installed and authenticated
AI CLI. The Python package has no third-party runtime dependencies.

## Files and precedence

Shared files live under `~/.assent/`:

```text
assent.toml       scheduler settings
adapter.toml      AI CLI commands and model mappings
instructions.md   AI session rules
format.md         plan-file contract
workflow.md       scheduler and acceptance contract
```

The project owns `AGENTS.md`, `.assent/verify.py`, its plans, and optional
`.assent/assent.toml` and `.assent/adapter.toml` overrides.

Settings resolve from built-in defaults, user configuration, project override,
and finally any supported command-line override. Tables merge by key; scalars
and arrays replace the lower value. Omit a value to inherit it. An empty array
is an intentional empty value, and so is a blank string, which is refused where
the setting needs text.

## Think in workflows

The central configuration chain is:

```text
ability: prompt + authority
        ↓
role: one or more abilities + model
        ↓
workflow: ordered roles and scheduler actions
```

An ability says what an AI session is responsible for. A role composes those
abilities. A workflow decides when that role runs and which mechanical result
opens the next repair attempt. Names such as `reviewer` or `fixer` are only
labels for people; the engine never infers authority from a name.

### Abilities

```toml
[abilities.task_review]
prompt = "Review only the current task's failure evidence."
writes = false
produces_verdict = true
```

- `prompt` is appended to the session instructions. Keep it specific to that
  workflow layer; the scheduler supplies the detailed protocol and evidence.
- `writes` states whether the role may edit source in its scheduler-authorized
  scope. It is the capability distinction that makes a reviewer read-only.
- `produces_verdict = true` makes the role return the structured review result.
  It defaults to `false`.

The prompt does not grant extra scope, Git authority, or permission to run
verification. Scheduler rules remain authoritative.

### Roles

```toml
[roles.task_reviewer_fixer]
ability = ["task_review", "task_fix"]
model = "prime"
```

`ability` is a nonempty ordered list. The role writes if any included ability
writes, and produces a verdict if any included ability does so. `model` is
optional. A workflow role entry may override it; an ordinary task step
otherwise inherits the task's profile. Every role step in `workflow.plan` and
`workflow.integration` must effectively state a model through the role or
workflow entry, because such a session answers for a whole unit and has no
single task to inherit one from.

### Scheduler actions

Actions are run by Assent outside AI sessions:

| Workflow | Action | Checks |
| --- | --- | --- |
| `task` | `focused_test` | the current task's `verify` command |
| `plan` | `focused_sweep` | the distinct union of focused commands in one completed plan |
| `integration` | `full_verify` | the reconstructed candidate for the exact selected plan set |

An action is legal only in its matching array. AI roles do not run these
actions or `.assent/verify.py` themselves.

## How an array runs

A passing action completes its layer immediately and skips everything after
it. A failing action advances to the next configured repair role; the next
action rechecks the resulting source. The array is therefore both the order of
work and the finite repair budget. Exhaustion preserves the evidence and edits
as `REVIEW UNRESOLVED, HUMAN DECISION`; it does not discard work.

Between two actions, use one of these shapes:

```toml
# One session reviews and repairs.
{ action = "focused_sweep" },
{ role = "plan_reviewer_fixer" }, # writes + produces_verdict
{ action = "focused_sweep" },

# Separate read-only review and write-capable repair sessions.
{ action = "focused_sweep" },
{ role = "plan_reviewer" },       # produces_verdict, no writes
{ role = "plan_fixer" },          # writes, no verdict
{ action = "focused_sweep" },
```

A writable verdict role must be the only role between the actions: it owns the
diagnosis and repair in the same session. A read-only verdict role may instead
be followed by exactly one write-capable, non-verdict fixer. These rules come
from the capability flags and position, never the role names.

## Three different repair responsibilities

Use different abilities and prompts because each layer answers a different
question:

| Layer | Repair scope |
| --- | --- |
| `workflow.task` | The current task's `BLOCKED` or `focused_test` evidence. It may settle a small planning omission, such as one exact missing scope path, and repair it in the same writable verdict session. It does not spend the plan repair budget. |
| `workflow.plan` | Whether the cumulative worktree from all completed tasks conforms to the existing plan. It runs one normal quality review before the first `focused_sweep`, then handles sweep failures and cross-task regressions through implicated existing tasks. |
| `workflow.integration` | Whether the same exact selected plan set can be reconstructed and pass `full_verify`. It handles candidate conflicts and complete-verifier failures without dropping a plan or accepting only a prefix. |

The integration workflow verifies and repairs; it never performs human
acceptance. Publication remains the later explicit `assent accept` decision.

### Default workflow

The packaged configuration uses one implementation session per task and splits
the reviewer and fixer at every repair layer. This validates ownership and scope
before a fixer writes. The abilities below are quoted with their prompts shortened;
`~/.assent/assent.toml` holds the full text and defines only the recommended
split roles. The combined form described above remains valid for custom settings:

```toml
[abilities.write_tests]
prompt = "Write or update tests that prove the supplied requirements."
writes = true

[abilities.implement_source]
prompt = "Implement the supplied requirements and satisfy the supplied focused checks."
writes = true

[abilities.task_review]
prompt = "Resolve only the current task's BLOCKED or focused_test evidence. Diagnose a task-local planning omission; when one exact scope path was omitted, identify it without inventing requirements."
writes = false
produces_verdict = true

[abilities.task_fix]
prompt = "Repair every authorized task-local finding, including an exact omitted scope path. Do not create tasks or requirements."
writes = true

[abilities.plan_quality_review]
prompt = "Review the completed cumulative worktree once for conformance to the existing plan before focused_sweep. Inspect cross-task interactions and whether changed tests prove cited requirements through observable semantics. Do not accept tests that merely mirror implementation constants, template examples, or incidental representation instead of proving the cited requirement. Report only blocking correctness, safety, unmet-requirement, or focused-test-gap findings tied to an existing task. Do not invent requirements or conduct a repository-wide debt search."
writes = false
produces_verdict = true

[abilities.plan_review]
prompt = "Review only focused_sweep failure evidence to decide whether the cumulative worktree conforms to the existing plan, including cross-task interactions and concrete regressions."
writes = false
produces_verdict = true

[abilities.plan_fix]
prompt = "Repair every authorized plan-level finding through its implicated existing tasks. Do not create tasks or requirements."
writes = true

[abilities.integration_review]
prompt = "Review only the exact selection's candidate-conflict or full_verify failure evidence. Identify every integration blocker without shrinking the selection, accepting a prefix, or inventing requirements."
writes = false
produces_verdict = true

[abilities.integration_fix]
prompt = "Repair every authorized integration finding in the scheduler-provided workspaces while preserving the exact selection. Do not run Git, Assent, focused tests, full verification, or accept."
writes = true

[roles.implementer]
ability = ["write_tests", "implement_source"]

[roles.task_reviewer]
ability = ["task_review"]
model = "prime"

[roles.task_fixer]
ability = ["task_fix"]
model = "lite"

[roles.plan_quality_reviewer]
ability = ["plan_quality_review"]
model = "prime"

[roles.plan_reviewer]
ability = ["plan_review"]
model = "prime"

[roles.plan_fixer]
ability = ["plan_fix"]
model = "prime"

[roles.integration_reviewer]
ability = ["integration_review"]
model = "prime"

[roles.integration_fixer]
ability = ["integration_fix"]
model = "prime"

[workflow]
task = [
  { role = "implementer" },
  { action = "focused_test" },
  { role = "task_reviewer" },
  { role = "task_fixer" },
  { action = "focused_test" },
]
plan = [
  { role = "plan_quality_reviewer", adapter = "codex" },
  { role = "plan_fixer", adapter = "codex" },
  { action = "focused_sweep" },
  { role = "plan_reviewer", adapter = "codex" },
  { role = "plan_fixer", adapter = "codex" },
  { action = "focused_sweep" },
  { role = "plan_reviewer", adapter = "codex" },
  { role = "plan_fixer", adapter = "codex" },
  { action = "focused_sweep" },
]
integration = [
  { action = "full_verify" },
  { role = "integration_reviewer", adapter = "codex" },
  { role = "integration_fixer", adapter = "codex" },
  { action = "full_verify" },
]
```

Every reviewer uses `prime`. The task fixer uses `lite` because it repairs one
validated task-local finding and is immediately rechecked by `focused_test`.
Plan and integration fixers stay `prime` for cross-task and cross-plan work.

The plan reviewer before the first action is the one unconditional cumulative
quality review. A PASS skips its paired fixer; a valid finding authorizes that
fixer. The first passing `focused_test`, `focused_sweep`, or `full_verify` skips
the remaining failure handlers in its own array. Later repeated plan reviewer/
fixer pairs are separate repair rounds, not additional normal reviews.

## Omissions and task overrides

- Omitted `workflow.task` gives each task one implicit session using its own
  model.
- A nonempty task workflow may put worker roles before `focused_test`. If it
  contains that action, it must end with it. A worker that returns `BLOCKED`
  advances directly to the next verdict role, using the existing evidence.
- `workflow.task = []` disables per-task sessions and makes the plan workflow
  execute the plan as one unit. The plan workflow must then be nonempty.
- Omitted or empty `workflow.plan` means no plan review.
- Omitted or empty `workflow.integration` disables automatic integration
  repair.

A task file may override only its task sequence. To split one task between a
test writer and a source implementer, use the `tests_writer` and
`source_implementer` roles the packaged configuration already defines. A
settings file that dropped them states them again in an effective settings file
such as `~/.assent/assent.toml`:

```toml
[roles.tests_writer]
ability = ["write_tests"]

[roles.source_implementer]
ability = ["implement_source"]
```

Then put only the sequence override in that task's `.e.toml` file:

```toml
workflow = [
  { role = "tests_writer" },
  { role = "source_implementer" },
  { action = "focused_test" },
]
```

This opens two separate AI sessions and then runs the task's `verify` command
as `focused_test`.

Omission inherits `[workflow].task`. `workflow = []` assigns that task to
plan-wide execution. Override roles still come from the effective `[roles]`
configuration.

## Adapters and models

`[adapter].name` selects one adapter or an ordered rotation. Built-in adapters
are Claude, Codex, and Antigravity. Their commands, arguments, and portable
model mappings live in `adapter.toml`. Authenticate each CLI before unattended
use; Assent uses its existing credentials and does not manage secrets.

Plans use the portable `prime`, `core`, and `lite` model tiers, and nothing
else. Effort is not a separate portable choice and is not a task field: each
adapter maps a tier to one complete invocation. Every adapter the settings can
reach -- the `[adapter].name` rotation and every adapter a workflow entry binds
itself to -- must map all three tiers, and a missing one is refused when the
config loads; an adapter nothing reaches needs no mapping at all.

```toml
[adapter.codex.models]
prime = "gpt-5.6-sol/high"
core  = "gpt-5.6-terra/high"
lite  = "gpt-5.6-luna/max"
```

The first `/` separates the vendor model from the vendor effort, and both are
passed to that CLI exactly as written. A model name may therefore not contain
`/`; a second separator is refused when the config loads. Omit the separator
entirely to pass no effort argument and inherit the vendor CLI's own default:

```toml
lite = "gpt-5.6-luna"
```

Because a tier already carries its reasoning investment, a model family's real
limits are written into the value where a human can read them. Antigravity's
`lite` tier ships as `gemini-3.5-flash/medium` because that family has no
`high`; nothing is shifted silently at run time. If a tier is being reached for
too often "but harder", the tier itself is configured too low -- change that one
line rather than annotating individual tasks.

### Workflow model precedence

Within one role session, a workflow role entry overrides its `[roles]`
definition. The remaining fallback depends on the workflow layer:

| Workflow role | Model fallback |
| --- | --- |
| `workflow.task` | Current task |
| `workflow.plan` or `workflow.integration` | None; the workflow entry or role must state a model |

A plan or integration role answers for a whole unit, so it has no task to
inherit a model from. Omitting it there is a config error `assent check`
reports, whether or not the role produces a verdict. An omitted `workflow.task`
opens one implicit session using the task's own model.

### Naming a vendor model directly

A task file accepts `prime`, `core`, and `lite` and nothing else; anything else
is refused while the plan is read. A vendor model id names one release, and a
task file outlives it, so the id belongs in configuration rather than in the
plan.

A `[roles]` entry or a workflow entry may instead name the vendor selection
itself, using the same `model/effort` grammar as the `models` table. There is no
marker syntax: any value that is not a tier is read as a vendor selection.

```toml
plan = [
  # a vendor selection outside codex's three tiers
  { role = "plan_reviewer_fixer", adapter = "codex", model = "gpt-5.6-sol/xhigh" },
  { action = "focused_sweep" },
]
```

This bypasses that adapter's `models` table entirely and does not modify
`adapter.toml`. Because a vendor string means nothing to another vendor, any
workflow step using one must resolve to exactly one adapter.

Every workflow role entry may select one adapter or an ordered fallback list:

```toml
{ role = "implementer", adapter = "codex" }
{ role = "plan_reviewer_fixer", model = "prime" } # global rotation
{ role = "task_fixer", adapter = "codex", model = "gpt-5.6-terra/low" }
{ role = "task_reviewer_fixer", adapter = ["claude", "codex"] }
```

When omitted, the role follows the global `[adapter].name` rotation. A string
fixes that role to one adapter; quota exhaustion waits for that adapter. A list
tries only its declared adapters in order: quota or adapter availability
failure preserves progress and advances without consuming a task retry, and
Assent waits after the whole list is unavailable or quota-exhausted. Test failure, `BLOCKED`,
and an invalid verdict advance through the workflow or retry policy instead of
changing adapters. Each workflow step starts from the first declared adapter.
Authentication failure preserves progress and skips that candidate. If every
candidate requires login, Assent stops with `AUTHENTICATION REQUIRED`; log in
and run the command again.

| Effective model | May omit `adapter`? | Result |
| --- | --- | --- |
| Portable `prime`, `core`, or `lite` | Yes | Use the global rotation and each adapter's tier mapping. |
| Vendor selection | Only when the global rotation contains exactly one adapter | Otherwise name one adapter on the workflow entry. |
| No model from the workflow entry or role | Only in `workflow.task` | Inherit the current task's model; plan and integration roles are invalid without a model. |

The default workflow's explicit `adapter = "codex"` entries pin those steps to
Codex by policy. They are not a restriction of the plan or integration layers.

## Initialization and troubleshooting

Initialize a project with its real complete verifier, then review the generated
script:

```text
assent init --test unittest
assent doctor
```

When an existing `assent.toml` or `adapter.toml` differs from its template, init
asks about that file separately and defaults to preserving it. For a verifier,
only content outside the marked project-test command block is framework; commands
inside the block are project-owned and ignored by this comparison. Missing or
invalid markers count as a framework difference. Replacement creates a
byte-exact `FILE.bak` sibling first, replacing that one backup on a later
update; existing numbered backups remain untouched. Replacing a project
settings override removes it so the shared settings apply. Replacing a verifier
without `--test CHOICE` opens the 0-9 test menu. Init validates the resulting
effective settings before writing anything.

Task files should use narrower focused commands. If configuration fails, the
diagnostic names the invalid key and source file. Also confirm that Git is
available, the selected AI CLI is logged in, and its model mappings name models
that CLI accepts.

See [Workflow](WORKFLOW.md) for the human process, [Commands](COMMANDS.md) for
CLI use, and [Operations](OPERATIONS.md) for runtime recovery.
