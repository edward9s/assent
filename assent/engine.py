"""Main loop: select task -> run -> accept -> checkpoint/retry/quota wait.

The pre-session decisions this loop shares with the read-only query commands
live in ``assent.preflight``; the report it refreshes as a best-effort side
effect is rendered by ``assent.inspection``.  Both are imported here and neither
imports back, so the query commands stay loadable without the scheduler.

Iron rules inherited from field experience on the workflow project: **never discard
output the execution AI has already burned tokens to produce.**
- Quota interrupted -> keep progress in a wip checkpoint, then after reset resume with a
  "continue" prompt instead of scrapping and rerunning.
- Acceptance failed -> do not restore the working tree; a retry fixes the existing work
  (cheap) rather than redoing it from scratch (expensive).
- Retries exhausted -> commit the not-yet-passing work into a BLOCKED checkpoint and hand
  the final call to a human.
- scope check is fail-closed: parsing a task file forces scope to be non-empty, so the
  batch parse at the start of a run is a zero-token refuse-to-run gate.

Acceptance defences added by assent (the format contract's "defence rules"):
- The only legal change the execution AI may make to its own task file is the status line;
  at acceptance time the checkpoint version and the on-disk version are compared field by
  field, and any other tampered field (loosened scope, swapped verify, changed deps) fails
  acceptance.
- scope/verify are always taken from the checkpoint version of the task file, never from
  the on-disk version.
- A task's own t file and r file are auto-excluded from scope (the status update and the
  journal append are part of the job).
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, TextIO

from assent import (AssentError, contracts, gitops, lockfile, shared_paths,
                    verification)
from assent.adapters import Adapter, get_adapter
from assent.adapters.process import (clear_stop_wake, interruptible_sleep,
                                     stop_wake_requested)
from assent.config import Config
from assent.folderdeps import find_unfinished_prerequisites
from assent.inspection import try_write_report
from assent.plan import (Plan, Task, append_entry, parse_task_file,
                         read_entries, same_except_status, set_status)
from assent.preflight import (GIT_REQUIRED_MESSAGE, SessionIdentity,
                              StackState, capability_errors, has_git_marker,
                              resolve_session, resolve_stack_state,
                              worktree_configuration_errors)

# Default prompt template (overridable via [prompt] template; variables are substituted
# literally, tolerating other braces inside the template).
_DEFAULT_PROMPT_TEMPLATE = (
    "You are the assent execution AI. First read the project rules {agents_md_path},\n"
    "then read the assent working instructions {instructions_path} and the task file {task_path}.\n"
    "Run only task {task_id}; do not touch other task files.\n"
    "This run's journal identity is by = \"{agent}\", requested_model = \"{requested_model}\".\n"
    "That resolved identity is authoritative for this run's journal entry, even when the\n"
    "working instructions or the existing entries only show other agent names.\n"
    "requested_model is the --model value passed to the AI CLI this run.\n"
    "This run's abstract effort = \"{effort}\", actual requested_effort = \"{requested_effort}\";\n"
    "requested_effort is the value actually passed to the AI CLI this run.\n"
    "To verify yourself, run this in the current working tree: {verify_command}\n"
    "This is the focused task gate. If an outer tool times out while the child result is\n"
    "unknown, do not start a concurrent duplicate and do not mark the task BLOCKED solely\n"
    "because of that timeout; determine the child result serially. The scheduler runs the\n"
    "same command after the AI session and owns the checkpoint/retry decision.\n"
    "When done:\n"
    "1. Change the status of {task_path} to DONE or BLOCKED -- the status line is the only\n"
    "   line you may change in the whole task file.\n"
    "2. Append one [[entry]] journal record to the end of {journal_path} (TOML, with time,\n"
    "   by = \"{agent}\", requested_model = \"{requested_model}\", requested_effort, event,\n"
    "   summary, detail; create the file if it does not exist).\n"
    "Do not run git commit; the scheduler owns the checkpoint."
)
_RETRY_SUFFIX = ("\nThe previous attempt failed acceptance. Reason: {failure_reason}. "
                 "The previous attempt's work is still in the working tree; review and fix "
                 "it on top of what is there, do not redo it.")
_RESUME_SUFFIX = ("\nThe previous run of this task was interrupted (quota exhausted or user "
                  "interrupt); the partial work already done is kept in the working tree "
                  "(possibly including a wip checkpoint). Review the current state first, "
                  "resume and finish the remaining part, and do not redo what is already done.")
_REWORK_SUFFIX = ("\nThe previous implementation of this task was rejected by a human "
                  "reviewer, and its status was reset to TODO.{reject_reason_clause}\n"
                  "The previous implementation and its tests may still be present in the "
                  "working tree; do not assume the existing code or existing tests are "
                  "correct as-is, re-evaluate their correctness from scratch. If the task "
                  "file has since been amended with new clauses, the task file is "
                  "authoritative.")
_CLOSEOUT_ONLY_MARKER = "verify passed; only closeout missing"
_CLOSEOUT_ONLY_REASON_TEMPLATE = (
    _CLOSEOUT_ONLY_MARKER + " -- status not updated to DONE/BLOCKED (currently "
    "{status}); focused verify already passed: {verify_command}")
_CLOSEOUT_RETRY_SUFFIX = (
    "\nThe previous attempt failed acceptance only because the task status was not "
    "updated. The implementation in the working tree is already complete, and this "
    "task's focused verify command has already passed: {verify_command}\n"
    "This session must only close out the task: change the status line in the task "
    "file to DONE, and append one [[entry]] journal record to the journal file. "
    "Do not modify any code or tests.")

_QUOTA_BUFFER = timedelta(minutes=2)  # reset time + buffer, to avoid being blocked again right at the edge
_QUOTA_TICK = 1.0                     # countdown refresh interval (seconds)
# Longest single sleep the non-tty countdown may take. A quota wait is often
# hours long, and a lone multi-hour sleep is what made a stop request invisible:
# on POSIX _thread.interrupt_main() only sets a pending exception that is
# delivered when bytecode next runs, so the wait had to finish first. Splitting
# it bounds that delivery delay without changing the total wait.
_COUNTDOWN_SEGMENT = 60.0
_DEFAULT_VERIFY_COMMAND = "python .assent/verify.py"
_ADAPTER_DIAGNOSTIC_LIMIT = 240
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)([\"']?(?:api[_ -]?key|access[_ -]?token|token|password|secret|"
    r"authorization)[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)")
_BEARER_RE = re.compile(r"(?i)(\bbearer\s+)[^\s,;]+")
_KNOWN_TOKEN_RE = re.compile(
    r"\b(?:sk(?:-ant)?|ghp|github_pat)-[A-Za-z0-9_-]{8,}\b")
_REWORK_REASON_RE = re.compile(
    r"(?:^|\n)reason: (.*?)(?=\nHEAD before operation:|\Z)", re.DOTALL)


@dataclass
class _SessionState:
    """Lets the outer interrupt handler read the resolved identity of the current session."""

    identity: SessionIdentity | None = None


@dataclass
class _AdapterRotation:
    """Run-scoped adapter cursor and quota evidence shared across tasks."""

    names: tuple[str, ...]
    adapters: tuple[Adapter, ...]
    index: int = 0
    exhausted: set[str] = field(default_factory=set)

    @property
    def name(self) -> str:
        return self.names[self.index]

    @property
    def adapter(self) -> Adapter:
        return self.adapters[self.index]

    def session_opened(self) -> None:
        """A non-quota result proves the rotation is no longer fully exhausted."""
        self.exhausted.clear()

    def advance_after_quota(self) -> bool:
        """Move to the next adapter; return whether this exhausted one complete cycle."""
        self.exhausted.add(self.name)
        self.index = (self.index + 1) % len(self.names)
        cycle_exhausted = len(self.exhausted) == len(self.names)
        if cycle_exhausted:
            self.exhausted.clear()
        return cycle_exhausted


class _BillingAbort(Exception):
    """An account-level billing/insufficient-balance failure the adapter classified.

    Unlike quota (a rate-limit window that resets on its own), a prepaid balance does not
    refill, so retrying is provably futile and the next TODO task would hit the identical
    failure.  This unwinds the whole run to the abort handler, which keeps progress in a wip
    checkpoint and leaves the task unresolved for a clean resume after a manual top-up.  It is
    dispatched purely on ``TaskResult.failure_kind == "billing"`` -- never on an adapter name --
    so a future adapter gets the behaviour for free by setting the same string.
    """


# --------------------------------------------------------------------------- #
# Prompt / small helpers
# --------------------------------------------------------------------------- #
def _diagnosed_shared_inputs(cfg: Config) -> tuple[str, ...]:
    """Directories a stored full-verifier diagnosis proved this folder needs.

    No generic rule can infer that an ignored directory is semantically
    required, so a complete verifier that already failed on one is the evidence
    that invalidates a profile which does not declare it.  A missing or
    unreadable receipt simply contributes nothing.
    """
    try:
        receipt = verification.read_verification_receipt(
            verification.receipt_path(cfg), gitops.main_worktree(cfg.root))
    except AssentError:
        return ()
    return verification.diagnosed_ignored_directories(receipt.failure_summary)


def _shared_paths_contract(cfg: Config) -> "shared_paths.Contract":
    """Return this source worktree's current usable shared-path contract.

    Both the bounded review clause and the closeout gate ask the same question of
    the same snapshot. Classification and agreement errors intentionally
    propagate: an unreadable or ambiguous manifest or an undeclared source link
    is a closeout refusal, never permission to finish as though no shared input
    existed.
    """
    main = gitops.main_worktree(cfg.root)
    contract = shared_paths.classify(main, cfg.root)
    if contract.settled:
        shared_paths.require_directory_link_agreement(main, cfg.root, contract)
    return contract


def _build_prompt(cfg: Config, task: Task, failure_reason: str | None,
                  session: SessionIdentity, resumed: bool = False) -> str:
    template = cfg.prompt_template or _DEFAULT_PROMPT_TEMPLATE
    text = (template
            .replace("{agents_md_path}", _agents_md_path_for_prompt(cfg))
            .replace("{instructions_path}", str(contracts.instructions_path()))
            .replace("{task_path}", cfg.rel(task.path))
            .replace("{journal_path}", cfg.rel(task.journal_path))
            .replace("{verify_command}",
                     _verify_command_for_prompt(cfg, task.verify))
            .replace("{task_id}", task.id)
            .replace("{task_title}", task.title)
            .replace("{agent}", session.agent)
            .replace("{requested_model}", session.requested_model)
            .replace("{effort}", session.effort or "")
            .replace("{requested_effort}", session.requested_effort or ""))
    # Startup provisioning already reports a classification failure before an
    # adapter is normally reached.  Prompt construction remains best-effort so
    # that a pre-session failure does not acquire a duplicated clause; the
    # closeout gate below handles the same error fail-closed.
    try:
        contract = _shared_paths_contract(cfg)
    except AssentError:
        contract = None
    if contract is not None:
        text += shared_paths.review_clause(contract)
    if resumed:
        text += _RESUME_SUFFIX
    else:
        text += _rework_prompt_suffix(task)
    if failure_reason and failure_reason.startswith(_CLOSEOUT_ONLY_MARKER):
        text += _CLOSEOUT_RETRY_SUFFIX.replace("{verify_command}", task.verify)
    elif failure_reason:
        text += _RETRY_SUFFIX.replace("{failure_reason}", failure_reason)
    return text


def _rework_prompt_suffix(task: Task) -> str:
    """Carry a rejection reason into the next TODO run of a task the human just reworked.

    Triggers only when the task's journal's last entry is a pending ``rework_requested``
    record (written by ``rework.py``, never modified here). Any read/parse problem, or a
    reason that cannot be located in that entry's detail text, degrades to a warning-only
    suffix (or none at all, if the trigger condition itself cannot be established) rather than
    failing prompt assembly.
    """
    try:
        entries = read_entries(task.journal_path)
    except AssentError:
        return ""
    if not entries or entries[-1].get("event") != "rework_requested":
        return ""
    detail = entries[-1].get("detail")
    reason = None
    if isinstance(detail, str):
        m = _REWORK_REASON_RE.search(detail)
        if m:
            reason = m.group(1).strip()
    clause = f" Rejection reason: {reason}" if reason else ""
    return _REWORK_SUFFIX.replace("{reject_reason_clause}", clause)


def _session_line(adapter_name: str, task: Task,
                  session: SessionIdentity) -> str:
    """The one opening line that states the whole resolved session identity.

    Four facts, in the order they are decided: which adapter runs, and each abstract choice
    beside the concrete value actually sent to that adapter's CLI, e.g.
    ``Session: codex | core->gpt-5.6-luna | heavy->max``.
    """
    return (f"  Session: {adapter_name} | {task.model}->{session.requested_model}"
            f" | {session.effort}->{session.requested_effort}")


def _short(text: str, limit: int = 60) -> str:
    """Squash to a single line and truncate, for use in a commit message."""
    return " ".join(text.split())[:limit]


def _bounded_adapter_diagnostic(output: str,
                                limit: int = _ADAPTER_DIAGNOSTIC_LIMIT) -> str:
    """Return a one-line, bounded diagnostic suitable for prompts and journals.

    Adapter output remains available live to the operator, but persisted scheduler evidence
    must not contain an arbitrary-length transcript, prompt, or common credential forms.
    Redaction happens before truncation so a token is not exposed merely because its label
    fell outside the retained tail.
    """
    redacted = _SECRET_ASSIGNMENT_RE.sub(r"\1[REDACTED]", output)
    redacted = _BEARER_RE.sub(r"\1[REDACTED]", redacted)
    redacted = _KNOWN_TOKEN_RE.sub("[REDACTED]", redacted)
    compact = " ".join(redacted.split())
    if not compact:
        return "no diagnostic output"
    if len(compact) <= limit:
        return compact
    return "..." + compact[-(limit - 3):]


def _adapter_failure_reason(exit_code: int, stalled: bool, output: str,
                            failure_kind: str | None = None) -> str:
    kind = "watchdog stalled" if stalled else "exited nonzero"
    diagnostic = _bounded_adapter_diagnostic(output)
    # An adapter may classify its own failure (quota, permission, unsupported model, ...).
    # It is recorded as the adapter's reading of the evidence; the verdict stays the
    # scheduler's, so the exit code and the diagnostic are still reported in full.
    label = f" classified by the adapter as {failure_kind}" if failure_kind else ""
    return (f"Adapter process {kind} (exit code {exit_code}){label}; "
            f"bounded diagnostic: {diagnostic}")


def _append_adapter_failure_entry(task: Task, session: SessionIdentity,
                                  exit_code: int, stalled: bool, summary: str,
                                  now: Callable[[], datetime],
                                  failure_kind: str | None = None) -> None:
    """Append the machine-readable scheduler evidence for one non-quota failure.

    This event needs a numeric ``exit_code`` in addition to the shared journal fields.  It is
    written here because the general journal helper deliberately exposes only the stable
    common schema.  Re-reading validates the complete TOML document after the append.
    """
    fields = [
        "[[entry]]",
        f"time = {json.dumps(now().isoformat(timespec='seconds'))}",
        'by = "scheduler"',
        f"agent = {json.dumps(session.agent)}",
        f"requested_model = {json.dumps(session.requested_model)}",
    ]
    if session.requested_effort is not None:
        fields.append(
            f"requested_effort = {json.dumps(session.requested_effort)}")
    fields += [
        f"event = {json.dumps('adapter_stall' if stalled else 'adapter_exit')}",
        f"exit_code = {int(exit_code)}",
        f"stalled = {'true' if stalled else 'false'}",
    ]
    if failure_kind:
        fields.append(f"failure_kind = {json.dumps(failure_kind)}")
    fields.append(f"summary = {json.dumps(summary, ensure_ascii=False)}")
    block = "\n".join(fields) + "\n"
    existing = (task.journal_path.read_text(encoding="utf-8")
                if task.journal_path.is_file() else "")
    with open(task.journal_path, "a", encoding="utf-8", newline="") as journal:
        if existing and not existing.endswith("\n"):
            journal.write("\n")
        if existing:
            journal.write("\n")
        journal.write(block)
    read_entries(task.journal_path)


def _checkpoint_subject(cfg: Config, kind: str, task: Task, detail: str) -> str:
    """Build a task checkpoint subject namespaced by the task folder."""
    return f"{kind}({cfg.tasks_name}/{task.id}): {detail}"


def _task_excludes(cfg: Config, task: Task) -> list[str]:
    """The scope-check exemption list for this task: its own t file and r file (the status
    update and journal are part of the job) plus the global runtime artifacts."""
    return [cfg.git_rel(task.path), cfg.git_rel(task.journal_path),
            *cfg.git_excludes]


def _verify_command_for_prompt(cfg: Config, command: str) -> str:
    """When running isolated, expand the default verify script to the main tree's absolute path."""
    if cfg.source_root is None or command.strip() != _DEFAULT_VERIFY_COMMAND:
        return command
    parts = [sys.executable, str((cfg.assent_dir / "verify.py").resolve())]
    return (subprocess.list2cmdline(parts) if sys.platform == "win32"
            else shlex.join(parts))


