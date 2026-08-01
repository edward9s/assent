# Configuration

*[README](../README.md) · [Traditional Chinese reader guide](zh-TW/CONFIGURATION.md)*

This is the English canonical guide to initialization, settings precedence,
adapters, model tiers, effort, and adapter troubleshooting. See the
[Traditional Chinese translation](zh-TW/CONFIGURATION.md),
[COMMANDS](COMMANDS.md) for command syntax, [VERIFICATION](VERIFICATION.md) for
receipt settings, and [OPERATIONS](OPERATIONS.md) for runtime boundaries.

## Requirements and file locations

Assent supports Python 3.11+ and requires Git. It is standard-library-only.
The configured AI adapter must have its CLI installed and authenticated before
an unattended run. The published installation is:

```text
python -m pip install assent
```

For a source checkout, the editable development installation is:

```text
python -m pip install -e .
```

Uninstalling the distribution removes the Python package and the `assent` CLI
entry point, but never deletes `~/.assent`, project `.assent/` directories,
worktrees, archives, or Git branches. Human-selected data cleanup remains
separate.

Assent's own shared files live once per machine:

```text
~/.assent/
├── assent.toml       # shared settings
├── instructions.md   # session rules contract
└── format.md         # task-format contract

<project>/
├── AGENTS.md         # project rules and the Assent bridge line
└── .assent/          # ignored, main worktree only
    ├── verify.py
    ├── assent.toml   # optional project override
    └── <work folder>/
```

The project does not receive copies of `instructions.md` or `format.md`.
`assent init` installs and refreshes them in the user home. The project keeps
its own `AGENTS.md`, verifier, task folders, reports, logs, receipts, archive,
and optional project override.

## Settings precedence

Lowest priority first:

1. Assent built-in defaults.
2. User settings in `~/.assent/assent.toml`.
3. Optional project override in `.assent/assent.toml`.
4. An explicit CLI selection where supported, such as `--config PATH`,
   `--jobs`, or another command-specific option.

Tables merge by key; scalars and arrays replace as a whole. A project override
shadows shared settings only for keys it states, is never migrated into the
user home, and is preserved byte-for-byte. `--config PATH` chooses which
project-level file plays the override role and locates the project from its
`.assent` parent; it is not a current-folder pointer.

Omitting a key is the only way to inherit:

- `key =` is invalid TOML, not an empty value.
- An empty table contributes no leaf overrides.
- An empty array is an explicit replacement where the field allows it.
- An empty or whitespace-only string is refused for settings that need useful
  text, such as a command, adapter name, or effort value.

Invalid TOML and invalid values fail before managed files are written.

## Initialization

From a Git project root, a fresh initialization installs the user-home
contracts and settings, creates `.assent/verify.py`, keeps `.assent/` ignored,
and refreshes the `AGENTS.md` bridge line. It asks for exactly one real project
verification choice: parallel unittest, pytest, npm test, Flutter test, or a
custom argv command. Scripts can supply it directly, for example:

```text
assent init --test unittest
assent init --test "custom:python -m unittest"
```

The generated verifier activates the selected command rather than leaving an
empty skeleton that could report success without testing the project.

On repeat init, Assent preserves an existing project verifier and refuses a new
`--test` choice, refreshes `~/.assent/instructions.md` and
`~/.assent/format.md` from the packaged text, and adds only missing active
settings keys. An existing `.assent/assent.toml` remains a reported override.
Reads, parses, and merges finish before the first write, so an invalid request
does not leave a partial upgrade. A project copy of a shared contract is
removed only when it exactly matches the packaged text; a differing copy is
kept and reported for human migration.

Before any AI session, Assent fails closed unless both user-home contracts are
present, readable, and byte-identical to the packaged contracts. It names a
missing or stale path and points to `assent init`; it never silently patches a
contract mid-run. Universal-newline comparison allows an editor's CRLF rewrite.

## Adapter selection

Adapters translate portable task settings into vendor CLI arguments. A task
uses the abstract model tier `prime`, `core`, or `lite`, and may state the
abstract effort `heavy`, `normal`, or `slight`. Adapter mappings, not adapter
code, own vendor-specific model names and effort values. An explicit effort is
never silently ignored or shifted.

### Claude

```toml
[adapter]
name = "claude"

[adapter.claude]
command = "claude"
extra_args = ["--permission-mode", "bypassPermissions"]

[adapter.claude.models]
prime = "fable"
core = "opus"
lite = "sonnet"
```

### Codex

```toml
[adapter]
name = "codex"

[adapter.codex]
command = "codex"
extra_args = ["--sandbox", "danger-full-access"]

[adapter.codex.models]
prime = "gpt-5.6-sol"
core = "gpt-5.6-terra"
lite = "gpt-5.6-luna"
```

### Antigravity

The Antigravity adapter uses the locally installed `agy` CLI and Gemini. It
requires one interactive login per machine, then uses print mode headlessly.
It validates model/effort combinations before opening a session.

```toml
[adapter]
name = "antigravity"

[adapter.antigravity]
command = "agy"
extra_args = ["--dangerously-skip-permissions"]

[adapter.antigravity.models]
prime = "gemini-3.1-pro"
core = "gemini-3.6-flash"
lite = "gemini-3.5-flash"

[adapter.antigravity.default_effort]
prime = "heavy"
core = "heavy"
lite = "heavy"

[adapter.antigravity.efforts.prime]
normal = "high"

[adapter.antigravity.efforts.lite]
heavy = "medium"
```

