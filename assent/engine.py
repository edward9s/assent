"""Main loop: select task -> run -> accept -> checkpoint/retry/quota wait + report generation.

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
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, TextIO

from assent import AssentError, gitops, lockfile, verification
from assent.adapters import Adapter, InvocationRequest, get_adapter
from assent.config import Config
from assent.folderdeps import (FolderBaseResolution,
                               find_unfinished_prerequisites,
                               parse_folder_dependencies,
                               resolve_folder_base)
from assent.plan import (Plan, Task, append_entry, parse_task_file,
                         read_entries, same_except_status, set_status)

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
    "requested_effort is the value actually passed to the AI CLI this run; an empty string\n"
    "means it is not passed and the CLI default applies.\n"
    "To verify yourself, run this in the current working tree: {verify_command}\n"
    "This is the focused task gate. If an outer tool times out while the child result is\n"
    "unknown, do not start a concurrent duplicate and do not mark the task BLOCKED solely\n"
    "because of that timeout; determine the child result serially. The scheduler runs the\n"
    "same command after the AI session and owns the checkpoint/retry decision.\n"
    "When done:\n"
    "1. Change the status of {task_path} to DONE or BLOCKED -- the status line is the only\n"
    "   line you may change in the whole task file.\n"
    "2. Append one [[entry]] journal record to the end of {journal_path} (TOML, with time,\n"
    "   by = \"{agent}\", requested_model = \"{requested_model}\", event, summary, detail;\n"
    "   also write requested_effort when it has a value; create the file if it does not exist).\n"
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
_DEFAULT_VERIFY_COMMAND = "python .assent/verify.py"
_GIT_REQUIRED_MESSAGE = "This project has no git repository yet; run git init first"
_ADAPTER_DIAGNOSTIC_LIMIT = 240
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)([\"']?(?:api[_ -]?key|access[_ -]?token|token|password|secret|"
    r"authorization)[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)")
_BEARER_RE = re.compile(r"(?i)(\bbearer\s+)[^\s,;]+")
_KNOWN_TOKEN_RE = re.compile(
    r"\b(?:sk(?:-ant)?|ghp|github_pat)-[A-Za-z0-9_-]{8,}\b")
_REWORK_REASON_RE = re.compile(
    r"(?:^|\n)reason: (.*?)(?=\nHEAD before operation:|\Z)", re.DOTALL)


@dataclass(frozen=True)
class _SessionIdentity:
    """The abstract choices and actual CLI identity shared by one task run."""

    agent: str
    requested_model: str
    effort: str | None
    requested_effort: str | None


@dataclass
class _SessionState:
    """Lets the outer interrupt handler read the resolved identity of the current session."""

    identity: _SessionIdentity | None = None


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
def _build_prompt(cfg: Config, task: Task, failure_reason: str | None,
                  session: _SessionIdentity, resumed: bool = False) -> str:
    template = cfg.prompt_template or _DEFAULT_PROMPT_TEMPLATE
    text = (template
            .replace("{agents_md_path}", _agents_md_path_for_prompt(cfg))
            .replace("{instructions_path}",
                     cfg.rel(cfg.assent_dir / "instructions.md"))
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


def _resolve_session(cfg: Config, adapter: Adapter,
                     task: Task) -> _SessionIdentity:
    """Resolve the identity before starting the adapter; the same result feeds the prompt,
    journal, and CLI command."""
    effort = _resolve_effort(cfg, task)
    return _SessionIdentity(
        agent=cfg.adapter_name,
        requested_model=adapter.resolve_model(task.model),
        effort=effort,
        requested_effort=_resolve_requested_effort(cfg, task.model, effort),
    )


def _planned_invocations(cfg: Config, adapter: Adapter, plan: Plan,
                         task_id: str | None = None) -> list[InvocationRequest]:
    """Resolve every invocation this run could still issue, without starting anything.

    Only tasks that can still run are resolved: a settled task will not open a session, and
    refusing a run because of a mapping a finished task once used would be noise.
    """
    requests: list[InvocationRequest] = []
    for task in plan.tasks:
        if task_id is not None and task.id != task_id:
            continue
        if task.status not in ("TODO", "WIP"):
            continue
        effort = _resolve_effort(cfg, task)
        requests.append(InvocationRequest(
            task_id=task.id, model=task.model, effort=effort,
            requested_model=adapter.resolve_model(task.model),
            requested_effort=_resolve_requested_effort(cfg, task.model, effort)))
    return requests


def _capability_errors(cfg: Config, adapter: Adapter, plan: Plan,
                       task_id: str | None = None) -> list[str]:
    """Ask the active adapter to prove every planned model/effort before anything starts.

    This is a zero-token gate: it runs before an AI session, a task checkpoint or any status
    write, so an invocation the vendor would refuse costs no quota and leaves no trace.  A
    resolution error (an unmapped tier, say) is itself a preflight failure.
    """
    try:
        requests = _planned_invocations(cfg, adapter, plan, task_id)
    except AssentError as e:
        return [str(e)]
    return adapter.preflight(requests)


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


def _append_adapter_failure_entry(task: Task, session: _SessionIdentity,
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


def _resolve_effort(cfg: Config, task: Task) -> str | None:
    """Abstract effort for the current adapter: task-file annotation wins; otherwise the
    adapter's tier default; if neither -> None (do not pass --effort).

    The current adapter's settings are looked up by name and fail closed for an unknown adapter,
    so a third adapter never inherits Claude's defaults."""
    return cfg.adapter_settings(cfg.adapter_name).resolve_effort(
        task.effort, task.model)