def _agents_md_path_for_prompt(cfg: Config) -> str:
    """Choose the project rules: prefer the branch version, else fall back to the main
    working tree's absolute path."""
    candidate = cfg.root / "AGENTS.md"
    if candidate.is_file():
        return cfg.rel(candidate)
    if cfg.source_root is not None:
        source = cfg.source_root / "AGENTS.md"
        if source.is_file():
            return str(source.resolve())
    return "AGENTS.md (if present; skip if absent)"


def _require_stack_ancestry(cfg: Config, state: StackState,
                            downstream_tip: str) -> None:
    """Require the downstream tip to contain the current declared base, if any."""
    source = state.base.speculative_upstream
    if source is None or gitops.is_ancestor(cfg.root, source.tip, downstream_tip):
        return
    raise AssentError(
        f"stale stack for {cfg.tasks_name}: current upstream "
        f"{source.folder} tip {source.tip} is not an ancestor of downstream "
        f"tip {downstream_tip}; all existing work is preserved. Run `assent "
        f"rework {cfg.tasks_name}` after deciding how to handle the upstream "
        "change, or replan the dependency")


def _prepare_worktree(cfg: Config) -> Config:
    """Create or validate the folder worktree before any adapter is started."""
    errors = worktree_configuration_errors(cfg)
    if errors:
        detail = "\n".join(f"  - {error}" for error in errors)
        raise AssentError(f"git worktree version-control layering error:\n{detail}")

    state_before = resolve_stack_state(cfg)
    candidate = gitops.worktree_path(cfg.root, cfg.tasks_name)
    existed_before = candidate.exists()
    try:
        root = gitops.ensure_worktree(
            cfg.root, cfg.tasks_name, state_before.base.resolved_base)
        worktree_cfg = cfg.for_worktree(root)

        if existed_before:
            # A reused worktree may still hold uncommitted work from a prior run
            # that exited uncleanly (hard power loss / kill) without ever reaching
            # an interrupt handler.  Whether that dirty state is clean, provably
            # attributable and recoverable, or a fail-closed refusal is decided by
            # the single run-startup gate in _run_locked below -- never discarded
            # here.  The branch/ancestry checks that follow read only committed
            # HEAD state, so they are unaffected by an uncommitted working tree.
            branch = gitops.current_branch(worktree_cfg.root)
            if not branch:
                raise AssentError(
                    f"existing worktree {root} is detached; refusing to switch or "
                    "rewrite it automatically")
            if not branch.startswith(worktree_cfg.branch_prefix):
                raise AssentError(
                    f"existing worktree {root} is on foreign branch {branch}; "
                    f"expected prefix {worktree_cfg.branch_prefix}")
        else:
            # A freshly created worktree is a clean checkout of the base commit; any
            # dirt here is a genuine setup fault, so keep failing closed immediately.
            gitops.ensure_clean(worktree_cfg.root, worktree_cfg.git_excludes)
            branch = gitops.ensure_branch(
                worktree_cfg.root, worktree_cfg.branch_prefix)

        downstream_tip = gitops.commit_of(worktree_cfg.root, "HEAD")
        if not branch.startswith(worktree_cfg.branch_prefix):
            raise AssentError(
                f"worktree branch {branch or '(detached)'} does not use required "
                f"prefix {worktree_cfg.branch_prefix}")
        if not existed_before and downstream_tip != state_before.base.resolved_base:
            raise AssentError(
                f"new worktree HEAD {downstream_tip} does not match resolved base "
                f"{state_before.base.resolved_base}")

        state_after = resolve_stack_state(cfg)
        tips_before = {source.folder: source.tip for source in state_before.sources}
        tips_after = {source.folder: source.tip for source in state_after.sources}
        if tips_after != tips_before:
            changes = sorted(set(tips_before) | set(tips_after))
            detail = ", ".join(
                f"{folder}: {tips_before.get(folder, '(missing)')} -> "
                f"{tips_after.get(folder, '(missing)')}"
                for folder in changes
                if tips_before.get(folder) != tips_after.get(folder))
            raise AssentError(
                "upstream source changed between stack resolution and worktree "
                f"validation ({detail})")
        _require_stack_ancestry(cfg, state_after, downstream_tip)

        # REVIEWED-PATHS provisions every declared missing link before any
        # adapter starts; REVIEWED-NONE starts with no links and no extra AI
        # instructions, and UNKNOWN or STALE touches the filesystem not at all.
        contract = shared_paths.prepare_worktree(
            gitops.main_worktree(cfg.root), worktree_cfg.root,
            required_evidence=_diagnosed_shared_inputs(cfg))
        print(shared_paths.describe(contract))
        print(f"Isolated worktree: {root}")
        print(f"Target snapshot: {state_after.base.target_snapshot}")
        stacked = state_before.base.speculative_upstream
        if stacked is None:
            print("Stacked upstream: none")
        else:
            print(f"Stacked upstream: {stacked.folder} @ {stacked.tip}")
        print(f"Work branch: {branch}")
        return worktree_cfg
    except BaseException as primary_error:
        # Only a path absent before this call can belong to this setup attempt.
        # Existing worktrees are never cleanup candidates, even when invalid.
        if not existed_before and candidate.exists():
            try:
                gitops.cleanup_unstarted_worktree(
                    cfg.root, cfg.tasks_name,
                    state_before.base.resolved_base, cfg.branch_prefix)
            except AssentError as cleanup_error:
                raise AssentError(
                    f"worktree setup failed ({primary_error}); cleanup was "
                    "incomplete and resources were retained for recovery: "
                    f"{cleanup_error}") from primary_error
        raise


