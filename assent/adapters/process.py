"""Neutral subprocess execution for adapters: streaming collection, watchdog, and stop.

Every vendor CLI adapter runs its binary the same way -- merge stderr into stdout, read
lines from a reader thread so a stalled child can be killed, echo each line for live
display, and reap the child on interrupt.  That mechanism is not vendor knowledge, so it
lives here rather than inside one vendor's module; a vendor adapter importing another
vendor adapter just to reuse it made the two impossible to change independently
(2026-07-27: moved out of ``assent.adapters.claude``, behaviour unchanged).
"""
from __future__ import annotations

import queue
import subprocess
import threading
import time
from pathlib import Path

_SENTINEL = object()


def run_subprocess(command: list[str], cwd: Path, stall_seconds: float,
                   echo=None, heartbeat_path: Path | None = None) -> tuple[int, str, bool]:
    """Run the subprocess, collecting output line by line; a reader thread + queue implements
    the watchdog (the standard approach from 2.4).

    stall_seconds <= 0 -> watchdog disabled (blocking read to EOF).
    echo: callback invoked for each line received (for live display); its own failures never
    affect collection or the verdict.
    heartbeat_path: an optional file whose mtime counts as activity in addition to a stdout
    line arriving.  A CLI that only prints at the end of a print-mode session (never mid-run)
    would otherwise be killed as stalled well before it finishes; this lets a caller that keeps
    its own log file (never read here, only stat'd) prove it is still alive.  None reproduces
    the exact single-timeout-then-kill behaviour used before this parameter existed.
    Returns (returncode, full output text, stalled). stalled=True means it was killed on timeout.
    stderr is merged into stdout so quota/error messages are never missed.
    """
    proc = subprocess.Popen(
        command, cwd=str(cwd),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1)

    q: "queue.Queue" = queue.Queue()

    def _reader(stream) -> None:
        try:
            for line in stream:
                q.put(line)
        finally:
            q.put(_SENTINEL)

    thread = threading.Thread(target=_reader, args=(proc.stdout,), daemon=True)
    thread.start()

    lines: list[str] = []
    stalled = False
    last_activity = time.time()
    try:
        try:
            while True:
                try:
                    if stall_seconds and stall_seconds > 0:
                        item = q.get(timeout=stall_seconds)
                    else:
                        item = q.get()
                except queue.Empty:
                    if heartbeat_path is not None:
                        try:
                            mtime = heartbeat_path.stat().st_mtime
                        except OSError:
                            mtime = None
                        if mtime is not None and mtime > last_activity:
                            last_activity = mtime
                            continue    # the log file proves the process is still alive
                    stalled = True
                    proc.kill()
                    break
                if item is _SENTINEL:
                    break
                lines.append(item)
                last_activity = time.time()
                if echo is not None:
                    try:
                        echo(item)
                    except Exception:   # Display-layer failures must never affect output
                        pass            # collection or quota detection

            proc.wait()
            if stalled:  # Best-effort drain of whatever is still queued (don't join the
                         # daemon thread, to avoid hanging)
                while True:
                    try:
                        item = q.get_nowait()
                    except queue.Empty:
                        break
                    if item is not _SENTINEL:
                        lines.append(item)
        except KeyboardInterrupt:
            # Never rely solely on the console's own signal propagation to the child: kill it
            # here too, so an interrupt always leaves no orphaned process behind, and reap it
            # so it is not left as a zombie once this function returns.
            proc.kill()
            proc.wait()
            raise
    finally:
        if proc.stdout is not None:
            proc.stdout.close()

    return proc.returncode, "".join(lines), stalled
