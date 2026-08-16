# Assent plan format

> `~/.assent/format.md` defines work-folder, task, and journal files. Read it
> before creating, reviewing, or changing a plan. `workflow.md` separately owns
> scheduler, CLI, report, receipt, and acceptance behavior. `assent check`
> passing is the mechanical format gate.

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
        ├── _folder.toml                 # optional plan dependency
        ├── t001_descriptive.e.toml      # task file
        └── t001_descriptive.r.toml      # append-only journal
```

`AGENTS.md` owns project rules. `.assent/verify.py` is the complete verifier and
never appears in a task's focused `verify` command. A worktree contains no
`.assent/`; the scheduler supplies absolute management-file paths when needed.
Any `.assent/` file found in Git is refused as a second source of truth.

## Work folders and dependencies

A work folder contains at least one `tNNN_name.e.toml`. Its name is also a Git
branch prefix and must be a portable Windows/Git-ref component: nonempty; no
whitespace, control character, slash, backslash, `~^:?*[<>"|`, `..`, or `@{`;
no leading `-` or `.`; no trailing `.` or `.lock`; and no Windows device name
such as `CON`, `PRN`, `AUX`, `NUL`, `COM1`, or `LPT1`.

Task numbers are append-only and never renumbered. Add follow-up work to the
same live, unaccepted folder when it still serves the same objective. Use a new
folder for a distinct objective, accepted/archived/rejected work, or a separate
source lineage.

Optional `_folder.toml` declares direct scheduling dependencies and at most one
source base:

```toml
after = ["bootstrap01", "docs01"]
base = "bootstrap01"
```

- `after` means “must finish before this folder.” It supplies order only.
- If `_folder.toml` exists, `after` is required; write `after = []` when empty.
- `base` means “start from this one unaccepted upstream commit.” It must also
  appear in `after`. Without `base`, start from the current integration target.
- Never infer `base` from `after`. Multiple `after` entries do not form a stack.
- When using `base`, state in task `behavior` or `notes` which inherited files
  or symbols the downstream work requires.
- Every referenced folder must resolve to a live work folder or the archive
  roster. Cycles and contradictory live-plus-archived identities are invalid.

A folder is complete exactly when every formal task is `DONE` or `SKIP`.
Scheduler ordering, selection, verification, acceptance, rework, rejection,
archive, and cleanup are defined in `~/.assent/workflow.md`.

## Task file: `tNNN_name.e.toml`

Filename = `t` + three digits + `_` + a nonempty descriptive name + `.e.toml`.
The descriptive `name` segment has no canonical-language requirement and
preserves the human-requested language, including Unicode. Task identity and
dependency references use only the filename prefix `tNNN`; the paired
`.r.toml` journal keeps the same descriptive segment. Files execute in
lexicographic filename order.

Only `.e.toml` is active. A legacy `tNNN_name.toml` in a live folder makes
`check` and `run` refuse rather than ignore or move it.

Use this 12-field skeleton; no other field is allowed:

```toml
title = "Skeleton and test infrastructure"
deps = []                        # upstream task ids; always write the array
model = "core"                   # prime | core | lite, or [exact model]
effort = "normal"                # optional portable value or [exact effort]
workflow = [{ role = "implementer" }]  # optional task-local override
status = "TODO"                  # TODO | WIP | DONE | BLOCKED | SKIP
scope = ["src/thing.py", "tests/test_thing.py"]
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

Required fields are `title`, `deps`, `model`, `status`, `scope`, `verify`,
`goal`, and `acceptance`. `effort`, `workflow`, `behavior`, and `notes` are
optional. Put structural fields before multiline prose; `status` must precede
all multiline strings so the scheduler can replace exactly that line.

### Field rules

- `deps` lists earlier task ids in this folder. Write `[]` when empty.
- `scope` is a nonempty list of project-relative path prefixes the task may
  change. It is fail-closed. The task's own status line and paired journal are
  scheduler exceptions and need not appear.
- `verify` is the narrow deterministic focused gate for this task. Use the
  smallest module, class, case, or command that covers its behavior. Keep the
  smallest representative integration test when filesystem, Git, or process
  behavior matters. Design the command to leave no non-ignored worktree change;
  a scheduler-owned focused action treats exit 0 that creates or modifies one
  as stale evidence, not a pass. Account for predictable caches and generated
  output in the starting Git snapshot's ignore rules or direct them outside the
  project rather than adding them to task scope. Every other exit fails. Never
  name `.assent/verify.py` or the full suite.
- `workflow` is an ordered task-local override. Each item contains exactly one
  `{ role = "..." }` or `{ action = "focused_test" }`. Roles come from effective
  settings. Omission inherits `[workflow].task`; omission at both levels gives
  one implicit task session. `workflow = []` disables per-task sessions for
  this task and assigns it to plan-wide execution. Exact workflow semantics are
  in `workflow.md`.
- A task must be executable by a fresh AI from project `AGENTS.md`,
  `instructions.md`, and this task file alone. A role prompt adds responsibility
  at runtime but does not replace missing requirements.

### Model and effort

Task files ordinarily use portable tiers:

| `model` | Use |
| --- | --- |
| `prime` | architecture, cross-module contracts, hardest reasoning |
| `core` | ordinary implementation and debugging |
| `lite` | mechanical edits, boilerplate, documentation sync |

`effort` is optional and orthogonal: `heavy`, `normal`, or `slight`. State it
only to override the configured default. `heavy` means high portable reasoning,
not a vendor's maximum.

An exact bracketed value deliberately bypasses its adapter mapping:

```toml
model = "[gpt-5.6-sol]"
effort = "[xhigh]"
```

Model and effort literals are independent. Their brackets are removed and the
case-preserved contents are passed directly. Any effective workflow step that
uses either literal must resolve to exactly one adapter. A literal model with
omitted effort passes no effort and uses the vendor default. A portable effort
beside a literal model uses the adapter's flat effort translation rather than a
tier-specific translation. An unbracketed value outside the portable vocabulary
is an error.

### Planning audit

Before closeout, audit every `goal`, `behavior`, and `acceptance` clause item by
item:

1. Locate the owning implementation, focused-test, and contract files. Inspect
   the repository when ownership is not already known; paths mentioned in prose
   are not a completeness proof.
2. Classify each located file as read-only context or a possible write.
3. Cover every possible write with an exact `scope` entry. Read-only context
   does not belong in scope.
4. Audit every `verify` command against the clean-worktree rule above,
   including predictable cache, coverage, compiler, and generator output.
5. Name known files and symbols in `behavior` or `notes`; do not make the
   executing AI rediscover facts the meeting already knows.
6. Make each acceptance item decidable without another human question.
7. Default ordinary work to `core` or `lite`; reserve `prime` for the cases in
   the table above.

### Media

Images, PDFs, audio, and video are ordinary project context, not schema fields.
Name an existing input and its purpose in `behavior` or `notes`; include it in
`scope` only when the task may create or modify it. Prefer versioned inputs.
`verify` covers machine-checkable facts such as existence, dimensions, and
format; perceptual judgment remains with the human acceptance decision.

### Status

| Status | Meaning |
| --- | --- |
| `TODO` | not started or explicitly reopened |
| `WIP` | progress exists and should resume; not completion evidence |
| `DONE` | executing AI claims completion; still requires scheduler gates and human acceptance |
| `BLOCKED` | the task workflow could not settle the task; evidence is retained |
| `SKIP` | intentionally omitted from this plan run |

During an ordinary task session, the AI may change only its own `status` line.
The scheduler may additionally append one exact, mechanically validated scope
omission returned by an authorized workflow repair; no AI edits `.e.toml` task
files or Git state directly.

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
arguments passed for that invocation. Omit `requested_effort` when a literal
model deliberately used the vendor default and no effort argument was passed.
`summary` is one verifiable sentence. The service may ultimately report a
different model; the journal records what Assent requested. Old `by = "ai"`
entries and older entries missing newer fields remain readable and are not
migrated. Scheduler entries may additionally name `agent` and event-specific
fields.

The exact event lifecycle, generated `_report.md`, verification receipts, and
derived `_auto_fix.toml` state belong to `workflow.md`.

## Cold-start gate

A fresh AI given only project `AGENTS.md`, `instructions.md`, and one `TODO`
task file must be able to state the goal, writable scope, acceptance conditions,
and next action without asking a question. If not, the plan is incomplete.
`assent check` is the final mechanical gate for the planning meeting.
