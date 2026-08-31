"""Read-only inspection of a plan: report, status, check.

None of these commands starts an AI session, takes the plan lock, or changes
Git state, so they stay usable while a run is in progress.  They answer three
questions about a plan as it stands right now:

- ``report`` renders and writes ``_report.md``, the acceptance meeting's agenda;
- ``status`` prints the progress counts, the checkpoint, the stack, and which
  task would run next;
- ``check`` validates the environment and the plan format -- the condition that
  adjourns a planning meeting.

Aggregation is mechanical work, zero tokens.  The shared pre-session decisions
(selection resolution, adapter capability, assignment rendering, stack state) come
from ``assent.preflight``, which ``assent.engine`` uses too, so a query and a
run can never answer them differently.  This module must not import
``assent.engine``: the report is written from inside a run as a best-effort
side effect, and the dependency runs that way only.
"""

from __future__ import annotations

import hashlib

import re

import subprocess

from collections import Counter

from datetime import datetime, timezone

from pathlib import Path

from typing import Callable

from assent import AssentError, contracts, gitops, ignored_dirs, usage

from assent.adapters import Adapter, get_adapter

from assent.config import PROJECT_LAYER, Config

from assent.plandeps import archived_plan_names, parse_plan_dependencies

from assent.plan_verification import receipt_report_lines

from assent.modeling import effort_identity, literal_value

from assent.plan import (Plan, RuntimeTestContract, Task,
                         parse_runtime_test_contract, read_entries,
                         read_workflow_state)

from assent.preflight import (GIT_REQUIRED_MESSAGE, SessionIdentity,
                              capability_errors, has_git_marker,
                              print_task_assignments, resolve_selection,
                              resolve_stack_state,
                              resolve_task_assignments,
                              runtime_test_adapter_names,
                              runtime_test_capability_errors,
                              worktree_configuration_errors)
from assent.verification_common import verifier_digest

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

def _query_git_root(cfg: Config) -> Path:
    """When a valid worktree already exists, read git info from the isolated branch instead."""
    if cfg.source_root is not None:
        return cfg.root
    candidate = gitops.worktree_path(cfg.root, cfg.tasks_name)
    top = _git_read(candidate, "rev-parse", "--show-toplevel")
    if top and Path(top).resolve() == candidate.resolve():
        return candidate
    return cfg.root

def _stack_report_lines(cfg: Config, plan: Plan) -> list[str]:
    """Describe the currently derived stack without authorizing any action."""
    if all(t.status in ("DONE", "SKIP") for t in plan.tasks):
        return ["Stack base: not applicable (plan complete)"]
    try:
        state = resolve_stack_state(cfg)
    except AssentError as e:
        return [f"Stack base: unavailable ({e})"]
    upstream = state.base.speculative_upstream
    if upstream is None:
        return ["Stack base: current target main",
                "Speculative upstream: none (all direct upstreams accepted)"]
    return [f"Stack base: {state.base.resolved_base}",
            f"Speculative upstream: {upstream.plan} @ {upstream.tip} (unaccepted)"]


