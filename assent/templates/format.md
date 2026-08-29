# Assent plan format

> `~/.assent/format.md` defines the plan folder and its task and journal
> files. Read it before creating, reviewing, or changing a plan.
> `workflow.md` separately owns scheduler, CLI, report, receipt, and
> acceptance behavior. `assent check` passing is the mechanical format gate.

## Locations

`assent init` installs shared contracts and settings under `~/.assent/`:

```text
~/.assent/
├── assent.toml
├── instructions.md
├── format.md
└── workflow.md
```

Project-specific plans live only in the project's ignored `.assent/`:

```text
project/
├── AGENTS.md
└── .assent/
    ├── verify.py
    └── <plan>/
        ├── _plan_deps.toml              # optional dependency declaration
        ├── _runtime_test.toml           # required runtime-test decision
        ├── t001_descriptive.e.toml      # task file
        └── t001_descriptive.r.toml      # append-only journal
```

`AGENTS.md` owns project rules. `.assent/verify.py` is the complete verifier and
never appears in a task's focused `verify` command. A worktree contains no
`.assent/`; the scheduler supplies absolute management-file paths when needed.
Any `.assent/` file found in Git is refused as a second source of truth.

## Plan folders and dependencies

A plan is a folder holding at least one `tNNN_name.e.toml`. Its name is also a
Git branch prefix and must be a portable Windows/Git-ref component: nonempty;
no whitespace, control character, slash, backslash, `~^:?*[<>"|`, `..`, or
`@{`; no leading `-` or `.`; no trailing `.` or `.lock`; and no Windows device
name such as `CON`, `PRN`, `AUX`, `NUL`, `COM1`, or `LPT1`.

Task numbers are append-only and never renumbered. Add follow-up work to the
same live, unaccepted plan when it still serves the same objective. Use a new
plan for a distinct objective, accepted/archived/rejected work, or a separate
source lineage.

A new name must not reuse an archived one: read `.assent/_archived.toml`
before choosing it. An archived name still owns its `_archive/<plan>.zip`,
so reusing it makes the roster describe a plan folder that is not the one on
disk. `assent check` refuses the collision.

Optional `_plan_deps.toml` declares direct scheduling dependencies and at
most one source base:

```toml
after = ["bootstrap01", "docs01"]
base = "bootstrap01"
```

- `after` means “must finish before this plan.” It supplies order only.
- If `_plan_deps.toml` exists, `after` is required; write `after = []` when
  empty.
- `base` means “start from this one unaccepted upstream commit.” It must also
  appear in `after`. Without `base`, start from the current integration target.
- Never infer `base` from `after`. Multiple `after` entries do not form a stack.
- When using `base`, state in task `behavior` or `notes` which inherited files
  or symbols the downstream work requires.
- Every referenced plan must resolve to a live plan folder or the archive
  roster. Cycles and contradictory live-plus-archived identities are invalid.

A plan is complete exactly when every formal task is `DONE` or `SKIP`.
Scheduler ordering, selection, verification, acceptance, rework, rejection,
archive, and cleanup are defined in `~/.assent/workflow.md`.

### Runtime-test contract: `_runtime_test.toml`

Every live plan contains exactly one `_runtime_test.toml` plan-level formal
file. It states the plan's runtime-test decision; omitting the file is not a
decision. The file has no other fields or compatibility reader:

```toml
execution = "disabled"
```

```toml
execution = "explicit"
command = "python -m unittest tests.test_runtime"
```

```toml
execution = "after_plan"
command = [
  "python -m unittest tests.test_runtime",
  "python tools/runtime_probe.py",
]
```

`execution` is required and is exactly one of `disabled`, `explicit`, or
`after_plan`. `disabled` forbids `command`. `explicit` and `after_plan`
require `command` as either one non-empty string or a non-empty array of
non-empty strings. Array order is execution order. There is no fallback, alias,
migration, or omitted-file meaning for any mode. A planning session creates this file
from the start, including when it selects `disabled`. The runtime workflow and
the timing of `after_plan` belong to `workflow.md`.

## Task file: `tNNN_name.e.toml`

Filename = `t` + three digits + `_` + a nonempty descriptive name + `.e.toml`.
The descriptive `name` segment has no canonical-language requirement and
preserves the human-requested language, including Unicode. Task identity and
dependency references use only the filename prefix `tNNN`; the paired
`.r.toml` journal keeps the same descriptive segment. Files execute in
lexicographic filename order.

Only `.e.toml` is active. A legacy `tNNN_name.toml` in a live plan makes
`check` and `run` refuse rather than ignore or move it.

Use this 10-field skeleton; no other field is allowed:

```toml
title = "Skeleton and test infrastructure"
deps = []                        # upstream task ids; always write the array
model = "core"                   # prime | core | lite -- nothing else
workflow = [{ role = "implementer" }]  # optional task-local override
status = "TODO"                  # TODO | WIP | DONE | BLOCKED | SKIP
verify = "python -m unittest tests.test_thing"

goal = """
The result to achieve, in one or two sentences.
"""

behavior = """
1. Concrete required behavior, item by item.
"""                              # optional

acceptance = """
- Verifiable completion conditions, item by item.
"""

notes = """
Known facts, file/symbol references, dependencies, and risks.
"""                              # optional
```

