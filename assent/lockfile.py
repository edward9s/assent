"""Task-folder file lock: only one assent run may operate on a given task folder at a time.

When a run starts, it acquires an OS-level "non-blocking exclusive lock" on the task folder
(msvcrt on Windows, fcntl on POSIX), held for the process's lifetime. The lock's lifetime is
tied to the file handle: however the process terminates (crash / kill / Ctrl+C), the OS
releases it automatically — so there is no stale lock, no PID-reuse problem, and no manual
cleanup needed. This is exactly why a "PID lock file + liveness check" scheme was not used.

The lock file is <tasks_dir>/assent.lock; it stays on disk and is never deleted (deleting it
would introduce a race). Its contents are only PID, start time, and folder name for
diagnostics — never used to decide anything.

Limitation: flock / msvcrt.locking semantics are unreliable on network filesystems (some NFS
and SMB implementations do not guarantee cross-host mutual exclusion); this lock only
guarantees mutual exclusion on the local filesystem.
"""
from __future__ import annotations

import contextlib
import os
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from assent import AssentError

LOCK_NAME = "assent.lock"

# Windows msvcrt.locking is a mandatory lock: it blocks even a "read" of that region by
# others. The lock is placed at a high byte offset far from the content, and content is
# never written there, so the losing side can still read the header's diagnostic content
# (Windows allows locking a region past EOF without growing the file). POSIX flock is
# advisory and locks the whole open file description, so offset is meaningless to it —
# reusing the same offset is harmless.
_LOCK_OFFSET = 1 << 30  # 1 GiB
_LOCK_BYTES = 1


if sys.platform == "win32":
    import msvcrt

    def _try_lock(handle) -> bool:
        handle.seek(_LOCK_OFFSET)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, _LOCK_BYTES)
        except OSError:
            return False
        return True

    def _unlock(handle) -> None:
        handle.seek(_LOCK_OFFSET)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, _LOCK_BYTES)
        except OSError:
            pass
else:
    import fcntl

    def _try_lock(handle) -> bool:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(handle) -> None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


class LockBusy(AssentError):
    """The task folder is already held by another run; the message includes the holder's PID and folder name."""


class LockMissing(AssentError):
    """The lock file does not exist; a caller that must not create it cannot safely acquire the same lock."""


def _write_diag(handle, tasks_name: str) -> None:
    """Write PID, start time (ISO 8601), and folder name into the lock file (rewritten by truncation, diagnostics only)."""
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    body = (
        f"pid = {os.getpid()}\n"
        f'started_at = "{started}"\n'
        f'folder = "{tasks_name}"\n'
    )
    handle.seek(0)
    handle.truncate()
    handle.write(body.encode("utf-8"))
    handle.flush()


def _read_diag(path: Path) -> dict:
    """Read the lock file's diagnostic content; unreadable or malformed content returns an empty dict (diagnostics never affect the mutual-exclusion decision)."""
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _short_time(started_at) -> str:
    """Take HH:MM from an ISO 8601 time string for message use; returns an empty string on failure."""
    if not isinstance(started_at, str):
        return ""
    try:
        return datetime.fromisoformat(started_at).strftime("%H:%M")
    except ValueError:
        return ""


def _busy_message(tasks_name: str, diag: dict) -> str:
    pid = diag.get("pid")
    detail = ""
    if isinstance(pid, int):
        detail = f"(PID {pid}"
        hhmm = _short_time(diag.get("started_at"))
        if hhmm:
            detail += f", started at {hhmm}"
        detail += ")"
    return (f"Another assent run is already processing task folder {tasks_name}{detail}. "
            "Only one run may operate on a task folder at a time.")


@contextlib.contextmanager
def hold_lock(tasks_dir: Path, tasks_name: str) -> Iterator[None]:
    """Acquire an OS-level non-blocking exclusive lock on <tasks_dir>/assent.lock, held until the with block exits.

    If the lock cannot be acquired: read the lock file's diagnostic content and raise
    LockBusy (message includes the holder's PID and folder name); the caller fails with
    exit code 1 based on this, without touching anything in the working tree. On success,
    the diagnostic content is written back into the lock file.
    """
    tasks_dir = Path(tasks_dir)
    tasks_dir.mkdir(parents=True, exist_ok=True)
    path = tasks_dir / LOCK_NAME
    # O_CREAT but not O_TRUNC: create the file if missing without truncating existing content
    # (truncating would destroy the holder's diagnostics). Binary mode: locking needs to seek
    # to a large offset like _LOCK_OFFSET, and text streams only accept the opaque cookie
    # returned by tell(), not an arbitrary offset; O_BINARY also avoids Windows newline
    # translation.
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
    handle = os.fdopen(os.open(str(path), flags, 0o644), "r+b")
    try:
        if not _try_lock(handle):
            raise LockBusy(_busy_message(tasks_name, _read_diag(path)))
        _write_diag(handle, tasks_name)
        try:
            yield
        finally:
            _unlock(handle)
    finally:
        handle.close()


@contextlib.contextmanager
def probe_lock(tasks_dir: Path, tasks_name: str) -> Iterator[None]:
    """Acquire an existing task-folder lock without ever creating or rewriting ``assent.lock``.

    ``clean`` must use the same lock as ``run``, yet must never touch ``.assent/`` plan
    archival. If the lock file did not exist and were created then deleted, a race would open
    between unlocking and deleting; so this refuses conservatively instead, and the caller
    skips cleanup. A folder that has run ``run`` normally already has a lock file.
    """
    path = Path(tasks_dir) / LOCK_NAME
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(str(path), flags)
    except FileNotFoundError as e:
        raise LockMissing(
            f"Task folder {tasks_name} has no existing {LOCK_NAME}; "
            "cannot prove it is unlocked without modifying .assent") from e
    except OSError as e:
        raise AssentError(f"Unable to open lock file for task folder {tasks_name}: {e}") from e

    handle = os.fdopen(descriptor, "r+b")
    try:
        if not _try_lock(handle):
            raise LockBusy(_busy_message(tasks_name, _read_diag(path)))
        try:
            yield
        finally:
            _unlock(handle)
    finally:
        handle.close()