def _resolve_requested_effort(cfg: Config, model: str,
                              effort: str | None) -> str | None:
    """Translate the abstract effort to the actual CLI value for the current adapter, by
    "tier section > flat > identity". Unknown adapters fail closed rather than falling back to
    Claude's translation table."""
    return cfg.adapter_settings(cfg.adapter_name).resolve_requested_effort(
        model, effort)


def _task_excludes(cfg: Config, task: Task) -> list[str]:
    """The scope-check exemption list for this task: its own t file and r file (the status
    update and journal are part of the job) plus the global runtime artifacts."""
    return [cfg.git_rel(task.path), cfg.git_rel(task.journal_path),
            *cfg.git_excludes]


def _git_read(root, *args: str) -> str | None:
    """Read-only git query; a missing git or a non-zero exit returns None (used by
    status/check, so it never raises a traceback)."""
    try:
        result = subprocess.run(
            ["git", *args], cwd=str(root),
            capture_output=True, encoding="utf-8", errors="replace")
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _has_git_marker(root: Path) -> bool:
    """The project root must initialize its own git; it may not borrow a parent directory's repo."""
    return (root / ".git").exists()


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


def _worktree_configuration_errors(cfg: Config) -> list[str]:
    """The .assent management surface must stay in the main tree; it must not produce a
    second real copy inside a worktree."""
    errors: list[str] = []
    assent_path = cfg.git_rel(cfg.assent_dir)
    tracked = sorted(set(gitops.tracked_paths(cfg.root, assent_path))
                     | set(gitops.tracked_paths(cfg.root, assent_path,
                                                ref="HEAD")))
    if tracked:
        shown = ", ".join(tracked[:5]) + (" ..." if len(tracked) > 5 else "")
        errors.append(f".assent already has Git-tracked files: {shown}"
                      " (with Git enabled the whole .assent must stay in the main working tree)")
    return errors


@dataclass(frozen=True)
class _StackState:
    """Resolved base plus every direct upstream identity used to verify races."""

    base: FolderBaseResolution
    sources: tuple[gitops.FolderSourceSnapshot, ...]


