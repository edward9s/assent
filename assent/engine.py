"""Finite task, plan, and integration workflow execution.

Every layer is an ordered array of role sessions and scheduler actions. Role
success advances once, action success completes the layer, and exhaustion
preserves edits and evidence for a human decision. Git and scheduler state stay
outside AI control, and token-burned work is never reverted automatically.
"""

from __future__ import annotations

import contextlib

import hashlib

import json

import os

import re

import signal

import subprocess

import sys

import time

from collections import Counter

from dataclasses import dataclass, field, replace

from datetime import datetime, timedelta, timezone

from pathlib import Path

from typing import Callable, TextIO

from assent import (AssentError, contracts, gitops, lockfile, reconcile,
                    ignored_dirs, runtime_test, usage, verification)

from assent.adapters import Adapter, InvocationRequest, get_adapter

from assent.adapters.process import (clear_stop_wake, interruptible_sleep,
                                     run_subprocess as _adapter_run_subprocess,
                                     stop_wake_requested)

from assent.config import (PROJECT_LAYER, Config, WorkflowActionStep, WorkflowRoleStep,
                           WorkflowTaskStep, load_config)

from assent.batch_verification import (SelectionCandidateConflict,
                                       selection_conflict_line,
                                       selection_conflicts_from_evidence,
                                       verify_selected_batch_action)

from assent.plandeps import (find_unfinished_prerequisites,
                               infer_plan_completion,
                               order_plans_by_dependency,
                               parse_plan_dependency_graph)

from assent.plan_verification_closeout import verify_plan_action

from assent.inspection import try_write_report

from assent.modeling import effort_identity

from assent.plan import (Plan, RuntimeQuotaWait,
                         Task, TaskWorkflowAction, append_entry,
                         encode_runtime_action_results,
                         parse_runtime_action_results,
                         parse_runtime_test_contract, parse_task_file,
                         plan_workflow_requires_human,
                         read_runtime_test_workflow_state,
                         read_selection_workflow_state,
                         read_workflow_state,
                         selection_workflow_state_path,
                         set_status, WorkflowState,
                         SelectionWorkflowState,
                         runtime_test_workflow_state_path,
                         workflow_state_path, write_selection_workflow_state,
                         write_runtime_test_workflow_state,
                         write_workflow_state)

from assent.preflight import (GIT_REQUIRED_MESSAGE, SessionIdentity,
                              StackState, capability_errors, has_git_marker,
                              literal_adapter_errors,
                              resolve_session, resolve_stack_state,
                              runtime_test_adapter_names,
                              runtime_test_capability_errors,
                              worktree_configuration_errors)

from assent.verification_common import (source_snapshot,
                                        summary as verification_summary)

def _invoke_adapter(
        cfg: Config, adapter: Adapter, adapter_name: str, prompt: str,
        requested_model: str, requested_effort: str | None, cwd, *,
        context_kind: str, context_id: str,
        plan_names: tuple[str, ...] | None = None):
    """Run one provider command and record its result without making usage gating."""
    invocation_id = usage.new_invocation_id()
    result = adapter.run_task(
        prompt, requested_model, requested_effort, cwd)
    try:
        usage.record_invocation(
            cfg.assent_dir, invocation_id=invocation_id, adapter=adapter_name,
            requested_model=requested_model, context_kind=context_kind,
            context_id=context_id, plan_names=plan_names or (cfg.tasks_name,),
            evidence=result.usage)
    except Exception:
        # Telemetry is derived observability evidence and can never replace the
        # provider result with a workflow failure.
        pass
    return result

_QUOTA_BUFFER = timedelta(minutes=2)  # reset time + buffer, to avoid being blocked again right at the edge

_QUOTA_TICK = 1.0                     # countdown refresh interval (seconds)

_COUNTDOWN_SEGMENT = 60.0

_ADAPTER_DIAGNOSTIC_LIMIT = 240

