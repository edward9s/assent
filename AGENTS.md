# Project instructions

## Project

assent — an AI plan format plus an automatic scheduler. Pure Python 3.11+
(standard library only, tomllib), Windows-first and cross-platform. CLI
subcommands: run / status / check / report / verify / clean / accept /
reconcile / reject / rework / archive / init / doctor.
Source lives in `assent/`, tests in `tests/` (unittest, not pytest).

This file governs development of the assent project itself. Rules followed
while operating an assent-managed session live in
`assent/templates/instructions.md`.

## Permanent constraints

- Unattended completion, human adjudication: the scheduler decides everything
  it can decide without a human, and routes what it cannot decide to the human
  acceptance meeting as `_report.md` evidence. A question the scheduler cannot
  settle is not a run failure. Design such an outcome as exit 0 plus a
  distinctly named report state plus an explicit gate at `accept` — never as a
  nonzero exit, because `run --all` stops launching further folders once any
  folder exits nonzero, so nonzero silently cancels unrelated queued work.
  Reserve nonzero for genuine failure: infrastructure, a refused precondition,
  or a broken gate, where continuing would be unsafe rather than undecided.
- Standard library only; introduce no third-party dependencies.
- Windows compatibility comes first: use pathlib for paths, force utf-8 output,
  lock with msvcrt (fcntl on POSIX).
- Full-project test command: `python .assent/verify.py`; every
  integrated change must keep the whole suite passing. This states the required
  integration outcome; it does not define session-level test execution policy.
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
- Assent cleanup must never pass a directory tree containing a junction,
  directory symlink, or other directory reparse point to Git or a recursive
  remover. It first detaches the link object itself without traversing its
  target; deleting a link object is distinct from deleting anything through
  its resolved path. Assent detaches each directory-link object before any
  recursive Git or filesystem removal and never traverses its resolved target.
  External link targets survive success, refusal, failure, interruption, and
  retry. If inventory, ownership, or detachment cannot be proven, cleanup
  refuses and retains the managed path for an Assent-owned retry.
- The fail-closed scope check is a safety floor; its meaning must not be
  relaxed.
- Every explicitly named live work-folder selection is audited in full before
  dispatch: each stated name, including a prefix before `...`, must resolve to
  an existing `.assent/` directory containing a formal `tNNN_name.e.toml` task
  file. Any unresolved name reports with the complete unresolved set and
  prevents every selected operation from starting; dynamic discovery modes keep
  their own contracts, and archive restore/recovery remains allowed to resume
  with an intentionally absent live directory.
- git is always required; no disable switch or git-less degraded mode may be
  introduced.
- Do not introduce a hand-maintained "current folder" pointer; the work folder
  is stated explicitly by argument or derived from task-file facts, and any
  ambiguity is refused.
- Human approval is the explicit `assent accept FOLDER` action plus the resulting
  Git integration; do not add a second per-task `review` state alongside task
  execution status.
- The configured `task`, `plan`, and `integration` workflow arrays are always
  active. Their scheduler actions are `focused_test`, `focused_sweep` (the
  distinct union of task verify commands without a receipt), and `full_verify`
  (the reconstructed candidate and receipt). A passing action completes that
  layer without an AI reviewer; a failing action advances to the next configured
  reviewer/fixer and then rechecks. A reviewer may return an exact mechanically
  valid scope omission; only the scheduler may mutate task state, task contracts,
  or Git state. Repair may
  reopen only existing implicated tasks. Eligible pre-existing technical debt may
  originate only in the initial completed-folder review, must stay visible for the
  later human acceptance agenda, and may not be introduced by blocked adjudication
  or recheck. Each repair round carries its reason, selects its finite fixer-profile
  assignments against the pre-round history, and persists every assignment before
  its first write-capable session. The finite arrays are the only convergence
  bound; exhaustion terminates automation as `REVIEW UNRESOLVED, HUMAN DECISION`,
  exit zero, with durable findings and edits for a human. Workflow repair never creates
  tasks, reverts or deletes source, accepts a folder, or changes the explicit human
  `accept` boundary.
- The default adapter permissions remain `danger-full-access` where configured:
  the read-only reviewer's prompt plus before/after write detection is a
  cooperative rule, not a security sandbox and not a preventive permission
  boundary.
