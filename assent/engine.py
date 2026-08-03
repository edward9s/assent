"""Main loop: select task -> run -> accept -> checkpoint/retry/quota wait.

The pre-session decisions this loop shares with the read-only query commands
live in ``assent.preflight``; the report it refreshes as a best-effort side
effect is rendered by ``assent.inspection``.  Both are imported here and neither
imports back, so the query commands stay loadable without the scheduler.

Iron rules inherited from field experience on the workflow project: **never discard
output the execution AI has already burned tokens to produce.**
- Quota or checkpoint-resume interrupted -> keep progress in a wip checkpoint, then resume with
  a "continue" prompt instead of scrapping and rerunning.
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

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Callable, TextIO

from assent import (AssentError, auto_fix, contracts, gitops, lockfile, rework,
                    shared_paths, verification)
from assent.adapters import Adapter, get_adapter
from assent.adapters.process import (clear_stop_wake, interruptible_sleep,
                                     stop_wake_requested)
from assent.config import Config
from assent.folderdeps import find_unfinished_prerequisites
from assent.inspection import try_write_report
from assent.plan import (Plan, Task, append_entry, parse_task_file,
                         read_entries, same_except_status, set_status,
                         add_scope_entries, scope_text_without_entries,
                         scope_text_with_entries, task_text_sha256)
from assent.preflight import (GIT_REQUIRED_MESSAGE, SessionIdentity,
                              StackState, auto_fix_fixer_capability_errors,
                              auto_fix_review_capability_errors, capability_errors,
                              has_git_marker, resolve_session, resolve_stack_state,
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
_RESUME_SUFFIX = ("\nThe previous adapter session was interrupted (quota exhausted, "
                  "checkpoint-resume control, or user "
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

_AUTO_FIX_REVIEW_PROMPT = """You are the read-only Assent folder reviewer.

Review context: {review_context}
Review stage: {review_stage}

This is a blocking decision gate, never an implementation session. Do not
edit, create, delete, rename, format, or otherwise write any project or
management-plane file. Do not run tests, formatters, generators, or any other
command that may write. Perform read-only inspection and rely on the exact
scheduler-supplied focused evidence below. This cooperative write check does
not make the worktree a security sandbox and cannot intercept effects outside
the project.

Complete verification has deliberately not run yet. The absence of a full
suite result, integration candidate, verification receipt, or any other
complete-verification evidence is never a finding. Do not run or request
complete verification.

{context_policy}

{stage_policy}

Review all of the following before deciding:
- the scheduler-supplied cumulative checkpoint diff
- every authoritative checkpoint task contract and relevant journal below
- any current on-disk task text explicitly labelled UNTRUSTED evidence
- the exact scheduler blocker reasons and focused-command evidence
- the remaining folder dependency state
- implementation and tests named by those contracts, plus directly necessary
  interacting code encountered through read-only inspection

Report only blocking correctness, safety, unmet-requirement, or focused-test-gap
findings allowed by the context and stage policies. A focused-test-gap finding
must identify one concrete task requirement that lacks a local, reliably
runnable focused regression test. Speculation, uncertainty without evidence,
idealized design, style, preference, optional improvement, and unrelated scope
expansion cannot block PASS.

Finish with exactly one provider-neutral `assent.auto_fix_review` JSON object
on the last non-empty output line and no later text. PASS has an empty findings
array. Every FAIL finding must supply all schema fields: kind, task_id, path,
summary, evidence, recommendation, scope_addition, transition,
prior_fingerprint, and transition_evidence. Use null where an optional field is
absent.

When the blocker is exactly an omitted task scope path, use kind
"scope_amendment", name the existing task_id, and make path and
scope_addition.path the same normalized exact project-relative file path.
scope_addition.path_state must be "existing_file" for an existing ordinary
file or "new_file" for an absent leaf below an existing ordinary directory.
Never propose a glob, directory, control/management path, removal, verifier
change, new task, or unrelated scope expansion. The scheduler validates and
persists an accepted exact addition before any fixer session.

Folder: {folder}
Source tree: {source_tree}
Base commit: {base_ref}

Scheduler-supplied cumulative checkpoint diff:
{cumulative_diff}

Exact durable blocker evidence:
{blocker_evidence}

Scheduler-supplied focused evidence:
{focused_evidence}

Remaining folder dependency state:
{dependency_state}

Prior review and repair evidence:
{prior_evidence}