# --------------------------------------------------------------------------- #
# run: main loop
# --------------------------------------------------------------------------- #
def run(cfg: Config, once: bool = False, task_id: str | None = None, *,
        adapter: Adapter | None = None,
        sleep: Callable[[float], None] | None = None,
        now: Callable[[], datetime] | None = None,
        run_level_verify: bool = False) -> int:
    """Run tasks until all are DONE/BLOCKED/SKIP (or only one with once/task_id). Returns the
    process exit code.

    First check folder prerequisites, then take the task folder's file lock; the lock covers
    the whole run (including the long sleeps of quota waiting); if another run is already
    running in the same folder, print a message and fail with exit code 1 without touching
    anything in the working tree. status / check / report are read-only, take no lock, and can
    be used while a run is in progress.
    """
    # The final global-contracts gate.  The CLI refuses earlier with the same
    # message, but a library caller reaches the adapters only through here, so
    # this check -- not that one -- is what guarantees no session can start
    # against a missing or out-of-date ~/.assent contract.
    try:
        contracts.require_contracts()
    except AssentError as e:
        print(f"Global contracts: FAIL ({e})")
        return 1

    try:
        unfinished = find_unfinished_prerequisites(cfg.tasks_dir)
    except AssentError as e:
        print(f"Prerequisite folder gate: FAIL ({e})")
        return 1
    if unfinished:
        print("Prerequisite folders not finished, refusing to run:")
        for prerequisite in unfinished:
            print(f"  - {prerequisite.message()}")
        return 1

    if not has_git_marker(cfg.root):
        print(GIT_REQUIRED_MESSAGE)
        return 1

    if sleep is None:
        # Not time.sleep: a stop request must end a quota segment at once rather
        # than after up to _COUNTDOWN_SEGMENT seconds.
        sleep = interruptible_sleep
    if now is None:
        now = lambda: datetime.now(timezone.utc)  # noqa: E731

    result: int
    try:
        with lockfile.hold_lock(cfg.tasks_dir, cfg.tasks_name):
            result = _run_locked(cfg, once, task_id, adapter, sleep, now)
    except lockfile.LockBusy as e:
        print(str(e))
        return 1
    if result != 0:
        return result

    # Full candidate verification is outside the AI session and outside the
    # folder lock above.  The verification layer reacquires locks in the one
    # safe order used by accept: repository integration, then folder.
    try:
        plan = Plan.parse(cfg.tasks_dir)
    except AssentError as e:
        print(f"Failed to parse task folder after run: {e}")
        return 1
    if all(task.status in ("DONE", "SKIP") for task in plan.tasks):
        if cfg.receipt_refresh == "auto":
            result = verification.verify_folder_if_needed(cfg)
        else:
            # Manual policy deliberately defers this per-folder receipt.  When
            # the invoking CLI already requested run-level verification, its
            # selected or dynamic candidate follows immediately after this
            # closeout and is the next step the user should see.
            if run_level_verify:
                print(f"verify {cfg.tasks_name}: receipt refresh deferred "
                      "(default) for the per-folder receipt under manual "
                      "policy; run-level verification follows this invocation")
            else:
                print(f"verify {cfg.tasks_name}: receipt refresh deferred "
                      "(default) for the per-folder receipt under manual "
                      "policy; run "
                      "`assent verify [--batch]` before accepting")
        try_write_report(cfg)
    return result


