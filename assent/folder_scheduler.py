"""Folder-level scheduling for ``run --all``.

A single work folder is still handled by a spawned ``assent run <folder>``
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
from assent.config import load_config
from assent.folderdeps import (archived_folder_names, is_upstream_complete,
                               parse_folder_dependency_graph,
                               resolve_folder_base)
from assent.plan import Plan

_POLL_SECONDS = 0.05
_GIT_REQUIRED_MESSAGE = "This project has no git repository yet; run git init first"
# 130 is the exit code a child produces after a normal KeyboardInterrupt
# cleanup; 3221225786 (0xC000013A, STATUS_CONTROL_C_EXIT) is the code the OS
# assigns when it terminates a child directly on Windows because no handler
# was installed. Both mean "interrupted", not "folder failed" -- treat them
# the same to avoid a false failure report.
_INTERRUPT_RETURNCODES = (130, 3221225786)
# How long a child may take to finish its own interrupt cleanup (wip
# checkpoint, r-file entry) before the parent escalates to a forced tree
# termination, and how long a POSIX process group gets between SIGTERM and
# SIGKILL. The parent never waits without a timeout: an unreachable child must
# not be able to hang the whole run.
_INTERRUPT_GRACE_SECONDS = 60
_TERMINATE_GRACE_SECONDS = 10


def _start_folder(config_path: str, folder: str) -> subprocess.Popen:
    """Start an isolated child process equivalent to ``assent run <folder>``.

    stdin is a pipe rather than DEVNULL so the parent has a signal-independent
    stop channel: closing it (or dying) gives the child EOF, which
    ``assent.__main__`` turns into KeyboardInterrupt. Console signals do not
    survive a non-console pty such as tmux's, a pipe always does.
    """
    command = [
        sys.executable, "-m", "assent", "run", folder,
        "--config", str(Path(config_path).resolve()),
    ]
    child_env = dict(os.environ)
    child_env["ASSENT_STDIN_STOP"] = "1"
    kwargs = {
        "env": child_env,
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


def _read_folder_output(
        folder: str, stream: TextIO,
        output: queue.Queue[tuple[str, str]]) -> None:
    """Keep draining one child's pipe, forwarding complete lines to the
    parent thread."""
    try:
        for line in stream:
            output.put((folder, line))
    finally:
        stream.close()


def _start_output_reader(
        folder: str, process: subprocess.Popen,
        output: queue.Queue[tuple[str, str]]) -> threading.Thread | None:
    """Start a background reader thread for a child that has a stdout pipe."""
    stream = getattr(process, "stdout", None)
    if stream is None:
        # Test doubles and third-party callers may implement only a minimal
        # Popen interface.
        return None
    reader = threading.Thread(
        target=_read_folder_output,
        args=(folder, stream, output),
        name=f"assent-output-{folder}",
        daemon=True,
    )
    reader.start()
    return reader


def _write_folder_line(folder: str, line: str) -> None:
    """Write one child output line to the parent's terminal only, falling
    back to stdout when there is no dedicated channel."""
    content = line.removesuffix("\n").removesuffix("\r")
    stream = sys.stdout
    write = getattr(stream, "write_terminal_only", stream.write)
    write(f"[{folder}] {content}\n")
    stream.flush()


def _drain_output(output: queue.Queue[tuple[str, str]]) -> None:
    """Have the parent thread serialize whatever complete output lines have
    arrived so far."""
    while True:
        try:
            folder, line = output.get_nowait()
        except queue.Empty:
            return
        _write_folder_line(folder, line)


def _finish_folder_output(
        folder: str, readers: dict[str, threading.Thread],
        output: queue.Queue[tuple[str, str]],
        timeout: float | None = None) -> None:
    """Wait for the given pipe to hit EOF, then drain its final output before
    the completion summary.

    After a normal exit EOF is certain, so the join is unbounded. On the
    interrupt path a grandchild that outlived the tree termination could still
    hold the pipe open, so the caller passes a timeout: losing a few trailing
    lines beats hanging the run.
    """
    reader = readers.pop(folder, None)
    if reader is not None:
        reader.join(timeout)
    _drain_output(output)


def _folder_plans(assent_dir: Path, folders: list[str]) -> dict[str, Plan]:
    """Reparse every formal task file; any bad file refuses to continue
    scheduling."""
    return {folder: Plan.parse(assent_dir / folder) for folder in folders}


def _is_complete(plan: Plan) -> bool:
    return all(task.status in ("DONE", "SKIP") for task in plan.tasks)


def _has_ongoing(plan: Plan) -> bool:
    return any(task.status in ("TODO", "WIP") for task in plan.tasks)


def _blocking_chains(
        folder: str, graph, plans: dict[str, Plan],
        archived: set[str]) -> list[str]:
    """List the stuck chains that lead from a folder all the way to a
    BLOCKED task."""
    plan = plans[folder]
    chains = [
        f"{folder} -> {task.id}(BLOCKED)"
        for task in plan.tasks if task.status == "BLOCKED"
    ]
    for dependency in graph[folder].after:
        # An archived upstream is complete and is not a live graph node, so it
        # can neither block nor be recursed into.
        if is_upstream_complete(dependency, plans, archived):
            continue
        for chain in _blocking_chains(dependency, graph, plans, archived):
            chains.append(f"{folder} -> {chain}")
    if not chains:
        statuses = ", ".join(
            f"{task.id}={task.status}" for task in plan.tasks
            if task.status not in ("DONE", "SKIP"))
        chains.append(f"{folder} -> not yet complete ({statuses})")
    return chains


def _print_stuck(graph, plans: dict[str, Plan], archived: set[str]) -> None:
    """List every unfinished folder and why it cannot be unlocked."""
    print("Cannot continue: the remaining work folders are all unlockable "
          "because of a BLOCKED task:")
    for folder in graph:
        if _is_complete(plans[folder]):
            continue
        for chain in _blocking_chains(folder, graph, plans, archived):
            print(f"  - {chain}")


def _has_usable_git(root: Path) -> bool:
    """Distinguish a real repository from a test or damaged ``.git`` marker."""
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, check=False)
    return result.returncode == 0


def _stack_launch_decision(config_path: str, folder: str) -> tuple[str | None, str | None]:
    """Return an auditable launch decision or a fail-closed refusal reason."""
    try:
        cfg = load_config(config_path, folder)
        base = resolve_folder_base(cfg.root, cfg.tasks_dir, excludes=cfg.git_excludes)
        candidate = gitops.worktree_path(cfg.root, cfg.tasks_name)
    except AssentError as e:
        return None, str(e)
    upstream = base.speculative_upstream
    reuse = "reuse" if candidate.exists() else "create"
    if upstream is None:
        return (f"Stack decision: {folder}: base target main {base.target_snapshot}; "
                f"no unaccepted upstream; worktree {reuse}."), None
    return (f"Stack decision: {folder}: base {base.resolved_base} from unaccepted "
            f"upstream {upstream.folder} @ {upstream.tip}; target main "
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


def _kill_tree(folder: str, process: subprocess.Popen, reason: str) -> None:
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
        print(f"Escalating work folder: {folder} (step: taskkill /T /F on pid "
              f"{process.pid}; reason: {reason})")
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=False)
        return
    print(f"Escalating work folder: {folder} (step: SIGTERM to process group "
          f"{process.pid}; reason: {reason})")
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    print(f"Escalating work folder: {folder} (step: SIGKILL to process group "
          f"{process.pid}; reason: still alive {_TERMINATE_GRACE_SECONDS} "
          f"seconds after SIGTERM)")
    os.killpg(process.pid, signal.SIGKILL)


def _wait_or_escalate(folder: str, process: subprocess.Popen) -> int | None:
    """Wait out the child's own cleanup, escalating rather than hanging."""
    try:
        return process.wait(timeout=_INTERRUPT_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        _kill_tree(folder, process,
                   f"no exit within {_INTERRUPT_GRACE_SECONDS} seconds of the "
                   f"stop request")
    try:
        return process.wait(timeout=_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        return None


def _kill_all(active: dict[str, subprocess.Popen], reason: str) -> None:
    """Second Ctrl+C: stop asking politely and take the whole tree down."""
    for folder, process in active.items():
        try:
            _kill_tree(folder, process, reason)
        except OSError as e:
            print(f"Failed to force-terminate work folder: {folder} ({e})")


def _interrupt_and_wait(
        active: dict[str, subprocess.Popen],
        readers: dict[str, threading.Thread],
        output: queue.Queue[tuple[str, str]]) -> None:
    """Forward the interrupt, then wait a bounded time for each child to save
    its own state and exit, escalating to a forced tree termination instead of
    waiting forever."""
    print("\nInterrupt received (Ctrl+C): notifying running work folders to "
          "clean up on their own...")
    escalate: set[str] = set()
    try:
        for folder, process in active.items():
            try:
                _send_interrupt(process)
                print(f"Interrupting work folder: {folder}")
            except (OSError, ValueError) as e:
                # Neither route reached the child, so waiting on its
                # cooperation would only waste the grace period.
                print(f"Failed to forward interrupt signal: {folder} ({e})")
                escalate.add(folder)
        for folder, process in active.items():
            try:
                if folder in escalate:
                    _kill_tree(folder, process,
                               "the stop request could not be delivered")
                returncode = _wait_or_escalate(folder, process)
                _finish_folder_output(folder, readers, output,
                                      _TERMINATE_GRACE_SECONDS)
                if returncode is None:
                    print(f"Work folder force-terminated: {folder} "
                          f"(no exit code)")
                else:
                    print(f"Work folder finished: {folder} "
                          f"(exit code {returncode})")
            except OSError as e:
                print(f"Failed waiting for work folder to finish: {folder} ({e})")
    except KeyboardInterrupt:
        print("\nSecond interrupt (Ctrl+C): force-terminating every remaining "
              "work folder now.")
        _kill_all(active, "second user interrupt")
    _drain_output(output)


def run_all(config_path: str, assent_dir: str | Path, jobs: int = 1) -> int:
    """Run every unfinished work folder in folder-dependency order."""
    assent_dir = Path(assent_dir)
    if not (assent_dir.parent / ".git").exists():
        print(_GIT_REQUIRED_MESSAGE)
        return 1
    active: dict[str, subprocess.Popen] = {}
    readers: dict[str, threading.Thread] = {}
    output: queue.Queue[tuple[str, str]] = queue.Queue()
    attempted: set[str] = set()
    failure = False
    try:
        while True:
            try:
                graph = parse_folder_dependency_graph(assent_dir)
                if not graph:
                    print("No work folder with a task file found.")
                    return 1
                inactive = [folder for folder in graph if folder not in active]
                # Another child process may be writing its own task file;
                # only reparse folders that are not currently running.
                plans = _folder_plans(assent_dir, inactive)
                # An archived upstream is gone from the live plans but still
                # resolvable through the roster; read it once per iteration and
                # judge every after-name through the shared completion
                # predicate so an archived name resolves instead of raising a
                # KeyError, and an unresolved name fails closed here.
                archived = archived_folder_names(assent_dir)

                if not active and all(
                        _is_complete(plan) for plan in plans.values()):
                    print("All work folders are complete (DONE/SKIP).")
                    return 0

                runnable = [
                    folder for folder, dependencies in graph.items()
                    if folder not in active
                    and folder not in attempted
                    and _has_ongoing(plans[folder])
                    and all(name not in active
                            and is_upstream_complete(name, plans, archived)
                            for name in dependencies.after)
                ]
            except AssentError as e:
                print(f"Folder scheduling failed: {e}")
                return 1
            while not failure and runnable and len(active) < jobs:
                folder = runnable.pop(0)
                if _has_usable_git(assent_dir.parent):
                    decision, refusal = _stack_launch_decision(config_path, folder)
                    if refusal is not None:
                        print(f"Work folder refused: {folder} ({refusal})")
                        attempted.add(folder)
                        failure = True
                        break
                    print(decision)
                try:
                    process = _start_folder(config_path, folder)
                    active[folder] = process
                    reader = _start_output_reader(folder, process, output)
                    if reader is not None:
                        readers[folder] = reader
                except OSError as e:
                    print(f"Failed to start work folder: {folder} ({e})")
                    failure = True
                    break
                print(f"Starting work folder: {folder}")

            if not active:
                if failure:
                    return 1
                _print_stuck(graph, plans, archived)
                return 1

            completed: list[tuple[str, int]] = []
            while not completed:
                _drain_output(output)
                for folder, process in active.items():
                    returncode = process.poll()
                    if returncode is not None:
                        completed.append((folder, returncode))
                if not completed:
                    time.sleep(_POLL_SECONDS)

            interrupted = False
            for folder, returncode in completed:
                process = active.pop(folder)
                attempted.add(folder)
                # The stop-channel pipe outlives the exited child until the
                # Popen object is collected; release it as each folder ends so
                # a long --all run does not accumulate handles.
                with contextlib.suppress(OSError, ValueError):
                    _close_stop_channel(process)
                _finish_folder_output(folder, readers, output)
                if returncode == 0:
                    print(f"Work folder complete: {folder} (exit code 0)")
                elif returncode in _INTERRUPT_RETURNCODES:
                    print(f"Work folder interrupted: {folder} (exit code {returncode})")
                    interrupted = True
                else:
                    log_path = assent_dir / folder / "_assent.log"
                    print(f"Work folder failed: {folder} (exit code {returncode}; "
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
