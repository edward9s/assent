# Project instructions

## Project

assent — an AI plan format plus an automatic scheduler. Pure Python 3.11+
(standard library only, tomllib), Windows-first and cross-platform. CLI
subcommands: run / status / check / report / verify / clean / accept /
reconcile / reject / rework / archive / init / doctor / shared-paths.
Source lives in `assent/`, tests in `tests/` (unittest, not pytest).

This file governs development of the assent project itself. Rules followed
while operating an assent-managed session live in
`assent/templates/instructions.md`.

## Permanent constraints

- Reliability by construction is the highest architectural principle: use the
  smallest architecture and fewest states that make invalid behavior difficult
  to form. Prefer removing a failure mode over detecting, tracking, routing, or
  recovering from it. Tests protect necessary behavior; they do not justify
  avoidable control paths.
- Semantic precision shares that priority: one term names one actual mechanism,
  and no name implies a capability Assent does not provide. Prefer the domain's
  existing concrete term over a synonym, alias, metaphor, or historical name.
- Unattended completion, human adjudication: the scheduler decides everything
  it can decide without a human, and routes what it cannot decide to the human
  acceptance meeting as `_report.md` evidence. A question the scheduler cannot
  settle is not a run failure. Design such an outcome as exit 0 plus a
  distinctly named report state plus an explicit gate at `accept` — never as a
  nonzero exit, because `run --all` stops launching further plans once any
  plan exits nonzero, so nonzero silently cancels unrelated queued work.
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
    its titles in — so in assent's own `.assent/` plans, write task titles
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
- Contract and reader documentation must be concise, present-tense, and
  reader-oriented. Keep only text needed to act or understand; do not add
  development chronology, meeting narrative, or changelog entries unless the
  file explicitly owns historical records.
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
- AI workflow sessions may edit ordinary candidate source, tests,
  configuration, and documentation when writable. They never edit task
  contracts, journals, scheduler state, receipts, Git state, or acceptance
  state. Protect this small control surface directly; do not recreate task
  path scopes or inferred ownership.
- Every writable repair role treats the authoritative requirements as the
  source of truth, determines whether each defect is in the tests or the
  implementation, and corrects whichever is wrong. It preserves correct tests
  and never weakens, narrows, deletes, rewrites, bypasses, or mocks them merely
  to make a check pass.
- Every explicitly named live plan selection is audited in full before
  dispatch: each stated name, including a prefix before `...`, must resolve to
  an existing `.assent/` directory containing a formal `tNNN_name.e.toml` task
  file. Any unresolved name reports with the complete unresolved set and
  prevents every selected operation from starting; dynamic discovery modes keep
  their own contracts, and archive restore/recovery remains allowed to resume
  with an intentionally absent live directory.
- git is always required; no disable switch or git-less degraded mode may be
  introduced.
- Do not introduce a hand-maintained "current plan" pointer; the plan
  is stated explicitly by argument or derived from task-file facts, and any
  ambiguity is refused.
- Human approval is the explicit `assent accept PLAN` action plus the resulting
  Git integration; do not add a second per-task `review` state alongside task
  execution status.
- During `assent run`, the configured `task`, `plan`, and `integration`
  workflow arrays are always active. An explicit `assent verify` instead runs
  only the requested mechanical verification and never enters a workflow role
  or automatic repair. The scheduler actions are `focused_test`,
  `focused_sweep` (distinct task verify commands without a receipt), and
  `full_verify` (the reconstructed candidate and receipt). One finite linear
  interpreter executes every layer: a successful role session advances once;
  a passing action completes the layer and skips later roles; a failing action
  records evidence and advances; exhaustion becomes `REVIEW UNRESOLVED, HUMAN
  DECISION`, exit zero. Workflow position and abilities define responsibility;
  names do not. Sessions are sequential and exchange only bounded evidence,
  never dialogue. There is no structured verdict, finding ledger, owner
  routing, cascade, repair phase, disposition protocol, scope amendment, or
  second review engine. Only the scheduler mutates task status, journals, Git,
  actions, or receipts. Integration conflict evidence mechanically names each
  conflicting plan and path. A configured integration role repairs a
  target-only conflict in its scheduler-owned reconcile worktree and a
  peer-only conflict in that plan's persistent source worktree; the scheduler
  alone stages, commits, advances the source, rebuilds the exact candidate, and
  rechecks. A multi-plan verifier failure without mechanically identified
  source attribution remains a human decision. Workflow repair never accepts
  a plan or changes the explicit human `accept` boundary.
  The effective `workflow.task` array is required and nonempty; task-local
  workflow omission inherits it, while an empty task-local array is refused.
  No empty or omitted task workflow delegates work to another layer, and every
  AI session comes from an explicitly configured role.
- The default adapter permissions remain `danger-full-access` where configured:
  the read-only reviewer's prompt plus before/after write detection is a
  cooperative rule, not a security sandbox and not a preventive permission
  boundary.