def _run_locked(cfg: Config, once: bool, task_id: str | None,
                adapter: Adapter | None,
                sleep: Callable[[float], None],
                now: Callable[[], datetime]) -> int:
    """The actual run body, after the task folder lock is held."""
    try:
        # Validate the requested folder itself before stack discovery.  This
        # preserves the task-file error as the primary zero-token diagnostic.
        plan = Plan.parse(cfg.tasks_dir)
    except AssentError as e:
        print(f"Failed to parse task folder: {e}")
        return 1

    # Every adapter is resolved and its planned invocations proven before the worktree exists,
    # so rotating later can never discover a configuration the vendor would refuse after a
    # session, status write, or Git change.  The injected adapter is the first slot's test seam;
    # production runs resolve every slot through get_adapter().
    adapters: list[Adapter] = []
    try:
        for index, name in enumerate(cfg.adapter_names):
            adapters.append(
                adapter if index == 0 and adapter is not None
                else get_adapter(name, cfg))
    except AssentError as e:
        print(str(e))
        return 1

    rotation = _AdapterRotation(cfg.adapter_names, tuple(adapters))
    preflight_failures: list[tuple[str, list[str]]] = []
    for name, current_adapter in zip(rotation.names, rotation.adapters):
        errors = capability_errors(
            cfg, current_adapter, plan, task_id, name)
        if errors:
            preflight_failures.append((name, errors))
    if preflight_failures:
        for name, errors in preflight_failures:
            print(f"{name} capability preflight: FAIL "
                  "(refusing before any AI session)")
            for message in errors:
                print(f"  - {message}")
        return 1

    if cfg.source_root is None:
        try:
            cfg = _prepare_worktree(cfg)
        except KeyboardInterrupt:
            print("\nInterrupted during worktree setup; no AI session was started.")
            return 130
        except (AssentError, OSError) as e:
            print(f"git worktree setup failed: {e}")
            return 1

    try:
        _recover_or_ensure_clean(cfg, now)
    except AssentError as e:
        print(f"git setup failed: {e}")
        return 1

    current_task: Task | None = None
    current_session: _SessionState | None = None
    try:
        while True:
            plan = Plan.parse(cfg.tasks_dir)
            if task_id is not None:
                task = plan.get(task_id)
                if task is None:
                    print(f"Task {task_id} not found in task folder")
                    return 1
                status_by_id = {t.id: t.status for t in plan.tasks}
                unmet = [d for d in task.deps
                         if status_by_id.get(d) not in ("DONE", "SKIP")]
                if unmet:
                    print(f"Task {task_id} has unfinished prerequisites: {', '.join(unmet)}")
                    return 1
                if task.status not in ("TODO", "WIP"):
                    print(f"Task {task_id} is currently {task.status}, "
                          f"not TODO/WIP; skipping")
                    return 0
                resumed = task.status == "WIP"
            else:
                selected = plan.next_task()
                if selected is None:
                    break
                task, resumed = selected

            session = _SessionState()
            current_task = task
            current_session = session
            _process_task(cfg, task, rotation, sleep, now, session, resumed)
            current_task = None
            current_session = None

            if once or task_id is not None:
                break
    except KeyboardInterrupt:
        # Ctrl+C on the Windows console reaches the child process (the AI session) too, so
        # the session is terminated by the OS signal; here the engine gathers the produced
        # progress into a wip checkpoint (never discard it) and exits with 130.
        print("\nInterrupt received (Ctrl+C): session terminated, keeping current progress...")
        if (current_task is not None and current_session is not None
                and current_session.identity is not None):
            _mark_interrupted_task(
                current_task, current_session.identity,
                "User interrupt; progress kept for next resume", now,
                detail="run received Ctrl+C")
        try:
            subject = (_checkpoint_subject(
                cfg, "wip", current_task, "user interrupt, progress kept")
                if current_task is not None
                else f"wip({cfg.tasks_name}): user interrupt, progress kept")
            if gitops.commit_if_dirty(cfg.root, subject, cfg.git_excludes):
                print("Progress gathered into a wip checkpoint (git revert it yourself if unsatisfied).")
        except AssentError as e:
            print(f"wip checkpoint failed: {e} (working tree left as is, nothing discarded)")
        try_write_report(cfg)
        print("Interrupted.")
        return 130
    except _BillingAbort as e:
        # Distinct from an acceptance failure and from the infrastructure abort below: the
        # account's prepaid balance is exhausted, which no retry or next task can resolve.
        # Keep the current task's progress, leave it unresolved, and stop the whole run.
        print(f"Run aborted (billing/balance): {e}")
        print("The account's prepaid balance is exhausted; retrying cannot fix this. "
              "Top up the account, then rerun to resume from the kept progress.")
        if (current_task is not None and current_session is not None
                and current_session.identity is not None):
            _mark_billing_task(current_task, current_session.identity, str(e), now)
        try:
            subject = (_checkpoint_subject(
                cfg, "wip", current_task, "billing abort, progress kept")
                if current_task is not None
                else f"wip({cfg.tasks_name}): billing abort, progress kept")
            if gitops.commit_if_dirty(cfg.root, subject, cfg.git_excludes):
                print("Progress gathered into a wip checkpoint.")
        except AssentError:
            pass
        try_write_report(cfg)
        return 1
    except (AssentError, OSError) as e:
        print(f"Run aborted (infrastructure error): {e}")
        if (current_task is not None and current_session is not None
                and current_session.identity is not None):
            _mark_interrupted_task(
                current_task, current_session.identity,
                "Aborted on infrastructure error; progress kept for next resume", now,
                detail=str(e))
        try:
            subject = (_checkpoint_subject(
                cfg, "wip", current_task, "infrastructure error abort, progress kept")
                if current_task is not None
                else f"wip({cfg.tasks_name}): infrastructure error abort, progress kept")
            if gitops.commit_if_dirty(cfg.root, subject, cfg.git_excludes):
                print("Progress gathered into a wip checkpoint.")
        except AssentError:
            pass
        try_write_report(cfg)
        return 1

    _print_summary(Plan.parse(cfg.tasks_dir))
    try_write_report(cfg)
    return 0