Required fields are `title`, `deps`, `model`, `status`, `verify`,
`goal`, and `acceptance`. `workflow`, `behavior`, and `notes` are
optional. Put structural fields before multiline prose; `status` must precede
all multiline strings so the scheduler can replace exactly that line.

### Field rules

- `deps` lists earlier task ids in this plan. Write `[]` when empty.
- `verify` is the narrow deterministic focused gate for this task. Use the
  smallest module, class, case, or command that covers its behavior. Keep the
  smallest representative integration test when filesystem, Git, or process
  behavior matters. Design the command to leave no non-ignored worktree change;
  a scheduler-owned focused action treats exit 0 that creates or modifies one
  as stale evidence, not a pass. Account for predictable caches and generated
  output in the starting Git snapshot's ignore rules or direct them outside the
  project. Every other exit fails. Never
  name `.assent/verify.py` or the full suite.
- `workflow` is an ordered task-local override. Each item contains exactly one
  `{ role = "..." }` or `{ action = "focused_test" }`. Roles come from effective
  settings. Omission inherits the required effective `[workflow].task` array.
  An empty array is invalid; use an explicit action-only array to request only
  mechanical verification. Exact workflow semantics are in `workflow.md`.
- A task must be executable by a fresh AI from project `AGENTS.md`,
  `instructions.md`, and this task file alone. A role prompt adds responsibility
  at runtime but does not replace missing requirements.

### Model

A task states difficulty once, as a portable tier:

| `model` | Use |
| --- | --- |
| `prime` | architecture, cross-module contracts, hardest reasoning |
| `core` | ordinary implementation and debugging |
| `lite` | mechanical edits, boilerplate, documentation sync |

Effort is not a separate task field. Each adapter maps a tier to one complete
`"<model>/<effort>"` invocation in `[adapter.<name>.models]`, so the same tier
already carries the reasoning investment that adapter should spend on it. If a
tier is being reached for too often "but harder", the tier itself is configured
too low; change that one line rather than annotating tasks.

`prime`, `core`, and `lite` are the only values a task file accepts. Anything
else is refused while the plan is read, with the valid words in the message.

A task file therefore cannot name a vendor model at all. That is deliberate: a
vendor model id names one release, while a task file is a plan artifact that is
archived, replayed, and read back long after that release is gone. A step that
genuinely needs a model outside its adapter's three tiers states it in
`assent.toml`, on the role or workflow entry that is already bound to one
adapter.

### Planning audit

Before closeout, audit every `goal`, `behavior`, and `acceptance` clause item by
item:

1. Locate relevant implementation, focused-test, and contract files. Inspect
   the repository when they are not already known; paths mentioned in prose are
   context, not a write boundary.
2. Audit every `verify` command against the clean-worktree rule above,
   including predictable cache, coverage, compiler, and generator output.
3. Name known files and symbols in `behavior` or `notes`; do not make the
   executing AI rediscover facts the meeting already knows.
4. Make each acceptance item decidable without another human question.
5. Default ordinary work to `core` or `lite`; reserve `prime` for the cases in
   the table above.

### Media

Images, PDFs, audio, and video are ordinary project context, not schema fields.
Name an existing input and its purpose in `behavior` or `notes`; include it in
the task requirements when the result must create or modify it. Prefer versioned inputs.
`verify` covers machine-checkable facts such as existence, dimensions, and
format; perceptual judgment remains with the human acceptance decision.

### Status

| Status | Meaning |
| --- | --- |
| `TODO` | not started or explicitly reopened |
| `WIP` | progress exists and should resume; not completion evidence |
| `DONE` | the scheduler's focused gate passed; human acceptance is still required |
| `BLOCKED` | the task workflow could not settle the task; evidence is retained |
| `SKIP` | intentionally omitted from this plan run |

AI sessions never edit `.e.toml` task files or Git state. The scheduler alone
changes status after a workflow action or a human command.

## Journal file: `tNNN_name.r.toml`

The paired journal is append-only and read only when evidence is needed. Create
it if absent; never edit an existing entry.

```toml
[[entry]]
time = "2026-07-17T02:03:04+00:00"
by = "codex"                    # claude | codex | antigravity | scheduler
requested_model = "gpt-5.6-terra"
requested_effort = "high"
event = "done"
summary = "Focused verification passed."
detail = '''Optional bounded process notes or evidence.'''
```

An AI closeout uses its prompt-specified `by` and the actual model and effort
arguments passed for that invocation. Omit `requested_effort` when the selection
omitted the effort and no effort argument was passed.
`summary` is one verifiable sentence. The service may ultimately report a
different model; the journal records what Assent requested. Old `by = "ai"`
entries and older entries missing newer fields remain readable and are not
migrated. Scheduler entries may additionally name `agent` and event-specific
fields.

The exact event lifecycle, generated `_report.md`, and verification receipts
belong to `workflow.md`.

## Cold-start gate

A fresh AI given only project `AGENTS.md`, `instructions.md`, and one `TODO`
task file must be able to state the goal, acceptance conditions, and next action
without asking a question. If not, the plan is incomplete.
`assent check` is the final mechanical gate for the planning meeting.