def _resolve_stack_state(cfg: Config) -> _StackState:
    """Resolve a reproducible base and snapshot all direct upstream tips."""
    base = resolve_folder_base(
        cfg.root, cfg.tasks_dir, excludes=cfg.git_excludes)
    dependencies = parse_folder_dependencies(cfg.tasks_dir)
    sources = tuple(
        gitops.resolve_folder_source(cfg.root, folder, cfg.git_excludes)
        for folder in dependencies.after
    )
    if base.speculative_upstream is not None:
        matching = next(
            (source for source in sources
             if source.folder == base.speculative_upstream.folder), None)
        if matching is None or matching.tip != base.speculative_upstream.tip:
            raise AssentError(
                "upstream source changed while the stack base was being resolved")
    return _StackState(base, sources)


def _require_stack_ancestry(cfg: Config, state: _StackState,
                            downstream_tip: str) -> None:
    """Require the downstream tip to contain every current direct upstream."""
    for source in state.sources:
        if gitops.is_ancestor(cfg.root, source.tip, downstream_tip):
            continue
        raise AssentError(
            f"stale stack for {cfg.tasks_name}: current upstream "
            f"{source.folder} tip {source.tip} is not an ancestor of downstream "
            f"tip {downstream_tip}; all existing work is preserved. Run `assent "
            f"rework {cfg.tasks_name}` after deciding how to handle the upstream "
            "change, or replan the dependency")


def _prepare_worktree(cfg: Config) -> Config:
    """Create or validate the folder worktree before any adapter is started."""
    errors = _worktree_configuration_errors(cfg)
    if errors:
        detail = "\n".join(f"  - {error}" for error in errors)
        raise AssentError(f"git worktree version-control layering error:\n{detail}")

    state_before = _resolve_stack_state(cfg)
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

        state_after = _resolve_stack_state(cfg)
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
        now: Callable[[], datetime] | None = None) -> int:
    """Run tasks until all are DONE/BLOCKED/SKIP (or only one with once/task_id). Returns the
    process exit code.

    First check folder prerequisites, then take the task folder's file lock; the lock covers
    the whole run (including the long sleeps of quota waiting); if another run is already
    running in the same folder, print a message and fail with exit code 1 without touching
    anything in the working tree. status / check / report are read-only, take no lock, and can
    be used while a run is in progress.
    """
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

    if not _has_git_marker(cfg.root):
        print(_GIT_REQUIRED_MESSAGE)
        return 1

    if sleep is None:
        sleep = time.sleep
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
        result = verification.verify_folder_if_needed(cfg)
        _try_write_report(cfg)
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

    # The adapter is resolved and its planned invocations proven before the worktree exists,
    # so a configuration the vendor would refuse costs no quota, no status write and no Git
    # change at all.
    if adapter is None:
        try:
            adapter = get_adapter(cfg.adapter_name, cfg)
        except AssentError as e:
            print(str(e))
            return 1

    capability_errors = _capability_errors(cfg, adapter, plan, task_id)
    if capability_errors:
        print(f"{cfg.adapter_name} capability preflight: FAIL "
              "(refusing before any AI session)")
        for message in capability_errors:
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
            _process_task(cfg, task, adapter, sleep, now, session, resumed)
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
        _try_write_report(cfg)
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
        _try_write_report(cfg)
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
        _try_write_report(cfg)
        return 1

    _print_summary(Plan.parse(cfg.tasks_dir))
    _try_write_report(cfg)
    return 0


def _recover_or_ensure_clean(cfg: Config, now: Callable[[], datetime]) -> None:
    """Run-startup cleanliness gate, extended with a provably-attributable recovery path.

    A hard power loss or a forced kill never reaches the Ctrl+C / quota / infrastructure
    interrupt handlers, so it leaves the worktree dirty with the task status and the wip
    checkpoint unwritten.  On the next run, if every uncommitted change is provably inside the
    scope of the task ``next_task()`` would resume, that progress is gathered into a ``wip``
    checkpoint and the run continues -- no AI session, zero tokens.  Every other dirty state
    (a change outside that scope, or no resumable candidate at all) keeps today's fail-closed
    behaviour: ``ensure_clean`` raises and the caller refuses to run rather than guessing
    attribution.
    """
    if _try_recover_attributable_worktree(cfg, now):
        return
    gitops.ensure_clean(cfg.root, cfg.git_excludes)