- The literal ASCII token `...` is remainder syntax shared by every
  plan-taking command (`run`, `verify`, `accept`, `clean`, `archive`): given
  once, as the last positional argument, it means "and every remaining plan
  the command itself would discover". It is not an alias for `--all` and may
  not be combined with it (nor with `verify --batch`/`--focus`, `run --once`/
  `--task`, or `archive --restore`, which takes exactly one plan). The
  expansion is snapshotted once, before anything is mutated, and each command
  keeps its own discovery rule: `verify` and `accept` add only finished plans,
  `run`, `clean` and `archive` add every plan and decide per plan
  afterwards. The remainder is appended after the explicit prefix, and each
  command then applies its own native ordering: `run` keeps the stated prefix
  order and takes the remainder in plan-dependency order, `verify` and
  `accept` normalize the whole selection to dependency order, and `clean`
  normalizes it upstream-first.
- Selection cardinality is what picks a command's path, not the presence of
  `...`: one plan is the single-plan path (plan receipt, direct accept,
  `archive_plan`), two or more is the exact selected batch. A
  remainder-expanded selection is an ordinary exact selection, so selected
  acceptance still requires evidence for exactly the expanded set and still
  never verifies.
- A successful `run` automatically follows the configured integration workflow
  for the same exact selection until `full_verify` passes or the finite array is
  exhausted. `--once` and `--task` defer integration when they leave the selected
  plan incomplete. No run path accepts; publication remains the later human
  `assent accept` action.
- A multi-plan `archive A B` keeps `archive PLAN`'s contract, not `--all`'s:
  the human named those plans, so an ineligible one is a refusal that exits
  nonzero after every named plan has been attempted, while `--all` skips an
  ineligible plan without failing.
- argparse help may be colorized by the standard library; only the `usage:`
  prefix and section headings are re-themed, away from Python 3.14's barely
  legible dark blue, and only inside argparse's own `_set_color`, so
  `NO_COLOR`, `FORCE_COLOR`, `PYTHON_COLORS`, and a redirected or unsupported
  stream still decide whether any escape is emitted. Never promise colored
  help, and never emit an escape sequence argparse's own checks turned off.
- Expensive full-project verification belongs to unattended `run` / `verify`,
  not the interactive acceptance decision. Human approval is the explicit
  `assent accept` action plus the resulting Git integration. Direct
  `accept PLAN` and selected `accept A B` never start verification; unless a
  source is already integrated by ancestry as an idempotent no-op, they require
  a fresh receipt that exactly matches the source, reconstructed integration
  tree, and verification-script digest. `accept --all` intentionally has two
  modes: a fresh PASSED batch receipt is replayed and released atomically
  without new verification, while absent or expired batch evidence uses the
  sequential per-plan `verify_plan_if_needed` step before each
  not-already-integrated accept, stops on the first failure, and preserves
  earlier publications. A malformed batch receipt refuses rather than falling
  back. Exact receipt replay and the human approval boundary remain mandatory.
- Every production plan-level complete verification operation, whether
  `verify_plan` or `verify_plan_if_needed`, refreshes that plan's
  `_report.md` exactly once after the receipt operation settles and all
  verification locks are released. The best-effort refresh observes PASSED,
  FAILED, stale-receipt replacement, fresh-receipt reuse, malformed-receipt
  refusal, incomplete-plan no-op, and interrupt outcomes without changing or
  masking the verification result. Focused verification and selected or
  dynamic batch verification write no plan receipt and therefore do not
  refresh a plan report.
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
  required ignored directory through `assent shared-paths declare`, which
  provisions the same-relative junction or directory symlink, and never to copy
  the tree or hand-create a source link; a full verifier that fails on a
  path inside a physically ignored source directory gets one appended
  `Ignored input diagnosis:` note naming that directory and the directory-link
  remedy. The note preserves the verifier output and exit code, is stored in
  whichever receipt records the failure summary, applies to single-plan,
  exact selected, dynamic batch, and localization-prefix verification alike,
  normalizes separators, and reports only a directory the verifier output
  itself names; it never enumerates or traverses an ignored tree.