_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)([\"']?(?:api[_ -]?key|access[_ -]?token|token|password|secret|"
    r"authorization)[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)")

_BEARER_RE = re.compile(r"(?i)(\bbearer\s+)[^\s,;]+")

_KNOWN_TOKEN_RE = re.compile(
    r"\b(?:sk(?:-ant)?|ghp|github_pat)-[A-Za-z0-9_-]{8,}\b")

_IGNORED_INPUT_VIOLATION = "ignored-input control violation: "

@dataclass
class _ActiveTask:
    """Current task/session identity for interrupt and billing closeout."""

    task: Task | None = None
    session: SessionIdentity | None = None

@dataclass(frozen=True)
class _RoleStep:
    """One AI session after task, plan, or integration defaults are resolved."""

    role: str
    adapters: tuple[str, ...]
    model: str
    writes: bool
    prompt: str

@dataclass
class _AdapterRotation:
    """Run-scoped adapter cursor and quota evidence shared across tasks."""

    names: tuple[str, ...]
    adapters: tuple[Adapter, ...]
    index: int = 0
    exhausted: set[str] = field(default_factory=set)
    auth_failed: set[str] = field(default_factory=set)
    pool: dict[str, Adapter] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.pool:
            self.pool = dict(zip(self.names, self.adapters))

    @property
    def name(self) -> str:
        return self.names[self.index]

    @property
    def adapter(self) -> Adapter:
        return self.adapters[self.index]

    def session_opened(self) -> None:
        """A non-quota result proves the rotation is no longer fully exhausted."""
        self.exhausted.clear()

    def advance_after_quota(self, failed: set[str] | None = None) -> bool:
        """Move to the next adapter; return whether this exhausted one complete cycle."""
        self.exhausted.add(self.name)
        unavailable = self.exhausted | self.auth_failed | (failed or set())
        recoverable = [index for index, name in enumerate(self.names)
                       if name not in self.auth_failed]
        if not recoverable:
            raise AssentError("Adapter rotation has no authenticated candidate")
        cycle_exhausted = all(
            self.names[index] in unavailable for index in recoverable)
        if cycle_exhausted:
            preferred = next(
                (index for index in recoverable
                 if self.names[index] in self.exhausted), recoverable[0])
            self.exhausted.clear()
            if failed is not None:
                failed.clear()
            self.index = preferred
            return True
        for offset in range(1, len(self.names) + 1):
            index = (self.index + offset) % len(self.names)
            if self.names[index] not in unavailable:
                self.index = index
                break
        return cycle_exhausted

    def subset(self, names: tuple[str, ...]) -> "_AdapterRotation":
        """Return a fresh step-local cursor backed by the preflighted adapters."""
        try:
            adapters = tuple(self.pool[name] for name in names)
        except KeyError as error:
            raise AssentError(
                f"Task workflow adapter {error.args[0]!r} was not preflighted") from error
        return _AdapterRotation(names, adapters, pool=self.pool)

    def advance_after_failure(self, failed: set[str]) -> bool:
        """Try the next not-yet-failed candidate in this step-local cursor."""
        failed.add(self.name)
        unavailable = failed | self.exhausted | self.auth_failed
        for offset in range(1, len(self.names)):
            index = (self.index + offset) % len(self.names)
            if self.names[index] not in unavailable:
                self.index = index
                return True
        recoverable = [index for index, name in enumerate(self.names)
                       if name not in self.auth_failed]
        failed.clear()
        self.exhausted.clear()
        self.index = recoverable[0]
        return False

    def advance_after_authentication(self, failed: set[str]) -> str:
        """Skip one unauthenticated candidate; switch, wait, or require login."""
        self.auth_failed.add(self.name)
        unavailable = self.auth_failed | self.exhausted | failed
        for offset in range(1, len(self.names)):
            index = (self.index + offset) % len(self.names)
            if self.names[index] not in unavailable:
                self.index = index
                return "switch"
        recoverable = [index for index, name in enumerate(self.names)
                       if name not in self.auth_failed]
        if not recoverable:
            return "required"
        preferred = next(
            (index for index in recoverable
             if self.names[index] in self.exhausted), recoverable[0])
        failed.clear()
        self.exhausted.clear()
        self.index = preferred
        return "wait"

class _BillingAbort(Exception):
    """An account-level billing/insufficient-balance failure the adapter classified.

    Unlike quota (a rate-limit window that resets on its own), a prepaid balance does not
    refill. This unwinds the run, keeps progress in a WIP checkpoint, and leaves the task
    resumable after a manual top-up. It is dispatched purely on
    ``TaskResult.failure_kind == "billing"``, never on an adapter name.
    """

class _AuthenticationRequired(AssentError):
    """Every declared adapter for one workflow step requires human login."""

def _authentication_required_message(rotation: _AdapterRotation,
                                     reason: str) -> str:
    names = ", ".join(rotation.names)
    return ("AUTHENTICATION REQUIRED: every declared adapter requires login "
            f"({names}). Sign in, then rerun to resume. Last failure: {reason}")

def _authentication_failover_action(rotation: _AdapterRotation,
                                    failed: set[str], reason: str) -> str:
    action = rotation.advance_after_authentication(failed)
    if action == "required":
        raise _AuthenticationRequired(
            _authentication_required_message(rotation, reason))
    return action

def _adapter_availability_failed(result: TaskResult) -> bool:
    """Return whether a failed provider session may use a declared fallback."""
    return ((result.exit_code != 0 or result.stalled)
            and result.failure_kind not in {
                "authentication", "billing", "interrupt", "permission",
                "unsupported_model"})

def _diagnosed_ignored_directory_inputs(cfg: Config) -> tuple[str, ...]:
    """Directories a stored full-verifier diagnosis proved this plan needs.

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
    return verification.diagnosed_ignored_dirs(receipt.failure_summary)

def _ignored_dir_decision(cfg: Config) -> ignored_dirs.Decision:
    """Classify and provision required ignored directories for this source."""
    return ignored_dirs.prepare_worktree(
        gitops.main_worktree(cfg.root), cfg.root,
        required_evidence=_diagnosed_ignored_directory_inputs(cfg))


@dataclass(frozen=True)
class _IgnoredInputGuard:
    """Exact ignored-directory contents visible to one AI role."""

    main: Path
    paths: tuple[str, ...]
    snapshots: tuple[tuple[str, str], ...]


def _ignored_input_guards(configs: tuple[Config, ...]) -> tuple[_IgnoredInputGuard, ...]:
    """Snapshot ignored inputs before an AI role can write through their links.

    A settled profile needs only its required paths. An unsettled review can
    declare any inventory path during the role, so that one session snapshots
    the complete candidate inventory instead.
    """
    paths_by_main: dict[Path, set[str]] = {}
    for cfg in configs:
        if cfg.source_root is None and cfg.tasks_dir == cfg.assent_dir:
            # Main runtime repair has no ignored-directory profile or links.
            continue
        main = gitops.main_worktree(cfg.root)
        decision = ignored_dirs.classify(
            main, cfg.root, ignored_dirs.read_manifest(main),
            required_evidence=_diagnosed_ignored_directory_inputs(cfg))
        paths = decision.required if decision.settled else decision.inventory
        paths_by_main.setdefault(main.resolve(), set()).update(paths)
    guards = []
    for main, paths in sorted(paths_by_main.items(), key=lambda item: str(item[0])):
        ordered = tuple(sorted(paths))
        guards.append(_IgnoredInputGuard(
            main, ordered,
            tuple((path, ignored_dirs.snapshot_target(main, path))
                  for path in ordered)))
    return tuple(guards)


def _ignored_input_violations(
        guards: tuple[_IgnoredInputGuard, ...],
        configs: tuple[Config, ...]) -> list[str]:
    """Return mutations and post-hoc declarations that crossed the input boundary."""
    violations: list[str] = []
    guarded_by_main = {guard.main: set(guard.paths) for guard in guards}
    for guard in guards:
        for relative, before in guard.snapshots:
            try:
                after = ignored_dirs.snapshot_target(guard.main, relative)
            except AssentError as error:
                violations.append(f"ignored input {relative}: {error}")
                continue
            if after != before:
                violations.append(f"ignored input changed: {relative}")
    for cfg in configs:
        if cfg.source_root is None and cfg.tasks_dir == cfg.assent_dir:
            continue
        main = gitops.main_worktree(cfg.root).resolve()
        try:
            decision = ignored_dirs.classify(
                main, cfg.root, ignored_dirs.read_manifest(main),
                required_evidence=_diagnosed_ignored_directory_inputs(cfg))
        except AssentError as error:
            violations.append(f"ignored input decision: {error}")
            continue
        added = sorted(set(decision.required) - guarded_by_main.get(main, set()))
        violations.extend(
            f"ignored input declared after the role started: {relative}"
            for relative in added)
    return violations


def _ignored_input_violation(evidence: tuple[str, ...]) -> str:
    """Return the durable ignored-input violation, if this source has one."""
    return next((item for item in evidence
                 if item.startswith(_IGNORED_INPUT_VIOLATION)), "")


def _record_ignored_input_violation(
        state: WorkflowState | SelectionWorkflowState,
        violations: list[str]) -> WorkflowState | SelectionWorkflowState:
    evidence = state.evidence + (
        _IGNORED_INPUT_VIOLATION + ", ".join(violations[:8]),)
    return replace(state, evidence=evidence[-_SESSION_EVIDENCE_ITEMS:])

@dataclass(frozen=True)
class _TestActionEvidence:
    status: str
    identity: str
    exit_code: int
    command: str
    summary: str


@dataclass(frozen=True)
class _RuntimeActionEvidence:
    status: str
    identity: str
    exit_code: int
    results: tuple[tuple[str, int | None], ...]
    summary: str

    @property
    def command(self) -> str:
        """Render the configured sequence for shared unresolved diagnostics."""
        return "\n".join(command for command, _exit_code in self.results)


_RUNTIME_SUMMARY_LIMIT = 4096


def _runtime_worktree_identity(cfg: Config) -> str:
    """Hash the current HEAD plus every ordinary working-tree change."""
    digest = hashlib.sha256()
    digest.update(gitops.commit_of(cfg.root, "HEAD").encode("ascii"))
    for relative in sorted(gitops.dirty_paths(cfg.root, cfg.git_excludes)):
        path = cfg.root / relative
        digest.update(b"\0" + relative.encode("utf-8") + b"\0")
        if path.is_symlink():
            digest.update(b"link\0" + os.readlink(path).encode("utf-8"))
        elif path.is_file():
            digest.update(b"file\0")
            with path.open("rb") as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(block)
        elif path.exists():
            digest.update(b"other")
        else:
            digest.update(b"missing")
    return digest.hexdigest()


def _runtime_test_identity(
        cfg: Config, commands: tuple[str, ...], *, allow_dirty: bool = False
        ) -> str:
    """Bind runtime evidence to the exact source and ordered commands."""
    if (not allow_dirty
            and not gitops.working_tree_status(
                cfg.root, cfg.git_excludes).is_clean):
        raise AssentError("source worktree is dirty at the runtime_test boundary")
    source = (_runtime_worktree_identity(cfg) if allow_dirty else
              gitops.commit_of(cfg.root, "HEAD"))
    return runtime_test.evidence_identity(source, commands)


def _runtime_test_record(state: WorkflowState) -> _RuntimeActionEvidence | None:
    if state.unit != "runtime_test" or state.action != "runtime_test":
        return None
    if not state.action_status:
        return None
    if len(state.action_evidence) != 2:
        raise AssentError("Runtime action evidence is unreadable")
    record = _RuntimeActionEvidence(
        state.action_status, state.action_source_tree, state.action_exit_code,
        parse_runtime_action_results(state.action_evidence[0]),
        state.action_evidence[1])
    if record.status not in {"PASSED", "FAILED", "STALE"}:
        raise AssentError("Runtime action evidence has invalid values")
    return record


def _with_runtime_test_record(
        state: WorkflowState, record: _RuntimeActionEvidence) -> WorkflowState:
    return replace(
        state, action="runtime_test", action_status=record.status,
        action_source_tree=record.identity,
        action_exit_code=record.exit_code,
        action_evidence=(encode_runtime_action_results(record.results),
                         record.summary))


def _bounded_runtime_summary(current: str, addition: str) -> str:
    merged = current + addition
    if len(merged) <= _RUNTIME_SUMMARY_LIMIT:
        return merged
    marker = "\n... runtime output truncated ...\n"
    remaining = _RUNTIME_SUMMARY_LIMIT - len(marker)
    start = remaining // 2
    return merged[:start] + marker + merged[-(remaining - start):]


def _terminate_runtime_process(process: subprocess.Popen) -> None:
    """Terminate and reap the runtime command's whole process group."""
    if process.poll() is not None:
        process.wait()
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def _run_runtime_test_action(
        cfg: Config, owner_dir: Path, commands: tuple[str, ...],
        state: WorkflowState, *, allow_dirty: bool = False
        ) -> tuple[WorkflowState, _RuntimeActionEvidence, bool]:
    """Run ordered runtime commands, stopping at the first failure."""
    identity = _runtime_test_identity(cfg, commands, allow_dirty=allow_dirty)
    existing = _runtime_test_record(state)
    if (existing is not None and existing.identity == identity
            and existing.status == "PASSED"):
        if tuple(command for command, _exit_code in existing.results) != commands:
            raise AssentError(
                "Runtime PASSED evidence names different commands")
        print(f"  runtime_test passed evidence reused (exit {existing.exit_code})")
        return state, existing, True

    armed = _RuntimeActionEvidence(
        "STALE", identity, 0,
        tuple((command, None) for command in commands),
        "Runtime commands have not completed for this source identity.\n")
    state = _with_runtime_test_record(state, armed)
    write_runtime_test_workflow_state(owner_dir, state)
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    options = dict(
        cwd=str(cfg.root), shell=True, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        errors="replace", bufsize=1, env=environment)
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    record = armed
    for index, command in enumerate(commands):
        heading = f"\nCommand {index + 1}/{len(commands)}: {command}\n"
        print(heading, end="", flush=True)
        record = replace(
            record, summary=_bounded_runtime_summary(record.summary, heading))
        state = _with_runtime_test_record(state, record)
        write_runtime_test_workflow_state(owner_dir, state)
        try:
            process = subprocess.Popen(command, **options)
        except (OSError, ValueError) as error:
            results = list(record.results)
            results[index] = (command, 1)
            record = replace(
                record, status="FAILED", exit_code=1, results=tuple(results),
                summary=_bounded_runtime_summary(
                    record.summary, f"Unable to start runtime command: {error}\n"))
            state = _with_runtime_test_record(state, record)
            write_runtime_test_workflow_state(owner_dir, state)
            break
        try:
            assert process.stdout is not None
            for line in process.stdout:
                print(line, end="", flush=True)
                record = replace(
                    record,
                    summary=_bounded_runtime_summary(record.summary, line))
                state = _with_runtime_test_record(state, record)
                write_runtime_test_workflow_state(owner_dir, state)
            return_code = process.wait()
        except KeyboardInterrupt:
            _terminate_runtime_process(process)
            record = replace(
                record,
                summary=_bounded_runtime_summary(
                    record.summary, "Runtime command interrupted.\n"))
            state = _with_runtime_test_record(state, record)
            write_runtime_test_workflow_state(owner_dir, state)
            raise
        except BaseException:
            _terminate_runtime_process(process)
            raise
        finally:
            if process.stdout is not None:
                process.stdout.close()

        results = list(record.results)
        results[index] = (command, return_code)
        failed = return_code != 0
        record = replace(
            record, status="FAILED" if failed else "STALE",
            exit_code=return_code if failed else 0,
            results=tuple(results),
            summary=_bounded_runtime_summary(
                record.summary,
                ("Runtime command passed.\n" if not failed else
                 f"Runtime command failed (exit code {return_code}).\n")))
        state = _with_runtime_test_record(state, record)
        write_runtime_test_workflow_state(owner_dir, state)
        if failed:
            break
    else:
        record = replace(record, status="PASSED", exit_code=0)

    try:
        unchanged = (_runtime_test_identity(
            cfg, commands, allow_dirty=allow_dirty) == identity)
    except AssentError:
        unchanged = False
    if not unchanged:
        record = replace(
            record, status="STALE",
            summary=_bounded_runtime_summary(
                record.summary,
                "Source or runtime command list changed during runtime_test.\n"))
    state = _with_runtime_test_record(state, record)
    write_runtime_test_workflow_state(owner_dir, state)
    print(f"  runtime_test evidence: {record.status} (exit {record.exit_code})")
    return state, record, False


def _runtime_test_prompt(state: WorkflowState) -> str:
    record = _runtime_test_record(state)
    if record is None:
        return ""
    outcomes = "\n".join(
        f"- {command}: "
        + ("not run" if exit_code is None else f"exit {exit_code}")
        for command, exit_code in record.results)
    return (
        "\nRUNTIME TEST EVIDENCE\n"
        f"Status: {record.status}\n"
        f"Exit code: {record.exit_code}\n"
        f"Commands:\n{outcomes}\n"
        f"Bounded output:\n{record.summary}\n")


def _write_source_workflow_state(owner: Path, state: WorkflowState) -> None:
    if state.unit == "runtime_test":
        write_runtime_test_workflow_state(owner, state)
    else:
        write_workflow_state(owner, state)

def _focused_sweep_identity(cfg: Config, plan: Plan) -> str:
    """Bind a plan sweep to the source tree and its distinct commands."""
    if not gitops.working_tree_status(cfg.root, cfg.git_excludes).is_clean:
        raise AssentError("source worktree is dirty at the focused_sweep boundary")
    commands = _plan_verify_commands(plan)
    command_sha256 = hashlib.sha256(
        json.dumps(commands, ensure_ascii=False, separators=(",", ":"))
        .encode("utf-8")).hexdigest()
    return f"{gitops.tree_of(cfg.root, 'HEAD')}:{command_sha256}"

def _focused_sweep_record(state: WorkflowState) -> _TestActionEvidence | None:
    if state.unit != "plan" or state.action != "focused_sweep":
        return None
    if not state.action_status:
        return None
    if len(state.action_evidence) != 2:
        raise AssentError("Plan focused_sweep action evidence is unreadable")
    record = _TestActionEvidence(
        state.action_status, state.action_source_tree, state.action_exit_code,
        state.action_evidence[0], state.action_evidence[1])
    if record.status not in {"PASSED", "FAILED", "STALE"}:
        raise AssentError("Plan focused_sweep action evidence has invalid values")
    return record

def _with_focused_sweep_record(
        state: WorkflowState, record: _TestActionEvidence) -> WorkflowState:
    return replace(
        state, action="focused_sweep", action_status=record.status,
        action_source_tree=record.identity,
        action_exit_code=record.exit_code,
        action_evidence=(record.command, record.summary))

def _run_focused_sweep_action(
        cfg: Config, plan: Plan,
        state: WorkflowState) -> tuple[WorkflowState, _TestActionEvidence, bool]:
    """Run or reuse the plan's distinct focused commands."""
    identity = _focused_sweep_identity(cfg, plan)
    existing = _focused_sweep_record(state)
    if (existing is not None and existing.identity == identity
            and existing.status == "PASSED"):
        print(f"  focused_sweep {existing.status.lower()} evidence reused "
              f"(exit {existing.exit_code})")
        return state, existing, True

    commands = _plan_verify_commands(plan)
    command = "\n".join(commands)
    armed = _TestActionEvidence(
        "STALE", identity, 0, command,
        "Focused sweep has not completed for this source identity.")
    state = _with_focused_sweep_record(state, armed)
    write_workflow_state(cfg.tasks_dir, state)
    evidence: list[str] = []
    exit_code = 0
    merged = _merged_unittest_passes(cfg, commands)
    for focused_command in commands:
        result = merged.get(focused_command)
        if result is None:
            result = _verify_subprocess(cfg, focused_command)
        _show_verify_result(focused_command, result)
        evidence.append(
            f"{'PASS' if result.returncode == 0 else 'FAIL'}: "
            f"exit {result.returncode}: {focused_command}")
        if result.returncode != 0 and exit_code == 0:
            exit_code = result.returncode
    record = replace(
        armed, status="PASSED" if exit_code == 0 else "FAILED",
        exit_code=exit_code, summary="\n".join(evidence))
    try:
        unchanged = _focused_sweep_identity(cfg, plan) == identity
    except AssentError:
        unchanged = False
    if not unchanged:
        record = replace(
            record, status="STALE",
            summary=verification_summary(
                record.summary,
                "Source or focused commands changed during focused_sweep."))
    state = _with_focused_sweep_record(state, record)
    write_workflow_state(cfg.tasks_dir, state)
    print(f"  focused_sweep evidence: {record.status} (exit {record.exit_code})")
    return state, record, False

def _focused_sweep_prompt(state: WorkflowState) -> str:
    record = _focused_sweep_record(state)
    if record is None:
        return ""
    return (
        "\nFOCUSED SWEEP EVIDENCE\n"
        f"Status: {record.status}\n"
        f"Source/command identity: {record.identity}\n"
        f"Command: {record.command}\n"
        f"Exit code: {record.exit_code}\n"
        f"Summary:\n{record.summary}\n"
        "This is plan-focused evidence only. It creates no verification "
        "receipt and cannot authorize accept.\n")

def _focused_test_identity(cfg: Config, task: Task) -> str:
    """Bind focused evidence to the checked-out source and exact task command."""
    if not gitops.working_tree_status(cfg.root, cfg.git_excludes).is_clean:
        raise AssentError("source worktree is dirty at the focused_test boundary")
    command_sha256 = hashlib.sha256(task.verify.encode("utf-8")).hexdigest()
    return f"{gitops.tree_of(cfg.root, 'HEAD')}:{command_sha256}"

def _focused_test_record(state: WorkflowState) -> _TestActionEvidence | None:
    if state.unit != "task" or state.action != "focused_test":
        return None
    if not state.action_status:
        return None
    if len(state.action_evidence) != 2:
        raise AssentError("Task focused_test action evidence is unreadable")
    record = _TestActionEvidence(
        state.action_status, state.action_source_tree, state.action_exit_code,
        state.action_evidence[0], state.action_evidence[1])
    if record.status not in {"PASSED", "FAILED", "STALE"}:
        raise AssentError("Task focused_test action evidence has invalid values")
    return record

def _with_focused_test_record(
        state: WorkflowState, record: _TestActionEvidence) -> WorkflowState:
    return replace(
        state, action="focused_test", action_status=record.status,
        action_source_tree=record.identity,
        action_exit_code=record.exit_code,
        action_evidence=(record.command, record.summary))

def _run_focused_test_action(
        cfg: Config, task: Task,
        state: WorkflowState) -> tuple[WorkflowState, _TestActionEvidence, bool]:
    """Run or reuse the task's source-bound focused command."""
    identity = _focused_test_identity(cfg, task)
    existing = _focused_test_record(state)
    if (existing is not None and existing.identity == identity
            and existing.status == "PASSED"):
        print(f"  focused_test {existing.status.lower()} evidence reused "
              f"(exit {existing.exit_code})")
        return state, existing, True

    armed = _TestActionEvidence(
        "STALE", identity, 0, task.verify,
        "Focused command has not completed for this source identity.")
    state = _with_focused_test_record(state, armed)
    write_workflow_state(cfg.tasks_dir, state)
    try:
        result = _verify_subprocess(cfg, task.verify)
    except OSError as error:
        record = replace(
            armed, status="FAILED", exit_code=1,
            summary=verification_summary(
                f"Unable to start focused command: {error}"))
    else:
        _show_verify_result(task.verify, result)
        fallback = ("Focused command passed." if result.returncode == 0 else
                    f"Focused command failed (exit code {result.returncode}).")
        record = replace(
            armed, status="PASSED" if result.returncode == 0 else "FAILED",
            exit_code=result.returncode,
            summary=verification_summary(
                result.stdout, result.stderr, fallback, retain_start=True))
        try:
            unchanged = _focused_test_identity(cfg, task) == identity
        except AssentError:
            unchanged = False
        if not unchanged:
            record = replace(
                record, status="STALE",
                summary=verification_summary(
                    record.summary,
                    "Source or focused command changed during focused_test."))
    state = _with_focused_test_record(state, record)
    write_workflow_state(cfg.tasks_dir, state)
    print(f"  focused_test evidence: {record.status} (exit {record.exit_code})")
    return state, record, False

def _focused_test_prompt(state: WorkflowState) -> str:
    record = _focused_test_record(state)
    if record is None:
        return ""
    return (
        "\nFOCUSED TEST EVIDENCE\n"
        f"Status: {record.status}\n"
        f"Source/command identity: {record.identity}\n"
        f"Command: {record.command}\n"
        f"Exit code: {record.exit_code}\n"
        f"Summary:\n{record.summary}\n")

def _role_policy(step: WorkflowTaskStep | object) -> str:
    resolved = step.resolved_role
    return "\n\n".join(ability.prompt for ability in resolved.abilities)

def _effective_task_workflow(
        cfg: Config, task: Task
        ) -> tuple[WorkflowTaskStep | WorkflowActionStep, ...]:
    """Resolve a task override, or inherit the project task workflow when omitted."""
    if task.workflow is None:
        return cfg.workflow_task
    steps: list[WorkflowTaskStep | WorkflowActionStep] = []
    for index, entry in enumerate(task.workflow):
        if isinstance(entry, TaskWorkflowAction):
            steps.append(WorkflowActionStep(entry.action))
            continue
        try:
            resolved = cfg.resolve_role(entry)
        except AssentError as error:
            raise AssentError(
                f"Task {task.id} workflow[{index}] names missing agent role {entry!r}") from error
        steps.append(WorkflowTaskStep(entry, resolved))
    return tuple(steps)

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

def _checkpoint_subject(cfg: Config, kind: str, task: Task, detail: str) -> str:
    """Build a task checkpoint subject namespaced by the plan."""
    return f"{kind}({cfg.tasks_name}/{task.id}): {detail}"

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
        f"{source.plan} tip {source.tip} is not an ancestor of downstream "
        f"tip {downstream_tip}; all existing work is preserved. Run `assent "
        f"rework {cfg.tasks_name}` after deciding how to handle the upstream "
        "change, or replan the dependency")

def _prepare_worktree(cfg: Config) -> Config:
    """Create or validate the plan worktree before any adapter is started."""
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
        tips_before = {source.plan: source.tip for source in state_before.sources}
        tips_after = {source.plan: source.tip for source in state_after.sources}
        if tips_after != tips_before:
            changes = sorted(set(tips_before) | set(tips_after))
            detail = ", ".join(
                f"{plan_name}: {tips_before.get(plan_name, '(missing)')} -> "
                f"{tips_after.get(plan_name, '(missing)')}"
                for plan_name in changes
                if tips_before.get(plan_name) != tips_after.get(plan_name))
            raise AssentError(
                "upstream source changed between stack resolution and worktree "
                f"validation ({detail})")
        _require_stack_ancestry(cfg, state_after, downstream_tip)

        decision = _ignored_dir_decision(worktree_cfg)
        print(ignored_dirs.describe(decision))
        print(f"Isolated worktree: {root}")
        print(f"Target snapshot: {state_after.base.target_snapshot}")
        stacked = state_before.base.speculative_upstream
        if stacked is None:
            print("Stacked upstream: none")
        else:
            print(f"Stacked upstream: {stacked.plan} @ {stacked.tip}")
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

