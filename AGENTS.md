# Project instructions

## Project

assent — an AI plan format plus an automatic scheduler. Pure Python 3.11+
(standard library only, tomllib), Windows-first and cross-platform. CLI
subcommands: run / status / check / report / clean / reject / rework / init.
Source lives in `assent/`, tests in `tests/` (unittest, not pytest).

## Permanent constraints

- Standard library only; introduce no third-party dependencies.
- Windows compatibility comes first: use pathlib for paths, force utf-8 output,
  lock with msvcrt (fcntl on POSIX).
- Test command: `python -m unittest discover -s tests`; every change must keep
  the whole suite passing.
- Language policy (English is canonical, Traditional Chinese is a reader
  translation):
  - English is canonical for identifiers and public APIs, tracked
    project/technical documents, `AGENTS.md`, packaged templates, prompts,
    configuration comments, source and test comments and docstrings, CLI help,
    diagnostics, status/report headings, and scheduler-generated log text.
  - Traditional Chinese (Taiwan usage) reader documentation lives only in
    `README.zh-TW.md` and `docs/zh-TW/`; those pages are translations and
    identify English as canonical.
  - User-authored task titles, notes and reasons, existing task and history
    logs, upstream CLI raw output, and intentional Unicode or external-protocol
    fixtures stay verbatim and are not translated as data.
  - A scheduler-generated checkpoint subject (`auto(...)`, `wip(...)`) embeds the
    task title verbatim, so it is both generated text and user data. The verbatim
    rule wins: assent never transliterates or translates a user's title on its way
    into a commit. It follows that a project keeps the commit language it writes
    its titles in — so in assent's own `.assent/` plan folders, write task titles
    in English, and this repository's own history stays canonical English without
    the tool having to rewrite anyone's words.
  - Do not place English and Chinese canonical contracts side by side in a
    generated `.assent/`; there is exactly one executable English contract.
- Comments must not rely on internal codes only the author understands in the
  moment (such as session labels `W1`/`W5`); use self-describing "date + what
  was done" statements so that the author six months later, and future readers,
  need not go digging. State the conclusion for finished work; do not leave
  dangling notes like "correct this in some later phase" that point at vanished
  context.
- Token-burned output is never discarded: no process change may introduce
  "revert the workspace on failure" behavior.
- The fail-closed scope check is a safety floor; its meaning must not be
  relaxed.
- git is always required; no disable switch or git-less degraded mode may be
  introduced.
- Do not introduce a hand-maintained "current folder" pointer; the work folder
  is stated explicitly by argument or derived from task-file facts, and any
  ambiguity is refused.
- `build/lib/` is an old build artifact; never modify it.
- `model` and `effort` are orthogonal abstract tiers: `model` uses
  prime/core/lite; the optional `effort` uses low/medium/high and is written
  explicitly only when a task must deviate from the model default. `high` means
  a portable high reasoning investment, not a vendor's native maximum tier; an
  adapter must not silently ignore or up/down-shift an effort a task states
  explicitly. Vendor-specific effort values belong to configuration mappings
  (peers of the models table) and must not be hardcoded in adapter code.
- When using assent, first read `.assent/instructions.md` in the project's main worktree; a worktree session uses the absolute path the scheduler provides. <!-- assent-instructions -->