Task contracts and journals:
{management_evidence}
"""

_AUTO_FIX_COMPLETED_POLICY = """This COMPLETED_FOLDER review examines the
bounded cumulative implementation. Only COMPLETED_FOLDER + INITIAL may report
eligible technical debt: it must be concrete, encountered in changed or
directly interacting code, local to an existing task's declared scope, and
reliably testable by that task's focused gate. Do not conduct a repository-wide
debt search."""

_AUTO_FIX_BLOCKED_POLICY = """This BLOCKED_ADJUDICATION reviews only the durable
blockers, their trusted requirements, and directly necessary interacting code.
Unfinished tasks, TODO work, and the incomplete folder state are expected and
cannot be findings. Technical debt is not eligible in this context. A
self-marked BLOCKED task legitimately has no focused result because ordinary
closeout skips that gate; that absence is not a finding."""

_AUTO_FIX_INITIAL_POLICY = """This INITIAL review has no prior reviewer finding
to resolve. Findings must use transition = \"initial\" and must not cite a prior
fingerprint."""

_AUTO_FIX_RECHECK_POLICY = """This RECHECK must decide the prior current
findings first. Omit resolved findings. Reproduce a still-present finding
without semantic rewording, using transition = \"still_present\" and its exact
scheduler prior_fingerprint. A new blocker is allowed only as a concrete repair
regression (transition = \"repair_regression\") or a newly exposed violation of
an existing task requirement (transition = \"newly_exposed\"), with concrete
transition_evidence. For repair_regression, that evidence must name the exact
path changed by the repair. For newly_exposed, it must name the existing task id
and whether the origin is its goal, behavior, acceptance, or other requirement.
Repeated technical-debt discovery is forbidden. When no
prior blocker remains and no qualifying new blocker is evidenced, the evidence
is sufficient and you must immediately PASS rather than continue searching for
improvements."""

_AUTO_FIX_REPAIR_SUFFIX = """

This is an Assent-authorized bounded auto-fix attempt. Preserve the current
implementation and repair the findings owned by this task; do not create tasks,
change task requirements or scope, revert code, accept work, or delete sources.
The exact durable repair brief is reproduced below. Re-evaluate current code
rather than assuming a finding is still present, and run this task's ordinary
focused gate before closeout.

Durable repair brief (preserved verbatim across restart):
{repair_brief}

Your closeout journal detail must contain exactly one line for every fingerprint
listed under Current findings, with no unknown or duplicate fingerprint:
ASSENT_REPAIR_DISPOSITION {{"fingerprint":"<scheduler fingerprint>","disposition":"fixed|not_reproducible|still_blocked","detail":"<nonempty concrete evidence or reason>"}}
Use only those three JSON string fields. DONE permits fixed or
not_reproducible; still_blocked requires BLOCKED. These acknowledgement lines
do not replace any structural, scope, focused, or independent-review gate.
"""

_AUTO_FIX_EVIDENCE_LIMIT = 64_000

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
    """Lets the outer interrupt handler read the current session's closeout state."""

    identity: SessionIdentity | None = None
    terminal_checkpoint: bool = False
    terminal_checkpoint_attempt: tuple[str, str] | None = None


@dataclass
class _ActiveTask:
    """Mutable interrupt witness shared by normal and auto-fix task loops."""

    task: Task | None = None
    session: _SessionState | None = None


@dataclass(frozen=True)
class _AutoFixReviewOutcome:
    """One final-focused/reviewer cycle and its durable repair evidence."""

    code: int
    state: auto_fix.AutoFixState | None = None
    human_reason: str | None = None


@dataclass(frozen=True)
class _AutoFixBlockerEvidence:
    """One terminal task failure supplied to blocked adjudication."""

    task: Task
    trigger: str
    reason: str
    focused_evidence: str
    worker_summary: str | None = None


class _AdapterProcessCreationError(OSError):
    """The adapter raised synchronously instead of returning a child result."""


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
                  session: SessionIdentity,
                  model: str | None = None) -> str:
    """The one opening line that states the whole resolved session identity.

    Four facts, in the order they are decided: which adapter runs, and each abstract choice
    beside the concrete value actually sent to that adapter's CLI, e.g.
    ``Session: codex | core->gpt-5.6-luna | heavy->max``.
    """
    return (f"  Session: {adapter_name} | {model or task.model}->{session.requested_model}"
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
        auto_fix_adapter: Adapter | None = None,
        auto_fix: bool = False,
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
            result = _run_locked(
                cfg, once, task_id, adapter, auto_fix_adapter, auto_fix,
                sleep, now)
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
                auto_fix_adapter: Adapter | None,
                auto_fix_enabled: bool,
                sleep: Callable[[float], None],
                now: Callable[[], datetime]) -> int:
    """The actual run body, after the task folder lock is held."""
    try:
        # Validate the requested folder itself before stack discovery.  This
        # preserves the task-file error as the primary zero-token diagnostic.
        plan = Plan.parse(cfg.tasks_dir)
        trusted_plan = plan
        trusted_contracts = _task_contract_snapshots(plan)
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

    active = _ActiveTask()
    blocker_evidence: list[_AutoFixBlockerEvidence] = []
    try:
        # Read the durable state even for an ordinary run.  A pending FAIL is
        # not an additional task status and ordinary execution may still make
        # limited progress, but a complete folder must not silently close over
        # unresolved review evidence just because the invocation omitted the
        # repair authorization.
        existing_auto_fix = _auto_fix_existing_state(cfg)
        resuming_auto_fix = bool(
            auto_fix_enabled
            and existing_auto_fix is not None
            and existing_auto_fix.verdict == "FAIL")
        if resuming_auto_fix:
            assert existing_auto_fix is not None
            recovery_error = _auto_fix_recovery_config_error(
                cfg, existing_auto_fix)
            if recovery_error is not None:
                print(f"Auto-fix recovery refused: {recovery_error}.")
                return 1
        review_enabled = auto_fix_enabled and cfg.auto_fix_review is not None
        while not resuming_auto_fix:
            current_plan = Plan.parse(cfg.tasks_dir)
            plan = (_authoritative_status_plan(trusted_plan, current_plan)
                    if review_enabled else current_plan)
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
            active.task = task
            active.session = session
            _process_task(
                cfg, task, rotation, sleep, now, session, resumed,
                blocker_evidence=(blocker_evidence if review_enabled else None))
            active.task = None
            active.session = None

            if once or task_id is not None:
                break

        if resuming_auto_fix:
            assert existing_auto_fix is not None
            return _run_auto_fix_repairs(
                cfg, existing_auto_fix, rotation, active,
                injected_reviewer=auto_fix_adapter, sleep=sleep, now=now,
                trusted_plan=trusted_plan,
                trusted_contracts=trusted_contracts)

        if auto_fix_enabled and cfg.auto_fix_review is not None:
            review_outcome = _run_auto_fix_review_once(
                cfg, once=once, task_id=task_id,
                injected_adapter=auto_fix_adapter, sleep=sleep, now=now,
                blockers=tuple(blocker_evidence),
                trusted_plan=trusted_plan,
                trusted_contracts=trusted_contracts)
            if review_outcome.code != 0:
                if (auto_fix_enabled and review_outcome.state is not None
                        and review_outcome.human_reason is None
                        and not (once or task_id is not None)):
                    return _run_auto_fix_repairs(
                        cfg, review_outcome.state, rotation, active,
                        injected_reviewer=auto_fix_adapter,
                        sleep=sleep, now=now,
                        trusted_plan=trusted_plan,
                        trusted_contracts=trusted_contracts)
                return review_outcome.code
    except KeyboardInterrupt:
        # Ctrl+C on the Windows console reaches the child process (the AI session) too, so
        # the session is terminated by the OS signal; here the engine gathers the produced
        # progress into a wip checkpoint (never discard it) and exits with 130.
        print("\nInterrupt received (Ctrl+C): session terminated, keeping current progress...")
        terminal_checkpoint = bool(
            active.session is not None and active.session.terminal_checkpoint)
        if (not terminal_checkpoint and active.session is not None):
            terminal_checkpoint = _terminal_checkpoint_matches_attempt(
                cfg, active.session.terminal_checkpoint_attempt)
            if terminal_checkpoint:
                active.session.terminal_checkpoint = True
        if (active.task is not None and active.session is not None
                and active.session.identity is not None and not terminal_checkpoint):
            _mark_interrupted_task(
                active.task, active.session.identity,
                "User interrupt; progress kept for next resume", now,
                detail="run received Ctrl+C")
        if terminal_checkpoint:
            print("Terminal auto checkpoint already exists; task remains DONE.")
        else:
            try:
                subject = (_checkpoint_subject(
                    cfg, "wip", active.task, "user interrupt, progress kept")
                    if active.task is not None
                    else f"wip({cfg.tasks_name}): user interrupt, progress kept")
                if gitops.commit_if_dirty(cfg.root, subject, cfg.git_excludes):
                    print("Progress gathered into a wip checkpoint (git revert it yourself if unsatisfied).")
            except AssentError as e:
                print(f"wip checkpoint failed: {e} (working tree left as is, nothing discarded)")
        try:
            try_write_report(cfg)
        except KeyboardInterrupt:
            # A second stop can arrive while the interrupt handler makes its best-effort
            # report refresh.  The terminal checkpoint, when present, remains authoritative.
            if not terminal_checkpoint:
                raise
        print("Interrupted.")
        return 130
    except _BillingAbort as e:
        # Distinct from an acceptance failure and from the infrastructure abort below: the
        # account's prepaid balance is exhausted, which no retry or next task can resolve.
        # Keep the current task's progress, leave it unresolved, and stop the whole run.
        print(f"Run aborted (billing/balance): {e}")
        print("The account's prepaid balance is exhausted; retrying cannot fix this. "
              "Top up the account, then rerun to resume from the kept progress.")
        if (active.task is not None and active.session is not None
                and active.session.identity is not None):
            _mark_billing_task(active.task, active.session.identity, str(e), now)
        try:
            subject = (_checkpoint_subject(
                cfg, "wip", active.task, "billing abort, progress kept")
                if active.task is not None
                else f"wip({cfg.tasks_name}): billing abort, progress kept")
            if gitops.commit_if_dirty(cfg.root, subject, cfg.git_excludes):
                print("Progress gathered into a wip checkpoint.")
        except AssentError:
            pass
        try_write_report(cfg)
        return 1
    except (AssentError, OSError) as e:
        print(f"Run aborted (infrastructure error): {e}")
        if (active.task is not None and active.session is not None
                and active.session.identity is not None):
            _mark_interrupted_task(
                active.task, active.session.identity,
                "Aborted on infrastructure error; progress kept for next resume", now,
                detail=str(e))
        try:
            subject = (_checkpoint_subject(
                cfg, "wip", active.task, "infrastructure error abort, progress kept")
                if active.task is not None
                else f"wip({cfg.tasks_name}): infrastructure error abort, progress kept")
            if gitops.commit_if_dirty(cfg.root, subject, cfg.git_excludes):
                print("Progress gathered into a wip checkpoint.")
        except AssentError:
            pass
        try_write_report(cfg)
        return 1

    final_plan = Plan.parse(cfg.tasks_dir)
    if not auto_fix_enabled and any(
            task.status not in ("DONE", "SKIP") for task in final_plan.tasks):
        _print_summary(final_plan)
        try_write_report(cfg)
        return 0
    pending = _auto_fix_existing_state(cfg)
    if pending is not None and pending.verdict == "FAIL":
        print("Auto-fix closeout refused: the folder has a pending FAIL state; "
              "rerun with --auto-fix and its current [auto_fix.review] policy.")
        _print_summary(final_plan)
        try_write_report(cfg)
        return 1
    _print_summary(final_plan)
    try_write_report(cfg)
    return 0


def _task_contract_snapshots(plan: Plan) -> dict[str, str]:
    """Capture the scheduler-trusted task contracts before a worker can edit them."""
    snapshots: dict[str, str] = {}
    for task in plan.tasks:
        try:
            snapshots[task.id] = task.path.read_text(encoding="utf-8")
        except OSError as e:
            raise AssentError(
                f"Unable to read trusted task contract {task.path}: {e}") from e
    return snapshots


def _contract_text_with_status(text: str, status: str) -> str:
    pattern = re.compile(
        r'^(\s*status\s*=\s*")(TODO|WIP|DONE|BLOCKED|SKIP)("\s*(?:#.*)?)$',
        re.MULTILINE)
    replaced, count = pattern.subn(
        lambda match: f"{match.group(1)}{status}{match.group(3)}", text,
        count=1)
    if count != 1:
        raise AssentError("Trusted task contract has no unique writable status line")
    return replaced


def _authoritative_status_plan(plan: Plan, current: Plan | None = None) -> Plan:
    """Refresh statuses while retaining every checkpoint-trusted structural field."""
    if current is None:
        current = Plan.parse(plan.dir)
    tasks: list[Task] = []
    for task in plan.tasks:
        fresh = current.get(task.id)
        if fresh is None:
            raise AssentError(
                f"Trusted task disappeared during execution: {task.id}")
        tasks.append(replace(task, status=fresh.status))
    return Plan(tasks, plan.dir)


def _authoritative_contracts(
        plan: Plan, trusted_contracts: dict[str, str]) -> dict[str, str]:
    return {
        task.id: _contract_text_with_status(trusted_contracts[task.id], task.status)
        for task in plan.tasks
    }


def _contracts_digest(plan: Plan, contracts_by_id: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for task in sorted(plan.tasks, key=lambda item: item.path.name):
        data = contracts_by_id[task.id].encode("utf-8")
        digest.update(task.path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(data)).encode("ascii"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return digest.hexdigest()


def _auto_fix_management_evidence(
        plan: Plan, contracts_by_id: dict[str, str]) -> str:
    """Reproduce authoritative contracts, untrusted tampering, and journals."""
    sections: list[str] = []
    for task in plan.tasks:
        trusted = contracts_by_id[task.id]
        sections.append(
            f"--- {task.id} task contract (trusted checkpoint; AUTHORITATIVE): "
            f"{task.path} ---\n{trusted.rstrip()}")
        try:
            on_disk = task.path.read_text(encoding="utf-8")
        except OSError as e:
            raise AssentError(
                f"Unable to read auto-fix review task contract {task.path}: {e}") from e
        if on_disk != trusted:
            sections.append(
                f"--- {task.id} current on-disk contract (UNTRUSTED EVIDENCE; "
                "the checkpoint contract remains authoritative) ---\n"
                f"{on_disk.rstrip()}")
        if not task.journal_path.is_file():
            journal = "(journal does not exist)"
        else:
            try:
                journal = task.journal_path.read_text(encoding="utf-8")
            except OSError as e:
                raise AssentError(
                    f"Unable to read auto-fix review journal "
                    f"{task.journal_path}: {e}") from e
        sections.append(
            f"--- {task.id} journal: {task.journal_path} ---\n"
            f"{journal.rstrip()}")
    return "\n\n".join(sections)


def _auto_fix_diff(cfg: Config, old_ref: str, new_ref: str = "HEAD") -> str:
    result = subprocess.run(
        ["git", "diff", "--find-renames", f"{old_ref}..{new_ref}", "--"],
        cwd=str(cfg.root), capture_output=True, encoding="utf-8",
        errors="replace")
    if result.returncode != 0:
        return ("(unable to render diff: "
                f"{_bounded_adapter_diagnostic(result.stderr or result.stdout)})")
    text = result.stdout or "(no cumulative checkpoint diff)"
    if len(text) > _AUTO_FIX_EVIDENCE_LIMIT:
        text = text[:_AUTO_FIX_EVIDENCE_LIMIT] + "\n... [diff truncated]"
    return text.rstrip()


def _auto_fix_changed_paths(cfg: Config, old_ref: str) -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{old_ref}..HEAD", "--"],
        cwd=str(cfg.root), capture_output=True, encoding="utf-8",
        errors="replace")
    if result.returncode != 0:
        raise AssentError(
            "Unable to validate auto-fix repair delta paths: "
            + _bounded_adapter_diagnostic(result.stderr or result.stdout))
    return tuple(line.strip().replace("\\", "/")
                 for line in result.stdout.splitlines() if line.strip())


def _auto_fix_dependency_state(plan: Plan) -> str:
    status_by_id = {task.id: task.status for task in plan.tasks}
    lines = []
    for task in plan.tasks:
        unmet = [dep for dep in task.deps
                 if status_by_id.get(dep) not in ("DONE", "SKIP")]
        suffix = f"; unmet deps: {', '.join(unmet)}" if unmet else ""
        lines.append(f"- {task.id}: {task.status}; deps: "
                     f"{', '.join(task.deps) or 'none'}{suffix}")
    return "\n".join(lines)


def _auto_fix_blocker_text(
        blockers: tuple[_AutoFixBlockerEvidence, ...]) -> str:
    if not blockers:
        return "(none; this is a completed-folder review)"
    rendered: list[str] = []
    for item in blockers:
        lines = [
            f"- {item.task.id} [{item.trigger}]: {item.reason}",
        ]
        if item.worker_summary is not None:
            lines.append(
                "  worker journal summary (verbatim): " + item.worker_summary)
        lines.append(f"  focused evidence: {item.focused_evidence}")
        rendered.append("\n".join(lines))
    return "\n".join(rendered)


def _auto_fix_prior_evidence(
        cfg: Config, state: auto_fix.AutoFixState | None) -> str:
    if state is None:
        return "(none; INITIAL review)"
    current = set(state.current_finding_fingerprints)
    lines = ["Prior current findings and recommendations:"]
    for finding in state.findings:
        if finding.fingerprint not in current:
            continue
        lines.append(
            f"- {finding.fingerprint} {finding.task_id or 'unassigned'} "
            f"{finding.path}: {finding.summary}\n"
            f"  evidence: {finding.evidence}\n"
            f"  recommendation: {finding.recommendation}")
    lines.append("Worker dispositions:")
    lines.extend(
        f"- {item.task_id} {item.fingerprint}: "
        f"{item.disposition}; {item.detail}"
        for item in state.worker_dispositions)
    if not state.worker_dispositions:
        lines.append("- none recorded")
    lines.append("Approved scope additions:")
    lines.extend(
        f"- {item.task_id}: {item.path} ({item.path_state})"
        for item in state.approved_scope_additions)
    if not state.approved_scope_additions:
        lines.append("- none")
    lines.append("Durable repair briefs:")
    lines.extend(
        f"--- {item.task_id} ---\n{item.brief}"
        for item in state.repair_briefs)
    if not state.repair_briefs:
        lines.append("- none")
    lines.append("Repair-only relevant diff:")
    lines.append(_auto_fix_diff(cfg, state.source_tree))
    lines.append("Prior observed states:")
    lines.extend(
        f"- {item.source_tree}: {', '.join(item.finding_fingerprints) or 'PASS'}"
        for item in state.observed_states)
    return "\n".join(lines)


def _auto_fix_review_identity(
        cfg: Config, plan: Plan, focused_evidence: str, *,
        contracts_by_id: dict[str, str] | None = None,
        review_context: str = "completed_folder",
        review_stage: str = "initial",
        blockers: tuple[_AutoFixBlockerEvidence, ...] = (),
        previous: auto_fix.AutoFixState | None = None,
        ) -> tuple[str, str, str, str]:
    """Return source tree, task-plan digest, prompt text, and prompt digest."""
    source_tree = gitops.tree_of(cfg.root, "HEAD")
    resolved_base = resolve_stack_state(cfg).base.resolved_base
    base_ref = gitops.merge_base(cfg.root, resolved_base, "HEAD")
    if contracts_by_id is None:
        contracts_by_id = {
            task.id: task.path.read_text(encoding="utf-8")
            for task in plan.tasks
        }
    prompt = _AUTO_FIX_REVIEW_PROMPT.format(
        folder=cfg.tasks_name,
        base_ref=base_ref,
        source_tree=source_tree,
        review_context=review_context.upper(),
        review_stage=review_stage.upper(),
        context_policy=(
            _AUTO_FIX_BLOCKED_POLICY
            if review_context == "blocked_adjudication"
            else _AUTO_FIX_COMPLETED_POLICY),
        stage_policy=(
            _AUTO_FIX_RECHECK_POLICY
            if review_stage == "recheck" else _AUTO_FIX_INITIAL_POLICY),
        cumulative_diff=_auto_fix_diff(cfg, base_ref),
        blocker_evidence=_auto_fix_blocker_text(blockers),
        focused_evidence=focused_evidence,
        dependency_state=_auto_fix_dependency_state(plan),
        prior_evidence=_auto_fix_prior_evidence(cfg, previous),
        management_evidence=_auto_fix_management_evidence(
            plan, contracts_by_id),
    )
    plan_digest = _contracts_digest(plan, contracts_by_id)
    prompt_digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    return source_tree, plan_digest, prompt, prompt_digest


def _auto_fix_existing_state(cfg: Config) -> auto_fix.AutoFixState | None:
    path = auto_fix.auto_fix_state_path(cfg)
    if not path.exists():
        return None
    return auto_fix.read_auto_fix_state(path)


def _auto_fix_recovery_config_error(
        cfg: Config, state: auto_fix.AutoFixState) -> str | None:
    """Refuse stale FAIL recovery unless its reviewer is still configured."""
    review = cfg.auto_fix_review
    if review is None:
        return ("the pending FAIL state requires a configured [auto_fix.review] "
                "before repair or closeout can resume")
    stored = (state.reviewer_adapter, state.reviewer_model,
              state.reviewer_effort)
    configured = (review.adapter, review.requested_model,
                  review.requested_effort)
    if stored != configured:
        return ("the pending FAIL state reviewer identity no longer matches "
                "the configured [auto_fix.review] identity "
                f"({stored[0]}/{stored[1]}/{stored[2]} -> "
                f"{configured[0]}/{configured[1]}/{configured[2]})")
    return None


def _auto_fix_surface_snapshot(cfg: Config) -> auto_fix.ProjectSurfaceSnapshot:
    """Capture only this review's protected project-management surfaces."""
    stable = [
        cfg.assent_dir / "verify.py",
        cfg.assent_dir / "manifest.toml",
        cfg.assent_dir / "_batch_verification.toml",
        cfg.assent_dir / "_archived.toml",
        cfg.assent_dir / "_archive",
    ]
    management_root = cfg.assent_dir.absolute()
    for source in cfg.sources:
        if source.path is None:
            continue
        try:
            source.path.absolute().relative_to(management_root)
        except ValueError:
            continue
        stable.append(source.path)
    return auto_fix.snapshot_project_surface(
        cfg.root, cfg.assent_dir, cfg.source_root,
        tasks_dir=cfg.tasks_dir, stable_management_files=stable)


def _auto_fix_surface_change(
        before: auto_fix.ProjectSurfaceSnapshot,
        cfg: Config, before_head: str,
        before_status: gitops.WorkingTreeStatus,
        before_primary_head: str | None,
        before_primary_status: gitops.WorkingTreeStatus | None) -> tuple[str, ...]:
    after = _auto_fix_surface_snapshot(cfg)
    changed = list(before.changed_paths(after))
    if gitops.commit_of(cfg.root, "HEAD") != before_head:
        changed.append("source:.git/HEAD")
    if gitops.working_tree_status(cfg.root, cfg.git_excludes) != before_status:
        changed.append("source:.git/index-or-status")
    if cfg.source_root is not None:
        if gitops.commit_of(cfg.source_root, "HEAD") != before_primary_head:
            changed.append("primary:.git/HEAD")
        if (gitops.working_tree_status(cfg.source_root, cfg.git_excludes)
                != before_primary_status):
            changed.append("primary:.git/index-or-status")
    return tuple(sorted(set(changed)))


def _blocked_evidence_from_journals(plan: Plan) -> tuple[_AutoFixBlockerEvidence, ...]:
    """Recover durable blocker reasons for a later full auto-fix invocation."""
    blockers: list[_AutoFixBlockerEvidence] = []
    for task in plan.tasks:
        if task.status != "BLOCKED":
            continue
        try:
            entries = read_entries(task.journal_path)
        except (AssentError, OSError):
            continue
        reason: str | None = None
        worker_summary: str | None = None
        focused = ("NOT RUN: no captured focused result is present for this "
                   "durable scheduler blocker.")
        for entry in reversed(entries):
            if entry.get("event") not in ("auto_fix_blocker", "blocked"):
                continue
            summary = str(entry.get("summary") or "")
            detail = str(entry.get("detail") or "")
            if "Focused evidence:\n" in detail:
                focused = detail.split("Focused evidence:\n", 1)[1]
            if entry.get("by") != "scheduler":
                reason = "Execution AI self-marked BLOCKED"
                worker_summary = summary or "(empty summary)"
                focused = ("NOT RUN: self-marked BLOCKED closeout legitimately "
                           "skips the focused gate.")
            elif (entry.get("event") == "auto_fix_blocker"
                  and focused.startswith("NOT RUN")
                  and "self-marked BLOCKED" in focused):
                reason = "Execution AI self-marked BLOCKED"
                marker = "Worker journal summary (verbatim):\n"
                if marker in detail:
                    worker_summary = detail.split(marker, 1)[1].split(
                        "\nFocused evidence:\n", 1)[0]
                elif summary != reason:
                    # Version-2 blocker entries used the worker summary as the
                    # scheduler entry summary. Preserve it during recovery.
                    worker_summary = summary or "(empty summary)"
            else:
                reason = summary or None
                if reason and "Verify command exit code is non-zero" in reason:
                    focused = reason
            break
        if reason is None:
            continue
        focused_lower = focused.lower()
        trigger = ("focused_gate_failure"
                   if ("focused" in focused_lower
                       or "verify command" in focused_lower)
                   and not focused.startswith("NOT RUN")
                   else "worker_blocked")
        blockers.append(_AutoFixBlockerEvidence(
            task, trigger, reason, focused, worker_summary))
    return tuple(blockers)


def _merge_blocker_evidence(
        plan: Plan, current: tuple[_AutoFixBlockerEvidence, ...],
        recovered: tuple[_AutoFixBlockerEvidence, ...],
        ) -> tuple[_AutoFixBlockerEvidence, ...]:
    """Return one current-or-durable blocker record per task in plan order."""
    by_id = {item.task.id: item for item in recovered}
    by_id.update((item.task.id, item) for item in current)
    return tuple(by_id[task.id] for task in plan.tasks if task.id in by_id)


def _restore_trusted_contracts_after_adjudication(
        plan: Plan, contracts_by_id: dict[str, str],
        now: Callable[[], datetime]) -> None:
    """Discard no source output while refusing to adopt worker contract tampering."""
    for trusted in plan.tasks:
        current = parse_task_file(trusted.path)
        changed_fields = same_except_status(trusted, current)
        if not changed_fields:
            continue
        authoritative = contracts_by_id[trusted.id]
        try:
            trusted.path.write_text(
                authoritative, encoding="utf-8", newline="")
        except OSError as e:
            raise AssentError(
                f"Unable to restore trusted checkpoint contract {trusted.path}: {e}") from e
        restored = parse_task_file(trusted.path)
        if same_except_status(trusted, restored) or restored.status != trusted.status:
            raise AssentError(
                f"Trusted checkpoint contract restoration failed: {trusted.path}")
        append_entry(
            trusted.journal_path, by="scheduler",
            event="auto_fix_contract_restore",
            summary=("Restored the trusted checkpoint task contract after "
                     "blocked adjudication"),
            detail=("The worker's on-disk structural edits were reviewed only as "
                    "untrusted evidence and were not adopted; changed fields: "
                    + ", ".join(changed_fields)),
            time_str=now().isoformat(timespec="seconds"))


def _run_auto_fix_review_once(
        cfg: Config, *, once: bool, task_id: str | None,
        injected_adapter: Adapter | None,
        sleep: Callable[[float], None],
        now: Callable[[], datetime],
        blockers: tuple[_AutoFixBlockerEvidence, ...] = (),
        trusted_plan: Plan | None = None,
        trusted_contracts: dict[str, str] | None = None,
        ) -> _AutoFixReviewOutcome:
    """Run one completed-folder review or quiescent blocked adjudication."""
    review = cfg.auto_fix_review
    if review is None:
        return _AutoFixReviewOutcome(0)

    plan = (_authoritative_status_plan(trusted_plan)
            if trusted_plan is not None else Plan.parse(cfg.tasks_dir))
    if trusted_contracts is None:
        trusted_contracts = _task_contract_snapshots(plan)
    contracts_by_id = _authoritative_contracts(plan, trusted_contracts)
    incomplete = [task for task in plan.tasks
                  if task.status not in ("DONE", "SKIP")]
    blocked = [task for task in incomplete if task.status == "BLOCKED"]
    runnable = plan.next_task()
    limited = once or task_id is not None
    review_context = "completed_folder"
    if incomplete:
        selected_blocker = bool(blockers)
        if (not blocked or runnable is not None
                or (limited and not selected_blocker)):
            suffix = " after the limited run" if limited else ""
            shown = ", ".join(f"{task.id}={task.status}" for task in incomplete)
            print(f"Auto-fix folder review deferred{suffix}; folder is incomplete "
                  f"({shown}).")
            return _AutoFixReviewOutcome(0)
        review_context = "blocked_adjudication"
        blockers = _merge_blocker_evidence(
            plan, blockers, _blocked_evidence_from_journals(plan))
        required = {item.id for item in blocked}
        available = {item.task.id for item in blockers}
        missing = sorted(required - available)
        if not blockers or missing:
            suffix = f" ({', '.join(missing)})" if missing else ""
            print("Auto-fix blocked adjudication refused: durable BLOCKED tasks "
                  f"have no readable scheduler evidence{suffix}.")
            return _AutoFixReviewOutcome(
                1, human_reason="blocked tasks have no durable scheduler evidence")

    done = [task for task in plan.tasks if task.status == "DONE"]
    if review_context == "completed_folder" and not done:
        print("Auto-fix folder review: all tasks are SKIP; no implementation review session needed.")
        return _AutoFixReviewOutcome(0)

    focused_lines: list[str] = []
    seen: set[str] = set()
    if review_context == "completed_folder":
        print("Auto-fix folder review: running final distinct focused checks.")
    else:
        print("Auto-fix blocked adjudication: using durable task failure evidence; "
              "no focused command is run by the reviewer gate.")
        focused_lines.extend(
            f"- {item.task.id}: {item.focused_evidence}" for item in blockers)
    for task in done if review_context == "completed_folder" else ():
        if task.verify in seen:
            continue
        seen.add(task.verify)
        verify_result = _verify_subprocess(cfg, task.verify)
        _show_verify_result(task.verify, verify_result)
        if verify_result.returncode != 0:
            diagnostic = _bounded_adapter_diagnostic(
                verify_result.stderr or verify_result.stdout or "")
            focused_lines.append(
                f"- FAIL ({verify_result.returncode}): {task.verify}; {diagnostic}")
            owners = [item for item in done if item.verify == task.verify]
            record = auto_fix.ReviewRecord("FAIL", tuple(
                auto_fix.ReviewFinding(
                    item.id, auto_fix.scheduler_finding_path(item.scope[0]),
                    "Final focused verification failed",
                    f"exit {verify_result.returncode}: {task.verify}; {diagnostic}")
                for item in owners))
            record = auto_fix.validate_review_findings(record, plan)
            source_tree, plan_digest, _prompt, prompt_digest = (
                _auto_fix_review_identity(
                    cfg, plan, "\n".join(focused_lines),
                    contracts_by_id=contracts_by_id))
            previous = _auto_fix_existing_state(cfg)
            state = auto_fix.state_for_review(
                record, previous=previous,
                source_tree=source_tree,
                task_plan_sha256=plan_digest,
                review_prompt_sha256=prompt_digest,
                reviewer_adapter=review.adapter,
                reviewer_model=review.requested_model,
                reviewer_effort=review.requested_effort)
            state = _auto_fix_attach_repair_briefs(
                cfg, plan, state,
                blocker_evidence=_auto_fix_blocker_text(blockers),
                focused_evidence="\n".join(focused_lines))
            auto_fix.write_auto_fix_state(auto_fix.auto_fix_state_path(cfg), state)
            print("Auto-fix folder review: focused verification failed; "
                  "scheduler findings were preserved and the reviewer was not started.")
            dirty = not gitops.working_tree_status(
                cfg.root, cfg.git_excludes).is_clean
            reason = ("focused verification changed the source worktree"
                      if dirty else None)
            return _AutoFixReviewOutcome(1, state, reason)
        focused_lines.append(f"- PASS: {task.verify}")
    if not gitops.working_tree_status(cfg.root, cfg.git_excludes).is_clean:
        print("Auto-fix folder review: focused verification changed the source worktree; "
              "reviewer was not started and the exact changes are preserved.")
        return _AutoFixReviewOutcome(
            1, human_reason="focused verification changed the source worktree")

    existing = _auto_fix_existing_state(cfg)
    review_stage = ("recheck"
                    if existing is not None and existing.verdict == "FAIL"
                    else "initial")
    review_previous = existing if review_stage == "recheck" else None
    if review_stage == "recheck":
        review_context = existing.review_context
    failure_trigger = None
    if review_context == "blocked_adjudication":
        failure_trigger = (
            "focused_gate_failure"
            if any(item.trigger == "focused_gate_failure" for item in blockers)
            else "worker_blocked")
    source_tree, plan_digest, prompt, prompt_digest = _auto_fix_review_identity(
        cfg, plan, "\n".join(focused_lines),
        contracts_by_id=contracts_by_id,
        review_context=review_context, review_stage=review_stage,
        blockers=blockers, previous=review_previous)

    def finish(outcome: _AutoFixReviewOutcome) -> _AutoFixReviewOutcome:
        """Restore worker task-contract tampering after its review evidence is captured."""
        if review_context != "blocked_adjudication":
            return outcome
        try:
            _restore_trusted_contracts_after_adjudication(
                plan, contracts_by_id, now)
        except AssentError as e:
            print("Auto-fix adjudication evidence was preserved, but trusted "
                  f"contract restoration failed: {e}")
            return _AutoFixReviewOutcome(1, outcome.state, str(e))
        return outcome

    freshness = dict(
        source_tree=source_tree,
        task_plan_sha256=plan_digest,
        review_prompt_sha256=prompt_digest,
        reviewer_adapter=review.adapter,
        reviewer_model=review.requested_model,
        reviewer_effort=review.requested_effort,
        review_context=review_context,
        failure_trigger=failure_trigger,
    )
    if existing is not None and auto_fix.auto_fix_state_is_fresh(
            existing, **freshness):
        print("Auto-fix folder review: reusing exact fresh PASS; no reviewer session started.")
        return finish(_AutoFixReviewOutcome(0, existing))

    try:
        reviewer = injected_adapter or get_adapter(review.adapter, cfg)
    except AssentError as e:
        print(f"Auto-fix reviewer resolution failed: {e}")
        return finish(_AutoFixReviewOutcome(1, human_reason=str(e)))
    session, errors = auto_fix_review_capability_errors(cfg, reviewer)
    if errors:
        print(f"{review.adapter} auto-fix review capability preflight: FAIL "
              "(refusing before the review session)")
        for message in errors:
            print(f"  - {message}")
        return finish(_AutoFixReviewOutcome(1, human_reason="; ".join(errors)))
    assert session is not None

    baseline = _auto_fix_surface_snapshot(cfg)
    baseline_head = gitops.commit_of(cfg.root, "HEAD")
    baseline_status = gitops.working_tree_status(cfg.root, cfg.git_excludes)
    baseline_primary_head = (gitops.commit_of(cfg.source_root, "HEAD")
                             if cfg.source_root is not None else None)
    baseline_primary_status = (
        gitops.working_tree_status(cfg.source_root, cfg.git_excludes)
        if cfg.source_root is not None else None)
    invalid_attempts = 0
    while True:
        print(f"Auto-fix review session: {session.agent} | "
              f"{review.model}->{session.requested_model} | "
              f"{review.effort}->{session.requested_effort}")
        try:
            result = reviewer.run_task(
                prompt, session.requested_model, session.requested_effort, cfg.root)
        except KeyboardInterrupt:
            changed = _auto_fix_surface_change(
                baseline, cfg, baseline_head, baseline_status,
                baseline_primary_head, baseline_primary_status)
            if changed:
                print("Auto-fix reviewer interruption interval contains project writes; "
                      "exact edits are preserved: "
                      + ", ".join(changed[:8]))
                return _AutoFixReviewOutcome(
                    130, human_reason="reviewer interrupted")
            else:
                print("Auto-fix reviewer interrupted; no verdict was recorded.")
            return finish(_AutoFixReviewOutcome(
                130, human_reason="reviewer interrupted"))
        except OSError as e:
            changed = _auto_fix_surface_change(
                baseline, cfg, baseline_head, baseline_status,
                baseline_primary_head, baseline_primary_status)
            suffix = (f"; project writes preserved: {', '.join(changed[:8])}"
                      if changed else "")
            print(f"Auto-fix reviewer infrastructure failure: {e}{suffix}")
            outcome = _AutoFixReviewOutcome(1, human_reason=str(e))
            return outcome if changed else finish(outcome)

        changed = _auto_fix_surface_change(
            baseline, cfg, baseline_head, baseline_status,
            baseline_primary_head, baseline_primary_status)
        if changed:
            shown = ", ".join(changed[:8]) + (" ..." if len(changed) > 8 else "")
            print("Protected project writes were detected during the reviewer interval; "
                  "PASS/FAIL was ignored and the exact edits are preserved "
                  f"({shown}).")
            return _AutoFixReviewOutcome(
                1, human_reason="reviewer project writes detected")

        if (result.checkpoint_resume and not result.quota_exhausted
                and not result.stalled and result.exit_code != 0):
            print("Auto-fix reviewer requested immediate checkpoint-resume continuation.")
            continue
        if result.quota_exhausted:
            print("Auto-fix reviewer quota exhausted; waiting before resuming the same review.")
            _wait_for_quota(cfg, result.reset_at, sleep, now)
            continue
        if result.failure_kind == "billing":
            reason = _adapter_failure_reason(
                result.exit_code, result.stalled, result.output, result.failure_kind)
            print(f"Auto-fix reviewer billing/balance failure: {reason}")
            return finish(_AutoFixReviewOutcome(1, human_reason=reason))
        if result.exit_code != 0 or result.stalled:
            reason = _adapter_failure_reason(
                result.exit_code, result.stalled, result.output, result.failure_kind)
            print(f"Auto-fix reviewer adapter failure: {reason}")
            if result.exit_code == 130 or result.failure_kind == "interrupt":
                return finish(_AutoFixReviewOutcome(130, human_reason=reason))
            return finish(_AutoFixReviewOutcome(1, human_reason=reason))

        try:
            record = auto_fix.parse_review_output(result.output)
            if (review_context == "blocked_adjudication"
                    and record.verdict == "PASS" and blocked):
                raise AssentError(
                    "A blocked adjudication cannot PASS while a task remains BLOCKED")
        except AssentError as e:
            if invalid_attempts < cfg.retry_per_task:
                invalid_attempts += 1
                print(f"Auto-fix reviewer returned invalid output ({e}); retrying "
                      f"({invalid_attempts}/{cfg.retry_per_task}).")
                continue
            print(f"Auto-fix reviewer returned invalid output after configured retries: {e}")
            return finish(_AutoFixReviewOutcome(1, human_reason=str(e)))

        try:
            resolved_record = auto_fix.validate_review_findings(record, plan)
            resolved_record = auto_fix.validate_review_transitions(
                resolved_record, review_stage=review_stage,
                previous=review_previous,
                repair_changed_paths=(
                    _auto_fix_changed_paths(cfg, review_previous.source_tree)
                    if review_stage == "recheck"
                    and review_previous is not None else None))
        except AssentError as e:
            # Preserve the reviewer's concrete output even though it cannot
            # authorize a write-capable task session.
            try:
                state = auto_fix.state_for_review(
                    record, previous=review_previous, review_stage=review_stage,
                    enforce_transitions=False, **freshness)
                auto_fix.write_auto_fix_state(
                    auto_fix.auto_fix_state_path(cfg), state)
            except AssentError as state_error:
                print("Auto-fix invalid reviewer evidence could not be encoded "
                      f"as repair state: {state_error}")
                return finish(_AutoFixReviewOutcome(1, human_reason=str(e)))
            print(f"Auto-fix findings require a human scope/plan decision: {e}")
            return finish(_AutoFixReviewOutcome(1, state, str(e)))

        state = auto_fix.state_for_review(
            resolved_record, previous=review_previous, review_stage=review_stage,
            **freshness)
        state = _auto_fix_attach_repair_briefs(
            cfg, plan, state,
            blocker_evidence=_auto_fix_blocker_text(blockers),
            focused_evidence="\n".join(focused_lines))
        auto_fix.write_auto_fix_state(auto_fix.auto_fix_state_path(cfg), state)
        if resolved_record.verdict == "PASS":
            print("Auto-fix folder review: PASS.")
            return finish(_AutoFixReviewOutcome(0, state))
        print("Auto-fix folder review: FAIL; blocking findings were preserved for repair.")
        for finding in resolved_record.findings:
            owner = f"{finding.task_id}: " if finding.task_id else ""
            print(f"  - {owner}{finding.path}: {finding.summary}")
        return finish(_AutoFixReviewOutcome(1, state))


def _auto_fix_profile_for_task(cfg: Config, task: Task) -> auto_fix.FixerProfile:
    """The primary worker's ordinary identity for one reopened task."""
    settings = cfg.adapter_settings(cfg.adapter_names[0])
    effort = settings.resolve_effort(task.effort, task.model)
    if effort is None:
        raise AssentError(
            f"Auto-fix task profile has no concrete effort: {task.id}")
    return auto_fix.FixerProfile(cfg.adapter_names[0], task.model, effort)


def _auto_fix_escalation_profiles(cfg: Config) -> tuple[auto_fix.FixerProfile, ...]:
    """Primary worker first, then each other worker adapter, all at prime/heavy."""
    return tuple(auto_fix.FixerProfile(name, "prime", "heavy")
                 for name in cfg.adapter_names)


def _auto_fix_adapter(
        rotation: _AdapterRotation,
        adapter_name: str) -> Adapter:
    try:
        return rotation.adapters[rotation.names.index(adapter_name)]
    except ValueError as e:
        raise AssentError(
            f"Auto-fix profile names an adapter outside the worker rotation: "
            f"{adapter_name}") from e


def _auto_fix_task_diff(cfg: Config, task: Task) -> str:
    """Render the bounded cumulative source delta relevant to one task."""
    base = resolve_stack_state(cfg).base.resolved_base
    merge_base = gitops.merge_base(cfg.root, base, "HEAD")
    result = subprocess.run(
        ["git", "diff", "--find-renames", f"{merge_base}..HEAD", "--",
         *task.scope], cwd=str(cfg.root), capture_output=True,
        encoding="utf-8", errors="replace")
    if result.returncode == 0:
        diff = result.stdout or "(no committed diff in this task's scope)"
    else:
        diff = ("(unable to render diff: "
                f"{_bounded_adapter_diagnostic(result.stderr or result.stdout)})")
    if len(diff) > _AUTO_FIX_EVIDENCE_LIMIT:
        diff = diff[:_AUTO_FIX_EVIDENCE_LIMIT] + "\n... [diff truncated]"
    return diff.rstrip()


def _auto_fix_repair_briefs(
        cfg: Config, plan: Plan, state: auto_fix.AutoFixState, *,
        blocker_evidence: str, focused_evidence: str,
        ) -> tuple[auto_fix.RepairBrief, ...]:
    """Build the exact durable reviewer-to-worker handoff for this decision."""
    current = set(state.current_finding_fingerprints)
    finding_lines: list[str] = []
    for finding in state.findings:
        if finding.fingerprint not in current:
            continue
        addition = "none"
        if finding.scope_addition_path is not None:
            addition = (
                f"{finding.scope_addition_path} "
                f"({finding.scope_addition_path_state})")
        finding_lines.append(
            f"- fingerprint: {finding.fingerprint}\n"
            f"  kind: {finding.kind}\n"
            f"  owner: {finding.task_id or 'unassigned'}\n"
            f"  path: {finding.path}\n"
            f"  problem: {finding.summary}\n"
            f"  evidence: {finding.evidence}\n"
            f"  reviewer recommendation: {finding.recommendation}\n"
            f"  approved scope addition: {addition}")
    findings = "\n".join(finding_lines) or "- none"
    additions = "\n".join(
        f"- {item.fingerprint} {item.task_id}: {item.path} ({item.path_state})"
        for item in state.approved_scope_additions) or "- none"
    profiles = "\n".join(
        f"- {item.adapter}/{item.model}/{item.effort}"
        for item in state.consumed_fixer_profiles) or "- none"
    fingerprints = state.current_finding_fingerprints
    briefs = []
    implicated = list(dict.fromkeys(
        finding.task_id for finding in state.findings
        if finding.fingerprint in current and finding.task_id is not None))
    for task in _auto_fix_cascade_tasks(plan, implicated):
        brief = (
            f"Task: {task.id}\n"
            f"Current findings:\n{findings}\n\n"
            "Original blocker evidence:\n"
            f"{blocker_evidence or '(none)'}\n\n"
            "Focused command evidence:\n"
            f"{focused_evidence or '(none)'}\n\n"
            f"Approved scope additions:\n{additions}\n\n"
            "Relevant cumulative diff:\n"
            f"{_auto_fix_task_diff(cfg, task)}\n\n"
            f"Prior fixer identities:\n{profiles}"
        )
        briefs.append(auto_fix.RepairBrief(task.id, fingerprints, brief))
    return tuple(briefs)


def _auto_fix_attach_repair_briefs(
        cfg: Config, plan: Plan, state: auto_fix.AutoFixState, *,
        blocker_evidence: str, focused_evidence: str) -> auto_fix.AutoFixState:
    if state.verdict != "FAIL":
        return state
    return auto_fix.with_repair_briefs(
        state, _auto_fix_repair_briefs(
            cfg, plan, state, blocker_evidence=blocker_evidence,
            focused_evidence=focused_evidence))


def _auto_fix_repair_context(
        task: Task, state: auto_fix.AutoFixState) -> str:
    """Inject the exact durable brief; never reconstruct it from memory."""
    matches = [item for item in state.repair_briefs if item.task_id == task.id]
    if len(matches) != 1:
        raise AssentError(
            f"Auto-fix state has no exact durable repair brief for {task.id}")
    brief = matches[0]
    if brief.finding_fingerprints != state.current_finding_fingerprints:
        raise AssentError(
            f"Auto-fix durable repair brief for {task.id} is stale")
    return _AUTO_FIX_REPAIR_SUFFIX.format(repair_brief=brief.brief)


def _auto_fix_failure_state(
        cfg: Config, state: auto_fix.AutoFixState,
        failures: list[tuple[Task, str]]) -> auto_fix.AutoFixState:
    """Join concrete ordinary-gate failures to the same durable ledger."""
    record = auto_fix.ReviewRecord("FAIL", tuple(
        auto_fix.ReviewFinding(
            task.id, auto_fix.scheduler_finding_path(task.scope[0]),
            "Automatic repair task gate failed", reason)
        for task, reason in failures))
    plan = Plan.parse(cfg.tasks_dir)
    record = auto_fix.validate_review_findings(record, plan)
    next_state = auto_fix.state_for_review(
        record, previous=state,
        source_tree=gitops.tree_of(cfg.root, "HEAD"),
        task_plan_sha256=auto_fix.sha256_files(
            task.path for task in plan.tasks),
        review_prompt_sha256=state.review_prompt_sha256,
        reviewer_adapter=state.reviewer_adapter,
        reviewer_model=state.reviewer_model,
        reviewer_effort=state.reviewer_effort)
    auto_fix.write_auto_fix_state(auto_fix.auto_fix_state_path(cfg), next_state)
    return next_state


_SCOPE_AMENDMENT_EVENT = "auto_fix_scope_amendment"
_SCOPE_AMENDMENT_SUMMARY = (
    "Scheduler appended reviewer-approved exact task scope paths")


def _scope_amendment_payload(
        additions: list[auto_fix.ApprovedScopeAddition]) -> tuple[str, str]:
    fingerprints = json.dumps(
        [item.fingerprint for item in additions], separators=(",", ":"))
    delta = json.dumps(
        [{"path": item.path, "path_state": item.path_state}
         for item in additions], sort_keys=True, separators=(",", ":"))
    return fingerprints, delta


def _scope_amendment_additions(
        amendment: auto_fix.ScopeAmendment,
        ) -> list[auto_fix.ApprovedScopeAddition]:
    return [
        auto_fix.ApprovedScopeAddition(fingerprint, amendment.task_id, path, state)
        for fingerprint, path, state in zip(
            amendment.finding_fingerprints, amendment.paths,
            amendment.path_states)
    ]


def _scope_amendment_was_journaled(
        task: Task, amendment: auto_fix.ScopeAmendment) -> bool:
    """Recognize the exact durable scheduler transaction in the journal."""
    try:
        entries = read_entries(task.journal_path)
    except (AssentError, OSError, ValueError):
        return False
    detail = _scope_amendment_detail(
        _scope_amendment_additions(amendment),
        task_before=amendment.task_before_sha256,
        task_after=amendment.task_after_sha256,
        plan_before=amendment.plan_before_sha256,
        plan_after=amendment.plan_after_sha256)
    return any(
        entry.get("by") == "scheduler"
        and entry.get("event") == _SCOPE_AMENDMENT_EVENT
        and entry.get("summary") == _SCOPE_AMENDMENT_SUMMARY
        and entry.get("detail") in {detail, detail + "\n"}
        for entry in entries)


def _scope_amendment_detail(
        additions: list[auto_fix.ApprovedScopeAddition], *,
        task_before: str, task_after: str,
        plan_before: str, plan_after: str) -> str:
    fingerprints, delta = _scope_amendment_payload(additions)
    return (
        f"finding fingerprints: {fingerprints}\n"
        f"scope delta: {delta}\n"
        f"task contract before sha256: {task_before}\n"
        f"task contract after sha256: {task_after}\n"
        f"task plan before sha256: {plan_before}\n"
        f"task plan after sha256: {plan_after}\n"
        "authorization: run --auto-fix reviewer decision"
    )


def _ensure_scope_amendment_writable(
        state_path, tasks: list[Task]) -> None:
    """Refuse the whole scope transaction before its first mutation."""
    targets = [state_path]
    for task in tasks:
        targets.extend((task.path, task.journal_path))
    for target in targets:
        writable = target if target.exists() else target.parent
        if not os.access(writable, os.W_OK):
            raise AssentError(
                f"Auto-fix scope amendment target is not writable: {target}")
    for task in tasks:
        read_entries(task.journal_path)


def _apply_reviewed_scope_amendments(
        cfg: Config, state: auto_fix.AutoFixState,
        plan: Plan, contracts_by_id: dict[str, str],
        now: Callable[[], datetime],
        ) -> tuple[auto_fix.AutoFixState, Plan, dict[str, str]]:
    """Complete or resume the scheduler-owned amendment before ordinary rework."""
    if not state.approved_scope_additions:
        return state, plan, contracts_by_id

    disk_plan = Plan.parse(cfg.tasks_dir)
    disk_tasks = {task.id: task for task in disk_plan.tasks}
    for addition in state.approved_scope_additions:
        if addition.task_id not in disk_tasks:
            raise AssentError(
                f"Auto-fix scope amendment task disappeared: {addition.task_id}")

    current_contracts = _task_contract_snapshots(disk_plan)
    recorded = {
        fingerprint
        for amendment in state.scope_amendments
        for fingerprint in amendment.finding_fingerprints}
    unrecorded = [
        item for item in state.approved_scope_additions
        if item.fingerprint not in recorded]
    if unrecorded:
        grouped: dict[str, list[auto_fix.ApprovedScopeAddition]] = {}
        for addition in unrecorded:
            grouped.setdefault(addition.task_id, []).append(addition)
        auto_fix.validate_scope_additions(cfg.root, disk_plan, unrecorded)
        affected = [disk_tasks[task_id] for task_id in grouped]
        _ensure_scope_amendment_writable(
            auto_fix.auto_fix_state_path(cfg), affected)

        before_digest = _contracts_digest(disk_plan, current_contracts)
        if before_digest != state.task_plan_sha256:
            raise AssentError(
                "Task plan drifted before the reviewed scope amendment")
        after_contracts = dict(current_contracts)
        after_tasks: list[Task] = []
        for task in disk_plan.tasks:
            additions = grouped.get(task.id, ())
            if not additions:
                after_tasks.append(task)
                continue
            paths = [item.path for item in additions]
            after_contracts[task.id] = scope_text_with_entries(
                current_contracts[task.id], paths)
            after_tasks.append(replace(task, scope=task.scope + paths))
        after_plan = Plan(after_tasks, disk_plan.dir)
        after_digest = _contracts_digest(after_plan, after_contracts)
        amendments = list(state.scope_amendments)
        for task_id, additions in grouped.items():
            amendments.append(auto_fix.ScopeAmendment(
                finding_fingerprints=tuple(
                    item.fingerprint for item in additions),
                task_id=task_id,
                paths=tuple(item.path for item in additions),
                path_states=tuple(item.path_state for item in additions),
                task_before_sha256=task_text_sha256(
                    current_contracts[task_id]),
                task_after_sha256=task_text_sha256(after_contracts[task_id]),
                plan_before_sha256=before_digest,
                plan_after_sha256=after_digest))
        state = auto_fix.with_scope_amendments(state, tuple(amendments))
        # This is the durable transaction boundary.  It records the exact
        # decision, delta, and before/after identities before any task changes.
        auto_fix.write_auto_fix_state(auto_fix.auto_fix_state_path(cfg), state)

    incomplete = [
        amendment for amendment in state.scope_amendments
        if not _scope_amendment_was_journaled(
            disk_tasks[amendment.task_id], amendment)]
    if not incomplete:
        return state, disk_plan, current_contracts

    pairs = list(dict.fromkeys(
        (item.plan_before_sha256, item.plan_after_sha256)
        for item in incomplete))
    for plan_before, plan_after in pairs:
        transaction = [
            item for item in state.scope_amendments
            if (item.plan_before_sha256, item.plan_after_sha256)
            == (plan_before, plan_after)]
        transaction_tasks = {item.task_id for item in transaction}
        if len(transaction_tasks) != len(transaction):
            raise AssentError(
                "Auto-fix scope transaction contains duplicate task records")
        affected = [disk_tasks[task_id] for task_id in transaction_tasks]
        _ensure_scope_amendment_writable(
            auto_fix.auto_fix_state_path(cfg), affected)

        live_plan = Plan.parse(cfg.tasks_dir)
        live_contracts = _task_contract_snapshots(live_plan)
        pre_contracts = dict(live_contracts)
        post_contracts = dict(live_contracts)
        pre_tasks: list[Task] = []
        post_tasks: list[Task] = []
        by_task = {item.task_id: item for item in transaction}
        applied: set[str] = set()
        all_additions: list[auto_fix.ApprovedScopeAddition] = []
        for task in live_plan.tasks:
            amendment = by_task.get(task.id)
            if amendment is None:
                pre_tasks.append(task)
                post_tasks.append(task)
                continue
            all_additions.extend(_scope_amendment_additions(amendment))
            text = live_contracts[task.id]
            digest = task_text_sha256(text)
            paths = list(amendment.paths)
            if digest == amendment.task_before_sha256:
                if any(path in task.scope for path in paths):
                    raise AssentError(
                        f"Unapplied scope amendment is partially present in {task.id}")
                after_text = scope_text_with_entries(text, paths)
                if task_text_sha256(after_text) != amendment.task_after_sha256:
                    raise AssentError(
                        f"Precomputed scope amendment no longer matches {task.id}")
                post_contracts[task.id] = after_text
                pre_tasks.append(task)
                post_tasks.append(replace(task, scope=task.scope + paths))
            elif digest == amendment.task_after_sha256:
                if task.scope[-len(paths):] != paths:
                    raise AssentError(
                        f"Applied scope amendment is not the exact suffix of {task.id}")
                before_text = scope_text_without_entries(text, paths)
                if task_text_sha256(before_text) != amendment.task_before_sha256:
                    raise AssentError(
                        f"Applied scope amendment no longer matches {task.id}")
                pre_contracts[task.id] = before_text
                pre_tasks.append(replace(task, scope=task.scope[:-len(paths)]))
                post_tasks.append(task)
                applied.add(task.id)
            else:
                raise AssentError(
                    f"Task contract drifted across the scope amendment: {task.id}")

        pre_plan = Plan(pre_tasks, live_plan.dir)
        post_plan = Plan(post_tasks, live_plan.dir)
        auto_fix.validate_scope_additions(cfg.root, pre_plan, all_additions)
        if _contracts_digest(pre_plan, pre_contracts) != plan_before:
            raise AssentError(
                "Scope amendment pre-plan does not match its durable digest")
        if _contracts_digest(post_plan, post_contracts) != plan_after:
            raise AssentError(
                "Scope amendment post-plan does not match its durable digest")
        if state.task_plan_sha256 not in {plan_before, plan_after}:
            raise AssentError(
                "Auto-fix state is outside the durable scope plan transition")

        for amendment in transaction:
            if amendment.task_id in applied:
                continue
            add_scope_entries(
                disk_tasks[amendment.task_id].path, list(amendment.paths),
                expected_sha256=amendment.task_before_sha256)

        amended_plan = Plan.parse(cfg.tasks_dir)
        amended_contracts = _task_contract_snapshots(amended_plan)
        if _contracts_digest(amended_plan, amended_contracts) != plan_after:
            raise AssentError(
                "Scope amendment result does not match its durable plan digest")
        if state.task_plan_sha256 == plan_before:
            state = auto_fix.with_plan_digest_transition(
                state, plan_before, plan_after)
            auto_fix.write_auto_fix_state(auto_fix.auto_fix_state_path(cfg), state)
        elif not any(
                item.before_sha256 == plan_before
                and item.after_sha256 == plan_after
                for item in state.plan_digest_transitions):
            raise AssentError(
                "Applied scope amendment is missing its durable plan transition")

        for amendment in transaction:
            task = amended_plan.get(amendment.task_id)
            assert task is not None
            if _scope_amendment_was_journaled(task, amendment):
                continue
            detail = _scope_amendment_detail(
                _scope_amendment_additions(amendment),
                task_before=amendment.task_before_sha256,
                task_after=amendment.task_after_sha256,
                plan_before=amendment.plan_before_sha256,
                plan_after=amendment.plan_after_sha256)
            append_entry(
                task.journal_path, by="scheduler",
                event=_SCOPE_AMENDMENT_EVENT,
                summary=_SCOPE_AMENDMENT_SUMMARY, detail=detail,
                time_str=now().isoformat(timespec="seconds"))

        disk_plan = Plan.parse(cfg.tasks_dir)
        disk_tasks = {task.id: task for task in disk_plan.tasks}

    final_plan = Plan.parse(cfg.tasks_dir)
    return state, final_plan, _task_contract_snapshots(final_plan)


def _auto_fix_cascade_tasks(plan: Plan, implicated: list[str]) -> list[Task]:
    """Return the finite automatic-rework dependency closure in plan order."""
    selected = set(implicated)
    changed = True
    while changed:
        changed = False
        for task in plan.tasks:
            if task.id in selected or not any(dep in selected for dep in task.deps):
                continue
            selected.add(task.id)
            changed = True
    return [task for task in plan.tasks
            if task.id in selected and task.status != "SKIP"]


def _auto_fix_profiles_are_exhausted(
        cfg: Config, state: auto_fix.AutoFixState,
        tasks: list[Task]) -> bool:
    used = {(item.adapter, item.model, item.effort)
            for item in state.consumed_fixer_profiles}
    escalations = _auto_fix_escalation_profiles(cfg)
    return bool(tasks) and all(not any(
        (candidate.adapter, candidate.model, candidate.effort) not in used
        for candidate in ((_auto_fix_profile_for_task(cfg, task),)
                          + escalations))
        for task in tasks)


def _auto_fix_round_assignments(
        state: auto_fix.AutoFixState,
        repair_tasks: list[Task],
        ) -> list[tuple[Task, auto_fix.FixerProfile | None]] | None:
    """Recover a wholly not-started round, or request a fresh finite round."""
    durable = state.repair_round_assignments
    if not durable:
        return None
    by_task = {item.task_id: item for item in durable}
    current = [by_task.get(task.id) for task in repair_tasks]
    if not any(item is not None for item in current):
        return None
    if any(item is not None and item.attempted for item in current):
        return None
    return [
        (task, item.profile if item is not None else None)
        for task, item in zip(repair_tasks, current)
    ]


def _auto_fix_mark_assignment_attempted(
        state: auto_fix.AutoFixState, task_id: str, attempted: bool,
        ) -> auto_fix.AutoFixState:
    """Update one durable child-start witness without changing its profile."""
    matches = [item for item in state.repair_round_assignments
               if item.task_id == task_id]
    if len(matches) != 1:
        raise AssentError(
            f"Auto-fix repair round has no exact assignment for {task_id}")
    assignments = tuple(
        replace(item, attempted=attempted) if item.task_id == task_id else item
        for item in state.repair_round_assignments)
    return auto_fix.with_repair_round_assignments(state, assignments)


def _auto_fix_finish_exhausted(
        cfg: Config, tasks: list[Task], now: Callable[[], datetime]) -> int:
    """Terminate nonzero with durable BLOCKED evidence and no new repair round."""
    for task in tasks:
        fresh = parse_task_file(task.path)
        if fresh.status != "BLOCKED":
            set_status(task.path, "BLOCKED")
        if not any(entry.get("event") == "auto_fix_exhausted"
                   for entry in read_entries(task.journal_path)):
            append_entry(
                task.journal_path, by="scheduler", event="auto_fix_exhausted",
                summary="Automatic repair profiles exhausted; task remains BLOCKED",
                detail=("The folder-global finite fixer sequence has no unused "
                        "profile. Source edits, scope amendments, findings, repair "
                        "briefs, and journals were preserved; no further mutation "
                        "round or interactive adjudication was started."),
                time_str=now().isoformat(timespec="seconds"))
    gitops.commit_if_dirty(
        cfg.root,
        _checkpoint_subject(
            cfg, "auto", tasks[0], "BLOCKED (auto-fix profiles exhausted)"),
        cfg.git_excludes)
    print("Auto-fix profiles exhausted; unresolved tasks remain BLOCKED and "
          "all evidence was preserved without another mutation round.")
    return 1


def _auto_fix_recover_dispositions(
        state: auto_fix.AutoFixState, plan: Plan) -> auto_fix.AutoFixState:
    """Rebuild valid post-checkpoint disposition evidence after a hard crash."""
    expected = state.current_finding_fingerprints
    dispositions = list(state.worker_dispositions)
    recorded = {(item.task_id, item.fingerprint) for item in dispositions}
    changed = False
    for task in plan.tasks:
        if task.status not in {"DONE", "BLOCKED"}:
            continue
        if all((task.id, fingerprint) in recorded for fingerprint in expected):
            continue
        try:
            entries = read_entries(task.journal_path)
        except AssentError:
            continue
        recovered = None
        for entry in reversed(entries):
            if entry.get("by") == "scheduler":
                continue
            try:
                recovered = auto_fix.parse_repair_dispositions(
                    entry.get("detail", ""), task_id=task.id,
                    task_status=task.status,
                    expected_fingerprints=expected)
            except AssentError:
                continue
            break
        if recovered is None:
            continue
        dispositions = [item for item in dispositions if item.task_id != task.id]
        dispositions.extend(recovered)
        recorded.update((item.task_id, item.fingerprint)
                        for item in recovered)
        changed = True
    if not changed:
        return state
    return auto_fix.with_worker_dispositions(state, tuple(dispositions))


def _run_auto_fix_repairs(
        cfg: Config, state: auto_fix.AutoFixState,
        rotation: _AdapterRotation, active: _ActiveTask, *,
        injected_reviewer: Adapter | None,
        sleep: Callable[[float], None],
        now: Callable[[], datetime],
        trusted_plan: Plan | None = None,
        trusted_contracts: dict[str, str] | None = None) -> int:
    """Consume the finite worker capability sequence until review passes or hands off."""
    state_path = auto_fix.auto_fix_state_path(cfg)
    recovery_error = _auto_fix_recovery_config_error(cfg, state)
    if recovery_error is not None:
        print(f"Auto-fix recovery refused: {recovery_error}.")
        return 1
    if not state.repair_briefs:
        recovery_plan = Plan.parse(cfg.tasks_dir)
        state = _auto_fix_attach_repair_briefs(
            cfg, recovery_plan, state,
            blocker_evidence="Recovered from the durable finding ledger.",
            focused_evidence=(
                "Recovered focused or blocker evidence is embedded in each "
                "current finding."))
        auto_fix.write_auto_fix_state(state_path, state)
    if state.phase == "NEEDS_REPAIR" and state.approved_scope_additions:
        authoritative_plan = (
            _authoritative_status_plan(trusted_plan)
            if trusted_plan is not None else Plan.parse(cfg.tasks_dir))
        authoritative_contracts = (
            _authoritative_contracts(authoritative_plan, trusted_contracts)
            if trusted_contracts is not None
            else _task_contract_snapshots(authoritative_plan))
        state, trusted_plan, trusted_contracts = (
            _apply_reviewed_scope_amendments(
                cfg, state, authoritative_plan, authoritative_contracts, now))
    while True:
        plan = Plan.parse(cfg.tasks_dir)
        recovered_state = _auto_fix_recover_dispositions(state, plan)
        if recovered_state != state:
            state = recovered_state
            auto_fix.write_auto_fix_state(state_path, state)
        try:
            record = auto_fix.validate_review_findings(
                auto_fix.current_review_record(state), plan)
        except AssentError as e:
            print(f"Auto-fix stopped for a human scope/plan decision: {e}")
            return 1
        implicated = list(dict.fromkeys(
            finding.task_id for finding in record.findings
            if finding.task_id is not None))
        if not implicated:
            print("Auto-fix stopped: no existing task owns the current findings.")
            return 1

        incomplete = [task for task in plan.tasks
                      if task.status not in ("DONE", "SKIP")]
        if state.phase == "AWAITING_REVIEW":
            if incomplete:
                shown = ", ".join(
                    f"{task.id}={task.status}" for task in incomplete)
                print("Auto-fix pending-review state is inconsistent with the "
                      f"task plan ({shown}); preserving it for human adjudication.")
                return 1
            outcome = _run_auto_fix_review_once(
                cfg, once=False, task_id=None,
                injected_adapter=injected_reviewer, sleep=sleep, now=now,
                trusted_plan=trusted_plan,
                trusted_contracts=trusted_contracts)
            if outcome.code == 0:
                return 0
            if outcome.state is None or outcome.human_reason is not None:
                return outcome.code
            state = outcome.state
            continue
        if state.phase == "REPAIRING" and not incomplete:
            # A task can reach its terminal checkpoint immediately before the
            # process dies.  The durable in-repair phase plus the DONE/SKIP
            # plan mechanically recovers that boundary without another fixer.
            state = auto_fix.with_repair_phase(state, "AWAITING_REVIEW")
            auto_fix.write_auto_fix_state(state_path, state)
            continue

        if state.phase == "NEEDS_REPAIR":
            prospective = _auto_fix_cascade_tasks(plan, implicated)
            if _auto_fix_profiles_are_exhausted(cfg, state, prospective):
                owners = [task for task in prospective
                          if task.id in set(implicated)]
                return _auto_fix_finish_exhausted(
                    cfg, owners or prospective, now)
            reason = "Automatic repair of durable folder-review findings"
            if rework.rework_tasks_locked(cfg, implicated, reason) != 0:
                return 1
            state = auto_fix.with_repair_phase(state, "REPAIRING")
            auto_fix.write_auto_fix_state(state_path, state)
            plan = Plan.parse(cfg.tasks_dir)
        elif state.phase != "REPAIRING":
            print(f"Auto-fix stopped: unsupported repair phase {state.phase!r}.")
            return 1
        repair_tasks = [task for task in plan.tasks
                        if task.status in ("TODO", "WIP")]
        if not repair_tasks:
            print("Auto-fix repair has no runnable TODO/WIP task; preserving "
                  "the current statuses for human adjudication.")
            return 1

        # Select the capability level once for the whole repair round.  Every
        # reopened task compares against the same pre-round history, so one
        # task consuming a shared normal identity cannot force its siblings or
        # dependency cascade to escalate.  Persist the complete round before
        # its first write-capable session; an interrupted round therefore
        # remains finite on restart.
        resuming_not_started = bool(state.repair_round_assignments)
        round_assignments = _auto_fix_round_assignments(state, repair_tasks)
        if round_assignments is None:
            resuming_not_started = False
            used_before_round = {
                (item.adapter, item.model, item.effort)
                for item in state.consumed_fixer_profiles}
            escalations = _auto_fix_escalation_profiles(cfg)
            round_assignments = []
            new_profiles: list[auto_fix.FixerProfile] = []
            new_identities: set[tuple[str, str, str]] = set()
            for task in repair_tasks:
                candidates = (_auto_fix_profile_for_task(cfg, task),) + escalations
                profile = next((candidate for candidate in candidates
                                if (candidate.adapter, candidate.model,
                                    candidate.effort) not in used_before_round), None)
                round_assignments.append((task, profile))
                if profile is None:
                    continue
                identity = (profile.adapter, profile.model, profile.effort)
                if identity not in new_identities:
                    new_profiles.append(profile)
                    new_identities.add(identity)
            if all(profile is None for _task, profile in round_assignments):
                print("Auto-fix profiles exhausted; unresolved state and evidence "
                      "were preserved for human adjudication.")
                return 1
            if new_profiles:
                state = auto_fix.replace_fixer_profiles(
                    state, state.consumed_fixer_profiles + tuple(new_profiles))
            durable_assignments = tuple(
                auto_fix.RepairRoundAssignment(
                    task.id, profile.adapter, profile.model, profile.effort,
                    attempted=False)
                for task, profile in round_assignments if profile is not None)
            state = auto_fix.with_repair_round_assignments(
                state, durable_assignments)
            state = auto_fix.with_worker_dispositions(state, ())
            auto_fix.write_auto_fix_state(state_path, state)

        failures: list[tuple[Task, str]] = []
        round_blockers: list[_AutoFixBlockerEvidence] = []
        round_dispositions: list[auto_fix.WorkerDisposition] = []
        for task, profile in round_assignments:
            if profile is None:
                failures.append((task, "No unused fixer profile remains"))
                continue

            try:
                fixer = _auto_fix_adapter(rotation, profile.adapter)
                session, errors = auto_fix_fixer_capability_errors(
                    cfg, fixer, profile.adapter, profile.model, profile.effort)
                if errors:
                    failures.append((task, "Fixer capability unavailable: "
                                     + "; ".join(errors)))
                    continue
                assert session is not None
                event = ("auto_fix_attempt_resume" if resuming_not_started
                         else "auto_fix_attempt")
                summary = (
                    "Retrying a repair assignment whose AI child did not start: "
                    if resuming_not_started else
                    "Bounded automatic repair profile consumed: ")
                append_entry(
                    task.journal_path, by="scheduler", event=event,
                    summary=(summary
                             + f"{profile.adapter}/{profile.model}/{profile.effort}"),
                    detail=(
                        "The exact pre-persisted assignment is being retried after "
                        "a synchronous process-creation failure proved that no AI "
                        "child started. No additional profile was consumed."
                        if resuming_not_started else
                        "The full round and its profiles were durably persisted "
                        "before this write-capable fixer session; all edits and "
                        "later gate evidence are preserved."),
                    agent=session.agent,
                    requested_model=session.requested_model,
                    requested_effort=session.requested_effort,
                    time_str=now().isoformat(timespec="seconds"))
                state = _auto_fix_mark_assignment_attempted(
                    state, task.id, True)
                auto_fix.write_auto_fix_state(state_path, state)
                task_rotation = _AdapterRotation(
                    (profile.adapter,), (fixer,))
                session_state = _SessionState()
                active.task = task
                active.session = session_state
                failure = _process_task(
                    cfg, task, task_rotation, sleep, now, session_state,
                    resumed=task.status == "WIP",
                    session_override=session,
                    profile_model=profile.model,
                    auto_fix_context=_auto_fix_repair_context(task, state),
                    retry_limit=0, billing_is_failure=True,
                    blocker_evidence=round_blockers,
                    auto_fix_fingerprints=state.current_finding_fingerprints,
                    repair_dispositions=round_dispositions)
                active.task = None
                active.session = None
                if round_dispositions:
                    state = auto_fix.with_worker_dispositions(
                        state, tuple(round_dispositions))
                    auto_fix.write_auto_fix_state(state_path, state)
                if failure is not None:
                    failures.append((task, failure))
            except _AdapterProcessCreationError as e:
                state = _auto_fix_mark_assignment_attempted(
                    state, task.id, False)
                auto_fix.write_auto_fix_state(state_path, state)
                active.task = None
                active.session = None
                print("Auto-fix fixer infrastructure failure before an AI child "
                      f"started; the same assignment remains resumable: {e}")
                return 1
            except OSError as e:
                if active.session is not None and active.session.identity is not None:
                    _mark_interrupted_task(
                        task, active.session.identity,
                        "Auto-fix adapter infrastructure failure; progress kept",
                        now, detail=str(e))
                failures.append((task, f"Fixer infrastructure failure: {e}"))
                active.task = None
                active.session = None

        if failures:
            if round_blockers:
                outcome = _run_auto_fix_review_once(
                    cfg, once=False, task_id=None,
                    injected_adapter=injected_reviewer, sleep=sleep, now=now,
                    blockers=tuple(round_blockers))
                if outcome.state is None or outcome.human_reason is not None:
                    return outcome.code
                state = outcome.state
            else:
                state = _auto_fix_failure_state(cfg, state, failures)
            continue
        state = auto_fix.with_repair_phase(state, "AWAITING_REVIEW")
        auto_fix.write_auto_fix_state(state_path, state)


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


def _preserve_interrupted_progress(cfg: Config, task: Task,
                                   session: SessionIdentity, *, event: str,
                                   summary: str, detail: str,
                                   checkpoint_reason: str,
                                   now: Callable[[], datetime]) -> None:
    """Record a resumable adapter interruption, checkpoint its dirty progress, and refresh.

    Quota exhaustion and the provider-neutral checkpoint-resume control have different
    continuation decisions, but both must preserve the same token-burned work before that
    decision.  The task is persisted as WIP first so a result that arrived after the execution
    AI wrote DONE cannot skip the next run's resume path; an explicit BLOCKED result remains
    untouched.  The caller owns the subsequent wait/rotation choice and sets the resume prompt.
    """
    try:
        fresh = parse_task_file(task.path)
        if fresh.status != "BLOCKED":
            set_status(task.path, "WIP")
    except Exception as e:  # status persistence must not discard progress or hide the interruption
        print(f"Writing back the resumable task status failed: {e} (working tree left as is, nothing discarded)")

    append_entry(task.journal_path, by="scheduler", event=event,
                 summary=summary, detail=detail,
                 agent=session.agent,
                 requested_model=session.requested_model,
                 requested_effort=session.requested_effort,
                 time_str=now().isoformat(timespec="seconds"))
    if gitops.commit_if_dirty(
            cfg.root, _checkpoint_subject(cfg, "wip", task, checkpoint_reason),
            cfg.git_excludes):
        print("  wip checkpoint created.")
    try_write_report(cfg)


def _commit_terminal_checkpoint(cfg: Config, task: Task, *, resumed: bool,
                                session_state: _SessionState) -> bool:
    """Create the one terminal auto checkpoint after a task passes every acceptance gate.

    Ordinary success keeps its existing dirty-tree behavior.  A task that resumed from WIP is
    different: its content may already be present in the preceding WIP commit, so a clean tree
    still needs one empty, namespaced auto marker proving terminal ownership.  The caller invokes
    this once, only after ``_evaluate`` returns ``done``.
    """
    subject = _checkpoint_subject(cfg, "auto", task, _short(task.title) or "done")
    pre_attempt_head = gitops.head_ref(cfg.root)
    if pre_attempt_head is not None:
        # This in-memory witness is intentionally armed at the closeout boundary, not at task
        # start.  It lets Ctrl+C recover a commit that succeeded just before the return path.
        session_state.terminal_checkpoint_attempt = (pre_attempt_head, subject)
    else:
        session_state.terminal_checkpoint_attempt = None
    if gitops.commit_if_dirty(cfg.root, subject, cfg.git_excludes):
        return True
    if resumed:
        gitops.commit_empty(cfg.root, subject)
        return True
    # No terminal operation was needed for a non-resumed clean task.  Do not leave a witness
    # armed while the post-closeout report is being refreshed.
    session_state.terminal_checkpoint_attempt = None
    return False


def _terminal_checkpoint_matches_attempt(
        cfg: Config, attempt: tuple[str, str] | None) -> bool:
    """Prove that an interrupted closeout created its exact auto commit.

    The attempt is armed only immediately before the scheduler's terminal commit, after
    ``_evaluate`` has passed.  Requiring first-parent history to contain the recorded subject
    with the recorded pre-attempt HEAD as its only parent rejects matching commits made by the
    execution AI, a retry, or any earlier phase of the task.
    """
    if attempt is None:
        return False
    base_ref, expected = attempt
    try:
        history = gitops.commit_history(cfg.root)
    except AssentError:
        return False
    return any(subject == expected and parents == (base_ref,)
               for _commit, parents, subject in history)


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


def _repair_dispositions_from_journal(
        task: Task, journal_start: int,
        expected_fingerprints: tuple[str, ...]) -> tuple[auto_fix.WorkerDisposition, ...]:
    """Validate the final worker-authored closeout entry for one repair session."""
    entries = read_entries(task.journal_path)
    worker_entries = [
        item for item in entries[journal_start:]
        if item.get("by") != "scheduler"
    ]
    if not worker_entries:
        raise AssentError("Repair closeout has no worker-authored journal entry")
    fresh = parse_task_file(task.path)
    return auto_fix.parse_repair_dispositions(
        worker_entries[-1].get("detail", ""), task_id=task.id,
        task_status=fresh.status,
        expected_fingerprints=expected_fingerprints)


def _process_task(cfg: Config, task: Task, rotation: _AdapterRotation,
                  sleep: Callable[[float], None],
                  now: Callable[[], datetime], session_state: _SessionState,
                  resumed: bool = False, *,
                  session_override: SessionIdentity | None = None,
                  profile_model: str | None = None,
                  auto_fix_context: str = "",
                  retry_limit: int | None = None,
                  billing_is_failure: bool = False,
                  blocker_evidence: list[_AutoFixBlockerEvidence] | None = None,
                  auto_fix_fingerprints: tuple[str, ...] = (),
                  repair_dispositions: list[auto_fix.WorkerDisposition] | None = None,
                  ) -> str | None:
    """Run a single task's full lifecycle; internally handles quota/control resumption and
    retries, and by the end the task is DONE/BLOCKED.

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
    attempted_failures: list[tuple[str, str]] = []
    # Repair disposition validation needs a precise journal boundary. Ordinary
    # sessions retain the historical behavior in which a malformed pre-existing
    # journal does not prevent the worker prompt or task execution from running.
    journal_start = (len(read_entries(task.journal_path))
                     if auto_fix_fingerprints else 0)
    while True:
        adapter = rotation.adapter
        adapter_name = rotation.name
        session = (session_override or
                   resolve_session(cfg, adapter, task, adapter_name))
        session_state.identity = session
        prompt = _build_prompt(cfg, task, failure_reason, session, resumed)
        if auto_fix_context:
            prompt += auto_fix_context
        print(_session_line(
            adapter_name, task, session, model=profile_model))
        main_tree_baseline = (gitops.dirty_paths(cfg.source_root, _task_excludes(cfg, task))
                              if cfg.source_root is not None else None)
        try:
            result = adapter.run_task(
                prompt, session.requested_model, session.requested_effort,
                cfg.root)
        except OSError as e:
            # Built-in adapters call their subprocess launcher synchronously and
            # return TaskResult only after a child existed. No returned result
            # plus OSError therefore identifies the pre-child creation boundary.
            raise _AdapterProcessCreationError(str(e)) from e
        if not result.quota_exhausted:
            rotation.session_opened()
        escape_reason = (
            _handle_main_tree_escape(cfg, task, main_tree_baseline, now)
            if main_tree_baseline is not None else None)

        if escape_reason is not None:
            print(f"  {escape_reason}")
            outcome, reason = "fail", escape_reason
            focused_evidence = (
                "NOT RUN: the isolated-worktree escape safety gate failed first.")
        elif (result.checkpoint_resume and not result.quota_exhausted
              and not result.stalled and result.exit_code != 0):
            print("  Checkpoint-resume control received -> keep progress (wip checkpoint).")
            _preserve_interrupted_progress(
                cfg, task, session, event="checkpoint_resume",
                summary=("Checkpoint-resume requested; progress kept, immediately "
                         "resuming the same adapter command"),
                detail=("The adapter emitted the exact final control record "
                        '{"type":"assent.checkpoint_resume"}; no quota wait, '
                        "adapter rotation, or retry was used."),
                checkpoint_reason="checkpoint-resume control, progress kept", now=now)
            print("  Resuming the same adapter command immediately without waiting, "
                  "rotating, or consuming a retry.")
            resumed = True
            continue
        elif result.quota_exhausted:  # quota exhaustion does not count as a failure
            print("  Quota exhausted -> keep progress (wip checkpoint).")
            wait_kind: str | None = None
            if len(rotation.names) == 1:
                if result.reset_at is None:
                    quota_summary = (
                        "Quota exhausted; progress kept, waiting for quota poll "
                        f"(every {cfg.quota_poll_minutes} minutes) before resuming")
                    quota_action = (
                        "  Waiting for quota poll "
                        f"(every {cfg.quota_poll_minutes} minutes) before resuming...")
                else:
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
            _preserve_interrupted_progress(
                cfg, task, session, event="quota", summary=quota_summary,
                detail="Quota evidence preserved the current task progress for resumption.",
                checkpoint_reason="quota interrupt, progress kept", now=now)
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
            if not billing_is_failure:
                raise _BillingAbort(reason)
            _append_adapter_failure_entry(
                task, session, result.exit_code, result.stalled, reason, now,
                failure_kind=result.failure_kind)
            outcome = "fail"
            focused_evidence = (
                "NOT RUN: the worker adapter failed before focused closeout.")
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
            focused_evidence = (
                "NOT RUN: the worker adapter or structural safety gate failed first.")
        else:
            outcome, reason, focused_evidence = _evaluate(cfg, task, start_ref)
        if outcome in {"done", "self_blocked"} and auto_fix_fingerprints:
            try:
                dispositions = _repair_dispositions_from_journal(
                    task, journal_start, auto_fix_fingerprints)
            except AssentError as e:
                outcome = "fail"
                reason = f"Repair disposition gate failed: {e}"
            else:
                if repair_dispositions is not None:
                    repair_dispositions.extend(dispositions)
        if outcome == "done":
            print("  Acceptance passed -> creating checkpoint")
            committed = _commit_terminal_checkpoint(
                cfg, task, resumed=resumed, session_state=session_state)
            # Report refresh is deliberately after the Git evidence.  Keep this marker before
            # entering it so Ctrl+C there cannot downgrade a task whose terminal ownership is
            # already recorded or create a second WIP checkpoint.
            session_state.terminal_checkpoint = committed
            if not committed:
                print("  (no new changes in the working tree; progress is already in a prior wip checkpoint)")
            elif resumed:
                print("  terminal auto checkpoint created; resumed progress was preserved")
            try_write_report(cfg)
            return None
        if outcome == "self_blocked":
            print("  Execution AI self-marked BLOCKED (legal output, handed to a human) -> creating checkpoint")
            gitops.commit_if_dirty(
                cfg.root, _checkpoint_subject(
                    cfg, "auto", task, "BLOCKED (execution AI self-marked)"),
                cfg.git_excludes)
            if blocker_evidence is not None:
                worker_entries = [
                    item for item in read_entries(task.journal_path)[journal_start:]
                    if item.get("by") != "scheduler"
                ]
                worker_reason = str(
                    worker_entries[-1].get("summary")
                    if worker_entries else "Execution AI self-marked BLOCKED")
                evidence = _AutoFixBlockerEvidence(
                    task, "worker_blocked", "Execution AI self-marked BLOCKED",
                    "NOT RUN: self-marked BLOCKED closeout legitimately skips the focused gate.",
                    worker_reason)
                blocker_evidence.append(evidence)
                append_entry(
                    task.journal_path, by="scheduler", event="auto_fix_blocker",
                    summary=evidence.reason,
                    detail=("Worker journal summary (verbatim):\n"
                            f"{worker_reason}\nFocused evidence:\n"
                            f"{evidence.focused_evidence}"),
                    agent=session.agent,
                    requested_model=session.requested_model,
                    requested_effort=session.requested_effort,
                    time_str=now().isoformat(timespec="seconds"))
            try_write_report(cfg)
            return "Execution AI self-marked BLOCKED"

        # outcome == "fail": no restore (output kept), retry with the reason; once retries are
        # exhausted the scheduler marks BLOCKED, and the not-yet-passing work is committed
        # together with the BLOCKED mark for a human to make the final call.
        print(f"  Acceptance failed: {reason}")
        attempted_failures.append((reason or "acceptance failed",
                                   focused_evidence))
        allowed_retries = (cfg.retry_per_task
                           if retry_limit is None else retry_limit)
        if attempts_used < allowed_retries:
            attempts_used += 1
            failure_reason = reason
            print(f"  Keeping existing work, retrying with the failure reason (attempt {attempts_used})...")
            continue
        print("  Retries exhausted -> scheduler marks BLOCKED (the not-yet-passing work is kept too)")
        _mark_blocked(cfg, task, session, reason or "acceptance failed", now,
                      attempts=attempts_used)
        if blocker_evidence is not None:
            combined_reason = " | ".join(dict.fromkeys(
                item[0] for item in attempted_failures))
            focused_items = list(dict.fromkeys(
                item[1] for item in attempted_failures))
            combined_focused = "\n".join(focused_items)
            trigger = ("focused_gate_failure"
                       if any(item.startswith("FAIL:")
                              for item in focused_items)
                       else "worker_blocked")
            evidence = _AutoFixBlockerEvidence(
                task, trigger, combined_reason, combined_focused)
            blocker_evidence.append(evidence)
            append_entry(
                task.journal_path, by="scheduler", event="auto_fix_blocker",
                summary=combined_reason,
                detail=f"Focused evidence:\n{combined_focused}",
                agent=session.agent,
                requested_model=session.requested_model,
                requested_effort=session.requested_effort,
                time_str=now().isoformat(timespec="seconds"))
        try_write_report(cfg)
        return reason or "acceptance failed"


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
              start_ref: str | None = None) -> tuple[str, str | None, str]:
    """Acceptance: structural/scope safety -> status -> verify.

    Returns ``(outcome, reason, focused_evidence)``.

    outcome in {"done", "self_blocked", "fail"}. scope/verify and all fields come from the
    trusted checkpoint version `task`; the on-disk version is only allowed to change the
    status line.
    """
    fresh, safety_reason = _inspect_task_safety(cfg, task, start_ref)
    if safety_reason:
        return ("fail", safety_reason,
                "NOT RUN: structural/scope safety failed before the focused gate.")
    assert fresh is not None

    # status check
    if fresh.status == "BLOCKED":
        return ("self_blocked", None,
                "NOT RUN: self-marked BLOCKED closeout skips the focused gate.")
    if fresh.status != "DONE":
        # Structure and scope are already clean here; the only remaining acceptance gap is
        # the status line. Probe the focused verify once (quietly, so a still-failing verify
        # leaves this path's output byte-for-byte identical to before) to tell a genuine
        # implementation gap apart from a session that simply dropped off before closeout.
        if _run_verify_quiet(cfg, task.verify) == 0:
            return ("fail", _CLOSEOUT_ONLY_REASON_TEMPLATE.format(
                status=fresh.status, verify_command=task.verify),
                f"PASS (closeout probe): {task.verify}")
        return ("fail", f"Status not updated to DONE/BLOCKED (currently {fresh.status})",
                f"FAIL (closeout probe): {task.verify}")

    # verify command (against the trusted checkpoint verify)
    rc = _run_verify(cfg, task.verify)
    focused_evidence = (f"{'PASS' if rc == 0 else 'FAIL'}: exit {rc}: "
                        f"{task.verify}")
    if rc != 0:
        return ("fail", "Verify command exit code is non-zero "
                f"(={rc}): {task.verify}",
                focused_evidence)

    # A session handed the bounded shared-path review clause must have run the
    # controlled operation; a source snapshot that is still UNKNOWN or STALE is
    # refused with a precise retry reason rather than closed out.
    try:
        contract = _shared_paths_contract(cfg)
    except AssentError as e:
        return ("fail", f"Shared-path contract could not be classified: {e}",
                focused_evidence)
    refusal = shared_paths.closeout_refusal(contract)
    if refusal:
        return "fail", refusal[:1].upper() + refusal[1:], focused_evidence

    return "done", None, focused_evidence


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
    result = _verify_subprocess(cfg, command)
    _show_verify_result(command, result)
    return result.returncode


def _show_verify_result(
        command: str, result: subprocess.CompletedProcess) -> None:
    """Render one already-completed focused command without rerunning it."""
    print(f"  verify: {command}")
    if result.returncode != 0:
        print(f"  verify failed (exit {result.returncode})")
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-8:]
        if tail:
            print("  -- verify output (tail) --")
            for line in tail:
                print(f"  | {line}")
    else:
        print("  verify passed (exit 0)")


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