def _try_recover_attributable_worktree(cfg: Config,
                                       now: Callable[[], datetime]) -> bool:
    """Return True only when the worktree was dirty and every change was provably attributable
    to the resumable candidate task, having just committed that progress into a wip checkpoint.

    A clean worktree, an unparsable plan, no resumable candidate, or any change outside the
    candidate's scope all return False, leaving the caller's fail-closed ``ensure_clean`` to
    decide.  Attribution reuses the same scope machinery that contains a running task's output
    (``changes_outside_scope`` with an empty ``since_ref`` -> only the current uncommitted
    changes), so the recovery can never claim work a task's scope would not have permitted.
    """
    if gitops.working_tree_status(cfg.root, cfg.git_excludes).is_clean:
        return False
    try:
        selection = Plan.parse(cfg.tasks_dir).next_task()
    except AssentError:
        return False
    if selection is None:
        return False
    candidate, _ = selection
    outside = gitops.changes_outside_scope(
        cfg.root, candidate.scope, excludes=_task_excludes(cfg, candidate))
    if outside:
        return False

    _mark_recovered_task(candidate, now)
    subject = _checkpoint_subject(
        cfg, "wip", candidate,
        "recovered dirty worktree from an unclean exit, scope-verified")
    gitops.commit_if_dirty(cfg.root, subject, cfg.git_excludes)
    print(f"Recovered a dirty worktree from an unclean process exit; scope-verified "
          f"against {candidate.id}, progress kept in a wip checkpoint.")
    return True


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