def _selection_snapshot(configs: tuple[Config, ...]) -> tuple[
        str, str, tuple[str, ...]]:
    """Read the exact target and source identity for a completed selection."""
    main = gitops.main_worktree(configs[0].root)
    target_ref = gitops.require_current_branch(main)
    target_commit = gitops.commit_of(main, target_ref)
    sources: list[str] = []
    for cfg in configs:
        completion = infer_plan_completion(cfg.tasks_dir)
        if not completion.complete:
            raise AssentError(
                f"selected plan {cfg.tasks_name} is incomplete: "
                + completion.reason)
        _branch, tip, _worktree = source_snapshot(cfg, main)
        sources.append(tip)
    return target_ref, target_commit, tuple(sources)


def ensure_selection_runtime_tests(
        configs: tuple[Config, ...],
        sleep: Callable[[float], None] | None = None,
        now: Callable[[], datetime] | None = None) -> tuple[int, str | None]:
    """Satisfy each selected after-plan gate against its own current source."""
    sleep = sleep or interruptible_sleep
    now = now or (lambda: datetime.now(timezone.utc))
    main = gitops.main_worktree(configs[0].root)
    for cfg in configs:
        _branch, source_tip, _worktree = source_snapshot(cfg, main)
        problem = runtime_test.after_plan_gate_problem(
            cfg.tasks_dir, source_tip)
        if problem is None:
            if parse_runtime_test_contract(
                    cfg.tasks_dir).execution == "after_plan":
                print(f"Runtime-test layer {cfg.tasks_name}: current PASSED "
                      "evidence reused.")
            continue
        print(f"Runtime-test layer {cfg.tasks_name}: {problem}; running the "
              "plan runtime workflow before integration.")
        code = run_runtime_test(
            cfg, sleep=sleep, now=now, unresolved_exit_code=0)
        if code != 0:
            return code, None
        _branch, source_tip, _worktree = source_snapshot(cfg, main)
        problem = runtime_test.after_plan_gate_problem(
            cfg.tasks_dir, source_tip)
        if problem is not None:
            return 0, f"{cfg.tasks_name} {problem}"
    return 0, None

@contextlib.contextmanager
def _selection_locks(configs: tuple[Config, ...]):
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            lockfile.hold_integration_lock(configs[0].assent_dir))
        for cfg in configs:
            stack.enter_context(
                lockfile.hold_lock(cfg.tasks_dir, cfg.tasks_name))
        yield

def _selection_worktree_configs(
        configs: tuple[Config, ...]) -> tuple[Config, ...]:
    """Resolve the persistent source worktree for every selected plan."""
    main = gitops.main_worktree(configs[0].root)
    resolved: list[Config] = []
    for cfg in configs:
        _branch, _tip, worktree = source_snapshot(cfg, main)
        if worktree is None:
            raise AssentError(
                f"selection repair requires the source worktree for "
                f"{cfg.tasks_name}")
        resolved.append(cfg.for_worktree(worktree))
    return tuple(resolved)

def _integration_steps(cfg: Config) -> tuple[
        _RoleStep | WorkflowActionStep, ...]:
    """Resolve integration configuration into the same linear step vocabulary."""
    steps: list[_RoleStep | WorkflowActionStep] = []
    for step in cfg.workflow_integration:
        if isinstance(step, WorkflowActionStep):
            steps.append(step)
        else:
            assert step.model is not None
            steps.append(_RoleStep(
                step.role, step.adapters, step.model, step.writes,
                _role_policy(step)))
    if not steps or not isinstance(steps[-1], WorkflowActionStep):
        steps.append(WorkflowActionStep("full_verify"))
    return tuple(steps)

def _session_progress(steps, step_index: int) -> tuple[int, int]:
    """Count only configured role sessions, excluding scheduler actions."""
    return (
        sum(isinstance(step, _RoleStep)
            for step in steps[:step_index + 1]),
        sum(isinstance(step, _RoleStep) for step in steps),
    )

def _integration_prompt(
        cfg: Config, plan: Plan, step: _RoleStep,
        state: SelectionWorkflowState, position: int, total: int) -> str:
    contracts_text = "\n\n".join(
        f"--- {task.id}: {task.path} ---\n"
        + task.path.read_text(encoding="utf-8").rstrip()
        for task in plan.tasks)
    prior = "\n\n".join(state.evidence) or "(none)"
    action = "\n".join(state.action_evidence) or "(full_verify has not run)"
    write_policy = (
        "You may edit any ordinary project source, test, configuration, or "
        "documentation file needed to repair this candidate."
        if step.writes else
        "This is read-only. Do not create, edit, delete, rename, format, or "
        "generate project files.")
    ignored_dir_clause = ignored_dirs.declaration_clause(_ignored_dir_decision(cfg))
    return f"""You are one Assent integration role session.

Read the project rules {_agents_md_path_for_prompt(cfg)} and the Assent session
rules {contracts.instructions_path()} before acting.

Configured AI session: {position} of {total}
Role: {step.role}
Role responsibility:
{step.prompt}

{write_policy}
Treat the plan candidate as one result. Do not assign findings or files to task
owners. Task contracts, journals, scheduler state, Git state, receipts, and
files below .git or .assent are read-only. Do not run Git, Assent, or the full
verifier; the scheduler owns them. The exact ignored-dirs command injected
below is the sole exception.
{ignored_dir_clause}

Authoritative task requirements:
{contracts_text}

Latest full_verify evidence:
{action}

Prior role-session evidence:
{prior}

Do not narrate plans, internal deliberation, or rhetorical questions. Complete
the work, then return only a concise account of what you inspected and, when
writable, what you changed. Sessions run sequentially and do not converse.
"""


_CONFLICT_OUTCOMES = {"TARGET_CONFLICT", "PEER_CONFLICT"}
_CONFLICT_CONTEXT_CHARS = 24_000


def _selection_conflicts(
        state: SelectionWorkflowState) -> tuple[SelectionCandidateConflict, ...]:
    """Return the exact conflict wave carried by the latest scheduler action."""
    conflicts = selection_conflicts_from_evidence(state.action_evidence)
    outcome = state.action_evidence[0] if state.action_evidence else ""
    if outcome in _CONFLICT_OUTCOMES and not conflicts:
        raise AssentError(
            "selection candidate conflict action has no typed conflict wave")
    if conflicts and outcome not in _CONFLICT_OUTCOMES:
        raise AssentError(
            "selection workflow contains conflict evidence without a conflict "
            "action result")
    selected = set(state.plan_names)
    for conflict in conflicts:
        if conflict.plan not in selected or conflict.target_tip != state.target_commit:
            raise AssentError(
                "selection conflict evidence does not match the active selection")
        for relative in conflict.paths:
            path = Path(relative.replace("\\", "/"))
            if (path.is_absolute() or not path.parts or ".." in path.parts
                    or path.parts[0] in {".git", ".assent"}):
                raise AssentError(
                    "selection conflict path is not an ordinary project file: "
                    + relative)
    return conflicts


def _bounded_conflict_context(text: str) -> str:
    if len(text) <= _CONFLICT_CONTEXT_CHARS:
        return text
    return text[:_CONFLICT_CONTEXT_CHARS] + "\n... [conflict evidence truncated]"


