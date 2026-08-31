"""Plan-level scheduling for a whole-project ``run``.

A single plan is still handled by a spawned ``assent run <plan>``
child process; this module is only responsible for dependency unlocking, the
concurrency cap, per-line output, and interrupt forwarding.
"""
from __future__ import annotations

import contextlib
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TextIO

from assent import AssentError, gitops
from assent.config import list_task_plans, load_config
from assent.plandeps import (archived_plan_names, is_upstream_complete,
                               parse_plan_dependency_graph,
                               resolve_plan_base)
from assent.plan import Plan, plan_workflow_needs_resume

_POLL_SECONDS = 0.05
_GIT_REQUIRED_MESSAGE = "This project has no git repository yet; run git init first"
# 130 is the exit code a child produces after a normal KeyboardInterrupt
# cleanup; 3221225786 (0xC000013A, STATUS_CONTROL_C_EXIT) is the code the OS
# assigns when it terminates a child directly on Windows because no handler
# was installed. Both mean "interrupted", not "plan failed" -- treat them
# the same to avoid a false failure report.
_INTERRUPT_RETURNCODES = (130, 3221225786)
# How long a child may take to finish its own interrupt cleanup (wip
# checkpoint, r-file entry) before the parent escalates to a forced tree
# termination, and how long a POSIX process group gets between SIGTERM and
# SIGKILL. The parent never waits without a timeout: an unreachable child must
# not be able to hang the whole run.
_INTERRUPT_GRACE_SECONDS = 60
_TERMINATE_GRACE_SECONDS = 10
# How many times a forced-cleanup step may be restarted after a further Ctrl+C
# preempted it. Each restart resumes idempotent work, so a handful of attempts
# is enough to outlast a person hammering the key, while the bound keeps a
# stuck key from pinning the scheduler in the loop.
_FORCED_CLEANUP_ATTEMPTS = 5


def _package_search_root() -> Path:
    """The directory that owns the running ``assent`` package.

    ``python -m assent`` puts the child's working directory at the front of its
    module search path, so a managed project that happens to contain its own
    ``assent/`` directory would otherwise decide which Assent the child runs.
    Pointing the child at the parent of this file's package makes it import the
    very code the parent process is executing, whether that is the flat
    repository checkout or an installed site-packages copy.
    """
    return Path(__file__).resolve().parent.parent