def _mark_interrupted_task(task: Task, session: _SessionIdentity, summary: str,
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


def _mark_billing_task(task: Task, session: _SessionIdentity, detail: str,
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


def _process_task(cfg: Config, task: Task, adapter: Adapter,
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
        session = _resolve_session(cfg, adapter, task)
        session_state.identity = session
        prompt = _build_prompt(cfg, task, failure_reason, session, resumed)
        print(f"  Opening session (model={task.model} -> {session.requested_model}, "
              f"effort(abstract)={session.effort or 'unspecified'} -> "
              f"requested_effort(actual)={session.requested_effort or 'CLI default'})...")
        result = adapter.run_task(
            prompt, session.requested_model, session.requested_effort, cfg.root)

        if result.quota_exhausted:  # quota exhaustion does not count as a failure
            print("  Quota exhausted -> keep progress (wip checkpoint) and wait for reset before resuming...")
            append_entry(task.journal_path, by="scheduler", event="quota",
                         summary="Quota exhausted; progress kept, waiting for reset before resuming",
                         agent=session.agent,
                         requested_model=session.requested_model,
                         requested_effort=session.requested_effort,
                         time_str=now().isoformat(timespec="seconds"))
            if gitops.commit_if_dirty(
                    cfg.root, _checkpoint_subject(
                        cfg, "wip", task, "quota interrupt, progress kept"),
                    cfg.git_excludes):
                print("  wip checkpoint created.")
            _try_write_report(cfg)
            _wait_for_quota(cfg, result.reset_at, sleep, now)
            resumed = True
            continue  # resume the same task, without counting a retry

        if result.failure_kind == "billing":
            # A zero prepaid balance is an account-level condition, not a per-task one:
            # retrying cannot fix it and every following TODO task would fail identically.
            # Abort the whole run here (no retry consumed) so the abort handler keeps this
            # task's progress and leaves it unresolved for a clean resume after a top-up.
            reason = _adapter_failure_reason(
                result.exit_code, result.stalled, result.output,
                result.failure_kind)
            print(f"  Adapter failure: {reason}")
            raise _BillingAbort(reason)

        if result.exit_code != 0 or result.stalled:
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
            _try_write_report(cfg)
            return
        if outcome == "self_blocked":
            print("  Execution AI self-marked BLOCKED (legal output, handed to a human) -> creating checkpoint")
            gitops.commit_if_dirty(
                cfg.root, _checkpoint_subject(
                    cfg, "auto", task, "BLOCKED (execution AI self-marked)"),
                cfg.git_excludes)
            _try_write_report(cfg)
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
        _try_write_report(cfg)
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


def _run_verify_quiet(cfg: Config, command: str) -> int:
    """Run verify without printing anything; exit code 0 = pass.

    Used for the closeout-only probe in ``_evaluate`` so that a still-failing verify leaves
    the caller's console output unchanged from before this probe existed.
    """
    return _verify_subprocess(cfg, command).returncode


def _mark_blocked(cfg: Config, task: Task, session: _SessionIdentity, reason: str,
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


def _countdown(seconds: float, label: str, sleep: Callable[[float], None], *,
               tick: float = _QUOTA_TICK, stream: TextIO | None = None) -> None:
    """Countdown wait. Terminal (tty) -> update one line in place with \\r, without stacking
    lines; non-tty (redirected to a file/pipe) -> print only one line to avoid flooding the
    log. The injected sleep lets tests avoid really sleeping."""
    if seconds <= 0:
        return
    stream = stream or sys.stdout
    interactive = hasattr(stream, "isatty") and stream.isatty()
    if not interactive:
        stream.write(f"  {label}: waiting about {int(seconds)} seconds before rerunning.\n")
        stream.flush()
        sleep(seconds)
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


# --------------------------------------------------------------------------- #
# report: zero-token human-readable report (the acceptance meeting's agenda)
# --------------------------------------------------------------------------- #
def render_report(cfg: Config, plan: Plan,
                  now: Callable[[], datetime] | None = None) -> str:
    """Aggregate the t/r files and git info into a one-page plain-text report. Aggregation is
    mechanical work, zero tokens."""
    if now is None:
        now = lambda: datetime.now(timezone.utc)  # noqa: E731
    counts = Counter(t.status for t in plan.tasks)

    git_root = _query_git_root(cfg)
    checkpoints: dict[str, str] = {}
    log = _git_read(git_root, "log", "--pretty=%h\t%s")
    if log:
        for line in log.splitlines():
            h, _, subject = line.partition("\t")
            for t in plan.tasks:
                prefix = f"auto({cfg.tasks_name}/{t.id}): "
                if t.id not in checkpoints and subject.startswith(prefix):
                    checkpoints[t.id] = h

    branch = _git_read(git_root, "branch", "--show-current") or "N/A"
    stamp = now().astimezone().strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        "Run report (_report.md; auto-generated by assent, do not edit by hand; regenerate: assent report)",
        "=" * 60,
        f"Plan folder: {cfg.tasks_name}",
        f"Generated at: {stamp}",
        f"Branch: {branch}",
        f"Progress: DONE {counts.get('DONE', 0)} / BLOCKED {counts.get('BLOCKED', 0)} / "
        f"WIP {counts.get('WIP', 0)} / TODO {counts.get('TODO', 0)} / "
        f"SKIP {counts.get('SKIP', 0)} ({len(plan.tasks)} total)",
        *_stack_report_lines(cfg, plan),
        "",
    ]
    for t in plan.tasks:
        mark = checkpoints.get(t.id)
        lines.append(f"{t.id}  {t.status:<8} {t.title}"
                     + (f"  [{mark}]" if mark else ""))
        if t.status in ("BLOCKED", "WIP"):
            entries = read_entries(t.journal_path)
            if entries:
                last = entries[-1]
                summary = str(last.get("summary", "")).strip()
                by = last.get("by", "?")
                if summary:
                    lines.append(f"      last journal ({by}): {summary}")
    blocked = [t for t in plan.tasks if t.status == "BLOCKED"]
    if blocked:
        lines += ["", "To decide: compare each BLOCKED task's r file and checkpoint commit, "
                      "edit the task file and set status back to TODO to continue, or mark SKIP to abandon."]
    lines += ["", *verification.receipt_report_lines(cfg)]
    return "\n".join(lines) + "\n"


def write_report(cfg: Config, plan: Plan,
                 now: Callable[[], datetime] | None = None) -> Path:
    """Write the report to the task folder's _report.md (a runtime artifact, not version-controlled)."""
    path = cfg.tasks_dir / "_report.md"
    path.write_text(render_report(cfg, plan, now), encoding="utf-8")
    return path


def _try_write_report(cfg: Config) -> None:
    """Best-effort report update at run wrap-up; a report failure never affects the main
    flow's result or exit code."""
    try:
        write_report(cfg, Plan.parse(cfg.tasks_dir))
    # This is a deliberate best-effort isolation boundary: any ordinary error including
    # permissions, file locks, and content parsing must not mask the task result;
    # KeyboardInterrupt/SystemExit still propagate as usual.
    except Exception:
        pass


def _stack_report_lines(cfg: Config, plan: Plan) -> list[str]:
    """Describe the currently derived stack without authorizing any action."""
    if all(t.status in ("DONE", "SKIP") for t in plan.tasks):
        return ["Stack base: not applicable (folder complete)"]
    try:
        state = _resolve_stack_state(cfg)
    except AssentError as e:
        return [f"Stack base: unavailable ({e})"]
    upstream = state.base.speculative_upstream
    if upstream is None:
        return ["Stack base: current target main",
                "Speculative upstream: none (all direct upstreams accepted)"]
    return [f"Stack base: {state.base.resolved_base}",
            f"Speculative upstream: {upstream.folder} @ {upstream.tip} (unaccepted)"]


def report(cfg: Config) -> int:
    """Subcommand: generate _report.md and print it to the terminal (zero tokens)."""
    try:
        plan = Plan.parse(cfg.tasks_dir)
    except AssentError as e:
        print(f"Failed to parse task folder: {e}")
        return 1
    text = render_report(cfg, plan)
    path = write_report(cfg, plan)
    print(text, end="")
    print(f"(written to {path})")
    return 0


# --------------------------------------------------------------------------- #
# status: zero-token progress query
# --------------------------------------------------------------------------- #
def status(cfg: Config) -> int:
    try:
        plan = Plan.parse(cfg.tasks_dir)
    except AssentError as e:
        print(f"Failed to parse task folder: {e}")
        return 1

    counts = Counter(t.status for t in plan.tasks)
    print(f"Task folder: {cfg.tasks_dir}")
    print(f"Progress: DONE {counts.get('DONE', 0)} / BLOCKED {counts.get('BLOCKED', 0)} / "
          f"SKIP {counts.get('SKIP', 0)} / WIP {counts.get('WIP', 0)} / "
          f"TODO {counts.get('TODO', 0)} ({len(plan.tasks)} total)")

    git_root = _query_git_root(cfg)
    branch = _git_read(git_root, "branch", "--show-current")
    print(f"Current branch: {branch or 'N/A'}")
    last = _git_read(git_root, "log", "-1", "--grep=^auto(", "--pretty=%h %s")
    print(f"Last checkpoint: {last or '(no auto() commit yet)'}")
    for line in _stack_report_lines(cfg, plan):
        print(line)

    selected = plan.next_task()
    if selected is not None:
        nxt, resumed = selected
        try:
            effort = _resolve_effort(cfg, nxt)
            requested_effort = _resolve_requested_effort(cfg, nxt.model, effort)
            effort_label = (f"{effort} -> {requested_effort}" if effort
                            else "CLI default")
        except AssentError as e:
            effort_label = f"unavailable ({e})"
        tag = " (WIP resume)" if resumed else ""
        print(f"Next task: {nxt.id} [{nxt.model} / {effort_label}] "
              f"{nxt.title}{tag}")
    elif counts.get("TODO", 0):
        print("Next task: (TODO remains, but blocked by unfinished prerequisites or a BLOCKED task)")
    else:
        print("Next task: (none, all DONE/BLOCKED/SKIP)")
    return 0


def _query_git_root(cfg: Config) -> Path:
    """When a valid worktree already exists, read git info from the isolated branch instead."""
    if cfg.source_root is not None:
        return cfg.root
    candidate = gitops.worktree_path(cfg.root, cfg.tasks_name)
    top = _git_read(candidate, "rev-parse", "--show-toplevel")
    if top and Path(top).resolve() == candidate.resolve():
        return candidate
    return cfg.root


# --------------------------------------------------------------------------- #
# check: zero-token environment and format validation (the meeting's adjourn condition)
# --------------------------------------------------------------------------- #
def check(cfg: Config) -> int:
    if not _has_git_marker(cfg.root):
        print(_GIT_REQUIRED_MESSAGE)
        return 1

    ok = True
    print(f"Config: OK ({cfg.assent_dir / 'assent.toml'} loaded, "
          f"task folder = {cfg.tasks_name})")

    # Task folder and task-file format (parsing is the full check: required fields, tiers,
    # non-empty scope, deps exist without cycles, no duplicate ids)
    plan: Plan | None = None
    try:
        plan = Plan.parse(cfg.tasks_dir)
        print(f"Task-file format: OK ({len(plan.tasks)} tasks, dependencies acyclic)")
    except AssentError as e:
        ok = False
        print(f"Task-file format: FAIL ({e})")

    # Dependency declaration format and reference integrity of the selected folder; the
    # whole-graph cycle check is validated by the no-argument CLI check.
    try:
        dependencies = parse_folder_dependencies(cfg.tasks_dir)
        after = ", ".join(dependencies.after) or "none"
        print(f"Folder dependencies: OK (after = {after})")
    except AssentError as e:
        ok = False
        print(f"Folder dependencies: FAIL ({e})")

    # adapter resolves
    adapter: Adapter | None = None
    try:
        adapter = get_adapter(cfg.adapter_name, cfg)
        print(f"adapter: OK ({cfg.adapter_name})")
    except AssentError as e:
        ok = False
        print(f"adapter: FAIL ({e})")

    # Every model/effort the plan could still send, proven against the active adapter's
    # capability contract; the same gate `run` applies before opening a session.
    if adapter is not None and plan is not None:
        capability_errors = _capability_errors(cfg, adapter, plan)
        if capability_errors:
            ok = False
            print(f"{cfg.adapter_name} capability preflight: FAIL")
            for message in capability_errors:
                print(f"  - {message}")
        else:
            print(f"{cfg.adapter_name} capability preflight: OK")

    # git repo
    inside = _git_read(cfg.root, "rev-parse", "--is-inside-work-tree")
    if inside == "true":
        print("git repo: OK")
        try:
            errors = _worktree_configuration_errors(cfg)
        except AssentError as e:
            errors = [str(e)]
        if errors:
            ok = False
            print("worktree version-control layering: FAIL")
            for error in errors:
                print(f"  - {error}")
        else:
            print("worktree version-control layering: OK")
    else:
        ok = False
        print("git repo: FAIL (project root is not a git working tree, or git is not installed/on PATH)")

    # The current adapter's CLI is executable (the probe is provided by the adapter itself, so
    # this is not hardcoded to only claude/codex; an unresolved adapter has already failed above)
    if adapter is not None:
        label = cfg.adapter_name
        probe_ok, message = adapter.probe_cli()
        if probe_ok:
            print(f"{label} CLI: OK ({message})")
        else:
            ok = False
            print(f"{label} CLI: FAIL ({message})")

    print("Result: passed" if ok else "Result: some items failed")
    return 0 if ok else 1