def _git_blob_text(root: Path, object_id: str, relative: str) -> str:
    result = subprocess.run(
        ["git", "show", f"{object_id}:{relative}"], cwd=str(root),
        capture_output=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        return "(path absent)"
    return _bounded_conflict_context(result.stdout)


def _conflict_three_way_evidence(
        root: Path, conflict: SelectionCandidateConflict, *,
        target_reconcile: bool) -> str:
    """Render bounded, read-only evidence for a source-bound conflict."""
    base = gitops.merge_base(root, conflict.target_tip, conflict.source_tip)
    ours = conflict.source_tip if target_reconcile else conflict.prefix_tree
    theirs = conflict.target_tip if target_reconcile else conflict.source_tip
    sections = []
    for relative in conflict.paths:
        sections.append(
            f"### {relative}\nBASE ({base}):\n"
            f"{_git_blob_text(root, base, relative)}\n"
            f"OURS ({ours}):\n{_git_blob_text(root, ours, relative)}\n"
            f"THEIRS ({theirs}):\n"
            f"{_git_blob_text(root, theirs, relative)}")
    return _bounded_conflict_context("\n\n".join(sections))


def _conflict_path_state(
        root: Path, paths: tuple[str, ...]) -> tuple[tuple[str, str, str], ...]:
    """Snapshot exact conflict-path content for read-only enforcement."""
    state: list[tuple[str, str, str]] = []
    for relative in paths:
        path = root / relative
        if path.is_symlink():
            state.append((relative, "link", os.readlink(path)))
        elif path.is_file():
            state.append((relative, "file", hashlib.sha256(
                path.read_bytes()).hexdigest()))
        elif path.exists():
            state.append((relative, "other", ""))
        else:
            state.append((relative, "missing", ""))
    return tuple(state)


def _prepare_integration_conflict_workspaces(
        configs: tuple[Config, ...], state: SelectionWorkflowState,
        conflicts: tuple[SelectionCandidateConflict, ...]) -> tuple[
            dict[str, reconcile.AutomaticReconcile], dict[str, Config]]:
    """Resolve scheduler-owned reconcile and source workspaces from Git facts."""
    by_plan = {cfg.tasks_name: cfg for cfg in configs}
    contexts: dict[str, reconcile.AutomaticReconcile] = {}
    source_configs: dict[str, Config] = {}
    with _selection_locks(configs):
        current = _selection_snapshot(configs)
        expected = (state.target_ref, state.target_commit, state.source_commits)
        if current != expected:
            raise AssentError(
                "selection source or target changed before conflict repair")
        main = gitops.main_worktree(configs[0].root)
        for cfg in configs:
            _branch, _tip, worktree = source_snapshot(cfg, main)
            if worktree is not None:
                source_configs[cfg.tasks_name] = cfg.for_worktree(worktree)
        for conflict in conflicts:
            cfg = by_plan[conflict.plan]
            if conflict.kind == "target_alone":
                contexts[conflict.plan] = (
                    reconcile.automatic_reconcile_prepare_locked(
                        cfg, conflict.target_tip, conflict.source_tip,
                        conflict.paths))
            elif conflict.plan not in source_configs:
                raise AssentError(
                    "peer conflict repair requires the source worktree for "
                    + conflict.plan)
    return contexts, source_configs


def _integration_conflict_prompt(
        configs: tuple[Config, ...], state: SelectionWorkflowState,
        step: _RoleStep, position: int, total: int,
        conflicts: tuple[SelectionCandidateConflict, ...],
        contexts: dict[str, reconcile.AutomaticReconcile],
        source_configs: dict[str, Config]) -> str:
    requirements: list[str] = []
    for cfg in configs:
        plan = Plan.parse(cfg.tasks_dir)
        requirements.append(f"## Plan {cfg.tasks_name}")
        requirements.extend(
            f"--- {task.id}: {task.path} ---\n"
            + task.path.read_text(encoding="utf-8").rstrip()
            for task in plan.tasks)

    workspaces: list[str] = []
    main = gitops.main_worktree(configs[0].root)
    for conflict in conflicts:
        if conflict.kind == "target_alone":
            context = contexts[conflict.plan]
            location = (str(context.worktree) if context.worktree is not None
                        else "(the scheduler already completed this reconcile)")
            purpose = (
                "managed reconcile worktree; edit only the exact conflict "
                "paths listed below")
            three_way = _conflict_three_way_evidence(
                main, conflict, target_reconcile=True)
        else:
            location = str(source_configs[conflict.plan].root)
            purpose = (
                "persistent source worktree; repair it so the complete exact "
                "selection merges without conflict")
            three_way = _conflict_three_way_evidence(
                main, conflict, target_reconcile=False)
        workspaces.append(
            f"## {conflict.plan}: {conflict.kind}\n"
            f"Workspace: {location}\nPurpose: {purpose}\n"
            "Exact conflict paths:\n"
            + "\n".join(f"- {path}" for path in conflict.paths)
            + "\nThree-way evidence:\n" + three_way)

    prior = "\n\n".join(state.evidence) or "(none)"
    write_policy = (
        "Repair the supplied conflict in the scheduler-named workspace(s)."
        if step.writes else
        "This is read-only. Do not change any workspace or project file.")
    return f"""You are one Assent integration role session.

Read the project rules {_agents_md_path_for_prompt(configs[0])} and the Assent
session rules {contracts.instructions_path()} before acting.

Configured AI session: {position} of {total}
Role: {step.role}
Role responsibility:
{step.prompt}

{write_policy}
Preserve the complete exact selection. Do not run Git, Assent, focused tests,
full verification, or accept. The scheduler owns Git state and will reconstruct
the integration candidate after this session. Task contracts, journals,
scheduler state, receipts, and files below .git or .assent are read-only.

Authoritative task requirements:
{chr(10).join(requirements)}

Conflict workspaces and evidence:
{_bounded_conflict_context(chr(10).join(workspaces))}

Prior role-session evidence:
{prior}

Do not narrate plans, internal deliberation, or rhetorical questions. Complete
the work, then return only a concise account of what you inspected and, when
writable, what you changed. Sessions run sequentially and do not converse.
"""

def _with_integration_evidence(
        state: SelectionWorkflowState, role: str,
        output: str) -> SelectionWorkflowState:
    evidence = state.evidence + (
        f"{role}:\n{_bounded_session_evidence(output)}",)
    return replace(
        state, evidence=evidence[-_SESSION_EVIDENCE_ITEMS:])

def _integration_conflict_violations(
        configs: tuple[Config, ...], step: _RoleStep,
        conflicts: tuple[SelectionCandidateConflict, ...],
        contexts: dict[str, reconcile.AutomaticReconcile],
        source_configs: dict[str, Config],
        management: dict[Path, bytes | None], primary_baseline: set[str],
        source_heads: dict[str, str | None],
        reconcile_baselines: dict[str, tuple[tuple[str, str, str], ...]],
        ) -> list[str]:
    """Detect writes outside the scheduler-named conflict workspaces."""
    violations = _management_changes(management)
    main = gitops.main_worktree(configs[0].root)
    violations.extend(
        f"primary worktree:{path}"
        for path in sorted(gitops.dirty_paths(main) - primary_baseline))

    peer_plans = {
        conflict.plan for conflict in conflicts if conflict.kind == "peer_only"
    }
    for plan_name, work_cfg in source_configs.items():
        if gitops.head_ref(work_cfg.root) != source_heads[plan_name]:
            violations.append(f"{plan_name}:Git HEAD")
        status = gitops.working_tree_status(
            work_cfg.root, work_cfg.git_excludes)
        if not status.is_clean and (not step.writes or plan_name not in peer_plans):
            violations.append(f"{plan_name}:source worktree")

    with _selection_locks(configs):
        for conflict in conflicts:
            if conflict.kind != "target_alone":
                continue
            refreshed = reconcile.automatic_reconcile_prepare_locked(
                next(cfg for cfg in configs
                     if cfg.tasks_name == conflict.plan),
                conflict.target_tip, conflict.source_tip, conflict.paths)
            if (not step.writes and refreshed.worktree is not None
                    and _conflict_path_state(
                        refreshed.worktree, conflict.paths)
                    != reconcile_baselines[conflict.plan]):
                violations.append(f"{conflict.plan}:reconcile worktree")
    return violations


def _persist_peer_conflict_edits(
        configs: tuple[Config, ...], state: SelectionWorkflowState,
        conflicts: tuple[SelectionCandidateConflict, ...],
        source_configs: dict[str, Config], subject: str,
        ) -> SelectionWorkflowState:
    """Checkpoint peer-conflict source edits and bind the new source tips."""
    peer_plans = {
        conflict.plan for conflict in conflicts if conflict.kind == "peer_only"
    }
    if not peer_plans:
        return state
    with _selection_locks(configs):
        main = gitops.main_worktree(configs[0].root)
        target_ref = gitops.require_current_branch(main)
        if (target_ref != state.target_ref
                or gitops.commit_of(main, target_ref) != state.target_commit):
            raise AssentError(
                "selection target changed while conflict repair was running")
        for index, cfg in enumerate(configs):
            work_cfg = source_configs.get(cfg.tasks_name)
            if work_cfg is None:
                _branch, tip, _worktree = source_snapshot(cfg, main)
            else:
                tip = gitops.commit_of(work_cfg.root, "HEAD")
            if tip != state.source_commits[index]:
                raise AssentError(
                    f"source for {cfg.tasks_name} changed while conflict "
                    "repair was running")
        for plan_name in sorted(peer_plans):
            work_cfg = source_configs[plan_name]
            gitops.commit_if_dirty(
                work_cfg.root, subject.replace("{plan}", plan_name),
                work_cfg.git_excludes)
        _target_ref, _target_commit, sources = _selection_snapshot(configs)
        state = replace(state, source_commits=sources)
        write_selection_workflow_state(configs[0].assent_dir, state)
    return state


def _run_integration_conflict_role(
        configs: tuple[Config, ...], state: SelectionWorkflowState,
        step: _RoleStep, position: int, total: int,
        sleep: Callable[[float], None],
        now: Callable[[], datetime]) -> tuple[int, SelectionWorkflowState]:
    """Run one linear integration role against typed conflict workspaces."""
    conflicts = _selection_conflicts(state)
    names = list(configs[0].adapter_names)
    for name in step.adapters:
        if name not in names:
            names.append(name)
    adapters = {name: get_adapter(name, configs[0]) for name in names}
    rotation = _AdapterRotation(
        step.adapters, tuple(adapters[name] for name in step.adapters),
        pool=adapters)
    failed_adapters: set[str] = set()

    while True:
        contexts, source_configs = _prepare_integration_conflict_workspaces(
            configs, state, conflicts)
        management: dict[Path, bytes | None] = {}
        for cfg in configs:
            management.update(_management_snapshot(
                cfg, Plan.parse(cfg.tasks_dir)))
        main = gitops.main_worktree(configs[0].root)
        primary_baseline = gitops.dirty_paths(main)
        source_heads = {
            plan_name: gitops.head_ref(work_cfg.root)
            for plan_name, work_cfg in source_configs.items()
        }
        reconcile_baselines = {
            plan_name: _conflict_path_state(context.worktree,
                                             context.conflict_paths)
            for plan_name, context in contexts.items()
            if context.worktree is not None
        }
        roots = [
            context.worktree for context in contexts.values()
            if context.worktree is not None
        ] + [
            source_configs[conflict.plan].root for conflict in conflicts
            if conflict.kind == "peer_only"
        ]
        cwd = roots[0] if roots else configs[0].root
        adapter_name = rotation.name
        adapter = rotation.adapter
        session = _role_session_identity(configs[0], step, adapter_name)
        prompt = _integration_conflict_prompt(
            configs, state, step, position, total, conflicts, contexts,
            source_configs)
        ignored_input_guards = _ignored_input_guards(
            tuple(source_configs.values()))
        print(_session_line(adapter_name, step.model, session))
        try:
            result = _invoke_adapter(
                configs[0], adapter, adapter_name, prompt,
                session.requested_model, session.requested_effort, cwd,
                context_kind="integration",
                context_id=f"workflow.integration[{state.step_index}]",
                plan_names=state.plan_names)
        except KeyboardInterrupt:
            ignored_violations = _ignored_input_violations(
                ignored_input_guards, tuple(source_configs.values()))
            if ignored_violations:
                state = _record_ignored_input_violation(
                    state, ignored_violations)
                assert isinstance(state, SelectionWorkflowState)
                write_selection_workflow_state(configs[0].assent_dir, state)
                raise AssentError(
                    "integration conflict role changed ignored input: "
                    + ", ".join(ignored_violations[:8]))
            violations = _integration_conflict_violations(
                configs, step, conflicts, contexts, source_configs,
                management, primary_baseline, source_heads,
                reconcile_baselines)
            if not violations and step.writes:
                state = _persist_peer_conflict_edits(
                    configs, state, conflicts, source_configs,
                    "wip({plan}): interrupted integration conflict repair")
            raise

        ignored_violations = _ignored_input_violations(
            ignored_input_guards, tuple(source_configs.values()))
        violations = list(ignored_violations)
        violations.extend(_integration_conflict_violations(
            configs, step, conflicts, contexts, source_configs,
            management, primary_baseline, source_heads, reconcile_baselines))
        if violations:
            if ignored_violations:
                state = _record_ignored_input_violation(
                    state, ignored_violations)
                assert isinstance(state, SelectionWorkflowState)
                write_selection_workflow_state(configs[0].assent_dir, state)
            print("Integration conflict role crossed its control boundary: "
                  + ", ".join(violations[:8]))
            return 1, state
        if step.writes:
            checkpoint = (
                "auto({plan}): integration conflict repair"
                if result.exit_code == 0 and not result.stalled
                else "wip({plan}): integration conflict repair checkpoint")
            state = _persist_peer_conflict_edits(
                configs, state, conflicts, source_configs, checkpoint)

        if result.checkpoint_resume and not result.quota_exhausted:
            continue
        if result.quota_exhausted:
            if len(rotation.names) == 1:
                _wait_for_quota(configs[0], result.reset_at, sleep, now)
            elif rotation.advance_after_quota(failed_adapters):
                _wait_for_rotation(configs[0], sleep)
            continue
        reason = _adapter_failure_reason(
            result.exit_code, result.stalled, result.output,
            result.failure_kind)
        if result.failure_kind == "authentication":
            action = _authentication_failover_action(
                rotation, failed_adapters, reason)
            if action != "switch":
                _wait_for_rotation(configs[0], sleep)
            continue
        if _adapter_availability_failed(result):
            if not rotation.advance_after_failure(failed_adapters):
                _wait_for_rotation(configs[0], sleep)
            continue
        if result.failure_kind == "billing":
            raise _BillingAbort(reason)
        if result.exit_code != 0 or result.stalled:
            print(f"Integration conflict role session failed: {reason}")
            return result.exit_code or 1, state

        state = _with_integration_evidence(state, step.role, result.output)
        state = replace(state, step_index=state.step_index + 1)
        write_selection_workflow_state(configs[0].assent_dir, state)
        return 0, state


def _run_integration_role(
        cfg: Config, state: SelectionWorkflowState, step: _RoleStep,
        position: int, total: int, sleep: Callable[[float], None],
        now: Callable[[], datetime]) -> tuple[int, SelectionWorkflowState]:
    """Run one integration role against its single source worktree."""
    work_cfg = _selection_worktree_configs((cfg,))[0]
    plan = Plan.parse(work_cfg.tasks_dir)
    names = list(cfg.adapter_names)
    for name in step.adapters:
        if name not in names:
            names.append(name)
    adapters = {name: get_adapter(name, cfg) for name in names}
    rotation = _AdapterRotation(
        step.adapters, tuple(adapters[name] for name in step.adapters),
        pool=adapters)
    failed_adapters: set[str] = set()
    while True:
        gitops.commit_if_dirty(
            work_cfg.root,
            f"wip({cfg.tasks_name}): before integration step "
            f"{state.step_index + 1}", work_cfg.git_excludes)
        adapter_name = rotation.name
        adapter = rotation.adapter
        session = _role_session_identity(cfg, step, adapter_name)
        management = _management_snapshot(work_cfg, plan)
        source_head = gitops.head_ref(work_cfg.root)
        primary_baseline = gitops.dirty_paths(cfg.root)
        ignored_input_guards = _ignored_input_guards((work_cfg,))
        print(_session_line(adapter_name, step.model, session))
        result = _invoke_adapter(
            cfg, adapter, adapter_name,
            _integration_prompt(
                work_cfg, plan, step, state, position, total),
            session.requested_model, session.requested_effort,
            work_cfg.root, context_kind="integration",
            context_id=f"workflow.integration[{state.step_index}]",
            plan_names=state.plan_names)
        ignored_violations = _ignored_input_violations(
            ignored_input_guards, (work_cfg,))
        if ignored_violations:
            state = _record_ignored_input_violation(state, ignored_violations)
            assert isinstance(state, SelectionWorkflowState)
            write_selection_workflow_state(cfg.assent_dir, state)
            print("Integration role crossed its control boundary: "
                  + ", ".join(ignored_violations[:8]))
            return 1, state
        if result.checkpoint_resume and not result.quota_exhausted:
            gitops.commit_if_dirty(
                work_cfg.root,
                f"wip({cfg.tasks_name}): {step.role} checkpoint resume",
                work_cfg.git_excludes)
            continue
        if result.quota_exhausted:
            gitops.commit_if_dirty(
                work_cfg.root,
                f"wip({cfg.tasks_name}): {step.role} quota interrupt",
                work_cfg.git_excludes)
            if len(rotation.names) == 1:
                _wait_for_quota(cfg, result.reset_at, sleep, now)
            elif rotation.advance_after_quota(failed_adapters):
                _wait_for_rotation(cfg, sleep)
            continue
        reason = _adapter_failure_reason(
            result.exit_code, result.stalled, result.output,
            result.failure_kind)
        if result.failure_kind == "authentication":
            gitops.commit_if_dirty(
                work_cfg.root,
                f"wip({cfg.tasks_name}): {step.role} authentication failover",
                work_cfg.git_excludes)
            action = _authentication_failover_action(
                rotation, failed_adapters, reason)
            if action != "switch":
                _wait_for_rotation(cfg, sleep)
            continue
        if _adapter_availability_failed(result):
            gitops.commit_if_dirty(
                work_cfg.root,
                f"wip({cfg.tasks_name}): {step.role} adapter failover",
                work_cfg.git_excludes)
            if not rotation.advance_after_failure(failed_adapters):
                _wait_for_rotation(cfg, sleep)
            continue
        if result.failure_kind == "billing":
            raise _BillingAbort(reason)
        if result.exit_code != 0 or result.stalled:
            gitops.commit_if_dirty(
                work_cfg.root,
                f"wip({cfg.tasks_name}): {step.role} failed",
                work_cfg.git_excludes)
            print(f"Integration role session failed: {reason}")
            return result.exit_code or 1, state

        violations = _management_changes(management)
        violations.extend(
            f"primary worktree:{path}"
            for path in sorted(gitops.dirty_paths(cfg.root) - primary_baseline))
        if gitops.head_ref(work_cfg.root) != source_head:
            violations.append("Git HEAD")
        if not step.writes and not gitops.working_tree_status(
                work_cfg.root, work_cfg.git_excludes).is_clean:
            violations.append("read-only session changed project files")
        if violations:
            print("Integration role crossed its control boundary: "
                  + ", ".join(violations[:8]))
            return 1, state

        gitops.commit_if_dirty(
            work_cfg.root,
            f"wip({cfg.tasks_name}): integration step "
            f"{state.step_index + 1} ({step.role})",
            work_cfg.git_excludes)
        _target_ref, _target_commit, sources = _selection_snapshot((cfg,))
        state = _with_integration_evidence(state, step.role, result.output)
        state = replace(
            state, source_commits=sources,
            step_index=state.step_index + 1)
        write_selection_workflow_state(cfg.assent_dir, state)
        return 0, state

def _reconcile_closeout_problem(
        context: reconcile.AutomaticReconcile,
        conflict: SelectionCandidateConflict) -> str | None:
    if not context.needs_editing:
        return None
    assert context.worktree is not None
    if gitops.merge_scene_is_unedited(
            context.worktree, conflict.source_tip, conflict.target_tip):
        return f"{conflict.plan} conflict paths were not repaired"
    detail = gitops.conflict_marker_problem(
        context.worktree, paths=conflict.paths)
    if detail is not None:
        return (f"{conflict.plan} conflict repair is not mechanically valid: "
                + _bounded_adapter_diagnostic(detail))
    return None


def _closeout_peer_conflict(
        configs: tuple[Config, ...], state: SelectionWorkflowState,
        conflict: SelectionCandidateConflict) -> str:
    """Record the repaired exact prefix in one peer source's ancestry."""
    index = state.plan_names.index(conflict.plan)
    repaired_tip = state.source_commits[index]
    if repaired_tip == conflict.source_tip:
        raise AssentError(
            f"{conflict.plan} source was not repaired for its peer conflict")
    if not conflict.prefix_sources:
        raise AssentError(
            f"{conflict.plan} peer conflict has no exact selection prefix")

    by_plan = {cfg.tasks_name: cfg for cfg in configs}
    cfg = by_plan[conflict.plan]
    main = gitops.main_worktree(cfg.root)
    _branch, current_tip, worktree = source_snapshot(cfg, main)
    if worktree is None or current_tip != repaired_tip:
        raise AssentError(
            f"{conflict.plan} repaired source worktree identity changed")
    work_cfg = cfg.for_worktree(worktree)
    if not gitops.working_tree_status(
            worktree, work_cfg.git_excludes).is_clean:
        raise AssentError(
            f"{conflict.plan} repaired source worktree is not clean")

    with gitops.temporary_integration_worktree(
            main, f"peer-{conflict.plan}",
            conflict.target_tip) as (prefix, _branch):
        for plan_name, source_tip in conflict.prefix_sources:
            outcome = gitops.merge_no_ff(
                prefix, source_tip,
                f"rebuild(peer/{conflict.plan}/{plan_name}): exact prefix")
            if not outcome.ok:
                raise AssentError(
                    f"unable to reconstruct {conflict.plan}'s exact peer "
                    f"prefix at {plan_name}")
        if gitops.tree_of(prefix, "HEAD") != conflict.prefix_tree:
            raise AssentError(
                f"{conflict.plan} exact peer prefix tree changed")
        prefix_commit = gitops.commit_of(prefix, "HEAD")

        outcome = gitops.merge_no_commit(worktree, prefix_commit)
        unexpected = sorted(
            set(outcome.conflicts) - set(conflict.paths))
        if unexpected:
            gitops.abort_merge(worktree)
            raise AssentError(
                f"{conflict.plan} peer closeout found unexpected conflict "
                f"paths: {', '.join(unexpected)}")
        if gitops.merge_head(worktree) is None:
            if not gitops.is_ancestor(main, prefix_commit, repaired_tip):
                raise AssentError(
                    f"{conflict.plan} peer closeout produced no merge")
            return repaired_tip

        try:
            gitops.restore_paths_from_commit(
                worktree, repaired_tip, conflict.paths)
            remaining = gitops.conflict_paths(worktree)
            if remaining:
                raise AssentError(
                    f"{conflict.plan} peer closeout left unmerged paths: "
                    + ", ".join(remaining))
            problem = gitops.conflict_marker_problem(worktree, cached=True)
            if problem is not None:
                raise AssentError(
                    f"{conflict.plan} peer closeout is not mechanically "
                    f"valid: {problem}")
            status = gitops.working_tree_status(
                worktree, work_cfg.git_excludes)
            if status.unstaged or status.untracked:
                unexpected_edits = sorted(
                    set(status.unstaged) | set(status.untracked))
                raise AssentError(
                    f"{conflict.plan} peer closeout left unstaged edits: "
                    + ", ".join(unexpected_edits))
            merge_commit = gitops.commit_merge(
                worktree,
                f"auto({conflict.plan}): integration peer conflict repair")
        except (AssentError, OSError):
            if gitops.merge_head(worktree) is not None:
                gitops.abort_merge(worktree)
            raise
        if gitops.commit_parents(worktree, merge_commit) != (
                repaired_tip, prefix_commit):
            raise AssentError(
                f"{conflict.plan} peer closeout created the wrong ancestry")
        return merge_commit


def _closeout_integration_conflicts(
        configs: tuple[Config, ...], state: SelectionWorkflowState,
        conflicts: tuple[SelectionCandidateConflict, ...],
        ) -> tuple[SelectionWorkflowState, str | None]:
    """Persist resolved conflicts into sources before rebuilding the candidate."""
    by_plan = {cfg.tasks_name: cfg for cfg in configs}
    with _selection_locks(configs):
        if _selection_snapshot(configs) != (
                state.target_ref, state.target_commit, state.source_commits):
            raise AssentError(
                "selection source or target changed before conflict closeout")
        contexts: dict[str, reconcile.AutomaticReconcile] = {}
        problems: list[str] = []
        for conflict in conflicts:
            if conflict.kind == "target_alone":
                context = reconcile.automatic_reconcile_prepare_locked(
                    by_plan[conflict.plan], conflict.target_tip,
                    conflict.source_tip, conflict.paths)
                contexts[conflict.plan] = context
                problem = _reconcile_closeout_problem(context, conflict)
                if problem:
                    problems.append(problem)
            else:
                index = state.plan_names.index(conflict.plan)
                if state.source_commits[index] == conflict.source_tip:
                    problems.append(
                        f"{conflict.plan} source was not repaired for its peer "
                        "conflict")
        if problems:
            return state, "; ".join(problems)

        for conflict in conflicts:
            if conflict.kind != "target_alone":
                continue
            merge_commit = reconcile.automatic_reconcile_continue_locked(
                by_plan[conflict.plan], conflict.target_tip,
                conflict.source_tip, conflict.paths)
            source_commits = list(state.source_commits)
            source_commits[state.plan_names.index(conflict.plan)] = merge_commit
            state = replace(state, source_commits=tuple(source_commits))
            write_selection_workflow_state(configs[0].assent_dir, state)
        for conflict in conflicts:
            if conflict.kind != "peer_only":
                continue
            merge_commit = _closeout_peer_conflict(configs, state, conflict)
            source_commits = list(state.source_commits)
            source_commits[state.plan_names.index(conflict.plan)] = merge_commit
            state = replace(state, source_commits=tuple(source_commits))
            write_selection_workflow_state(configs[0].assent_dir, state)
    return state, None


def _integration_human_decision(reason: str) -> int:
    print("Integration workflow: REVIEW UNRESOLVED, HUMAN DECISION; " + reason)
    return 0

def _integration_automated_work_complete(outcome: str) -> int:
    if outcome == "PEER_CONFLICT":
        result = "cross-plan conflicts still prevent full verification"
    elif outcome == "TARGET_CONFLICT":
        result = "source-target conflicts still prevent full verification"
    else:
        result = "full verification did not pass"
    print("Integration workflow finished: Assent completed all configured "
          f"automated work, but {result}. Review the integration evidence "
          "and rework before `assent accept`.")
    return 0

def run_selection_workflow(config_path: str, assent_dir, plan_names,
                           *, sleep: Callable[[float], None] | None = None,
                           now: Callable[[], datetime] | None = None) -> int:
    """Execute the exact selection as one finite linear workflow."""
    sleep = sleep or interruptible_sleep
    now = now or (lambda: datetime.now(timezone.utc))
    try:
        assent_dir = Path(assent_dir)
        graph = parse_plan_dependency_graph(assent_dir)
        ordered = tuple(order_plans_by_dependency(graph, set(plan_names)))
        if len(ordered) != len(plan_names):
            raise AssentError("exact plan selection is invalid")
        configs = tuple(load_config(config_path, name) for name in ordered)
        pending = tuple(
            cfg.tasks_name for cfg in configs
            if plan_workflow_requires_human(
                cfg.tasks_dir, cfg.plan_workflow_step_count))
        if pending:
            return _integration_human_decision(
                "plan workflow adjudication is still pending for "
                + ", ".join(pending) + ".")
        steps = _integration_steps(configs[0])
        code, runtime_problem = ensure_selection_runtime_tests(
            configs, sleep, now)
        if code != 0:
            return code
        with _selection_locks(configs):
            locked_snapshot = _selection_snapshot(configs)
            prior_state = read_selection_workflow_state(
                configs[0].assent_dir)
            if (prior_state is not None
                    and prior_state.plan_names == ordered
                    and (prior_state.target_ref, prior_state.target_commit)
                    == locked_snapshot[:2]
                    and prior_state.source_commits != locked_snapshot[2]):
                prior_state = replace(
                    prior_state,
                    source_commits=locked_snapshot[2],
                    evidence=tuple(
                        item for item in prior_state.evidence
                        if not item.startswith(_IGNORED_INPUT_VIOLATION)),
                    step_index=next(
                        index for index, step in enumerate(steps)
                        if isinstance(step, WorkflowActionStep)),
                    action="",
                    action_status="", action_candidate_tree="",
                    action_exit_code=0, action_evidence=(),
                    verification_script_sha256="",
                    ignored_directory_inputs_sha256="")
                write_selection_workflow_state(
                    configs[0].assent_dir, prior_state)
        target_ref, target_commit, source_commits = _selection_snapshot(configs)
        identity = (ordered, target_ref, target_commit, source_commits)
        state = read_selection_workflow_state(configs[0].assent_dir)
        if (state is None
                or (state.plan_names, state.target_ref, state.target_commit,
                    state.source_commits) != identity):
            state = SelectionWorkflowState(
                ordered, target_ref, target_commit, source_commits, 0)
            write_selection_workflow_state(configs[0].assent_dir, state)
        elif violation := _ignored_input_violation(state.evidence):
            print("Integration workflow cannot resume after an ignored-input "
                  f"control violation without source rework: {violation}")
            return 1
        elif state.action_status == "PASSED":
            first_action = next(
                index for index, step in enumerate(steps)
                if isinstance(step, WorkflowActionStep))
            state = replace(state, step_index=first_action)
            write_selection_workflow_state(configs[0].assent_dir, state)
        elif state.step_index >= len(steps):
            if runtime_problem is not None:
                return _integration_human_decision(
                    "runtime-test gate is unresolved: "
                    + runtime_problem + ".")
            outcome = (state.action_evidence[0]
                       if state.action_evidence else "")
            return _integration_automated_work_complete(outcome)
        if runtime_problem is not None:
            return _integration_human_decision(
                "runtime-test gate is unresolved: " + runtime_problem + ".")
    except (AssentError, OSError) as error:
        print(f"Selection full_verify: failed ({error})")
        return 1

    while state.step_index < len(steps):
        step = steps[state.step_index]
        if isinstance(step, _RoleStep):
            session_position, session_total = _session_progress(
                steps, state.step_index)
            try:
                conflicts = _selection_conflicts(state)
            except AssentError as error:
                print(f"Integration role stopped: {error}")
                return 1
            if not conflicts and len(configs) != 1:
                return _integration_human_decision(
                    "a failing multi-plan candidate has no unique source "
                    "branch to edit automatically.")
            try:
                if conflicts:
                    code, state = _run_integration_conflict_role(
                        configs, state, step, session_position, session_total,
                        sleep, now)
                else:
                    code, state = _run_integration_role(
                        configs[0], state, step, session_position,
                        session_total, sleep, now)
            except KeyboardInterrupt:
                print("Integration role interrupted; edits were preserved.")
                return 130
            except _BillingAbort as error:
                print(f"Integration role stopped: billing/balance: {error}")
                return 1
            except (AssentError, OSError) as error:
                print(f"Integration role stopped: {error}")
                return 1
            if code != 0:
                return code
            continue

        try:
            conflicts = _selection_conflicts(state)
            if conflicts:
                state, problem = _closeout_integration_conflicts(
                    configs, state, conflicts)
                if problem is not None:
                    message = "Conflict repair closeout failed: " + problem
                    print(f"Integration full_verify: not started ({problem})")
                    state = replace(
                        state, step_index=state.step_index + 1,
                        action="full_verify", action_status="FAILED",
                        action_exit_code=1,
                        action_evidence=state.action_evidence + (message,))
                    write_selection_workflow_state(
                        configs[0].assent_dir, state)
                    if state.step_index >= len(steps):
                        outcome = (state.action_evidence[0]
                                   if state.action_evidence else "")
                        return _integration_automated_work_complete(outcome)
                    continue
        except (AssentError, lockfile.LockBusy, OSError) as error:
            print(f"Integration conflict closeout failed: {error}")
            return 1

        try:
            code, runtime_problem = ensure_selection_runtime_tests(
                configs, sleep, now)
            if code != 0:
                return code
            with _selection_locks(configs):
                current = _selection_snapshot(configs)
                if current[:2] != (state.target_ref, state.target_commit):
                    raise AssentError(
                        "integration target changed before full verification")
                if current[2] != state.source_commits:
                    state = replace(
                        state, source_commits=current[2], action="",
                        action_status="", action_candidate_tree="",
                        action_exit_code=0, action_evidence=(),
                        verification_script_sha256="",
                        ignored_directory_inputs_sha256="")
                    write_selection_workflow_state(
                        configs[0].assent_dir, state)
                for cfg, source_tip in zip(configs, current[2]):
                    problem = runtime_test.after_plan_gate_problem(
                        cfg.tasks_dir, source_tip)
                    if problem is not None:
                        runtime_problem = f"{cfg.tasks_name} {problem}"
                        break
        except (AssentError, lockfile.LockBusy, OSError) as error:
            print(f"Runtime-test gate failed: {error}")
            return 1
        if runtime_problem is not None:
            return _integration_human_decision(
                "runtime-test gate is unresolved: "
                + runtime_problem + ".")

        if state.action_status == "PASSED":
            print("Integration verification: checking existing PASSED receipt.")
        else:
            print(f"Integration workflow step {state.step_index + 1}/"
                  f"{len(steps)}: full_verify")
        recheck = state.action_status == "FAILED"
        result = (verify_plan_action(configs[0], recheck=recheck)
                  if len(configs) == 1 else
                  verify_selected_batch_action(
                      config_path, configs[0].assent_dir, ordered,
                      recheck=recheck))
        if (len(configs) == 1 and result.outcome == "TARGET_CONFLICT"
                and not any(item.startswith(configs[0].tasks_name + ":")
                            for item in result.evidence)
                and result.reused):
            result = verify_plan_action(configs[0], recheck=True)
        if result.outcome == "INFRASTRUCTURE_FAILED":
            detail = result.evidence[0] if result.evidence else result.outcome
            print(f"Selection full_verify: failed ({detail})")
            return 1
        if (not result.target_commit or not result.source_commits
                or not result.candidate_tree
                or not result.verification_script_sha256
                or not result.ignored_directory_inputs_sha256):
            print("Selection full_verify returned incomplete evidence")
            return 1
        if len(configs) == 1 and result.outcome == "TARGET_CONFLICT":
            paths = tuple(dict.fromkeys(
                item.split(":", 1)[1]
                for item in result.evidence
                if item.startswith(configs[0].tasks_name + ":")
                and ":" in item))
            if not paths:
                print("Selection full_verify returned no exact conflict paths")
                return 1
            conflict = SelectionCandidateConflict(
                configs[0].tasks_name, paths, result.source_commits[0],
                result.target_commit, (), result.candidate_tree, (),
                "target_alone")
            if not selection_conflicts_from_evidence(result.evidence):
                result = replace(
                    result, evidence=result.evidence
                    + (selection_conflict_line(conflict),))
        try:
            with _selection_locks(configs):
                current = _selection_snapshot(configs)
                expected = (state.target_ref, state.target_commit,
                            state.source_commits)
                if current != expected:
                    raise AssentError(
                        "selection source or target changed after verification")
                if (result.plan_names != ordered
                        or result.target_commit != state.target_commit
                        or result.source_commits != state.source_commits):
                    raise AssentError(
                        "verification evidence does not match the selection")
                state = replace(
                    state, step_index=state.step_index + 1,
                    action="full_verify",
                    action_status="PASSED" if result.passed else "FAILED",
                    action_candidate_tree=result.candidate_tree,
                    action_exit_code=result.exit_code,
                    action_evidence=(result.outcome,) + result.evidence,
                    verification_script_sha256=(
                        result.verification_script_sha256),
                    ignored_directory_inputs_sha256=result.ignored_directory_inputs_sha256)
                write_selection_workflow_state(
                    configs[0].assent_dir, state)
        except (AssentError, lockfile.LockBusy) as error:
            print(f"Selection full_verify: failed ({error})")
            return 1
        if result.passed:
            print("Integration full_verify: PASS; no role session started.")
            return 0
        if state.step_index >= len(steps):
            return _integration_automated_work_complete(result.outcome)
    outcome = state.action_evidence[0] if state.action_evidence else ""
    return _integration_automated_work_complete(outcome)

def run_dynamic_selection_workflow(config_path: str, assent_dir) -> int:
    """Snapshot a whole-project run's current verification selection once."""
    try:
        assent_dir = Path(assent_dir)
        root = assent_dir.parent
        main = gitops.main_worktree(root)
        target = gitops.commit_of(
            main, gitops.require_current_branch(main))
        selection, configs = verification.select_batch_plans(
            config_path, assent_dir, main, target)
    except AssentError as error:
        print(f"Selection full_verify: failed ({error})")
        return 1
    for plan_name, reason in selection.skipped:
        print(f"Selection full_verify: skip {plan_name} ({reason})")
    plan_names = tuple(
        name for name in selection.plan_names
        if not plan_workflow_requires_human(
            assent_dir / name, configs[name].plan_workflow_step_count))
    for name in selection.plan_names:
        if name not in plan_names:
            print(f"Selection full_verify: skip {name} "
                  "(plan workflow requires human adjudication)")
    if not plan_names:
        print("Selection full_verify: no plan has anything left to verify")
        return 0
    return run_selection_workflow(config_path, assent_dir, plan_names)

def run(cfg: Config, *, adapter: Adapter | None = None,
        sleep: Callable[[float], None] | None = None,
        now: Callable[[], datetime] | None = None) -> int:
    """Run tasks until all are DONE/BLOCKED/SKIP. Return the process exit code.

    First check plan prerequisites, then take the plan's file lock; the lock covers
    the whole run (including the long sleeps of quota waiting); if another run is already
    running in the same plan, print a message and fail with exit code 1 without touching
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
        print(f"Prerequisite plan gate: FAIL ({e})")
        return 1
    if unfinished:
        print("Prerequisite plans not finished, refusing to run:")
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
            result = _run_locked(cfg, adapter, sleep, now)
    except lockfile.LockBusy as e:
        print(str(e))
        return 1
    if result != 0:
        return result

    # Full candidate verification is outside the AI session and outside the
    # plan lock above.  The verification layer reacquires locks in the one
    # safe order used by accept: repository integration, then plan.
    try:
        plan = Plan.parse(cfg.tasks_dir)
    except AssentError as e:
        print(f"Failed to parse plan directory after run: {e}")
        return 1
    if all(task.status in ("DONE", "SKIP") for task in plan.tasks):
        print(f"integration {cfg.tasks_name}: plan execution complete; "
              "the invocation integration workflow follows")
        try_write_report(cfg)
    return result


def _runtime_test_source_steps(
        cfg: Config) -> tuple[_RoleStep | WorkflowActionStep, ...]:
    workflow = cfg.workflow_runtime_test
    if workflow is None:
        raise AssentError("Effective [workflow].runtime_test is not configured")
    steps: list[_RoleStep | WorkflowActionStep] = []
    for step in workflow:
        if isinstance(step, WorkflowActionStep):
            steps.append(step)
            continue
        assert step.model is not None
        steps.append(_RoleStep(
            step.role, step.adapters, step.model, step.writes,
            _role_policy(step)))
    return tuple(steps)


def _runtime_test_capability_preflight(
        cfg: Config, adapters: dict[str, Adapter]) -> bool:
    """Refuse runtime execution when a resolved role is not sendable."""
    failures = runtime_test_capability_errors(cfg, adapters)
    for name, errors in failures.items():
        print(f"{name} runtime-test capability preflight: FAIL "
              "(refusing before any runtime workflow)")
        for message in errors:
            print(f"  - {message}")
    return not failures


def run_runtime_test(cfg: Config, *, adapter: Adapter | None = None,
                     sleep: Callable[[float], None] | None = None,
                     now: Callable[[], datetime] | None = None,
                     unresolved_exit_code: int = 1) -> int:
    """Run one plan contract's runtime commands and finite repair workflow."""
    try:
        contracts.require_contracts()
        if not has_git_marker(cfg.root):
            raise AssentError(GIT_REQUIRED_MESSAGE)
        plan = Plan.parse(cfg.tasks_dir)
        contract = parse_runtime_test_contract(cfg.tasks_dir)
        if contract.execution == "disabled":
            raise AssentError(
                f"Runtime testing is disabled for plan {cfg.tasks_name}")
        assert contract.commands is not None
        steps = _runtime_test_source_steps(cfg)

        adapter_names = list(cfg.adapter_names)
        for name in runtime_test_adapter_names(cfg):
            if name not in adapter_names:
                adapter_names.append(name)
        adapters = {
            name: (adapter if name == cfg.adapter_names[0]
                   and adapter is not None else get_adapter(name, cfg))
            for name in adapter_names
        }
        rotation = _AdapterRotation(
            cfg.adapter_names,
            tuple(adapters[name] for name in cfg.adapter_names), pool=adapters)
        if not _runtime_test_capability_preflight(cfg, adapters):
            return 1
        sleep = sleep or interruptible_sleep
        now = now or (lambda: datetime.now(timezone.utc))
        with lockfile.hold_lock(cfg.tasks_dir, cfg.tasks_name):
            prior = read_runtime_test_workflow_state(cfg.tasks_dir)
            if prior is not None and prior.step_index >= len(steps):
                runtime_test_workflow_state_path(cfg.tasks_dir).unlink(
                    missing_ok=True)
            work_cfg = _prepare_worktree(cfg) if cfg.source_root is None else cfg
            _recover_or_ensure_clean(work_cfg, now)
            code = _process_source_workflow(
                work_cfg, plan, None, steps, rotation, sleep, now,
                _ActiveTask(), unit="runtime_test",
                runtime_commands=contract.commands, state_owner=cfg.tasks_dir)
            if code != 0:
                return code
            state = read_runtime_test_workflow_state(cfg.tasks_dir)
            if state is None or state.action_status != "PASSED":
                return unresolved_exit_code
            return 0
    except lockfile.LockBusy as error:
        print(str(error))
        return 1
    except KeyboardInterrupt:
        print("Interrupted; runtime workflow state and worktree were preserved.")
        return 130
    except _BillingAbort as error:
        print(f"Runtime test stopped for billing: {error}")
        return 1
    except (AssentError, OSError) as error:
        print(f"Runtime test refused: {error}")
        return 1


def run_main_runtime_test(cfg: Config, *, adapter: Adapter | None = None,
                          sleep: Callable[[float], None] | None = None,
                          now: Callable[[], datetime] | None = None) -> int:
    """Run and repair the project command in the primary working tree."""
    try:
        contracts.require_contracts()
        if cfg.source_of("runtime_test.command") != PROJECT_LAYER:
            raise AssentError(
                "[runtime_test].command must be stated in the project config")
        commands = cfg.runtime_test_commands
        if commands is None:
            raise AssentError(
                "Project config is missing [runtime_test].command")
        steps = _runtime_test_source_steps(cfg)
        if not has_git_marker(cfg.root):
            raise AssentError(GIT_REQUIRED_MESSAGE)
        primary = gitops.main_worktree(cfg.root)
        if primary.resolve() != cfg.root.resolve():
            raise AssentError("assent test must use the primary worktree config")

        adapter_names = list(cfg.adapter_names)
        for name in runtime_test_adapter_names(cfg):
            if name not in adapter_names:
                adapter_names.append(name)
        adapters = {
            name: (adapter if name == cfg.adapter_names[0]
                   and adapter is not None else get_adapter(name, cfg))
            for name in adapter_names
        }
        rotation = _AdapterRotation(
            cfg.adapter_names,
            tuple(adapters[name] for name in cfg.adapter_names), pool=adapters)
        if not _runtime_test_capability_preflight(cfg, adapters):
            return 1
        sleep = sleep or interruptible_sleep
        now = now or (lambda: datetime.now(timezone.utc))
        owner = cfg.assent_dir
        state_path = runtime_test_workflow_state_path(owner)

        with lockfile.hold_integration_lock(cfg.assent_dir):
            gitops.require_current_branch(primary)
            head = gitops.commit_of(primary, "HEAD")
            state = read_runtime_test_workflow_state(owner)
            if (state is not None and state.base_ref == head
                    and state.candidate_head
                    and state.candidate_head != head):
                raise AssentError(
                    "runtime state names a separate source HEAD "
                    f"{state.candidate_head}, but the current working tree is at "
                    f"{head}; existing runtime work was preserved for explicit "
                    "Git review")
            if (state is not None
                    and (state.base_ref != head or state.step_index >= len(steps))):
                state_path.unlink(missing_ok=True)
                state = None
            if state is None:
                state = WorkflowState(
                    "runtime_test", "", 0, False, head,
                    action="runtime_test", candidate_head=head)
                write_runtime_test_workflow_state(owner, state)

            code = _process_source_workflow(
                cfg, Plan([], cfg.assent_dir), None, steps, rotation,
                sleep, now, _ActiveTask(), unit="runtime_test",
                runtime_commands=commands, state_owner=owner,
                checkpoint_changes=False)
            if code != 0:
                return code

            state = read_runtime_test_workflow_state(owner)
            assert state is not None
            if state.action_status != "PASSED":
                return 1
            state_path.unlink(missing_ok=True)
            print(f"Main runtime test passed in current working tree {primary}.")
            return 0
    except lockfile.LockBusy as error:
        print(str(error))
        return 1
    except KeyboardInterrupt:
        print("Interrupted; main runtime-test state and working-tree edits were preserved.")
        return 130
    except _BillingAbort as error:
        print(f"Main runtime test stopped for billing: {error}; edits preserved.")
        return 1
    except (AssentError, OSError) as error:
        print(f"Main runtime test refused: {error}")
        return 1

def _run_locked(cfg: Config, adapter: Adapter | None,
                sleep: Callable[[float], None],
                now: Callable[[], datetime]) -> int:
    """The actual run body, after the plan lock is held."""
    try:
        # Validate the requested plan itself before stack discovery.  This
        # preserves the task-file error as the primary zero-token diagnostic.
        plan = Plan.parse(cfg.tasks_dir)
        parse_runtime_test_contract(cfg.tasks_dir)
    except AssentError as e:
        print(f"Failed to parse plan directory: {e}")
        return 1
    # Every adapter is resolved and its planned invocations proven before the worktree exists,
    # so rotating later can never discover a configuration the vendor would refuse after a
    # session, status write, or Git change.  The injected adapter is the first slot's test seam;
    # production runs resolve every slot through get_adapter().
    adapter_names = list(cfg.adapter_names)
    for workflow in (cfg.workflow_task, cfg.workflow_plan,
                     cfg.workflow_integration):
        for step in workflow:
            names = (step.adapters if isinstance(
                step, (WorkflowTaskStep, WorkflowRoleStep)) else None)
            for name in names or ():
                if name not in adapter_names:
                    adapter_names.append(name)
    adapters: dict[str, Adapter] = {}
    try:
        for name in adapter_names:
            adapters[name] = (
                adapter if name == cfg.adapter_names[0] and adapter is not None
                else get_adapter(name, cfg))
    except AssentError as e:
        print(str(e))
        return 1

    rotation = _AdapterRotation(
        cfg.adapter_names, tuple(adapters[name] for name in cfg.adapter_names),
        pool=adapters)
    preflight_failures: list[tuple[str, list[str]]] = []
    task_adapter_names = list(cfg.adapter_names)
    for step in cfg.workflow_task:
        if isinstance(step, WorkflowTaskStep):
            for name in step.adapters or ():
                if name not in task_adapter_names:
                    task_adapter_names.append(name)
    for name in task_adapter_names:
        current_adapter = adapters[name]
        errors = capability_errors(cfg, current_adapter, plan, name)
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
    try:
        cursor = read_workflow_state(cfg.tasks_dir)
        if (cursor is not None and cursor.unit == "plan"
                and any(task.status not in ("DONE", "SKIP")
                        for task in plan.tasks)):
            workflow_state_path(cfg.tasks_dir).unlink(missing_ok=True)

        while True:
            plan = Plan.parse(cfg.tasks_dir)
            selected = plan.next_task()
            if selected is None:
                break
            task, _resumed = selected

            if task.status == "TODO":
                # TODO is a fresh workflow attempt. A cursor belongs only to
                # the WIP or BLOCKED attempt that wrote it.
                workflow_state_path(cfg.tasks_dir).unlink(missing_ok=True)
                set_status(task.path, "WIP")
                append_entry(
                    task.journal_path, by="scheduler", event="started",
                    summary="Task workflow started",
                    time_str=now().isoformat(timespec="seconds"))
                task = parse_task_file(task.path)

            print(f"\nTask {task.id}: {task.title}")
            task_steps = _task_source_steps(cfg, task)
            code = _process_source_workflow(
                cfg, plan, task, task_steps, rotation,
                sleep, now, active)
            if code != 0:
                return code

        final_plan = Plan.parse(cfg.tasks_dir)
        if all(task.status in ("DONE", "SKIP") for task in final_plan.tasks):
            plan_steps = _plan_source_steps(cfg)
            if plan_steps:
                code = _process_source_workflow(
                    cfg, final_plan, None, plan_steps, rotation,
                    sleep, now, active)
                if code != 0:
                    return code
        _print_summary(Plan.parse(cfg.tasks_dir))
        try_write_report(cfg)
        return 0

    except KeyboardInterrupt:
        # Ctrl+C on the Windows console reaches the child process (the AI session) too, so
        # the session is terminated by the OS signal; here the engine gathers the produced
        # progress into a wip checkpoint (never discard it) and exits with 130.
        print("\nInterrupt received (Ctrl+C): session terminated, keeping current progress...")
        if active.task is not None and active.session is not None:
            _mark_interrupted_task(
                active.task, active.session,
                "User interrupt; progress kept for next resume", now,
                detail="run received Ctrl+C")
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
            raise
        print("Interrupted.")
        return 130
    except _AuthenticationRequired as e:
        print(f"Run stopped: {e}")
        try_write_report(cfg)
        return 1
    except _BillingAbort as e:
        # Distinct from an acceptance failure and from the infrastructure abort below: the
        # account's prepaid balance is exhausted, which no retry or next task can resolve.
        # Keep the current task's progress, leave it unresolved, and stop the whole run.
        print(f"Run aborted (billing/balance): {e}")
        print("The account's prepaid balance is exhausted; retrying cannot fix this. "
              "Top up the account, then rerun to resume from the kept progress.")
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
        if active.task is not None and active.session is not None:
            _mark_interrupted_task(
                active.task, active.session,
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

def _recover_or_ensure_clean(cfg: Config, now: Callable[[], datetime]) -> None:
    """Gather an interrupted plan worktree without inferring task ownership."""
    if gitops.working_tree_status(cfg.root, cfg.git_excludes).is_clean:
        return
    if not gitops.commit_if_dirty(
            cfg.root,
            f"wip({cfg.tasks_name}): recovered interrupted workflow",
            cfg.git_excludes):
        raise AssentError("dirty plan worktree could not be checkpointed")
    print("Recovered interrupted plan-worktree changes in a WIP checkpoint.")

def _mark_interrupted_task(task: Task, session: SessionIdentity, summary: str,
                           now: Callable[[], datetime], *, detail: str) -> None:
    """Keep an interrupted role's task resumable and journal the interruption."""
    try:
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
    """Keep a billing-aborted task resumable and journal the manual requirement."""
    try:
        set_status(task.path, "WIP")
    except Exception as e:  # abort cleanup must not mask the billing abort with a secondary error
        print(f"Writing back the billing task status failed: {e} (working tree left as is, nothing discarded)")

    try:
        append_entry(
            task.journal_path, by="scheduler", event="billing",
            summary=("Aborted: account balance/credit exhausted; this needs a manual "
                     "top-up, then rerun to resume (progress kept)"),
            detail=detail,
            agent=session.agent, requested_model=session.requested_model,
            requested_effort=session.requested_effort,
            time_str=now().isoformat(timespec="seconds"))
    except Exception as e:  # status and journal are attempted independently; one failing does not block the other
        print(f"Writing the billing journal failed: {e} (working tree left as is, nothing discarded)")

def _plan_verify_commands(plan: Plan) -> list[str]:
    return list(dict.fromkeys(task.verify for task in plan.tasks))

_SESSION_EVIDENCE_ITEMS = 8

_SESSION_EVIDENCE_CHARS = 8_000

def _task_source_steps(
        cfg: Config, task: Task,
        ) -> tuple[_RoleStep | WorkflowActionStep, ...]:
    """Resolve one task workflow into role sessions and focused actions."""
    workflow = _effective_task_workflow(cfg, task)
    steps: list[_RoleStep | WorkflowActionStep] = []
    for step in workflow:
        if isinstance(step, WorkflowActionStep):
            steps.append(step)
            continue
        steps.append(_RoleStep(
            step.role, step.adapters or cfg.adapter_names,
            step.resolved_role.model or task.model, step.writes,
            _role_policy(step)))
    if not steps:
        raise AssentError("Effective task workflow must not be empty")
    if not isinstance(steps[-1], WorkflowActionStep):
        steps.append(WorkflowActionStep("focused_test"))
    return tuple(steps)

def _plan_source_steps(
        cfg: Config,
        ) -> tuple[_RoleStep | WorkflowActionStep, ...]:
    """Resolve the configured plan workflow without inferring role semantics."""
    steps: list[_RoleStep | WorkflowActionStep] = []
    for step in cfg.workflow_plan:
        if isinstance(step, WorkflowActionStep):
            steps.append(step)
            continue
        assert step.model is not None
        steps.append(_RoleStep(
            step.role, step.adapters, step.model, step.writes,
            _role_policy(step)))
    if steps and not isinstance(steps[-1], WorkflowActionStep):
        steps.append(WorkflowActionStep("focused_sweep"))
    return tuple(steps)

def _bounded_session_evidence(output: str) -> str:
    text = output.strip() or "(session returned no text)"
    if len(text) <= _SESSION_EVIDENCE_CHARS:
        return text
    return text[:_SESSION_EVIDENCE_CHARS] + "\n... [session evidence truncated]"

def _with_session_evidence(
        state: WorkflowState, role: str, output: str) -> WorkflowState:
    evidence = state.evidence + (
        f"{role}:\n{_bounded_session_evidence(output)}",)
    return replace(
        state, evidence=evidence[-_SESSION_EVIDENCE_ITEMS:])

def _management_snapshot(cfg: Config, plan: Plan) -> dict[Path, bytes | None]:
    """Snapshot the small control surface no role session may edit."""
    paths = {
        *(task.path for task in plan.tasks),
        *(task.journal_path for task in plan.tasks),
        cfg.assent_dir / "_plan_deps.toml",
        cfg.assent_dir / "assent.toml",
        cfg.assent_dir / "verify.py",
        cfg.assent_dir / "_batch_verification.toml",
        cfg.assent_dir / "_runtime_test_workflow.toml",
        cfg.tasks_dir / "_runtime_test.toml",
        cfg.tasks_dir / "_runtime_test_workflow.toml",
        cfg.tasks_dir / "_verification.toml",
        selection_workflow_state_path(cfg.assent_dir),
        workflow_state_path(cfg.tasks_dir),
    }
    snapshot: dict[Path, bytes | None] = {}
    for path in paths:
        try:
            snapshot[path] = path.read_bytes() if path.is_file() else None
        except OSError as error:
            raise AssentError(
                f"Unable to snapshot protected control file {path}: {error}") from error
    return snapshot

def _management_changes(before: dict[Path, bytes | None]) -> list[str]:
    changed: list[str] = []
    for path, prior in before.items():
        try:
            current = path.read_bytes() if path.is_file() else None
        except OSError:
            current = b"<unreadable>"
        if current != prior:
            changed.append(str(path))
    return changed

def _source_workflow_prompt(
        cfg: Config, plan: Plan, task: Task | None, step: _RoleStep,
        state: WorkflowState, position: int, total: int) -> str:
    runtime_test = state.unit == "runtime_test"
    main_runtime_test = runtime_test and cfg.tasks_dir == cfg.assent_dir
    unit = ("runtime_test for main" if main_runtime_test else
            (f"runtime_test for plan {cfg.tasks_name}" if runtime_test else
            (f"task {task.id}" if task is not None else f"plan {cfg.tasks_name}"))
            )
    contracts_text = "\n\n".join(
        f"--- {item.id}: {item.path} ---\n"
        + item.path.read_text(encoding="utf-8").rstrip()
        for item in (plan.tasks if task is None else [task]))
    if main_runtime_test:
        contracts_text = (
            "Repair the project source so the configured main runtime-test "
            "command passes. Work directly in the current primary working tree; "
            "leave every edit visible for the operator's ordinary Git review.")
    prior = "\n\n".join(state.evidence) or "(none)"
    action = (_runtime_test_prompt(state) if runtime_test else
              (_focused_test_prompt(state) if task is not None
               else _focused_sweep_prompt(state)))
    write_policy = (
        "You may edit any ordinary project source, test, configuration, or "
        "documentation file in this working tree that is needed to "
        "satisfy the stated behavior."
        if step.writes else
        "This is a read-only review session. Do not create, edit, delete, "
        "rename, format, or generate any project file.")
    task_focus = (
        "Focus on this task's behavior, but do not treat predicted file paths "
        "or task ownership as a write boundary."
        if task is not None else
        "Review and repair the cumulative candidate as one plan result; do not "
        "assign findings or files to task owners.")
    runtime_policy = (
        "\nThe runtime commands are scheduler-owned. Do not run Git, Assent, or "
        "any runtime command. AI text cannot declare the runtime test passed. "
        "A writable role may repair the root cause across ordinary source, "
        "tests, fixtures, project configuration, and documentation in this "
        "working tree.\n"
        if runtime_test else "")
    ignored_dir_policy = ""
    if not main_runtime_test:
        ignored_dir_policy = (
            "The exact ignored-dirs command injected below is the sole Assent "
            "exception.\n"
            + ignored_dirs.declaration_clause(_ignored_dir_decision(cfg)))
    return f"""You are one Assent role session.

Read the project rules {_agents_md_path_for_prompt(cfg)} and the Assent session
rules {contracts.instructions_path()} before acting.

Workflow unit: {unit}
Configured AI session: {position} of {total}
Role: {step.role}

Role responsibility:
{step.prompt}

{write_policy}
{task_focus}
{runtime_policy}

Task contracts are read-only. Journals, scheduler state, Git state, receipts,
and files below .git or .assent are also read-only. Do not run Git, Assent, a
scheduler-owned focused action, or the full verifier. The scheduler owns every
checkpoint, task status, journal entry, and action result.
{ignored_dir_policy}

Authoritative task requirements:
{contracts_text}

Prior session evidence:
{prior}
{action}
Do not narrate plans, internal deliberation, or rhetorical questions. Complete
the work, then return only a concise account of what you inspected and, when
writable, what you changed. This output is evidence for the next configured
step, not a conversation with another session.
"""

def _role_session_identity(
        cfg: Config, step: _RoleStep, adapter_name: str) -> SessionIdentity:
    requested_model, requested_effort = cfg.adapter_settings(
        adapter_name).resolve(step.model)
    return SessionIdentity(
        adapter_name, requested_model, requested_effort)

def _session_line(
        adapter_name: str, model: str, session: SessionIdentity) -> str:
    """State the selected tier or literal and the exact provider invocation."""
    return (f"  Session: {adapter_name} | {model}->"
            f"{session.requested_model}/{effort_identity(session.requested_effort)}")

def _run_source_role(
        cfg: Config, plan: Plan, task: Task | None, step: _RoleStep,
        state: WorkflowState, workflow_total: int,
        session_position: int, session_total: int,
        rotation: _AdapterRotation,
        sleep: Callable[[float], None], now: Callable[[], datetime],
        active: _ActiveTask, *, state_owner: Path | None = None,
        checkpoint_changes: bool = True
        ) -> tuple[int, WorkflowState]:
    """Run one role session; only adapter availability may repeat the step."""
    def checkpoint(subject: str) -> None:
        if checkpoint_changes:
            gitops.commit_if_dirty(cfg.root, subject, cfg.git_excludes)

    step_rotation = rotation.subset(step.adapters)
    failed_adapters: set[str] = set()
    while True:
        if state.unit == "runtime_test" and state.quota_waits:
            state = _resume_runtime_quota_waits(
                cfg, state_owner or cfg.tasks_dir, state, step_rotation,
                failed_adapters, sleep, now)
        checkpoint(
            f"wip({cfg.tasks_name}): before workflow step {state.step_index + 1}")
        adapter = step_rotation.adapter
        adapter_name = step_rotation.name
        session = _role_session_identity(cfg, step, adapter_name)
        state = replace(
            state, started=True,
            candidate_head=(gitops.head_ref(cfg.root) or "")
            if state.candidate_head else "")
        _write_source_workflow_state(state_owner or cfg.tasks_dir, state)
        management = _management_snapshot(cfg, plan)
        source_head = gitops.head_ref(cfg.root)
        primary_head = (gitops.head_ref(cfg.source_root)
                        if cfg.source_root is not None else source_head)
        primary_baseline = (
            gitops.dirty_paths(cfg.source_root)
            if cfg.source_root is not None else set())
        prompt = _source_workflow_prompt(
            cfg, plan, task, step, state, session_position, session_total)
        ignored_input_guards = _ignored_input_guards((cfg,))
        print(f"\n{state.unit.title()} workflow step "
              f"{state.step_index + 1}/{workflow_total}: {step.role}")
        print(_session_line(adapter_name, step.model, session))
        active.task = task
        active.session = session
        try:
            result = _invoke_adapter(
                cfg, adapter, adapter_name, prompt, session.requested_model,
                session.requested_effort, cfg.root, context_kind=state.unit,
                context_id=f"workflow.{state.unit}[{state.step_index}]")
        except KeyboardInterrupt:
            ignored_violations = _ignored_input_violations(
                ignored_input_guards, (cfg,))
            if ignored_violations:
                state = _record_ignored_input_violation(
                    state, ignored_violations)
                assert isinstance(state, WorkflowState)
                _write_source_workflow_state(
                    state_owner or cfg.tasks_dir, state)
                raise AssentError(
                    "role changed ignored input: "
                    + ", ".join(ignored_violations[:8]))
            checkpoint(f"wip({cfg.tasks_name}): {step.role} interrupted")
            if state.candidate_head:
                state = replace(
                    state, candidate_head=gitops.head_ref(cfg.root) or "")
            _write_source_workflow_state(
                state_owner or cfg.tasks_dir, state)
            raise
        if result.failure_kind == "billing":
            reason = _adapter_failure_reason(
                result.exit_code, result.stalled, result.output,
                result.failure_kind)
            if task is not None:
                _mark_billing_task(task, session, reason, now)
            active.task = None
            active.session = None
            if state.candidate_head:
                state = replace(
                    state, candidate_head=gitops.head_ref(cfg.root) or "")
                _write_source_workflow_state(
                    state_owner or cfg.tasks_dir, state)
            raise _BillingAbort(reason)
        active.task = None
        active.session = None

        ignored_violations = _ignored_input_violations(
            ignored_input_guards, (cfg,))
        if ignored_violations:
            state = _record_ignored_input_violation(state, ignored_violations)
            assert isinstance(state, WorkflowState)
            _write_source_workflow_state(state_owner or cfg.tasks_dir, state)
            print("Role session crossed its control boundary: "
                  + ", ".join(ignored_violations[:8]))
            return 1, state

        if result.checkpoint_resume and not result.quota_exhausted:
            checkpoint(f"wip({cfg.tasks_name}): {step.role} checkpoint resume")
            continue
        if result.quota_exhausted:
            checkpoint(f"wip({cfg.tasks_name}): {step.role} quota interrupt")
            if state.unit == "runtime_test":
                reset_at = result.reset_at
                if reset_at is not None:
                    reset_at = (reset_at.astimezone(timezone.utc)
                                if reset_at.tzinfo is not None else None)
                waits = {wait.adapter: wait for wait in state.quota_waits}
                waits[adapter_name] = RuntimeQuotaWait(adapter_name, reset_at)
                state = replace(
                    state,
                    quota_waits=tuple(
                        waits[name] for name in step_rotation.names
                        if name in waits),
                    candidate_head=(gitops.head_ref(cfg.root) or "")
                    if state.candidate_head else "")
                _write_source_workflow_state(
                    state_owner or cfg.tasks_dir, state)
            elif len(step_rotation.names) == 1:
                _wait_for_quota(cfg, result.reset_at, sleep, now)
            elif step_rotation.advance_after_quota(failed_adapters):
                _wait_for_rotation(cfg, sleep)
            continue
        reason = _adapter_failure_reason(
            result.exit_code, result.stalled, result.output,
            result.failure_kind)
        if result.failure_kind == "authentication":
            checkpoint(
                f"wip({cfg.tasks_name}): {step.role} authentication failover")
            action = _authentication_failover_action(
                step_rotation, failed_adapters, reason)
            if action == "switch":
                print(f"Role authentication failure: {reason}; switching "
                      f"{adapter_name} -> {step_rotation.name}.")
            else:
                _wait_for_rotation(cfg, sleep)
            continue
        if _adapter_availability_failed(result):
            checkpoint(f"wip({cfg.tasks_name}): {step.role} adapter failover")
            if step_rotation.advance_after_failure(failed_adapters):
                print(f"Role adapter failure: {reason}; switching "
                      f"{adapter_name} -> {step_rotation.name}.")
            else:
                print(f"Role adapters unavailable: {reason}; waiting before "
                      f"restarting with {step_rotation.name}.")
                _wait_for_rotation(cfg, sleep)
            continue
        if result.exit_code != 0 or result.stalled:
            checkpoint(f"wip({cfg.tasks_name}): {step.role} failed")
            if state.candidate_head:
                state = replace(
                    state, candidate_head=gitops.head_ref(cfg.root) or "")
                _write_source_workflow_state(
                    state_owner or cfg.tasks_dir, state)
            print(f"Role session failed: {reason}")
            return result.exit_code or 1, state

        violations = _management_changes(management)
        if cfg.source_root is not None:
            primary_after = gitops.dirty_paths(cfg.source_root)
            violations.extend(
                f"primary worktree:{path}"
                for path in sorted(primary_after - primary_baseline))
        if gitops.head_ref(cfg.root) != source_head:
            violations.append("Git HEAD")
        if (cfg.source_root is not None
                and gitops.head_ref(cfg.source_root) != primary_head):
            violations.append("primary worktree:Git HEAD")
        if not step.writes and not gitops.working_tree_status(
                cfg.root, cfg.git_excludes).is_clean:
            violations.append("read-only session changed project files")
        if violations:
            print("Role session crossed its control boundary: "
                  + ", ".join(violations[:8]))
            return 1, state

        checkpoint(
            f"wip({cfg.tasks_name}): workflow step {state.step_index + 1} "
            f"({step.role})")
        state = _with_session_evidence(state, step.role, result.output)
        state = replace(
            state, step_index=state.step_index + 1, started=False,
            quota_waits=(),
            candidate_head=(gitops.head_ref(cfg.root) or "")
            if state.candidate_head else "")
        _write_source_workflow_state(state_owner or cfg.tasks_dir, state)
        if task is not None:
            append_entry(
                task.journal_path, by="scheduler", event="session",
                summary=f"Role session {step.role!r} completed",
                detail=_bounded_session_evidence(result.output),
                agent=session.agent,
                requested_model=session.requested_model,
                requested_effort=session.requested_effort,
                time_str=now().isoformat(timespec="seconds"))
        return 0, state

def _finish_task_workflow(
        cfg: Config, task: Task, record: _TestActionEvidence,
        now: Callable[[], datetime]) -> None:
    set_status(task.path, "DONE")
    append_entry(
        task.journal_path, by="scheduler", event="done",
        summary="Scheduler focused_test passed",
        detail=(f"Command: {record.command}\nExit code: {record.exit_code}\n"
                f"Summary:\n{record.summary}"),
        time_str=now().isoformat(timespec="seconds"))
    workflow_state_path(cfg.tasks_dir).unlink(missing_ok=True)
    subject = _checkpoint_subject(
        cfg, "auto", task, _short(task.title) or "done")
    if not gitops.commit_if_dirty(cfg.root, subject, cfg.git_excludes):
        gitops.commit_empty(cfg.root, subject)

def _finish_plan_workflow(
        cfg: Config, state: WorkflowState,
        steps: tuple[_RoleStep | WorkflowActionStep, ...], *,
        state_owner: Path | None = None) -> None:
    state = replace(state, step_index=len(steps), started=False)
    _write_source_workflow_state(state_owner or cfg.tasks_dir, state)

def _source_workflow_unresolved(
        cfg: Config, task: Task | None, state: WorkflowState,
        record: _TestActionEvidence, now: Callable[[], datetime], *,
        state_owner: Path | None = None,
        reason: str = "the configured steps were exhausted") -> int:
    if task is not None:
        set_status(task.path, "BLOCKED")
        append_entry(
            task.journal_path, by="scheduler", event="blocked",
            summary="Task workflow exhausted while focused_test failed",
            detail=(f"Command: {record.command}\nExit code: {record.exit_code}\n"
                    f"Summary:\n{record.summary}"),
            time_str=now().isoformat(timespec="seconds"))
    _write_source_workflow_state(state_owner or cfg.tasks_dir, state)
    print(f"{state.unit.title()} workflow: REVIEW UNRESOLVED, HUMAN DECISION; "
          f"{reason}. Evidence and edits were "
          "preserved.")
    return 0

def _source_workflow_gate_unresolved(
        cfg: Config, task: Task | None, state: WorkflowState,
        action: str, summary: str, now: Callable[[], datetime], *,
        state_owner: Path | None = None) -> int:
    """Close an exhausted workflow whose final action never started."""
    if task is not None:
        set_status(task.path, "BLOCKED")
        append_entry(
            task.journal_path, by="scheduler", event="blocked",
            summary=("Task workflow exhausted while the ignored-directory "
                     "decision remained unsettled"),
            detail=f"Action not started: {action}\nReason:\n{summary}",
            time_str=now().isoformat(timespec="seconds"))
    _write_source_workflow_state(state_owner or cfg.tasks_dir, state)
    print(f"{state.unit.title()} workflow: REVIEW UNRESOLVED, HUMAN DECISION; "
          "the configured steps were exhausted. Evidence and edits were "
          "preserved.")
    return 0

def _process_source_workflow(
        cfg: Config, plan: Plan, task: Task | None,
        steps: tuple[_RoleStep | WorkflowActionStep, ...],
        rotation: _AdapterRotation, sleep: Callable[[float], None],
        now: Callable[[], datetime], active: _ActiveTask, *,
        unit: str | None = None,
        runtime_commands: tuple[str, ...] | None = None,
        state_owner: Path | None = None,
        checkpoint_changes: bool = True) -> int:
    """Execute one source workflow as a finite linear step array."""
    if not steps:
        return 0
    unit = unit or ("task" if task is not None else "plan")
    task_id = task.id if task is not None else ""
    owner = state_owner or cfg.tasks_dir
    state = (read_runtime_test_workflow_state(owner)
             if unit == "runtime_test" else read_workflow_state(owner))
    if (state is None or state.unit != unit or state.task_id != task_id):
        state = WorkflowState(
            unit, task_id, 0, False, gitops.head_ref(cfg.root) or "HEAD",
            action="runtime_test" if unit == "runtime_test" else "")
        _write_source_workflow_state(owner, state)
    elif violation := _ignored_input_violation(state.evidence):
        print(f"Workflow cannot resume without rework: {violation}")
        return 1
    if state.step_index >= len(steps):
        if state.action_status == "PASSED":
            return 0
        record = (_runtime_test_record(state) if unit == "runtime_test" else
                  (_focused_test_record(state) if task is not None
                   else _focused_sweep_record(state)))
        if record is None:
            raise AssentError("Workflow ended without a scheduler action")
        return _source_workflow_unresolved(
            cfg, task, state, record, now, state_owner=owner)

    while state.step_index < len(steps):
        step = steps[state.step_index]
        main_runtime_test = (
            unit == "runtime_test" and cfg.tasks_dir == cfg.assent_dir)
        if isinstance(step, _RoleStep):
            if (unit == "runtime_test"
                    and not main_runtime_test
                    and _runtime_test_record(state) is None
                    and _ignored_dir_decision(cfg).settled):
                # A prior action was refused before it started, and its only
                # precondition is now settled. Resume at the next scheduler
                # action without spending another repair session.
                state = replace(
                    state, step_index=state.step_index + 1, started=False)
                _write_source_workflow_state(owner, state)
                continue
            session_position, session_total = _session_progress(
                steps, state.step_index)
            source_head = gitops.head_ref(cfg.root)
            source_identity = (
                None if checkpoint_changes else _runtime_worktree_identity(cfg))
            code, state = _run_source_role(
                cfg, plan, task, step, state, len(steps),
                session_position, session_total, rotation, sleep, now, active,
                state_owner=owner, checkpoint_changes=checkpoint_changes)
            if code != 0:
                return code
            source_changed = (
                gitops.head_ref(cfg.root) != source_head
                if checkpoint_changes else
                _runtime_worktree_identity(cfg) != source_identity)
            if unit == "runtime_test" and not source_changed:
                record = _runtime_test_record(state)
                if record is None:
                    # The preceding action was refused before it started. The
                    # role may have settled its injected ignored-directory
                    # decision without changing tracked source; let the next
                    # scheduler action evaluate that precondition again.
                    continue
                state = replace(state, step_index=len(steps), started=False)
                return _source_workflow_unresolved(
                    cfg, task, state, record, now, state_owner=owner,
                    reason="the writable role made no source change")
            continue

        if checkpoint_changes:
            gitops.commit_if_dirty(
                cfg.root,
                f"wip({cfg.tasks_name}): before {step.action}",
                cfg.git_excludes)
        print(f"\n{unit.title()} workflow step {state.step_index + 1}/"
              f"{len(steps)}: {step.action}")
        decision = (None if main_runtime_test else _ignored_dir_decision(cfg))
        if decision is not None and not decision.settled:
            summary = (ignored_dirs.closeout_refusal(decision)
                       or f"Run `{ignored_dirs.DECLARE_COMMAND}`")
            gate_evidence = f"{step.action} not started:\n{summary}"
            state = replace(
                state, evidence=state.evidence + (gate_evidence,),
                action=("runtime_test" if unit == "runtime_test" else ""),
                action_status="", action_source_tree="",
                action_exit_code=0, action_evidence=())
            _write_source_workflow_state(owner, state)
            print(f"  {step.action} not started: {summary}")
            if state.step_index == len(steps) - 1:
                state = replace(
                    state, step_index=len(steps), started=False)
                return _source_workflow_gate_unresolved(
                    cfg, task, state, step.action, summary, now,
                    state_owner=owner)
            state = replace(
                state, step_index=state.step_index + 1, started=False)
            _write_source_workflow_state(owner, state)
            continue
        elif unit == "runtime_test":
            if runtime_commands is None:
                raise AssentError("Runtime workflow has no contract commands")
            state, record, _reused = _run_runtime_test_action(
                cfg, owner, runtime_commands, state,
                allow_dirty=not checkpoint_changes)
        elif task is not None:
            state, record, _reused = _run_focused_test_action(cfg, task, state)
        else:
            state, record, _reused = _run_focused_sweep_action(
                cfg, plan, state)
        if record.status == "PASSED":
            if unit == "runtime_test":
                _finish_plan_workflow(
                    cfg, state, steps, state_owner=owner)
            elif task is not None:
                _finish_task_workflow(cfg, task, record, now)
            else:
                _finish_plan_workflow(cfg, state, steps)
            if unit != "runtime_test":
                try_write_report(cfg)
            return 0
        if state.step_index == len(steps) - 1:
            state = replace(
                state, step_index=len(steps), started=False)
            return _source_workflow_unresolved(
                cfg, task, state, record, now, state_owner=owner)
        state = replace(
            state, step_index=state.step_index + 1, started=False)
        _write_source_workflow_state(owner, state)
    raise AssertionError("source workflow loop escaped its finite step array")

def _verify_subprocess(cfg: Config, command: str) -> subprocess.CompletedProcess:
    """Run verify in the target working tree and return the completed process (no output).

    A task gate is always a narrow command -- the plan parser refuses one naming
    `.assent/verify.py` -- so the command keeps its original shell semantics and needs
    no main-tree expansion; the cwd is the current target working tree.
    """
    return subprocess.run(
        command, shell=True, cwd=str(cfg.root),
        capture_output=True, encoding="utf-8", errors="replace")

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

_UNITTEST_MODULE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")

def _unittest_modules(command: str) -> tuple[str, ...] | None:
    """Return the modules of an exact ``python -m unittest <modules>`` command.

    The shape is recognized conservatively and nothing else qualifies: a
    different interpreter or runner, extra flags, unusual spacing, or a path
    argument all return ``None``, so such a command keeps running exactly on
    its own as before.
    """
    tokens = command.split(" ")
    if tokens[:3] != ["python", "-m", "unittest"] or len(tokens) < 4:
        return None
    modules = tokens[3:]
    if not all(_UNITTEST_MODULE.match(module) for module in modules):
        return None
    return tuple(modules)

def _merged_unittest_passes(
        cfg: Config,
        commands: list[str]) -> dict[str, subprocess.CompletedProcess]:
    """Prove overlapping unittest commands with one run over their module union.

    Within one fixed tree state, distinct declared commands that name a shared
    module re-import and re-execute that module once per command.  Running the
    ordered union once instead proves every command in the group when it exits
    0.  A nonzero merged run proves nothing and returns nothing, so the caller
    falls back to its ordinary per-command execution and keeps today's failure
    attribution and diagnostics.
    """
    modules_by_command = {command: _unittest_modules(command)
                          for command in commands}
    matched = [command for command in commands
               if modules_by_command[command] is not None]
    if len(matched) < 2:
        return {}
    union: list[str] = []
    for command in matched:
        for module in modules_by_command[command] or ():
            if module not in union:
                union.append(module)
    result = _verify_subprocess(cfg, "python -m unittest " + " ".join(union))
    if result.returncode != 0:
        return {}
    return {command: result for command in matched}

def _verify_focused_locked(
        cfg: Config, task_id: str | None = None, *,
        failure_output: list[str] | None = None) -> int:
    """Run one task check or the DONE-task sweep from the source worktree.

    Focused verification is deliberately separate from receipt-producing full
    verification.  It only proves that the task-level commands pass in the
    plan's own source worktree, so it never creates an integration candidate
    or touches a verification receipt.
    """
    plan_name = cfg.tasks_name
    main = gitops.main_worktree(cfg.root)
    source = gitops.resolve_plan_source(main, plan_name, cfg.git_excludes)
    source_cfg = cfg.for_worktree(source.worktree)

    plan = Plan.parse(cfg.tasks_dir)
    if task_id is not None:
        task = plan.get(task_id)
        if task is None:
            raise AssentError(f"task {task_id} not found in plan {plan_name}")
        commands = [task.verify]
        label = f"verify {plan_name} --focus {task_id}"
        kind = "focused test"
    else:
        commands = []
        seen: set[str] = set()
        for task in plan.tasks:
            if task.status != "DONE" or task.verify in seen:
                continue
            seen.add(task.verify)
            commands.append(task.verify)
        if not commands:
            raise AssentError(
                f"plan {plan_name} has no DONE task with an eligible focused "
                "verify command")
        label = f"verify {plan_name} --focus"
        kind = "focused sweep"

    # --focus provisions the persistent source worktree like every other verify
    # entry point, and writes no receipt of any kind.
    ignored_dirs.prepare_sources(main, [(plan_name, source.worktree)])
    print(f"{label}: source worktree {source.worktree}")
    print(f"{label}: {kind} cannot authorize `accept`; "
          "complete integration verification has not run")
    # One fixed tree, so overlapping unittest commands are proven together; a
    # failed or ineligible merge leaves every command to run one at a time.
    merged = (_merged_unittest_passes(source_cfg, commands)
              if task_id is None else {})
    for command in commands:
        proven = merged.get(command)
        if proven is not None:
            _show_verify_result(command, proven)
            continue
        result = _verify_subprocess(source_cfg, command)
        _show_verify_result(command, result)
        if result.returncode != 0:
            if failure_output is not None:
                failure_output.append("\n".join(
                    stream for stream in (result.stdout, result.stderr)
                    if stream))
            print(f"{label}: failed; this focused result cannot "
                  "authorize `accept`")
            return 1
    print(f"{label}: passed; complete integration verification "
          "has not run and this result cannot authorize `accept`")
    return 0

def verify_focused(cfg: Config, task_id: str | None = None) -> int:
    """Run one task check or a DONE-task sweep without making receipts."""
    plan_name = cfg.tasks_name
    label = (f"verify {plan_name} --focus {task_id}" if task_id is not None
             else f"verify {plan_name} --focus")
    try:
        with lockfile.hold_lock(cfg.tasks_dir, plan_name):
            return _verify_focused_locked(cfg, task_id)
    except lockfile.LockBusy as e:
        print(f"{label}: refused ({e})")
        return 1
    except AssentError as e:
        print(f"{label}: failed ({e})")
        return 1

def _quota_wait_seconds(cfg: Config, reset_at: datetime | None,
                        now: Callable[[], datetime]) -> float:
    """How long to wait this round for quota (seconds). If the reset time can be parsed ->
    sleep until reset + buffer (0 if already past); if it cannot -> one poll interval. A pure
    function, easy to test on its own."""
    if reset_at is not None:
        return max(0.0, (reset_at + _QUOTA_BUFFER - now()).total_seconds())
    return float(cfg.quota_poll_minutes * 60)


def _resume_runtime_quota_waits(
        cfg: Config, owner: Path, state: WorkflowState,
        rotation: _AdapterRotation, failed: set[str],
        sleep: Callable[[float], None], now: Callable[[], datetime]
        ) -> WorkflowState:
    """Select an available runtime adapter or wait for the earliest retry."""
    configured = set(rotation.names)
    unexpected = sorted(
        wait.adapter for wait in state.quota_waits
        if wait.adapter not in configured)
    if unexpected:
        raise AssentError(
            "Runtime quota state does not match the current role adapters: "
            + ", ".join(unexpected))
    while state.quota_waits:
        instant = now()
        waits = {wait.adapter: wait for wait in state.quota_waits}
        expired = {
            name for name, wait in waits.items()
            if wait.reset_at is not None
            and wait.reset_at + _QUOTA_BUFFER <= instant
        }
        if expired:
            state = replace(
                state, quota_waits=tuple(
                    wait for wait in state.quota_waits
                    if wait.adapter not in expired))
            _write_source_workflow_state(owner, state)
            waits = {wait.adapter: wait for wait in state.quota_waits}

        unavailable = set(waits) | rotation.auth_failed | failed
        for offset in range(len(rotation.names)):
            index = (rotation.index + offset) % len(rotation.names)
            if rotation.names[index] not in unavailable:
                rotation.index = index
                return state

        retry_waits = tuple(
            wait for wait in state.quota_waits
            if wait.adapter not in rotation.auth_failed)
        if not retry_waits:
            return state
        known = [wait for wait in retry_waits if wait.reset_at is not None]
        has_unknown = any(wait.reset_at is None for wait in retry_waits)
        earliest = min(
            (wait.reset_at for wait in known), default=None)
        known_seconds = (_quota_wait_seconds(cfg, earliest, now)
                         if earliest is not None else None)
        poll_seconds = (float(cfg.quota_poll_minutes * 60)
                        if has_unknown else None)
        poll_unknown = (poll_seconds is not None
                        and (known_seconds is None
                             or poll_seconds <= known_seconds))
        _wait_for_quota(
            cfg, None if poll_unknown else earliest, sleep, now)
        instant = now()
        state = replace(
            state, quota_waits=tuple(
                wait for wait in state.quota_waits
                if not ((poll_unknown and wait.reset_at is None)
                        or (wait.reset_at is not None
                            and wait.reset_at + _QUOTA_BUFFER <= instant))))
        _write_source_workflow_state(owner, state)
    return state

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
               stream: TextIO | None = None,
               monotonic: Callable[[], float] = time.monotonic) -> None:
    """Countdown wait. Terminal (tty) -> update one line in place with \\r, without stacking
    lines; non-tty (redirected to a file/pipe) -> print one message, then sleep in segments of
    at most ``segment`` seconds so a stop request lands within one segment on every platform
    (see _COUNTDOWN_SEGMENT); the total wait is unchanged. The injected sleep lets tests avoid
    really sleeping.

    The segments remain the platform-independent backstop, but the production sleep is
    ``interruptible_sleep``, so a stop request ends the current segment immediately; both
    loops then stop counting down rather than sitting out the rest of a multi-hour wait
    while a KeyboardInterrupt is already pending. The terminal line is shortened to fit
    before the last column, and its monotonic deadline prevents slow rendering from delaying
    the rerun."""
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

    try:
        columns = os.get_terminal_size(stream.fileno()).columns
    except (AttributeError, OSError, ValueError):
        columns = 80
    line_width = max(1, columns - 1)  # writing the last column can wrap immediately
    deadline = monotonic() + seconds
    last_line = ""
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        h, rem = divmod(int(remaining + 0.999), 3600)
        m, s = divmod(rem, 60)
        stamp = f"{h:02d}:{m:02d}:{s:02d}"
        candidates = (
            f"  {label}: countdown {stamp} before rerunning... ",
            f"  {label}: {stamp}",
            f"  Retry in {stamp}",
        )
        last_line = next(
            (candidate for candidate in candidates
             if len(candidate) <= line_width),
            stamp[-line_width:],
        )
        transient_write("\r" + last_line)
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        step = tick if tick < remaining else remaining
        sleep(step)
        if stop_wake_requested():
            break
    transient_write("\r" + " " * len(last_line) + "\r")

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