First-time Antigravity setup:

1. Install `agy` using [Google's official CLI installation and authentication
   documentation](https://antigravity.google/docs/cli/install).
2. Start the interactive CLI with `agy` and complete browser sign-in. If it
   prints an authorization URL, open it and finish that flow.
3. Confirm `agy --version` is at least 1.1.5 and inspect `agy models` before an
   unattended run.

Assent uses credentials already held by AGY. It does not open a login browser,
read or modify credentials, switch accounts, or change workspace trust. To
sign out, start interactive `agy` and enter `/logout`; it is not a shell
subcommand.

## Model and effort resolution

Model and effort are orthogonal. Effort is selected deterministically:

1. The task file's explicit `effort`.
2. A configured per-tier `default_effort` override.
3. The built-in per-tier default.

A partial `default_effort` table overrides only the tiers it states; absent,
empty, or omitted tiers retain built-in values. Every supported invocation
passes a concrete requested effort to the adapter.

Effort translation resolves each abstract key from:

1. `[adapter.<name>.efforts.<tier>]`;
2. `[adapter.<name>.efforts]`; then
3. the built-in baseline: `heavy -> high`, `normal -> medium`, `slight -> low`.

Each key falls back independently. Abstract values are not sent to a vendor
CLI when a translation is required. For example, if a newer Gemini model
supports `medium`, a project may override the quality-first mapping:

```toml
[adapter.antigravity.efforts.prime]
normal = "medium"
```

The effective matrix is:

| Effort | Claude prime/core/lite | Codex prime/core/lite | Antigravity prime/core/lite |
| --- | --- | --- | --- |
| slight | `low` / `low` / `low` | `low` / `low` / `low` | `low` / `low` / `low` |
| normal | `medium` / `medium` / `medium` | `medium` / `medium` / `medium` | `high` / `medium` / `medium` |
| heavy | `high` / `high` / `high` | `high` / `high` / `high` | `high` / `high` / `medium` |

Antigravity's prime Gemini 3.1 Pro lacks `medium`, so normal maps visibly to
`high`. Lite Gemini 3.5 Flash lacks `high`, so heavy maps to `medium`, its
family ceiling. Antigravity 1.1.5+ is required for `--effort`, stable model
slugs, and unattended fixes; earlier versions fail preflight.

The session line records all four audit facts in one line:

```text
Session: codex | core->gpt-5.6-terra | heavy->high
```

The left side is the task's portable value and the right side is the actual
CLI argument.

## Antigravity timeout and troubleshooting

`print_timeout_minutes` limits one AGY print invocation; the Assent watchdog
limits silence from the session. They are independent:

```toml
[adapter.antigravity]
print_timeout_minutes = 120
```

The value must be positive and should exceed the longest expected task.

For `preflight failed: invalid model selection`, inspect `agy models`, test the
model/effort combination with `agy --print --model <MODEL> ...`, and correct
the model table or tier-specific effort mapping. For authentication errors,
run interactive `agy` and finish sign-in before unattended work. For
`command not found: agy`, install it and verify `agy --version`.

Quota exhaustion records a WIP checkpoint. With one adapter, the scheduler
waits for an exact reset time when available or the configured quota poll; with
an adapter list, it rotates immediately to the next configured adapter and
waits only after all are exhausted. Resume with `assent run <FOLDER>`.

An adapter can request immediate continuation only by ending a finished,
non-stalled, nonzero session with the exact non-empty line:

```text
{"type":"assent.checkpoint_resume"}
```

Assent hides that terminal control line from live human output, preserves raw
diagnostics, creates WIP, and reruns the same adapter with a continue prompt.
The record has no account, quota, reset, or capability-probe meaning.
A wrapper may replace a provider quota result with it only after arranging an
immediate continuation; if it forwards provider quota, Assent performs the
normal wait or rotation. When quota evidence and this record are both present,
the ordinary quota path wins.

## Media and custom adapters

Images, PDFs, audio, and other media are ordinary project context. The task
schema does not gain `inputs`, image, audio, video, or attachment fields. Name
an existing media file and its purpose in `behavior` or `notes`; list media in
`scope` only when the task may create or modify it. Keep reproducible media in
the worktree, not generated `.assent/`, and leave perceptual judgment to the
human `accept` decision. `verify` remains machine-checkable.

To add an AI CLI, subclass `Adapter` and implement its existing two-step
interface: `resolve_model(model: str) -> str` maps the abstract tier to the
actual `requested_model`, and `run_task(prompt, requested_model,
requested_effort, cwd) -> TaskResult` receives the already translated values.
`TaskResult` carries `exit_code`, `output`, `quota_exhausted`, `reset_at`, and
the distinct checkpoint-resume outcome. Vendor detection stays inside the
adapter; the scheduler does not acquire vendor-specific semantics.

## Related settings

The `[verification] receipt_refresh` setting controls whether complete
folder-level evidence is refreshed automatically at folder closeout (`"auto"`)
or waits for an explicit `assent verify` (`"manual"`, the default). It does
not change the invocation-level `run --verify` request or acceptance rules.
See [Verification](VERIFICATION.md) for the full evidence contract.