def _recover_or_ensure_clean(cfg: Config, now: Callable[[], datetime]) -> None:
    """Run-startup cleanliness gate, extended with a provably-attributable recovery path.

    A hard power loss or a forced kill never reaches the Ctrl+C / quota / infrastructure
    interrupt handlers, so it leaves the worktree dirty with the task status and the wip
    checkpoint unwritten.  On the next run, if every uncommitted change is provably inside the
    scope of the task ``next_task()`` would resume -- or, failing that, of a single DONE task
    the scheduler never checkpointed -- that progress is gathered into a ``wip`` checkpoint and
    the run continues -- no AI session, zero tokens.  Every other dirty state (a change outside
    that scope, ambiguous ownership, or no provable owner at all) keeps today's fail-closed
    behaviour: ``ensure_clean`` raises and the caller refuses to run rather than guessing
    attribution.
    """
    if _try_recover_attributable_worktree(cfg, now):
        return
    gitops.ensure_clean(cfg.root, cfg.git_excludes)


def _try_recover_attributable_worktree(cfg: Config,
                                       now: Callable[[], datetime]) -> bool:
    """Return True only when the worktree was dirty and every change was provably attributable
    to one task, having just committed that progress into a wip checkpoint.

    Two owners can be proven, in this order: the task ``next_task()`` would resume, and -- only
    when that candidate does not own the dirt -- a single DONE task the scheduler never got to
    checkpoint (see ``_uncheckpointed_done_dirt_owner``).  A clean worktree, an unparsable plan,
    or dirt no single task provably owns all return False, leaving the caller's fail-closed
    ``ensure_clean`` to decide.  Attribution reuses the same scope machinery that contains a
    running task's output (``changes_outside_scope`` with an empty ``since_ref`` -> only the
    current uncommitted changes), so the recovery can never claim work a task's scope would not
    have permitted.
    """
    if gitops.working_tree_status(cfg.root, cfg.git_excludes).is_clean:
        return False
    try:
        plan = Plan.parse(cfg.tasks_dir)
    except AssentError:
        return False
    owner = _resumable_dirt_owner(cfg, plan) or _uncheckpointed_done_dirt_owner(cfg, plan)
    if owner is None:
        return False

    _mark_recovered_task(owner, now)
    subject = _checkpoint_subject(
        cfg, "wip", owner,
        "recovered dirty worktree from an unclean exit, scope-verified")
    gitops.commit_if_dirty(cfg.root, subject, cfg.git_excludes)
    print(f"Recovered a dirty worktree from an unclean process exit; scope-verified "
          f"against {owner.id}, progress kept in a wip checkpoint.")
    return True


def _resumable_dirt_owner(cfg: Config, plan: Plan) -> Task | None:
    """The resumable candidate task, when its scope contains every uncommitted change."""
    selection = plan.next_task()
    if selection is None:
        return None
    candidate, _ = selection
    if gitops.changes_outside_scope(
            cfg.root, candidate.scope, excludes=_task_excludes(cfg, candidate)):
        return None
    return candidate


def _uncheckpointed_done_dirt_owner(cfg: Config, plan: Plan) -> Task | None:
    """The one DONE task that provably owns the dirt after a crash between the execution AI's
    DONE mark and the scheduler's ``auto(...)`` checkpoint; None when ownership is not provable.

    That crash leaves the produced work uncommitted while ``next_task()`` has already moved on
    to the following TODO task, so the resumable-candidate path above must never be allowed to
    claim it.  Eligibility needs both halves of the evidence: no ``auto(<folder>/<task>)``
    checkpoint in this branch's history, and a scope containing every uncommitted path.  A
    missing checkpoint alone proves nothing -- a DONE task whose changes all landed in an
    earlier ``wip`` checkpoint legitimately has none -- so a second plausible owner, an existing
    terminal checkpoint, out-of-scope dirt, or unreadable history all fail closed.
    """
    try:
        history = gitops.commit_history(cfg.root)
    except AssentError:
        return None
    subjects = {subject for _commit, _parents, subject in history}

    owners = [
        task for task in plan.tasks
        if task.status == "DONE"
        and not any(s.startswith(f"auto({cfg.tasks_name}/{task.id}): ")
                    for s in subjects)
        and not gitops.changes_outside_scope(
            cfg.root, task.scope, excludes=_task_excludes(cfg, task))
    ]
    return owners[0] if len(owners) == 1 else None


def _mark_recovered_task(task: Task, now: Callable[[], datetime]) -> None:
    """Persist a crash-recovered candidate as WIP and journal the scope-verified recovery.

    Unlike ``_mark_interrupted_task`` there is no AI session at this point, so no
    ``agent`` / ``requested_model`` / ``requested_effort`` identity exists -- a hard crash
    leaves those genuinely unknown, and no fake session identity is fabricated to fill them
    (``append_entry`` accepts a ``scheduler`` entry with those fields omitted).  BLOCKED is
    preserved as a legal terminal state (though ``next_task()`` never yields a BLOCKED
    candidate); a secondary write error is only warned about so it never masks the recovery.
    """
    summary = ("Recovered a dirty worktree from an unclean process exit; "
               f"scope-verified against {task.id}, progress kept")
    try:
        fresh = parse_task_file(task.path)
        if fresh.status != "BLOCKED":
            set_status(task.path, "WIP")
    except Exception as e:  # recovery must not mask itself with a secondary status-write error
        print(f"Writing back the recovered task status failed: {e} (working tree left as is, nothing discarded)")

    try:
        append_entry(
            task.journal_path, by="scheduler", event="interrupt",
            summary=summary,
            detail=("run startup detected a dirty worktree from an unclean process "
                    "exit and scope-verified it against the resumable candidate"),
            time_str=now().isoformat(timespec="seconds"))
    except Exception as e:  # status and journal are attempted independently; one failing does not block the other
        print(f"Writing the recovery journal failed: {e} (working tree left as is, nothing discarded)")


def _mark_interrupted_task(task: Task, session: SessionIdentity, summary: str,
                           now: Callable[[], datetime], *, detail: str) -> None:
    """On abort, persist the current task as WIP and write a machine journal entry; a
    secondary error is only warned about.

    BLOCKED is a legal terminal state the execution AI can produce and must not be overwritten
    by the interrupt handling. DONE is not yet a trusted terminal state until the scheduler's
    acceptance passes, so it is still reverted to WIP, letting the next run resume it and pass
    the gates again.
    """
    try:
        fresh = parse_task_file(task.path)
        if fresh.status != "BLOCKED":
            set_status(task.path, "WIP")
    except Exception as e:  # abort cleanup must not mask the original exit code with a secondary error
        print(f"Writing back the interrupted task status failed: {e} (working tree left as is, nothing discarded)")

    try:
        append_entry(
            task.journal_path, by="scheduler", event="interrupt",
            summary=summary, detail=detail,
            agent=session.agent, requested_model=session.requested_model,
            requested_effort=session.requested_effort,
            time_str=now().isoformat(timespec="seconds"))
    except Exception as e:  # status and journal are attempted independently; one failing does not block the other
        print(f"Writing the interrupt journal failed: {e} (working tree left as is, nothing discarded)")