- Which ignored directories a project shares is a reviewed decision cached in
  the primary worktree's untracked, never-committed `.assent/manifest.toml`;
  it is Assent-owned local execution memory, not project source, and its only
  writer is the validated `assent shared-paths declare` operation. Under
  `[shared_paths]` it retains whole profiles by fingerprint -- normalized
  project-relative `paths`, a complete collapsed ordinary ignored-directory inventory,
  explicit non-shared dispositions with reasons, exact tracked `watch` files,
  and a digest of those files plus the tracked Git-ignore rules -- so parallel
  branches never make the cache oscillate and an omitted directory cannot
  be accepted. A source snapshot is UNKNOWN, REVIEWED-NONE (a matching
  `paths = []` profile is an answer and must never trigger another review),
  REVIEWED-PATHS (Assent provisions the exact Windows junction or POSIX
  directory symlink to the primary worktree's same relative path itself), or
  STALE; conflicting matching profiles fail closed. One further state,
  NO-IGNORED-DIRECTORY-CANDIDATE, is the deterministic zero-token fast path:
  a successful primary-worktree query found no existing ordinary ignored
  directory. It is distinct from REVIEWED-NONE and never claims that shared
  input is semantically unnecessary. Discovery failure refuses; a new directory
  makes the next classification UNKNOWN. Complete-verifier `required_evidence`
  requires a provisionable primary directory or refuses with the exact target
  problem. UNKNOWN and STALE add one bounded `assent shared-paths declare`
  clause to a source role; the following scheduler action refuses to run until
  the validated operation settles the decision. Inventory comes from the primary worktree;
  ignored leaf files remain on their separate automatic verifier path.
  Every verification entry point and `assent reconcile` classify and reconcile
  before any candidate, verifier, or managed worktree exists, and plan and
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
- Cross-plan speculative execution stacks only on an explicitly declared
  `base`, so at most one not-yet-accepted upstream tip is ever in a stack. A
  plan that declares no `base` is cut from the integration target; the
  scheduler must never infer a base from `after` or otherwise build an implicit
  integration engine.
- Rework preserves existing code by default. A from-scratch rework must be an
  explicit human choice and may reverse only a checkpoint tail whose ownership
  is mechanically provable.
- `build/lib/` is an old build artifact; never modify it.
- `model` is the only portable selection: the tiers prime/core/lite. Effort is
  not an independent axis and is not a task field. Each adapter maps a tier to
  one complete `"<model>/<effort>"` invocation in `[adapter.<name>.models]`;
  the first `/` separates the two, a model name may not contain `/`, and a
  selection with no `/` deliberately passes no effort argument and inherits the
  vendor CLI default. There is no abstract effort vocabulary and no translation
  table, so an adapter never up/down-shifts anything: the configured value is
  sent verbatim, and a model family's real ceiling is written into that value
  where a human can read it. A task file accepts the three tiers and nothing
  else, so a vendor id cannot be written into a plan artifact that outlives the
  release it names. An `assent.toml` role or workflow entry may instead state a
  vendor `model/effort` selection directly -- any value that is not a tier is
  read as one -- which bypasses the table and is sent only through a step that
  resolves to exactly one adapter. Such a value never mutates adapter settings.
  Vendor-specific model and effort strings belong in adapter mappings or in that
  one place, and must not be hardcoded in adapter code.
- An adapter command may request an immediate continuation only with the exact,
  provider-neutral `{"type":"assent.checkpoint_resume"}` terminal control
  record. Assent owns the WIP checkpoint and resume lifecycle; the record carries
  no account, pool, quota-capacity, or reset semantics, requires no configuration
  or capability probe, and ordinary vendor quota output keeps the existing
  wait/adapter-rotation behavior.
- An adapter authentication failure is candidate-local availability evidence,
  never a task failure: preserve progress, skip that adapter for the current
  workflow step, and try the next declared candidate without changing task
  status. If every candidate requires authentication, stop with nonzero
  `AUTHENTICATION REQUIRED`, keep the task resumable rather than `BLOCKED`, and
  do not wait; when authentication and quota failures are mixed, wait only for
  a quota-exhausted candidate that can recover.
- Selection is one lookup with nothing behind it: the tier's entry in that
  adapter's `models` table, or the vendor selection itself. A stated `models`
  table replaces the built-in one whole, so its keys are validated against the
  known tiers at config load and an unmapped tier is a preflight failure rather
  than a run-time surprise. Only a selection that omits `/` inherits the vendor
  CLI default effort.
- Media inputs (image, PDF, audio, and the like) are ordinary project context,
  not a schema feature. The fixed task fields stay as they are: a task names an
  existing media file by project-relative path and purpose in `behavior` or
  `notes`.
  Do not add `inputs`, image, audio, or video fields, an adapter attachment
  protocol, media-capability inference, or a second review state; `verify`
  keeps the machine-checkable requirements and perceptual judgment stays part
  of the explicit `accept`.
## Functional categories

Assent has two implementation categories: the linear unattended workflow and
the Git-based publication workflow. They share configuration and verification
foundations but no recursive review engine.

Git-based workflow: `gitops.py`, `accept.py`, `archive.py`, `reconcile.py`,
`reject.py`, `rework.py`, `clean.py`, `init.py`, `plan.py`, `batch_accept.py`,
`batch_receipt.py`, `plan_verification.py`,
`plan_verification_closeout.py`, `batch_verification.py`, `verification.py`,
`verification_common.py`, `shared_paths.py`.

Unattended workflow: `plan_scheduler.py` and the linear task, plan, and
integration interpreter in `engine.py`, including adapter rotation and
quota/resume handling.

Shared foundation used by more than one category: `config.py`, `contracts.py`,
`lockfile.py`, `pathops.py`, `user_home.py`, `preflight.py`, `plandeps.py`,
`plan_source.py`, `inspection.py`, `doctor.py`, `terminal_log.py`,
`adapters/`, `__main__.py`, `main.py`.

- When using assent, first read `~/.assent/instructions.md`, the global working instructions shared by every project; a scheduled worktree session uses the absolute path the scheduler provides. An AI session never initiates the full suite or `.assent/verify.py`; the scheduler owns workflow `full_verify`, and an interactive session runs complete verification only when the human explicitly requests it. <!-- assent-instructions -->
