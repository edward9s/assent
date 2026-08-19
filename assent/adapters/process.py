"""Neutral subprocess execution for adapters: streaming collection, watchdog, and stop.

Every vendor CLI adapter runs its binary the same way -- merge stderr into stdout, read
lines from a reader thread so a stalled child can be killed, echo each line for live
display, and reap the child on interrupt.  That mechanism is not vendor knowledge, so it
lives here rather than inside one vendor's module; a vendor adapter importing another
vendor adapter just to reuse it made the two impossible to change independently
(2026-07-27: moved out of ``assent.adapters.claude``, behaviour unchanged).

It also owns the stop-wake mechanism every scheduler-owned blocking wait shares
(2026-07-28); see the section below for why it lives here.
"""
from __future__ import annotations

import queue
import subprocess
import threading
import time
from pathlib import Path

_SENTINEL = object()
# Pushed into a waiting queue only to end a blocking get(); never collected as output.
_WAKE = object()


# --------------------------------------------------------------------------- #
# Stop wake
# --------------------------------------------------------------------------- #
# ``run --all`` stops a plan child by closing its stdin, and the child's
# watcher turns that EOF into ``_thread.interrupt_main()``.  That call only marks
# a KeyboardInterrupt *pending*: it is delivered when the main thread next runs
# bytecode.  A main thread parked in ``time.sleep`` (the quota countdown) or in a
# blocking ``queue.get`` (the output collection below) therefore keeps waiting --
# for a whole countdown segment, for the watchdog duration, or forever when the
# watchdog is disabled.  All that time the child legitimately still owns the
# plan lock, so the next ``run --all`` is refused even though the command
# looked finished.
#
# The fix is one wake mechanism shared by every such wait: an Event the sleeps
# wait on, plus a sentinel pushed into each registered output queue.  Waking is
# all it does -- the pending KeyboardInterrupt is still what stops the run; this
# only gives it a bytecode boundary to land on.  Nothing polls, so the watchdog's
# elapsed-time semantics are untouched.
#
# It lives in this module because it is the one place ``assent.engine`` and every
# vendor adapter already depend on, and nothing in ``assent`` imports back into
# it.
_wake = threading.Event()
_wake_lock = threading.Lock()
_wake_queues: list["queue.Queue"] = []


def wake_stop_waiters() -> None:
    """Release every scheduler-owned blocking wait in this process."""
    with _wake_lock:
        _wake.set()
        pending = list(_wake_queues)
    for waiting in pending:
        waiting.put(_WAKE)


def clear_stop_wake() -> None:
    """Forget a previous stop request so a later, unrelated wait still waits.

    ``run`` and ``run_subprocess`` are in-process library and test entry points
    as well as scheduler steps; without this, one stop request would make every
    later countdown and adapter session return immediately.
    """
    with _wake_lock:
        _wake.clear()


def stop_wake_requested() -> bool:
    return _wake.is_set()


def interruptible_sleep(seconds: float) -> None:
    """``time.sleep`` that also returns as soon as a stop has been requested."""
    _wake.wait(seconds)


def _register_wake_queue(waiting: "queue.Queue") -> None:
    """Let wake_stop_waiters() unblock this queue until it is unregistered."""
    with _wake_lock:
        _wake_queues.append(waiting)
        requested = _wake.is_set()
    if requested:   # requested between clear_stop_wake() and this registration
        waiting.put(_WAKE)


def _unregister_wake_queue(waiting: "queue.Queue") -> None:
    with _wake_lock:
        if waiting in _wake_queues:
            _wake_queues.remove(waiting)


def run_subprocess(command: list[str], cwd: Path, stall_seconds: float,
                   echo=None, heartbeat_path: Path | None = None,
                   input_text: str | None = None) -> tuple[int, str, bool]:
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
    input_text, when supplied, is encoded as UTF-8 and delivered through a pipe on a
    separate writer thread.  The raw pipe is used for input so newline bytes are not
    rewritten by the platform text layer.  stderr is merged into stdout so quota/error
    messages are never missed.
    """
    clear_stop_wake()   # this session's waits are not stopped by an earlier run's request
    popen_options = dict(
        cwd=str(cwd),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1)
    if input_text is not None:
        popen_options["stdin"] = subprocess.PIPE
    proc = subprocess.Popen(command, **popen_options)

    q: "queue.Queue" = queue.Queue()

    input_thread = None

    def _close_input() -> None:
        stream = proc.stdin
        if stream is not None:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def _write_input(stream, text: str) -> None:
        try:
            data = text.encode("utf-8")
            # Popen's text-mode stdin is a TextIOWrapper.  Writing through its
            # binary buffer keeps the UTF-8 bytes, including newlines, exact on
            # Windows as well as POSIX.  The fallback keeps the helper usable with
            # simple file-like test doubles.
            raw = getattr(stream, "buffer", None)
            if raw is None:
                stream.write(text)
                stream.flush()
            else:
                view = memoryview(data)
                while view:
                    written = raw.write(view)
                    if written is None or written <= 0:
                        raise OSError("stdin pipe did not accept input")
                    view = view[written:]
                raw.flush()
        except (BrokenPipeError, OSError, UnicodeError, ValueError):
            # A child is allowed to exit or close stdin before the full prompt is
            # delivered.  Its exit status and collected output remain authoritative.
            pass
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    if input_text is not None and proc.stdin is not None:
        input_thread = threading.Thread(
            target=_write_input, args=(proc.stdin, input_text),
            name="assent-subprocess-stdin", daemon=True)
        input_thread.start()

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
    _register_wake_queue(q)
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
                    try:
                        proc.kill()
                    except OSError:
                        pass
                    _close_input()
                    break
                if item is _WAKE:
                    # A stop was requested.  The pending KeyboardInterrupt has
                    # normally already been delivered on the way to this line;
                    # raising covers a caller that woke us without one.
                    raise KeyboardInterrupt
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
                    if item is not _SENTINEL and item is not _WAKE:
                        lines.append(item)
        except KeyboardInterrupt:
            # Never rely solely on the console's own signal propagation to the child: kill it
            # here too, so an interrupt always leaves no orphaned process behind, and reap it
            # so it is not left as a zombie once this function returns.
            try:
                proc.kill()
            except OSError:
                pass
            _close_input()
            proc.wait()
            raise
    finally:
        _unregister_wake_queue(q)
        _close_input()
        if input_thread is not None:
            # Closing the parent's pipe end above releases a writer blocked behind
            # a child that stopped consuming input.  Never let cleanup wait forever.
            input_thread.join(timeout=1)
        if proc.stdout is not None:
            proc.stdout.close()

    return proc.returncode, "".join(lines), stalled