def _mark_billing_task(task: Task, session: SessionIdentity, detail: str,
                       now: Callable[[], datetime]) -> None:
    """On a billing abort, persist the task as WIP and write a distinct billing journal entry.

    The status is left unresolved (WIP, never BLOCKED) so the next run resumes it cleanly once
    the account is topped up; the work already produced stays in the tree.  The summary names
    the manual top-up requirement so it is unmistakable in the journal, separate from a normal
    acceptance failure.  A secondary error here is only warned about, never masking the abort.
    """
    try:
        fresh = parse_task_file(task.path)
        if fresh.status != "BLOCKED":
            set_status(task.path, "WIP")
    except Exception as e:  # abort cleanup must not mask the billing abort with a secondary error
        print(f"Writing back the billing task status failed: {e} (working tree left as is, nothing discarded)")

    try:
        append_entry(
            task.journal_path, by="scheduler", event="billing",
            summary=("Aborted: account balance/credit exhausted; this needs a manual "
                     "top-up, then rerun to resume (no retry consumed, progress kept)"),
            detail=detail,
            agent=session.agent, requested_model=session.requested_model,
            requested_effort=session.requested_effort,
            time_str=now().isoformat(timespec="seconds"))
    except Exception as e:  # status and journal are attempted independently; one failing does not block the other
        print(f"Writing the billing journal failed: {e} (working tree left as is, nothing discarded)")


def _handle_main_tree_escape(cfg: Config, task: Task, baseline: set[str],
                             now: Callable[[], datetime]) -> str | None:
    """Detect and, where possible, port back paths a just-finished session wrote into the main
    tree (``cfg.source_root``) instead of its isolated worktree (``cfg.root``).

    ``baseline`` is the main tree's dirty-path snapshot taken immediately before the session
    started; the diff against a fresh snapshot is what the session wrote. No new dirt ->
    returns None and the caller proceeds exactly as before (byte-for-byte identical to a run
    without this check). Any new dirt always makes this attempt's evaluation fail -- a session
    that wrote outside its isolated worktree cannot be trusted to have produced attributable,
    verifiable output, regardless of what verify/status on the worktree side would otherwise
    have said:
    - every escaped path inside this task's scope -> ported into the worktree and restored in
      the main tree (all-or-nothing), with a mechanical scheduler journal record of exactly
      what moved;
    - any escaped path outside scope -> unattributable, main tree left untouched entirely;
    - a proven in-scope path that still fails to port (e.g. the worktree copy already
      diverged) -> fail closed, both trees left untouched, needs a human to port manually.

    Known limitation (accepted): under parallel folder runs, scope attribution of concurrent
    main-tree dirt is heuristic; overlapping scope between parallel tasks can misattribute a
    path, but the fail-closed branch above guarantees it never silently corrupts the main
    tree's content.
    """
    current = gitops.dirty_paths(cfg.source_root, _task_excludes(cfg, task))
    escaped = sorted(current - baseline)
    if not escaped:
        return None

    outside = set(gitops.changes_outside_scope(
        cfg.source_root, task.scope, excludes=_task_excludes(cfg, task)))
    outside_escaped = sorted(outside & set(escaped))
    if outside_escaped:
        shown = ", ".join(outside_escaped[:5]) + (" ..." if len(outside_escaped) > 5 else "")
        return (f"session wrote outside the isolated worktree, outside this task's scope "
                f"(main tree not touched): {shown}")

    ok, apply_reason = gitops.port_back_main_tree_escape(
        cfg.source_root, cfg.root, escaped)
    if not ok:
        return (f"session wrote outside the isolated worktree; automatic port-back failed "
                f"({apply_reason}); main tree and worktree left unchanged, port back manually")

    shown = ", ".join(escaped[:5]) + (" ..." if len(escaped) > 5 else "")
    append_entry(
        task.journal_path, by="scheduler", event="main_tree_escape",
        summary=(f"Ported {len(escaped)} path(s) that escaped into the main tree back "
                 "into the isolated worktree"),
        detail=f"paths ported back and restored in the main tree: {', '.join(escaped)}",
        time_str=now().isoformat(timespec="seconds"))
    return f"session wrote outside the isolated worktree; changes ported back ({shown})"


