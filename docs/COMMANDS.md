# Commands

*[README](../README.md) · [Traditional Chinese reader guide](zh-TW/COMMANDS.md)*

This is the English canonical command and selection reference. The
[Traditional Chinese translation](zh-TW/COMMANDS.md) follows the same topic
boundaries. See [WORKFLOW](WORKFLOW.md) for the lifecycle,
[CONFIGURATION](CONFIGURATION.md) for settings, [VERIFICATION](VERIFICATION.md)
for receipts, and [OPERATIONS](OPERATIONS.md) for cleanup and recovery.

## Common syntax

The common form is:

```text
assent <command> [options] [FOLDER ...]
```

`run`, `status`, `check`, `report`, `verify`, `clean`, `archive`, `accept`,
`reconcile`, `reject`, and `rework` accept `--config PATH`; it selects the
optional project-level config file (default `.assent/assent.toml`) and also
locates the project from that path. It is a per-subcommand option, not a
top-level global option. `--config` and a folder argument are orthogonal.
`init`, `doctor`, and `shared-paths` have their own project-location contracts.

When no folder is named, `run` derives one unambiguous runnable folder from
task state and `_folder.toml`'s `after` prerequisites; ambiguity is refused.
`status`, `check`, and `report` cover all folders when their folder is omitted.
Other command-specific discovery rules are listed below.

Folder names are portable Windows/Git-ref names. They must be non-empty and
contain no whitespace, path separators, control characters, Git-ref-forbidden
characters (`~`, `^`, `:`, `?`, `*`, `[`), or Windows-forbidden characters
(`<`, `>`, `"`, `|`). They cannot start with `-` or `.`, contain `..` or `@{`,
end with `.` or `.lock`, or be a reserved Windows device name. Validation runs
before a worktree or branch is created.

## Selection audit and `...`

Every explicitly named live folder is audited before dispatch. Each stated
name, including a prefix before `...`, must resolve to an existing `.assent/`
directory containing a formal `tNNN_name.e.toml` task file. If any name is
unresolved, Assent reports the complete unresolved set and returns nonzero
before any selected folder runs, verifies, publishes, cleans, or archives. It
does not create a missing folder, lock, or log. This audit does not replace a
command's readiness, lock, receipt, or Git eligibility checks.

The literal ASCII token `...`, exactly once as the last positional argument,
is a remainder selector shared by `run`, `verify`, `accept`, `clean`, and
`archive`:

```text
assent run A B ...
assent verify A ...
assent accept A ...
assent clean A ...
assent archive A ...
```

It means “append every remaining folder this command would discover.” The
expansion is snapshotted before mutation and is not an alias for `--all`.
Combining it with `--all`, repeating it, or placing it anywhere but last is a
usage error. `verify` and `accept` add only finished folders; `run`, `clean`,
and `archive` add every work folder and decide eligibility afterward. The
remainder follows the explicit prefix. `run` preserves prefix order and
dependency-orders the remainder; `verify` and `accept` normalize the entire
selection to dependency order; `clean` uses upstream-first order.

The expanded selection is printed before work begins; selecting no folder is a
refusal. `...` selects folders but does not switch command modes, so bare
`assent run ...` uses the ordinary exact selection path, not the `--all`
scheduler. `--jobs` remains an `--all` option. Cardinality chooses the path:
one folder is the folder-receipt/direct-accept/one-folder-archive path, while
two or more is an exact selected batch. Thus `assent verify A ...` writes one
receipt for exactly the expanded set and `assent accept A ...` requires that
exact fresh evidence without verifying.

The remainder selector is rejected with `verify --batch`, `verify --focus`,
`run --once`, `run --task`, and `archive --restore`.

## `run --verify`

`--verify` chains complete verification only after a zero exit from the run. A
nonzero run is returned as-is and verifies nothing. The verification's exit
code becomes the command's exit code.

| Invocation | Complete verification scope |
| --- | --- |
| `assent run --verify` | The automatically selected folder's receipt. |
| `assent run A --verify` | A's folder receipt. |
| `assent run A B --verify` | A and B as one exact selected batch. |
| `assent run A ... --verify` | Exactly the explicit-prefix-plus-remainder selection. |
| `assent run --all --verify` | The whole-project dynamic batch. |
| `assent run ... --verify` | The whole-project dynamic batch. |
| `assent run A --once --verify` | A's folder receipt only if the limited run left every task complete. |
| `assent run A --task t003 --verify` | The same single-folder condition. |

With `--once` or `--task`, an incomplete folder fails the request before any
candidate or full verifier exists and writes no receipt; the refusal names the
incomplete task IDs and statuses. This invocation-level request ignores the
configured receipt-refresh policy.

## Command reference