- The literal ASCII token `...` is remainder syntax shared by every
  folder-taking command (`run`, `verify`, `accept`, `clean`, `archive`): given
  once, as the last positional argument, it means "and every remaining folder
  the command itself would discover". It is not an alias for `--all` and may
  not be combined with it (nor with `verify --batch`/`--focus`, `run --once`/
  `--task`, or `archive --restore`, which takes exactly one folder). The
  expansion is snapshotted once, before anything is mutated, and each command
  keeps its own discovery rule: `verify` and `accept` add only finished folders,
  `run`, `clean` and `archive` add every work folder and decide per folder
  afterwards. The remainder is appended after the explicit prefix, and each
  command then applies its own native ordering: `run` keeps the stated prefix
  order and takes the remainder in folder-dependency order, `verify` and
  `accept` normalize the whole selection to dependency order, and `clean`
  normalizes it upstream-first.
- Selection cardinality is what picks a command's path, not the presence of
  `...`: one folder is the single-folder path (folder receipt, direct accept,
  `archive_folder`), two or more is the exact selected batch. A
  remainder-expanded selection is an ordinary exact selection, so selected
  acceptance still requires evidence for exactly the expanded set and still
  never verifies.
- A successful `run` automatically follows the configured integration workflow
  for the same exact selection until `full_verify` passes or the finite array is
  exhausted. `--once` and `--task` defer integration when they leave the selected
  folder incomplete. No run path accepts; publication remains the later human
  `assent accept` action.
- A multi-folder `archive A B` keeps `archive FOLDER`'s contract, not `--all`'s:
  the human named those folders, so an ineligible one is a refusal that exits
  nonzero after every named folder has been attempted, while `--all` skips an
  ineligible folder without failing.
- argparse help may be colorized by the standard library; only the `usage:`
  prefix and section headings are re-themed, away from Python 3.14's barely
  legible dark blue, and only inside argparse's own `_set_color`, so
  `NO_COLOR`, `FORCE_COLOR`, `PYTHON_COLORS`, and a redirected or unsupported
  stream still decide whether any escape is emitted. Never promise colored
  help, and never emit an escape sequence argparse's own checks turned off.
- Expensive full-project verification belongs to unattended `run` / `verify`,
  not the interactive acceptance decision. Human approval is the explicit
  `assent accept` action plus the resulting Git integration. Direct
  `accept FOLDER` and selected `accept A B` never start verification; unless a
  source is already integrated by ancestry as an idempotent no-op, they require
  a fresh receipt that exactly matches the source, reconstructed integration
  tree, and verification-script digest. `accept --all` intentionally has two
  modes: a fresh PASSED batch receipt is replayed and released atomically
  without new verification, while absent or expired batch evidence uses the
  sequential per-folder `verify_folder_if_needed` step before each
  not-already-integrated accept, stops on the first failure, and preserves
  earlier publications. A malformed batch receipt refuses rather than falling
  back. Exact receipt replay and the human approval boundary remain mandatory.
- Every production folder-level complete verification operation, whether
  `verify_folder` or `verify_folder_if_needed`, refreshes that folder's
  `_report.md` exactly once after the receipt operation settles and all
  verification locks are released. The best-effort refresh observes PASSED,
  FAILED, stale-receipt replacement, fresh-receipt reuse, malformed-receipt
  refusal, incomplete-folder no-op, and interrupt outcomes without changing or
  masking the verification result. Focused verification and selected or
  dynamic batch verification write no folder receipt and therefore do not
  refresh a folder report.
- A verification receipt is a deletable derived artifact, never an independent
  source of truth: source commits, the reconstructed integration tree, and the
  verification-script digest must reproduce it before it can authorize accept.
- Complete verification mirrors exactly two kinds of artifact from the source
  worktrees that enter the candidate, never arbitrary ignored content:
  reviewed-profile ignored directory links provisioned by Assent -- Windows junctions and
  directory symlinks, POSIX directory symlinks -- and ordinary ignored leaf
  files that sit inside an otherwise tracked directory, such as a generated
  `*.g.dart` beside its tracked source. Both may be at the root or nested below
  tracked parents. Discovery uses Git's own ignore walk with whole ignored
  trees collapsed, so ignored directory trees, build output, caches, editor
  state, `.git`, `.assent`, and everything inside a discovered link's target
  are pruned rather than enumerated, as is any file whose parent chain is not
  part of the candidate's tracked tree. A directory is mirrored as a link to
  the same resolved target and a file as a candidate-side link to the source
  file (same-volume hard link on Windows, file symlink on POSIX); nothing is
  copied and no hardlink twin is prepared by hand. Each destination must be
  absent from the candidate and Git-ignored there; a provisioned artifact never
  replaces or shadows tracked content. Several sources contribute their union:
  one path resolving to one directory target, or to a file with one content
  digest, is deduplicated, while conflicting targets, differing file contents,
  a kind mismatch, an ancestor/descendant overlap, a dangling or unsupported
  link, an occupied destination, an unsafe parent path, and a link that cannot
  be created refuse before the verifier runs or any PASSED evidence is written.
  The mirror exists for the verifier run alone; it is removed before the
  temporary worktree is, deepest path first, followed by only the empty parents
  provisioning created, so neither creating nor cleaning a candidate ever
  traverses, modifies, or deletes a linked target, and the source worktree's own
  links, files, and targets survive success, failure, and interruption alike.
  Do not add `--force`, a project `local_inputs` setting, a blanket `.gitignore`
  overlay, or copies of ignored directory contents into Git.