def _process_task(cfg: Config, task: Task, rotation: _AdapterRotation,
                  sleep: Callable[[float], None],
                  now: Callable[[], datetime], session_state: _SessionState,
                  resumed: bool = False) -> None:
    """Run a single task's full lifecycle; internally handles quota waiting and retries, and by
    the end the task is DONE/BLOCKED.

    `task` is the trusted version parsed at task-selection time (= the previous checkpoint):
    scope/verify and all fields are taken from it, and the only legal change the execution AI
    may make to the on-disk version is the status line (compared in _evaluate).
    """
    print(f"\nTask {task.id}: {task.title}")
    if resumed:
        print("  (WIP detected: task interrupted last time, resuming with a continue prompt)")

    # The HEAD at this task's start: the scope check must cover all changes since the start
    # (including wip checkpoints).
    start_ref = gitops.head_ref(cfg.root)

    attempts_used = 0
    failure_reason: str | None = None
    while True:
        adapter = rotation.adapter
        adapter_name = rotation.name
        session = resolve_session(cfg, adapter, task, adapter_name)
        session_state.identity = session
        prompt = _build_prompt(cfg, task, failure_reason, session, resumed)
        print(_session_line(adapter_name, task, session))
        main_tree_baseline = (gitops.dirty_paths(cfg.source_root, _task_excludes(cfg, task))
                              if cfg.source_root is not None else None)
        result = adapter.run_task(
            prompt, session.requested_model, session.requested_effort, cfg.root)
        if not result.quota_exhausted:
            rotation.session_opened()
        escape_reason = (
            _handle_main_tree_escape(cfg, task, main_tree_baseline, now)
            if main_tree_baseline is not None else None)

        if escape_reason is not None:
            print(f"  {escape_reason}")
            outcome, reason = "fail", escape_reason
        elif result.quota_exhausted:  # quota exhaustion does not count as a failure
            print("  Quota exhausted -> keep progress (wip checkpoint).")
            wait_kind: str | None = None
            if len(rotation.names) == 1:
                quota_summary = (
                    "Quota exhausted; progress kept, waiting for quota reset "
                    "before resuming")
                quota_action = "  Waiting for quota reset before resuming..."
                wait_kind = "quota"
            else:
                cycle_exhausted = rotation.advance_after_quota()
                if cycle_exhausted:
                    quota_summary = (
                        "Quota exhausted; progress kept, every adapter in the "
                        "rotation is quota-exhausted; waiting for rotation poll "
                        f"before continuing with {rotation.name}")
                    quota_action = (
                        "  Every adapter in the rotation is quota-exhausted; "
                        f"waiting {cfg.rotation_poll_minutes} minute(s) before "
                        f"continuing with {rotation.name}.")
                    wait_kind = "rotation"
                else:
                    quota_summary = (
                        "Quota exhausted; progress kept, switching immediately "
                        f"to adapter {rotation.name}")
                    quota_action = (
                        f"  Switching adapter {adapter_name} -> {rotation.name} "
                        "immediately; resuming the same task without "
                        "consuming a retry.")
            append_entry(task.journal_path, by="scheduler", event="quota",
                         summary=quota_summary,
                         agent=session.agent,
                         requested_model=session.requested_model,
                         requested_effort=session.requested_effort,
                         time_str=now().isoformat(timespec="seconds"))
            if gitops.commit_if_dirty(
                    cfg.root, _checkpoint_subject(
                        cfg, "wip", task, "quota interrupt, progress kept"),
                    cfg.git_excludes):
                print("  wip checkpoint created.")
            try_write_report(cfg)
            print(quota_action)
            if wait_kind == "quota":
                _wait_for_quota(cfg, result.reset_at, sleep, now)
            elif wait_kind == "rotation":
                _wait_for_rotation(cfg, sleep)
            resumed = True
            continue  # resume the same task, without counting a retry
        elif result.failure_kind == "billing":
            # A zero prepaid balance is an account-level condition, not a per-task one:
            # retrying cannot fix it and every following TODO task would fail identically.
            # Abort the whole run here (no retry consumed) so the abort handler keeps this
            # task's progress and leaves it unresolved for a clean resume after a top-up.
            reason = _adapter_failure_reason(
                result.exit_code, result.stalled, result.output,
                result.failure_kind)
            print(f"  Adapter failure: {reason}")
            raise _BillingAbort(reason)
        elif result.exit_code != 0 or result.stalled:
            reason = _adapter_failure_reason(
                result.exit_code, result.stalled, result.output,
                result.failure_kind)
            _append_adapter_failure_entry(
                task, session, result.exit_code, result.stalled, reason, now,
                failure_kind=result.failure_kind)
            print(f"  Adapter failure: {reason}")

            # A failed subprocess cannot authorize either terminal result or run
            # verification.  Safety inspection still runs against its preserved changes so
            # that a later scheduler BLOCKED record retains every material failure reason.
            _fresh, safety_reason = _inspect_task_safety(
                cfg, task, start_ref)
            if safety_reason:
                reason = f"{reason}; safety inspection failed: {safety_reason}"
            outcome = "fail"
        else:
            outcome, reason = _evaluate(cfg, task, start_ref)
        if outcome == "done":
            print("  Acceptance passed -> creating checkpoint")
            if not gitops.commit_if_dirty(
                    cfg.root, _checkpoint_subject(
                        cfg, "auto", task, _short(task.title) or "done"),
                    cfg.git_excludes):
                print("  (no new changes in the working tree; progress is already in a prior wip checkpoint)")
            try_write_report(cfg)
            return
        if outcome == "self_blocked":
            print("  Execution AI self-marked BLOCKED (legal output, handed to a human) -> creating checkpoint")
            gitops.commit_if_dirty(
                cfg.root, _checkpoint_subject(
                    cfg, "auto", task, "BLOCKED (execution AI self-marked)"),
                cfg.git_excludes)
            try_write_report(cfg)
            return

        # outcome == "fail": no restore (output kept), retry with the reason; once retries are
        # exhausted the scheduler marks BLOCKED, and the not-yet-passing work is committed
        # together with the BLOCKED mark for a human to make the final call.
        print(f"  Acceptance failed: {reason}")
        if attempts_used < cfg.retry_per_task:
            attempts_used += 1
            failure_reason = reason
            print(f"  Keeping existing work, retrying with the failure reason (attempt {attempts_used})...")
            continue
        print("  Retries exhausted -> scheduler marks BLOCKED (the not-yet-passing work is kept too)")
        _mark_blocked(cfg, task, session, reason or "acceptance failed", now,
                      attempts=attempts_used)
        try_write_report(cfg)
        return


def _inspect_task_safety(cfg: Config, task: Task,
                         start_ref: str | None = None) -> tuple[Task | None, str | None]:
    """Re-parse and inspect the non-model structural and scope safety floors.

    The checks deliberately collect both task-file tampering and out-of-scope changes when
    possible so an adapter failure does not hide independent safety evidence.
    """
    try:
        fresh = parse_task_file(task.path)
    except AssentError as e:
        return None, ("Re-parsing the task file failed (the execution AI may have broken "
                      f"it): {e}")

    issues: list[str] = []
    tampered = same_except_status(task, fresh)
    if tampered:
        issues.append(
            f"Task file fields other than status were modified: {', '.join(tampered)}"
            " (the execution AI may only change the status line)")

    outside = gitops.changes_outside_scope(
        cfg.root, task.scope, since_ref=start_ref,
        excludes=_task_excludes(cfg, task))
    if outside:
        shown = ", ".join(outside[:5]) + (" ..." if len(outside) > 5 else "")
        issues.append(f"Changes outside scope appeared: {shown}")
    return fresh, "; ".join(issues) if issues else None


def _evaluate(cfg: Config, task: Task,
              start_ref: str | None = None) -> tuple[str, str | None]:
    """Acceptance: structural/scope safety -> status -> verify. Returns
    (outcome, reason).

    outcome in {"done", "self_blocked", "fail"}. scope/verify and all fields come from the
    trusted checkpoint version `task`; the on-disk version is only allowed to change the
    status line.
    """
    fresh, safety_reason = _inspect_task_safety(cfg, task, start_ref)
    if safety_reason:
        return "fail", safety_reason
    assert fresh is not None

    # status check
    if fresh.status == "BLOCKED":
        return "self_blocked", None
    if fresh.status != "DONE":
        # Structure and scope are already clean here; the only remaining acceptance gap is
        # the status line. Probe the focused verify once (quietly, so a still-failing verify
        # leaves this path's output byte-for-byte identical to before) to tell a genuine
        # implementation gap apart from a session that simply dropped off before closeout.
        if _run_verify_quiet(cfg, task.verify) == 0:
            return "fail", _CLOSEOUT_ONLY_REASON_TEMPLATE.format(
                status=fresh.status, verify_command=task.verify)
        return "fail", f"Status not updated to DONE/BLOCKED (currently {fresh.status})"

    # verify command (against the trusted checkpoint verify)
    rc = _run_verify(cfg, task.verify)
    if rc != 0:
        return "fail", f"Verify command exit code is non-zero (={rc}): {task.verify}"

    # A session handed the bounded shared-path review clause must have run the
    # controlled operation; a source snapshot that is still UNKNOWN or STALE is
    # refused with a precise retry reason rather than closed out.
    try:
        contract = _shared_paths_contract(cfg)
    except AssentError as e:
        return "fail", f"Shared-path contract could not be classified: {e}"
    refusal = shared_paths.closeout_refusal(contract)
    if refusal:
        return "fail", refusal[:1].upper() + refusal[1:]

    return "done", None


def _verify_subprocess(cfg: Config, command: str) -> subprocess.CompletedProcess:
    """Run verify in the target working tree and return the completed process (no output).

    For isolated runs the default script is loaded from the main tree by absolute path; other
    commands keep their original shell semantics; the cwd for both is the current target
    working tree.
    """
    if cfg.source_root is not None and command.strip() == _DEFAULT_VERIFY_COMMAND:
        return subprocess.run(
            [sys.executable, str((cfg.assent_dir / "verify.py").resolve())],
            cwd=str(cfg.root), capture_output=True, encoding="utf-8",
            errors="replace")
    return subprocess.run(
        command, shell=True, cwd=str(cfg.root),
        capture_output=True, encoding="utf-8", errors="replace")


def _run_verify(cfg: Config, command: str) -> int:
    """Run verify in the target working tree; exit code 0 = pass. Echoes the command and
    result (with a failure tail) to stdout."""
    print(f"  verify: {command}")
    result = _verify_subprocess(cfg, command)
    if result.returncode != 0:
        print(f"  verify failed (exit {result.returncode})")
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-8:]
        if tail:
            print("  -- verify output (tail) --")
            for line in tail:
                print(f"  | {line}")
    else:
        print("  verify passed (exit 0)")
    return result.returncode