def _ignored_input_report_lines(cfg: Config) -> list[str]:
    """Expose local directories used by verification but absent from Git."""
    try:
        worktree = _query_git_root(cfg)
        main = gitops.main_worktree(worktree)
        decision = ignored_dirs.classify(
            main, worktree, ignored_dirs.read_manifest(main))
    except AssentError as error:
        return [f"Local ignored-directory inputs: unavailable ({error})"]
    if decision.required:
        return ["Local ignored-directory inputs (not delivered by Git): "
                + ", ".join(decision.required)]
    if not decision.settled:
        return [f"Local ignored-directory inputs: {decision.state} (unresolved)"]
    return ["Local ignored-directory inputs: none"]

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
        f"Plan: {cfg.tasks_name}",
        f"Generated at: {stamp}",
        f"Branch: {branch}",
        f"Progress: DONE {counts.get('DONE', 0)} / BLOCKED {counts.get('BLOCKED', 0)} / "
        f"WIP {counts.get('WIP', 0)} / TODO {counts.get('TODO', 0)} / "
        f"SKIP {counts.get('SKIP', 0)} ({len(plan.tasks)} total)",
        *_stack_report_lines(cfg, plan),
        *_ignored_input_report_lines(cfg),
        "",
    ]
    try:
        workflow = read_workflow_state(cfg.tasks_dir)
    except AssentError as error:
        lines += [f"Workflow: unavailable ({error})", ""]
    else:
        if (workflow is not None and workflow.unit == "plan"
                and workflow.action_status != "PASSED"):
            lines += [
                "Workflow: REVIEW UNRESOLVED, HUMAN DECISION",
                f"Last focused_sweep: {workflow.action_status or 'IN PROGRESS'}",
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
        lines += ["", "To decide: inspect each BLOCKED task's journal and "
                  "checkpoint. Rework it to continue, or mark it SKIP."]
    lines += ["", *usage.report_lines(cfg.assent_dir, cfg.tasks_name),
              "", *receipt_report_lines(cfg)]
    return "\n".join(lines) + "\n"

def write_report(cfg: Config, plan: Plan,
                 now: Callable[[], datetime] | None = None) -> Path:
    """Write the report to the plan's _report.md (a runtime artifact, not version-controlled)."""
    path = cfg.tasks_dir / "_report.md"
    path.write_text(render_report(cfg, plan, now), encoding="utf-8",
                    newline="\n")
    return path

def try_write_report(cfg: Config) -> None:
    """Best-effort report update at run wrap-up; a report failure never affects the main
    flow's result or exit code."""
    try:
        write_report(cfg, Plan.parse(cfg.tasks_dir))
    # This is a deliberate best-effort isolation boundary: any ordinary error including
    # permissions, file locks, and content parsing must not mask the task result;
    # KeyboardInterrupt/SystemExit still propagate as usual.
    except Exception:
        pass

def report(cfg: Config) -> int:
    """Subcommand: generate _report.md and print it to the terminal (zero tokens)."""
    try:
        plan = Plan.parse(cfg.tasks_dir)
    except AssentError as e:
        print(f"Failed to parse plan directory: {e}")
        return 1
    text = render_report(cfg, plan)
    path = write_report(cfg, plan)
    print(text, end="")
    print(f"(written to {path})")
    return 0

def status(cfg: Config) -> int:
    try:
        plan = Plan.parse(cfg.tasks_dir)
    except AssentError as e:
        print(f"Failed to parse plan directory: {e}")
        return 1

    counts = Counter(t.status for t in plan.tasks)
    print(f"Plan directory: {cfg.tasks_dir}")
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
            requested_model, requested_effort = resolve_selection(cfg, nxt.model)
            selection_label = (
                f"{requested_model}/{effort_identity(requested_effort)}")
        except AssentError as e:
            selection_label = f"unavailable ({e})"
        tag = " (WIP resume)" if resumed else ""
        print(f"Next task: {nxt.id} [{nxt.model} -> {selection_label}] "
              f"{nxt.title}{tag}")
    elif counts.get("TODO", 0):
        print("Next task: (TODO remains, but blocked by unfinished prerequisites or a BLOCKED task)")
    else:
        print("Next task: (none, all DONE/BLOCKED/SKIP)")
    return 0

def _config_source_lines(cfg: Config) -> list[str]:
    """State the layers behind the effective settings, lowest priority first.

    The project file is an optional override and a project locator, so a setup that
    states everything user-wide is complete: naming the absent file as optional keeps
    that ordinary case from reading like a failure.
    """
    parts = [source.layer if source.path is None
             else f"{source.layer} ({source.path})" for source in cfg.sources]
    lines = [f"Config: OK (plan = {cfg.tasks_name})",
             "Config sources (lowest priority first): " + ", ".join(parts)]
    if not any(source.layer == PROJECT_LAYER for source in cfg.sources):
        lines.append(f"  project override {cfg.assent_dir / 'assent.toml'}: "
                     "absent (optional)")
    return lines

def _contract_lines() -> tuple[bool, list[str]]:
    """Validate both global contracts, reporting each problem on its own line."""
    try:
        errors = contracts.contract_errors()
    except AssentError as e:
        return False, [f"Global contracts: FAIL ({e})"]
    if not errors:
        return True, [f"Global contracts: OK ({contracts.contract_dir()}: "
                      f"{', '.join(contracts.CONTRACT_NAMES)} current)"]
    return False, ["Global contracts: FAIL",
                   *(f"  - {message}" for message in errors),
                   f"  {contracts.CONTRACT_REMEDY}"]


def _project_verifier_lines(cfg: Config) -> tuple[bool, list[str]]:
    """Require the planning meeting to configure the complete verifier."""
    try:
        verifier_digest(cfg)
    except AssentError as error:
        return False, [f"Project verifier: FAIL ({error})"]
    return True, [f"Project verifier: OK ({cfg.assent_dir / 'verify.py'})"]


def _runtime_test_contract_lines(
        cfg: Config) -> tuple[bool, RuntimeTestContract | None]:
    """Parse the required plan runtime-test contract and report its mode."""
    try:
        contract = parse_runtime_test_contract(cfg.tasks_dir)
    except AssentError as error:
        print(f"Runtime-test contract: FAIL ({error})")
        return False, None
    print(f"Runtime-test contract: OK (execution = {contract.execution})")
    if contract.execution == "disabled":
        print("Runtime-test workflow: not required (execution = disabled)")
        return True, contract
    if not cfg.workflow_runtime_test:
        print("Runtime-test workflow: FAIL (effective [workflow].runtime_test "
              "is missing or empty)")
        return False, contract
    print(f"Runtime-test workflow: OK ({len(cfg.workflow_runtime_test)} steps)")
    return True, contract

def _assignment_source_lines(
        cfg: Config,
        blocks: list[tuple[str, list[tuple[Task, SessionIdentity]]]]
        ) -> list[str]:
    """Name the layer behind every setting the printed assignments actually used.

    Only the keys the resolution consumed are shown -- the adapter selection and the
    models entry of each tier in the plan -- so the provenance answers "why this
    invocation" without dumping the whole config.  A literal selection consumed no
    settings key at all and therefore contributes none.
    """
    lines = [f"Setting sources: adapter.name = {cfg.source_of('adapter.name')}"
             f" (active: {', '.join(cfg.adapter_names)})"]
    for adapter_name, assignments in blocks:
        keys = [f"models.{task.model}" for task, _session in assignments
                if literal_value(task.model) is None]
        used = ", ".join(
            f"{key} = {cfg.source_of(f'adapter.{adapter_name}.{key}')}"
            for key in dict.fromkeys(keys))
        lines.append(f"  {adapter_name}: {used}")
    return lines

def check(cfg: Config) -> int:
    ok = True
    if not has_git_marker(cfg.root):
        print(GIT_REQUIRED_MESSAGE)
        ok = False

    for line in _config_source_lines(cfg):
        print(line)
    # The same contracts `run` refuses to start a session without.
    contract_ok, contract_lines = _contract_lines()
    ok = ok and contract_ok
    for line in contract_lines:
        print(line)

    verifier_ok, verifier_lines = _project_verifier_lines(cfg)
    ok = ok and verifier_ok
    for line in verifier_lines:
        print(line)

    runtime_ok, runtime_contract = _runtime_test_contract_lines(cfg)
    ok = ok and runtime_ok

    # Parsing proves the complete task-file schema.
    plan: Plan | None = None
    try:
        plan = Plan.parse(cfg.tasks_dir)
        print(f"Task-file format: OK ({len(plan.tasks)} tasks; dependencies acyclic)")
        try:
            blocks = resolve_task_assignments(cfg, plan)
            print_task_assignments(blocks)
            for line in _assignment_source_lines(cfg, blocks):
                print(line)
        except AssentError as e:
            ok = False
            print(f"Task assignment: FAIL ({e})")
    except AssentError as e:
        ok = False
        print(f"Task-file format: FAIL ({e})")

    # A live plan may not reuse an archived name.  Nothing refuses the name when the
    # plan is created, and the collision is otherwise only noticed indirectly -- when
    # some other plan happens to name it in `after` -- so `check`, the meeting's exit
    # gate, is where it becomes visible while renaming is still cheap.
    try:
        if cfg.tasks_name in archived_plan_names(cfg.assent_dir):
            ok = False
            print(f"Plan name: FAIL ({cfg.tasks_name} is already in the archive "
                  "roster; rename this plan, or restore the archived one first)")
        else:
            print(f"Plan name: OK ({cfg.tasks_name} is not an archived name)")
    except AssentError as e:
        ok = False
        print(f"Plan name: FAIL (archive roster unreadable: {e})")

    # Dependency declaration format and reference integrity of the selected plan; the
    # whole-graph cycle check is validated by the no-argument CLI check.
    try:
        dependencies = parse_plan_dependencies(cfg.tasks_dir)
        after = ", ".join(dependencies.after) or "none"
        print(f"Plan dependencies: OK (after = {after})")
    except AssentError as e:
        ok = False
        print(f"Plan dependencies: FAIL ({e})")

    # adapter resolves
    adapter: Adapter | None = None
    adapter_name = cfg.adapter_names[0]
    try:
        adapter = get_adapter(adapter_name, cfg)
        print(f"adapter: OK ({adapter_name})")
    except AssentError as e:
        ok = False
        print(f"adapter: FAIL ({e})")

    if (runtime_contract is not None
            and runtime_contract.execution != "disabled"
            and cfg.workflow_runtime_test is not None):
        runtime_adapters: dict[str, Adapter] = {}
        runtime_adapter_errors: dict[str, str] = {}
        for name in runtime_test_adapter_names(cfg):
            if name == adapter_name and adapter is not None:
                runtime_adapters[name] = adapter
                continue
            try:
                runtime_adapters[name] = get_adapter(name, cfg)
            except AssentError as error:
                runtime_adapter_errors[name] = str(error)
        runtime_failures = runtime_test_capability_errors(cfg, runtime_adapters)
        for name, error in runtime_adapter_errors.items():
            runtime_failures.setdefault(name, []).append(error)
        for name in runtime_test_adapter_names(cfg):
            failures = runtime_failures.get(name, [])
            if failures:
                ok = False
                print(f"{name} runtime-test capability preflight: FAIL")
                for message in failures:
                    print(f"  - {message}")
            else:
                print(f"{name} runtime-test capability preflight: OK")

    # Every model/effort the plan could still send, proven against the active adapter's
    # capability contract; the same gate `run` applies before opening a session.
    if adapter is not None and plan is not None:
        errors = capability_errors(cfg, adapter, plan)
        if errors:
            ok = False
            print(f"{adapter_name} capability preflight: FAIL")
            for message in errors:
                print(f"  - {message}")
        else:
            print(f"{adapter_name} capability preflight: OK")

    # git repo
    inside = _git_read(cfg.root, "rev-parse", "--is-inside-work-tree")
    if inside == "true":
        print("git repo: OK")
        try:
            errors = worktree_configuration_errors(cfg)
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
        label = adapter_name
        probe_ok, message = adapter.probe_cli()
        if probe_ok:
            print(f"{label} CLI: OK ({message})")
        else:
            ok = False
            print(f"{label} CLI: FAIL ({message})")

    print("Result: passed" if ok else "Result: some items failed")
    return 0 if ok else 1