- The ignored-input handoff is documented where each reader actually looks: the
  packaged scheduled-task instructions tell an executing session to record a
  required ignored directory through `assent shared-paths review`, which
  provisions the same-relative junction or directory symlink, and never to copy
  the tree or hand-create a source link; a full verifier that fails on a
  path inside a physically ignored source directory gets one appended
  `Ignored input diagnosis:` note naming that directory and the directory-link
  remedy. The note preserves the verifier output and exit code, is stored in
  whichever receipt records the failure summary, applies to single-folder,
  exact selected, dynamic batch, and localization-prefix verification alike,
  normalizes separators, and reports only a directory the verifier output
  itself names; it never enumerates or traverses an ignored tree.
- Which ignored directories a project shares is a reviewed decision cached in
  the primary worktree's untracked, never-committed `.assent/manifest.toml`;
  it is Assent-owned local execution memory, not project source, and its only
  writer is the validated `assent shared-paths review` operation. Under
  `[shared_paths]` it retains whole profiles by fingerprint -- normalized
  project-relative `paths`, exact tracked `watch` files, and a digest of those
  files plus the tracked Git-ignore rules -- so parallel branches never make the
  cache oscillate. A source snapshot is UNKNOWN, REVIEWED-NONE (a matching
  `paths = []` profile is an answer and must never trigger another review),
  REVIEWED-PATHS (Assent provisions the exact Windows junction or POSIX
  directory symlink to the primary worktree's same relative path itself), or
  STALE; conflicting matching profiles fail closed. One further state,
  NO-IGNORED-DIRECTORY-CANDIDATE, is the deterministic zero-token fast path:
  it means only that a successful Git ignored-entry query of the primary
  worktree found no existing ordinary ignored directory outside `.git/` and
  `.assent/`, never that the project semantically needs no shared input. It is
  settled without a manifest profile, junction, or AI review, contributes a
  receipt-digest identity distinct from REVIEWED-NONE, and is recomputed
  cheaply at every applicable gate. It fails closed: a Git ignored-entry
  discovery error is an actionable refusal and is never turned into an empty
  candidate set, ignored leaf files do not count, any existing ordinary
  ignored directory does count even when a review later answers `paths = []`,
  and a directory appearing later makes the next classification UNKNOWN unless
  a matching cached profile already answers it. Complete-verifier
  `required_evidence` naming a missing directory is never settled as
  NO-IGNORED-DIRECTORY-CANDIDATE: it is classified for review when a valid
  primary target exists and otherwise refuses with the exact missing or
  not-ignored target problem. Candidate enumeration deliberately asks the
  primary worktree, because every allowed link target must be an existing
  ordinary Git-ignored directory at that same primary relative path and a
  fresh source checkout is expected to hold no ignored inputs; a directory or
  ignore rule that exists only on an unaccepted source branch is not yet a
  provisionable primary target and must produce an actionable refusal rather
  than a "none needed" claim. UNKNOWN and STALE add one
  bounded review clause to the next already-scheduled session and refuse its
  closeout until settled; an unchanged fingerprint consumes no review tokens.
  Every verification entry point and `assent reconcile` classify and reconcile
  before any candidate, verifier, or managed worktree exists, and folder and
  batch receipts bind one `shared_inputs_sha256` -- snapshotted immediately
  before and after the full verifier -- that acceptance rechecks before
  publishing a ref without ever repairing a link or invoking AI. Do not add a
  copy fallback, glob, all-ignored mode, force flag, Git staging of the
  manifest, or any claim that semantic necessity can be inferred from
  `.gitignore` alone.
  Every contributing source's ignored directory links must equal its active
  profile and resolve to those exact primary targets. An undeclared manual link
  is unreviewed evidence under every state and refuses verification,
  reconciliation, receipt freshness, reporting, and acceptance; ordinary
  ignored leaf files keep their separate automatic candidate-link behavior.
- Cross-folder speculative execution stacks only on an explicitly declared
  `base`, so at most one not-yet-accepted upstream tip is ever in a stack. A
  folder that declares no `base` is cut from the integration target; the
  scheduler must never infer a base from `after` or otherwise build an implicit
  integration engine.
- Rework preserves existing code by default. A from-scratch rework must be an
  explicit human choice and may reverse only a checkpoint tail whose ownership
  is mechanically provable.
- `build/lib/` is an old build artifact; never modify it.
- `model` and `effort` are orthogonal abstract tiers: `model` uses
  prime/core/lite; the optional `effort` uses heavy/normal/slight and is written
  explicitly only when a task must deviate from the model default. `heavy` means
  a portable high reasoning investment, not a vendor's native maximum tier; an
  adapter must not silently ignore or up/down-shift an effort a task states
  explicitly. Abstract and vendor effort names intentionally differ; when a
  translation is missing, the settings-layer built-in baseline maps heavy ->
  high, normal -> medium, and slight -> low instead of sending the abstract
  name as a CLI value. Vendor-specific effort values belong to configuration
  mappings (peers of the models table) and must not be hardcoded in adapter
  code.
- An adapter command may request an immediate continuation only with the exact,
  provider-neutral `{"type":"assent.checkpoint_resume"}` terminal control
  record. Assent owns the WIP checkpoint and resume lifecycle; the record carries
  no account, pool, quota-capacity, or reset semantics, requires no configuration
  or capability probe, and ordinary vendor quota output keeps the existing
  wait/adapter-rotation behavior.
- Effort selection is deterministic: task explicit value, then the configured
  per-tier `default_effort` override, then the built-in per-tier default. A
  stated `default_effort` table overrides per tier rather than replacing the
  built-in one, so an absent, empty, or partial table still leaves every known
  tier with a value. Every supported invocation therefore passes a concrete
  requested effort; no code path may reintroduce "pass no effort and inherit
  the vendor CLI default".
- Media inputs (image, PDF, audio, and the like) are ordinary project context,
  not a schema feature. The fixed task fields stay as they are: a task names an
  existing media file by project-relative path and purpose in `behavior` or
  `notes`, and lists in `scope` only the media it may create or modify.
  Do not add `inputs`, image, audio, or video fields, an adapter attachment
  protocol, media-capability inference, or a second review state; `verify`
  keeps the machine-checkable requirements and perceptual judgment stays part
  of the explicit `accept`.
## Functional categories

Planning meetings talk about assent in three functional categories: unattended
running, in-flight review, and the git-based workflow. Only the third is also a
physical file boundary. The first two are one recursively-coupled state machine
on purpose -- the review-and-repair loop calls back into the same task-processing
and run-locking functions it is itself called from, including a direct recursive
call into the top-level run-lock function for a `RECHECK` review -- so they are
distinguished by function name inside `engine.py` and `auto_fix.py` rather than
by file. No split of those two files is pending or expected. The lists below are
orientation only; the behavioral contracts stay where they already are.

Git-based workflow: `gitops.py`, `accept.py`, `archive.py`, `reconcile.py`,
`reject.py`, `rework.py`, `clean.py`, `init.py`, `plan.py`, `batch_accept.py`,
`batch_receipt.py`, `folder_verification.py`,
`folder_verification_closeout.py`, `batch_verification.py`, `verification.py`,
`verification_common.py`, `shared_paths.py`.

Unattended running: `folder_scheduler.py`, plus the scheduling, session, and
resume surface inside `engine.py` -- `_run_locked`, `_process_task`,
`_evaluate`, adapter rotation, and quota/resume handling.

In-flight review: `auto_fix.py`, plus the review-and-repair surface inside
`engine.py` -- `_run_auto_fix_review_once`, `_run_auto_fix_repairs`,
`_FocusedGateLedger`, `_AutoFixBlockerEvidence`.

Shared foundation used by more than one category: `config.py`, `contracts.py`,
`lockfile.py`, `pathops.py`, `user_home.py`, `preflight.py`, `folderdeps.py`,
`folder_source.py`, `inspection.py`, `doctor.py`, `terminal_log.py`,
`adapters/`, `__main__.py`, `main.py`.

- When using assent, first read `~/.assent/instructions.md`, the global working instructions shared by every project; a scheduled worktree session uses the absolute path the scheduler provides. <!-- assent-instructions -->