def _start_plan(config_path: str, plan_name: str) -> subprocess.Popen:
    """Start an isolated child process equivalent to ``assent run <plan>``.

    stdin is a pipe rather than DEVNULL so the parent has a signal-independent
    stop channel: closing it (or dying) gives the child EOF, which
    ``assent.__main__`` turns into KeyboardInterrupt. Console signals do not
    survive a non-console pty such as tmux's, a pipe always does.

    The child runs from the package's own root rather than the project's, so
    the absolute ``--config`` path is what still locates the project.
    """
    command = [sys.executable, "-m", "assent", "run", plan_name]
    command.extend(("--config", str(Path(config_path).resolve())))
    child_env = dict(os.environ)
    child_env["ASSENT_STDIN_STOP"] = "1"
    # Marks the child as one plan's run rather than the human's invocation, so
    # its timing line is labeled apart from this parent's end-to-end total.
    child_env["ASSENT_PLAN_RUN"] = "1"
    kwargs = {
        "env": child_env,
        "cwd": str(_package_search_root()),
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "bufsize": 1,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(command, **kwargs)


def _read_plan_output(
        plan_name: str, stream: TextIO,
        output: queue.Queue[tuple[str, str]]) -> None:
    """Keep draining one child's pipe, forwarding complete lines to the
    parent thread."""
    try:
        for line in stream:
            output.put((plan_name, line))
    finally:
        stream.close()


def _start_output_reader(
        plan_name: str, process: subprocess.Popen,
        output: queue.Queue[tuple[str, str]]) -> threading.Thread | None:
    """Start a background reader thread for a child that has a stdout pipe."""
    stream = getattr(process, "stdout", None)
    if stream is None:
        # Test doubles and third-party callers may implement only a minimal
        # Popen interface.
        return None
    reader = threading.Thread(
        target=_read_plan_output,
        args=(plan_name, stream, output),
        name=f"assent-output-{plan_name}",
        daemon=True,
    )
    reader.start()
    return reader


def _write_plan_line(plan_name: str, line: str) -> None:
    """Write one child output line to the parent's terminal only, falling
    back to stdout when there is no dedicated channel."""
    content = line.removesuffix("\n").removesuffix("\r")
    stream = sys.stdout
    write = getattr(stream, "write_terminal_only", stream.write)
    write(f"[{plan_name}] {content}\n")
    stream.flush()


def _drain_output(output: queue.Queue[tuple[str, str]]) -> None:
    """Have the parent thread serialize whatever complete output lines have
    arrived so far."""
    while True:
        try:
            plan_name, line = output.get_nowait()
        except queue.Empty:
            return
        _write_plan_line(plan_name, line)


def _finish_plan_output(
        plan_name: str, readers: dict[str, threading.Thread],
        output: queue.Queue[tuple[str, str]],
        timeout: float | None = None) -> None:
    """Wait for the given pipe to hit EOF, then drain its final output before
    the completion summary.

    After a normal exit EOF is certain, so the join is unbounded. On the
    interrupt path a grandchild that outlived the tree termination could still
    hold the pipe open, so the caller passes a timeout: losing a few trailing
    lines beats hanging the run.
    """
    reader = readers.pop(plan_name, None)
    if reader is not None:
        reader.join(timeout)
    _drain_output(output)


def _plan_plans(assent_dir: Path, plan_names: list[str]) -> dict[str, Plan]:
    """Reparse every formal task file; any bad file refuses to continue
    scheduling."""
    return {plan_name: Plan.parse(assent_dir / plan_name) for plan_name in plan_names}


def _is_complete(plan: Plan) -> bool:
    return all(task.status in ("DONE", "SKIP") for task in plan.tasks)


def _has_ongoing(plan: Plan) -> bool:
    return any(task.status in ("TODO", "WIP") for task in plan.tasks)


def _blocking_chains(
        plan_name: str, graph, plans: dict[str, Plan],
        archived: set[str]) -> list[str]:
    """List the stuck chains that lead from a plan all the way to a
    BLOCKED task."""
    plan = plans[plan_name]
    chains = [
        f"{plan_name} -> {task.id}(BLOCKED)"
        for task in plan.tasks if task.status == "BLOCKED"
    ]
    for dependency in graph[plan_name].after:
        # An archived upstream is complete and is not a live graph node, so it
        # can neither block nor be recursed into.
        if is_upstream_complete(dependency, plans, archived):
            continue
        for chain in _blocking_chains(dependency, graph, plans, archived):
            chains.append(f"{plan_name} -> {chain}")
    if not chains:
        statuses = ", ".join(
            f"{task.id}={task.status}" for task in plan.tasks
            if task.status not in ("DONE", "SKIP"))
        chains.append(f"{plan_name} -> not yet complete ({statuses})")
    return chains


def _print_stuck(graph, plans: dict[str, Plan], archived: set[str]) -> None:
    """List every unfinished plan and why it cannot be unlocked."""
    print("Human decision required: no remaining plan is runnable "
          "because of a BLOCKED task:")
    for plan_name in graph:
        if _is_complete(plans[plan_name]):
            continue
        for chain in _blocking_chains(plan_name, graph, plans, archived):
            print(f"  - {chain}")


def _has_usable_git(root: Path) -> bool:
    """Distinguish a real repository from a test or damaged ``.git`` marker."""
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, check=False)
    return result.returncode == 0


def _plan_worktree_needs_recovery(config_path: str, plan_name: str) -> bool:
    """Return whether the plan child must gather a surviving dirty worktree."""
    cfg = load_config(config_path, plan_name)
    worktree = gitops.worktree_path(cfg.root, cfg.tasks_name)
    return (worktree.exists()
            and not gitops.working_tree_status(
                worktree, cfg.git_excludes).is_clean)


def _plan_workflow_needs_resume(config_path: str, plan_name: str) -> bool:
    """Return whether a completed task set still has plan steps to execute."""
    cfg = load_config(config_path, plan_name)
    return plan_workflow_needs_resume(
        cfg.tasks_dir, cfg.plan_workflow_step_count)


