"""Folder-level scheduling for ``run --all``.

A single work folder is still handled by a spawned ``assent run <folder>``
child process; this module is only responsible for dependency unlocking, the
concurrency cap, per-line output, and interrupt forwarding.
"""
from __future__ import annotations

import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TextIO

from assent import AssentError
from assent.folderdeps import parse_folder_dependency_graph
from assent.plan import Plan

_POLL_SECONDS = 0.05
_GIT_REQUIRED_MESSAGE = "This project has no git repository yet; run git init first"
# 130 is the exit code a child produces after a normal KeyboardInterrupt
# cleanup; 3221225786 (0xC000013A, STATUS_CONTROL_C_EXIT) is the code the OS
# assigns when it terminates a child directly on Windows because no handler
# was installed. Both mean "interrupted", not "folder failed" -- treat them
# the same to avoid a false failure report.
_INTERRUPT_RETURNCODES = (130, 3221225786)


def _start_folder(config_path: str, folder: str) -> subprocess.Popen:
    """Start an isolated child process equivalent to ``assent run <folder>``."""
    command = [
        sys.executable, "-m", "assent", "run", folder,
        "--config", str(Path(config_path).resolve()),
    ]
    kwargs = {
        "stdin": subprocess.DEVNULL,
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
        output: queue.Queue[tuple[str, str]]) -> None:
    """Wait for the given pipe to hit EOF, then drain its final output before
    the completion summary."""
    reader = readers.pop(folder, None)
    if reader is not None:
        reader.join()
    _drain_output(output)


def _folder_plans(agents_dir: Path, folders: list[str]) -> dict[str, Plan]:
    """Reparse every formal task file; any bad file refuses to continue
    scheduling."""
    return {folder: Plan.parse(agents_dir / folder) for folder in folders}


def _is_complete(plan: Plan) -> bool:
    return all(task.status in ("DONE", "SKIP") for task in plan.tasks)


def _has_ongoing(plan: Plan) -> bool:
    return any(task.status in ("TODO", "WIP") for task in plan.tasks)


def _blocking_chains(folder: str, graph, plans: dict[str, Plan]) -> list[str]:
    """List the stuck chains that lead from a folder all the way to a
    BLOCKED task."""
    plan = plans[folder]
    chains = [
        f"{folder} -> {task.id}(BLOCKED)"
        for task in plan.tasks if task.status == "BLOCKED"
    ]
    for dependency in graph[folder].after:
        if _is_complete(plans[dependency]):
            continue
        for chain in _blocking_chains(dependency, graph, plans):
            chains.append(f"{folder} -> {chain}")
    if not chains:
        statuses = ", ".join(
            f"{task.id}={task.status}" for task in plan.tasks
            if task.status not in ("DONE", "SKIP"))
        chains.append(f"{folder} -> not yet complete ({statuses})")
    return chains


def _print_stuck(graph, plans: dict[str, Plan]) -> None:
    """List every unfinished folder and why it cannot be unlocked."""
    print("Cannot continue: the remaining work folders are all unlockable "
          "because of a BLOCKED task:")
    for folder in graph:
        if _is_complete(plans[folder]):
            continue
        for chain in _blocking_chains(folder, graph, plans):
            print(f"  - {chain}")


def _send_interrupt(process: subprocess.Popen) -> None:
    """Forward the user's interrupt only to this call's own child process group."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        process.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        os.killpg(process.pid, signal.SIGINT)


def _interrupt_and_wait(
        active: dict[str, subprocess.Popen],
        readers: dict[str, threading.Thread],
        output: queue.Queue[tuple[str, str]]) -> None:
    """Forward the interrupt, then wait for each child to save its own state
    and exit."""
    print("\nInterrupt received (Ctrl+C): notifying running work folders to "
          "clean up on their own...")
    for folder, process in active.items():
        try:
            _send_interrupt(process)
            print(f"Interrupting work folder: {folder}")
        except (OSError, ValueError) as e:
            print(f"Failed to forward interrupt signal: {folder} ({e})")
    for folder, process in active.items():
        try:
            returncode = process.wait()
            _finish_folder_output(folder, readers, output)
            print(f"Work folder finished: {folder} (exit code {returncode})")
        except OSError as e:
            print(f"Failed waiting for work folder to finish: {folder} ({e})")
    _drain_output(output)


def run_all(config_path: str, agents_dir: str | Path, jobs: int = 1) -> int:
    """Run every unfinished work folder in folder-dependency order."""
    agents_dir = Path(agents_dir)
    if not (agents_dir.parent / ".git").exists():
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
                graph = parse_folder_dependency_graph(agents_dir)
                if not graph:
                    print("No work folder with a task file found.")
                    return 1
                inactive = [folder for folder in graph if folder not in active]
                # Another child process may be writing its own task file;
                # only reparse folders that are not currently running.
                plans = _folder_plans(agents_dir, inactive)
            except AssentError as e:
                print(f"Folder scheduling failed: {e}")
                return 1

            if not active and all(_is_complete(plan) for plan in plans.values()):
                print("All work folders are complete (DONE/SKIP).")
                return 0

            runnable = [
                folder for folder, dependencies in graph.items()
                if folder not in active
                and folder not in attempted
                and _has_ongoing(plans[folder])
                and all(name not in active and _is_complete(plans[name])
                        for name in dependencies.after)
            ]
            while not failure and runnable and len(active) < jobs:
                folder = runnable.pop(0)
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
                _print_stuck(graph, plans)
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
                del active[folder]
                attempted.add(folder)
                _finish_folder_output(folder, readers, output)
                if returncode == 0:
                    print(f"Work folder complete: {folder} (exit code 0)")
                elif returncode in _INTERRUPT_RETURNCODES:
                    print(f"Work folder interrupted: {folder} (exit code {returncode})")
                    interrupted = True
                else:
                    log_path = agents_dir / folder / "_agents.log"
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