| Command | Effect and important boundaries | Token cost |
| --- | --- | --- |
| `assent run [FOLDER]` | Runs a folder until tasks are `DONE`, `BLOCKED`, or `SKIP`. `--once` stops after the next task; `--task ID` runs one task after checking upstreams. | AI sessions only |
| `assent run A B` | Runs exactly A then B in written order and stops on the first failure. It does not verify or accept implicitly. | AI sessions only |
| `assent run A B --all` | Runs the explicit prefix, then remaining incomplete folders in dependency order. | AI sessions only |
| `assent run --all [--jobs N]` | Runs every incomplete folder with the dependency scheduler; `--jobs` caps concurrent folders. | AI sessions only |
| `assent status [FOLDER]` | Shows progress, next task, branch, and last checkpoint. | Zero |
| `assent check [FOLDER]` | Validates task format, dependency cycles, configuration, and environment; it is the planning adjournment gate. | Zero |
| `assent report [FOLDER]` | Generates and displays `_report.md`. | Zero |
| `assent verify <FOLDER>` | Builds one temporary integration candidate, runs the complete verifier once, and refreshes a folder receipt without changing the target or opening AI. | Zero |
| `assent verify A B` | Verifies exactly A and B in dependency order as one selected batch and writes one batch receipt. A selected conflict refuses. | Zero |
| `assent verify --batch` | Dynamically verifies finished, not-yet-integrated folders as one batch; an interactive conflict can offer one skip decision. | Zero |
| `assent verify <FOLDER> --focus` | Repeats distinct `DONE`-task `verify` commands in the source worktree; it writes no receipt and cannot authorize acceptance. | Zero |
| `assent accept <FOLDER>` | Explicit human approval for one completed folder. It never runs the complete verifier; except for an ancestry no-op, it requires fresh matching `PASSED` evidence and publishes a guarded merge. | Zero |
| `assent accept A B` | Explicit approval for exactly the matching dependency-ordered batch receipt; replays it without verification and publishes all selected folders atomically or none. | Zero |
| `assent accept --all` | Fresh `PASSED` batch evidence is replayed atomically. Missing or expired evidence uses sequential per-folder verify-then-accept; malformed evidence refuses. | Zero plus any sequential verifier |
| `assent reconcile <FOLDER>` | Prepares one finished folder's source-versus-target conflict for human edits in a managed worktree. It changes neither target nor task status and writes no receipt. | Zero |
| `assent reconcile --continue <FOLDER>` | Validates staged conflict resolution, commits the merge, advances the source branch, and removes only proven managed resources. It does not run verification. | Zero |
| `assent reconcile --abort <FOLDER>` | Removes only the proven managed reconcile worktree and temporary branch, refusing while edits remain. | Zero |
| `assent clean [FOLDER ...]` | Removes only fully merged, clean worktrees and same-folder-prefix branches that Assent can prove safe. With no folder it considers all; several are upstream-first. | Zero |
| `assent archive <FOLDER ...>` | Runs the clean contract, compresses eligible plans into `.assent/_archive/`, and updates the roster. Named ineligible folders make the request fail after attempts. | Zero |
| `assent archive --all` | Archives independently eligible folders and skips ineligible ones without failing the whole dynamic request. | Zero |
| `assent archive --restore FOLDER` | Restores exactly one archive; it takes neither `--all` nor `...`. | Zero |
| `assent reject <FOLDER>` | Human-adjudicated destructive rejection: records tips, archives uncommitted changes as WIP, removes the managed worktree, deletes same-prefix branches, and resets `DONE`/`WIP`/`BLOCKED` to `TODO`. | Zero |
| `assent rework <FOLDER> <TASK>` | Non-destructively reopens one task and keeps code by default. `--cascade` is required to reopen started downstream tasks; `--reason` records the decision; `--revert-code` adds only a provable reverse commit. | Zero |
| `assent shared-paths review ...` | The only operation allowed to write the primary worktree's shared ignored-directory review manifest. See [Verification](VERIFICATION.md). | Zero |
| `assent init [--test CHOICE]` | Installs user-home contracts/settings and project `.assent/verify.py`, the `AGENTS.md` bridge line, and `.gitignore`. Fresh init selects exactly one real verifier; repeat init preserves the verifier and merges missing settings. | Zero |
| `assent doctor` | Diagnoses Python, Git, adapter CLIs, and temporary-directory writability without an existing project. | Zero |
| `assent --version` | Prints the installed distribution version without a project or subcommand. | Zero |

Each subcommand's `-h`/`--help` is the authoritative syntax for that command.
Assent does not silently connect to a remote, pull, rebase, force-push, delete
source, or resolve conflicts during acceptance.

## Acceptance modes in brief

Direct `accept <FOLDER>` and selected `accept A B` never start the complete
verifier. A direct folder already contained in the target is an ancestry-proven
idempotent no-op; otherwise it needs a fresh receipt matching source tip,
reconstructed integration tree, and verifier digest. Selected acceptance needs
a fresh `PASSED` batch receipt for exactly its dependency-ordered set. Missing,
malformed, stale, or drifted evidence refuses.

`accept --all` deliberately has two modes. A fresh `PASSED` batch receipt is
replayed and released atomically for exactly its recorded folders, without
new verification. Missing or expired/non-`PASSED` evidence invokes
`verify_folder_if_needed` before each not-already-integrated folder, then
accepts it in dependency order; it stops on the first real failure and keeps
earlier publications. A malformed batch receipt refuses rather than falling
back. Already-integrated folders are ancestry no-ops, and a cleaned source is
skipped only after integration was proven.

## Colored help

On Python 3.14 and later, when standard-library argparse enables color, Assent
re-themes only the `usage:` prefix and section headings. `NO_COLOR`,
`FORCE_COLOR`, `PYTHON_COLORS`, redirection, and unsupported streams still
control whether escapes appear. Python 3.11–3.13 help is plain. Color is never
promised.

## Related guides

- [Workflow](WORKFLOW.md) — plan, run, review, rework, and reject.
- [Configuration](CONFIGURATION.md) — init, settings precedence, adapters,
  models, effort, and troubleshooting.
- [Verification](VERIFICATION.md) — focused/full/batch verification, receipts,
  ignored inputs, reconcile, and acceptance evidence.
- [Operations](OPERATIONS.md) — worktrees, locks, parallelism, recovery,
  cleanup, archive, and safety.