def _stack_launch_decision(config_path: str, plan_name: str) -> tuple[str | None, str | None]:
    """Return an auditable launch decision or a fail-closed refusal reason."""
    try:
        cfg = load_config(config_path, plan_name)
        base = resolve_plan_base(cfg.root, cfg.tasks_dir, excludes=cfg.git_excludes)
        candidate = gitops.worktree_path(cfg.root, cfg.tasks_name)
    except AssentError as e:
        return None, str(e)
    upstream = base.speculative_upstream
    reuse = "reuse" if candidate.exists() else "create"
    if upstream is None:
        return (f"Stack decision: {plan_name}: base target main {base.target_snapshot}; "
                f"no unaccepted upstream; worktree {reuse}."), None
    return (f"Stack decision: {plan_name}: base {base.resolved_base} from unaccepted "
            f"upstream {upstream.plan} @ {upstream.tip}; target main "
            f"{base.target_snapshot}; worktree {reuse}."), None


def _close_stop_channel(process: subprocess.Popen) -> None:
    """Close the child's stdin pipe, which is the primary stop request."""
    stream = getattr(process, "stdin", None)
    if stream is None:
        return
    stream.close()


def _send_interrupt(process: subprocess.Popen) -> None:
    """Ask this call's own child to stop, by both available routes.

    The stdin stop channel comes first because it is the one that works
    everywhere; the console/process-group signal stays as a second route for a
    child that is not reading stdin (an older child, or one blocked before the
    watcher started).
    """
    if process.poll() is not None:
        return
    _close_stop_channel(process)
    if os.name == "nt":
        process.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        os.killpg(process.pid, signal.SIGINT)


def _kill_tree(plan_name: str, process: subprocess.Popen, reason: str) -> None:
    """Forcibly terminate a child and every process it started.

    Forcing a kill is safe here and does not discard token-burned output:

    * the wip checkpoint is written before the quota countdown, so the work
      that exists at this point is already committed;
    * the run lock is an OS-level file lock, released by the kernel when the
      holder dies, so a killed child cannot leave a stale lock behind;
    * ``templates/format.md``'s "Unclean exit" section and the startup gate in
      ``engine.run`` already define a forcibly killed run as a recoverable
      state: the next run either gathers the surviving dirty worktree into a
      wip checkpoint or refuses, and never throws it away.

    The tree matters because the child owns an AI CLI grandchild; killing only
    the child would leave that grandchild orphaned.
    """
    if process.poll() is not None:
        return
    if os.name == "nt":
        print(f"Escalating plan: {plan_name} (step: taskkill /T /F on pid "
              f"{process.pid}; reason: {reason})")
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=False)
        return
    print(f"Escalating plan: {plan_name} (step: SIGTERM to process group "
          f"{process.pid}; reason: {reason})")
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    print(f"Escalating plan: {plan_name} (step: SIGKILL to process group "
          f"{process.pid}; reason: still alive {_TERMINATE_GRACE_SECONDS} "
          f"seconds after SIGTERM)")
    os.killpg(process.pid, signal.SIGKILL)