def _verify_focused_locked(cfg: Config) -> int:
    """Run the distinct DONE-task checks from one folder's source worktree.

    Focused verification is deliberately separate from receipt-producing full
    verification.  It only proves that the task-level commands pass in the
    folder's own source worktree, so it never creates an integration candidate
    or touches a verification receipt.
    """
    folder = cfg.tasks_name
    main = gitops.main_worktree(cfg.root)
    source = gitops.resolve_folder_source(main, folder, cfg.git_excludes)
    source_cfg = cfg.for_worktree(source.worktree)

    commands: list[str] = []
    seen: set[str] = set()
    for task in Plan.parse(cfg.tasks_dir).tasks:
        if task.status != "DONE" or task.verify in seen:
            continue
        seen.add(task.verify)
        commands.append(task.verify)
    if not commands:
        raise AssentError(
            f"folder {folder} has no DONE task with an eligible focused verify "
            "command")

    # --focus provisions the persistent source worktree like every other verify
    # entry point, and writes no receipt of any kind.
    shared_paths.prepare_sources(main, [(folder, source.worktree)])
    print(f"verify {folder} --focus: source worktree {source.worktree}")
    print("verify --focus: focused task verification cannot authorize `accept`; "
          "complete integration verification has not run")
    for command in commands:
        if _run_verify(source_cfg, command) != 0:
            print(f"verify {folder} --focus: failed; this focused result cannot "
                  "authorize `accept`")
            return 1
    print(f"verify {folder} --focus: passed; complete integration verification "
          "has not run and this result cannot authorize `accept`")
    return 0


def verify_focused(cfg: Config) -> int:
    """Run one folder's eligible focused task checks without making receipts."""
    folder = cfg.tasks_name
    try:
        with lockfile.hold_lock(cfg.tasks_dir, folder):
            return _verify_focused_locked(cfg)
    except lockfile.LockBusy as e:
        print(f"verify {folder} --focus: refused ({e})")
        return 1
    except AssentError as e:
        print(f"verify {folder} --focus: failed ({e})")
        return 1


def _run_verify_quiet(cfg: Config, command: str) -> int:
    """Run verify without printing anything; exit code 0 = pass.

    Used for the closeout-only probe in ``_evaluate`` so that a still-failing verify leaves
    the caller's console output unchanged from before this probe existed.
    """
    return _verify_subprocess(cfg, command).returncode


def _mark_blocked(cfg: Config, task: Task, session: SessionIdentity, reason: str,
                  now: Callable[[], datetime], attempts: int | None = None) -> None:
    """The scheduler marks the task BLOCKED + appends a machine record to the r file + creates a checkpoint.

    The working tree may still hold work that did not pass acceptance (not restored): it is
    gathered into the BLOCKED checkpoint too, so when a human wraps up they can decide in the
    git history whether to keep or change it, and no tokens are wasted.
    """
    set_status(task.path, "BLOCKED")
    detail = (f"Still failed acceptance after {attempts} retries" if attempts
              else "Judged failed without any retry")
    append_entry(task.journal_path, by="scheduler", event="blocked",
                 summary=f"Scheduler marked BLOCKED: {reason}", detail=detail,
                 agent=session.agent,
                 requested_model=session.requested_model,
                 requested_effort=session.requested_effort,
                 time_str=now().isoformat(timespec="seconds"))
    gitops.commit_if_dirty(
        cfg.root, _checkpoint_subject(
            cfg, "auto", task, f"BLOCKED - {_short(reason, 50)}"),
        cfg.git_excludes)


def _quota_wait_seconds(cfg: Config, reset_at: datetime | None,
                        now: Callable[[], datetime]) -> float:
    """How long to wait this round for quota (seconds). If the reset time can be parsed ->
    sleep until reset + buffer (0 if already past); if it cannot -> one poll interval. A pure
    function, easy to test on its own."""
    if reset_at is not None:
        return max(0.0, (reset_at + _QUOTA_BUFFER - now()).total_seconds())
    return float(cfg.quota_poll_minutes * 60)


def _wait_for_quota(cfg: Config, reset_at: datetime | None,
                    sleep: Callable[[float], None],
                    now: Callable[[], datetime]) -> None:
    """Quota bridging: count down in place in the run's own terminal (unix-style \\r overwrites
    the same line)."""
    seconds = _quota_wait_seconds(cfg, reset_at, now)
    if reset_at is not None:
        label = f"Quota resets at {reset_at.astimezone().strftime('%H:%M:%S')}"
    else:
        label = f"Quota poll (every {cfg.quota_poll_minutes} minutes)"
    _countdown(seconds, label, sleep)


def _wait_for_rotation(cfg: Config, sleep: Callable[[float], None]) -> None:
    """Wait one fixed polling interval after every adapter exhausted its quota."""
    seconds = float(cfg.rotation_poll_minutes * 60)
    label = f"Adapter rotation poll (every {cfg.rotation_poll_minutes} minutes)"
    _countdown(seconds, label, sleep)


def _countdown(seconds: float, label: str, sleep: Callable[[float], None], *,
               tick: float = _QUOTA_TICK, segment: float = _COUNTDOWN_SEGMENT,
               stream: TextIO | None = None) -> None:
    """Countdown wait. Terminal (tty) -> update one line in place with \\r, without stacking
    lines; non-tty (redirected to a file/pipe) -> print one message, then sleep in segments of
    at most ``segment`` seconds so a stop request lands within one segment on every platform
    (see _COUNTDOWN_SEGMENT); the total wait is unchanged. The injected sleep lets tests avoid
    really sleeping.

    The segments remain the platform-independent backstop, but the production sleep is
    ``interruptible_sleep``, so a stop request ends the current segment immediately; both
    loops then stop counting down rather than sitting out the rest of a multi-hour wait
    while a KeyboardInterrupt is already pending."""
    if seconds <= 0:
        return
    clear_stop_wake()   # an earlier run's stop request must not shorten this wait
    stream = stream or sys.stdout
    interactive = hasattr(stream, "isatty") and stream.isatty()
    if not interactive:
        stream.write(f"  {label}: waiting about {int(seconds)} seconds before rerunning.\n")
        stream.flush()
        left = seconds
        while left > 0:
            step = segment if segment < left else left
            sleep(step)
            if stop_wake_requested():
                break   # the pending KeyboardInterrupt lands at the next bytecode
            left -= step
        return
    remaining = seconds

    # terminal_log.TeeTextIO provides a terminal-only channel: the countdown is a transient
    # UI and should not be written to _assent.log every second. A test/plain TextIO falls
    # back to write.
    terminal_only = getattr(stream, "write_terminal_only", None)

    def transient_write(text: str) -> None:
        if terminal_only is not None:
            terminal_only(text)
        else:
            stream.write(text)
            stream.flush()
    while remaining > 0:
        h, rem = divmod(int(remaining + 0.999), 3600)
        m, s = divmod(rem, 60)
        transient_write(f"\r  {label}: countdown {h:02d}:{m:02d}:{s:02d} before rerunning... ")
        step = tick if tick < remaining else remaining
        sleep(step)
        if stop_wake_requested():
            break
        remaining -= step
    transient_write("\r" + " " * 48 + "\r")  # clear the countdown line and return the cursor


def _print_summary(plan: Plan) -> None:
    counts = Counter(t.status for t in plan.tasks)
    print("\n===== Run summary =====")
    print(f"DONE: {counts.get('DONE', 0)}  BLOCKED: {counts.get('BLOCKED', 0)}  "
          f"SKIP: {counts.get('SKIP', 0)}  TODO: {counts.get('TODO', 0)}  "
          f"WIP: {counts.get('WIP', 0)}  ({len(plan.tasks)} tasks total)")
    blocked = [t for t in plan.tasks if t.status == "BLOCKED"]
    if blocked:
        print("BLOCKED tasks (handed to a human):")
        for t in blocked:
            print(f"  - {t.id}: {t.title}")
    if counts.get("TODO", 0) == 0 and counts.get("WIP", 0) == 0:
        print("All tasks are DONE/BLOCKED/SKIP.")