def _wait_or_escalate(plan_name: str, process: subprocess.Popen) -> int | None:
    """Wait out the child's own cleanup, escalating rather than hanging."""
    try:
        return process.wait(timeout=_INTERRUPT_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        _kill_tree(plan_name, process,
                   f"no exit within {_INTERRUPT_GRACE_SECONDS} seconds of the "
                   f"stop request")
    try:
        return process.wait(timeout=_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        return None


def _kill_all(active: dict[str, subprocess.Popen], reason: str) -> None:
    """Second Ctrl+C: stop asking politely and take the whole tree down."""
    for plan_name, process in active.items():
        try:
            _kill_tree(plan_name, process, reason)
        except OSError as e:
            print(f"Failed to force-terminate plan: {plan_name} ({e})")


def _reap_all(
        active: dict[str, subprocess.Popen],
        readers: dict[str, threading.Thread],
        output: queue.Queue[tuple[str, str]]) -> None:
    """Collect every force-terminated child so neither a zombie process nor a
    stop-channel pipe survives the run."""
    for plan_name, process in active.items():
        try:
            returncode = process.wait(timeout=_TERMINATE_GRACE_SECONDS)
            print(f"Plan force-terminated: {plan_name} "
                  f"(exit code {returncode})")
        except subprocess.TimeoutExpired:
            print(f"Plan force-terminated: {plan_name} (no exit code)")
        except OSError as e:
            print(f"Failed waiting for plan to finish: {plan_name} ({e})")
        with contextlib.suppress(OSError, ValueError):
            _close_stop_channel(process)
        _finish_plan_output(plan_name, readers, output, _TERMINATE_GRACE_SECONDS)


def _run_uninterruptible(action) -> None:
    """Repeat one forced-cleanup step until Ctrl+C stops preempting it.

    Every step is idempotent -- an already-dead child is skipped, a closed pipe
    stays closed -- so a repeat only finishes what the interrupt cut short. The
    attempt count is bounded because a key held down must not be able to hold
    the scheduler here forever; the last attempt's interrupt is dropped rather
    than allowed to escape, since escaping is exactly the traceback this path
    exists to prevent.
    """
    for _ in range(_FORCED_CLEANUP_ATTEMPTS):
        try:
            action()
            return
        except KeyboardInterrupt:
            continue


def _force_cleanup(
        active: dict[str, subprocess.Popen],
        readers: dict[str, threading.Thread],
        output: queue.Queue[tuple[str, str]], reason: str) -> None:
    """Take every remaining child down and collect it, whatever the keyboard
    does from here on."""
    _run_uninterruptible(lambda: print(
        "\nSecond interrupt (Ctrl+C): force-terminating every remaining "
        "plan now."))
    _run_uninterruptible(lambda: _kill_all(active, reason))
    _run_uninterruptible(lambda: _reap_all(active, readers, output))
    _run_uninterruptible(lambda: _drain_output(output))


def _interrupt_and_wait(
        active: dict[str, subprocess.Popen],
        readers: dict[str, threading.Thread],
        output: queue.Queue[tuple[str, str]]) -> None:
    """Forward the interrupt, then wait a bounded time for each child to save
    its own state and exit, escalating to a forced tree termination instead of
    waiting forever.

    The announcement is inside the guard together with the rest: it writes
    through the terminal-log sink, so a second Ctrl+C can land there just as
    easily as in the wait, and an escape at that point would leave the children
    running with nothing having been notified.

    ``active`` is emptied on the way out, so this is idempotent: every child in
    it has either exited or been through the forced tree termination, and a
    caller may call it again on any exit path without notifying anyone twice.
    """
    escalate: set[str] = set()
    try:
        print("\nInterrupt received (Ctrl+C): notifying running plans "
              "to clean up on their own...")
        for plan_name, process in active.items():
            try:
                _send_interrupt(process)
                print(f"Interrupting plan: {plan_name}")
            except (OSError, ValueError) as e:
                # Neither route reached the child, so waiting on its
                # cooperation would only waste the grace period.
                print(f"Failed to forward interrupt signal: {plan_name} ({e})")
                escalate.add(plan_name)
        for plan_name, process in active.items():
            try:
                if plan_name in escalate:
                    _kill_tree(plan_name, process,
                               "the stop request could not be delivered")
                returncode = _wait_or_escalate(plan_name, process)
                _finish_plan_output(plan_name, readers, output,
                                      _TERMINATE_GRACE_SECONDS)
                if returncode is None:
                    print(f"Plan force-terminated: {plan_name} "
                          f"(no exit code)")
                else:
                    print(f"Plan finished: {plan_name} "
                          f"(exit code {returncode})")
            except OSError as e:
                print(f"Failed waiting for plan to finish: {plan_name} ({e})")
        _drain_output(output)
    except KeyboardInterrupt:
        _force_cleanup(active, readers, output, "second user interrupt")
    finally:
        active.clear()


def run_all(config_path: str, assent_dir: str | Path, jobs: int = 1) -> int:
    """Run every unfinished plan in plan-dependency order."""
    assent_dir = Path(assent_dir)
    if not (assent_dir.parent / ".git").exists():
        print(_GIT_REQUIRED_MESSAGE)
        return 1
    # Repair declarative inputs before dependency or task parsing can reject an
    # older plan.  This pass is sequential, so concurrent plan workers never
    # race while repairing shared configuration or global contracts.
    from assent import engine
    for plan_name in list_task_plans(assent_dir):
        try:
            cfg = load_config(config_path, plan_name)
        except AssentError as error:
            print(f"Config error: {error}")
            return 1
        result = engine.run_preflight(cfg)
        if result != 0:
            return result
    active: dict[str, subprocess.Popen] = {}
    readers: dict[str, threading.Thread] = {}
    output: queue.Queue[tuple[str, str]] = queue.Queue()
    attempted: set[str] = set()
    failure = False
    try:
        while True:
            try:
                graph = parse_plan_dependency_graph(assent_dir)
                if not graph:
                    print("No plan with a task file found.")
                    return 1
                inactive = [plan_name for plan_name in graph if plan_name not in active]
                # Another child process may be writing its own task file;
                # only reparse plans that are not currently running.
                plans = _plan_plans(assent_dir, inactive)
                needs_recovery = {
                    plan_name: _plan_worktree_needs_recovery(
                        config_path, plan_name)
                    for plan_name in inactive
                }
                needs_plan_workflow = {
                    plan_name: (
                        _is_complete(plans[plan_name])
                        and _plan_workflow_needs_resume(
                            config_path, plan_name))
                    for plan_name in inactive
                }
                # An archived upstream is gone from the live plans but still
                # resolvable through the roster; read it once per iteration and
                # judge every after-name through the shared completion
                # predicate so an archived name resolves instead of raising a
                # KeyError, and an unresolved name fails closed here.
                archived = archived_plan_names(assent_dir)

                if (not active
                        and all(_is_complete(plan) for plan in plans.values())
                        and not any(needs_recovery.values())
                        and not any(needs_plan_workflow.values())):
                    print("All plans are complete (DONE/SKIP).")
                    return 0

                runnable = [
                    plan_name for plan_name, dependencies in graph.items()
                    if plan_name not in active
                    and plan_name not in attempted
                    and (_has_ongoing(plans[plan_name])
                         or needs_recovery[plan_name]
                         or needs_plan_workflow[plan_name])
                    and all(name not in active
                            and is_upstream_complete(name, plans, archived)
                            for name in dependencies.after)
                ]
            except AssentError as e:
                print(f"Plan scheduling failed: {e}")
                return 1
            while not failure and runnable and len(active) < jobs:
                plan_name = runnable.pop(0)
                if _has_usable_git(assent_dir.parent):
                    decision, refusal = _stack_launch_decision(config_path, plan_name)
                    if refusal is not None:
                        print(f"Plan refused: {plan_name} ({refusal})")
                        attempted.add(plan_name)
                        failure = True
                        break
                    print(decision)
                try:
                    process = _start_plan(config_path, plan_name)
                    active[plan_name] = process
                    reader = _start_output_reader(plan_name, process, output)
                    if reader is not None:
                        readers[plan_name] = reader
                except OSError as e:
                    print(f"Failed to start plan: {plan_name} ({e})")
                    failure = True
                    break
                print(f"Starting plan: {plan_name}")

            if not active:
                if failure:
                    return 1
                _print_stuck(graph, plans, archived)
                return 0

            completed: list[tuple[str, int]] = []
            while not completed:
                _drain_output(output)
                for plan_name, process in active.items():
                    returncode = process.poll()
                    if returncode is not None:
                        completed.append((plan_name, returncode))
                if not completed:
                    time.sleep(_POLL_SECONDS)

            interrupted = False
            for plan_name, returncode in completed:
                process = active.pop(plan_name)
                attempted.add(plan_name)
                # The stop-channel pipe outlives the exited child until the
                # Popen object is collected; release it as each plan ends so
                # a long whole-project run does not accumulate handles.
                with contextlib.suppress(OSError, ValueError):
                    _close_stop_channel(process)
                _finish_plan_output(plan_name, readers, output)
                if returncode == 0:
                    print(f"Plan complete: {plan_name} (exit code 0)")
                elif returncode in _INTERRUPT_RETURNCODES:
                    print(f"Plan interrupted: {plan_name} (exit code {returncode})")
                    interrupted = True
                else:
                    log_path = assent_dir / plan_name / "_assent.log"
                    print(f"Plan failed: {plan_name} (exit code {returncode}; "
                          f"see {log_path} for details)")
                    failure = True
            if interrupted:
                _interrupt_and_wait(active, readers, output)
                return 130
            if failure and not active:
                return 1
    except KeyboardInterrupt:
        _interrupt_and_wait(active, readers, output)
        return 130
    finally:
        # The parent owns every child it started, on every exit path -- a
        # refusal or a scheduling error must not return while children it
        # launched keep running and holding their plan locks.
        # _interrupt_and_wait empties `active`, so the interrupt paths above do
        # not repeat the notification here.
        if active:
            _interrupt_and_wait(active, readers, output)
