"""Mirror ``assent run``'s terminal output into the current task folder.

The terminal keeps native output (colors and cursor repositioning included), while the
task folder's ``_assent.log`` keeps portable, immediately-flushed plain text. Errors can
happen before config is even loaded, so this module reads config itself on a best-effort
basis, without depending on config.py and without raising outward.
"""
from __future__ import annotations

import re
import sys
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, TextIO

from assent.config import list_task_folders
from assent.plan import Plan

_ANSI_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[@-_])")
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0000FE00-\U0000FE0F"
    "\U0001F1E6-\U0001F1FF"
    "‍"
    "]",
)

# The project locator, not a file that has to exist: a run's log belongs to the
# project's own .assent directory even when every setting comes from the user-wide
# ~/.assent/assent.toml, so the user home is never consulted here.
_DEFAULT_CONFIG = ".assent/assent.toml"
_FOLDER_RE = re.compile(r"^[^\s/\\]+$")


def sanitize_log_text(text: str) -> str:
    """Remove terminal-only bytes and emoji, retaining readable plain text."""
    text = _ANSI_RE.sub("", text)
    text = _EMOJI_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    return "".join(ch for ch in text if ch == "\n" or ord(ch) >= 32 and ord(ch) != 127)


class _LogSink:
    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self.lock = threading.Lock()

    def write(self, text: str) -> None:
        clean = sanitize_log_text(text)
        if not clean:
            return
        with self.lock:
            self.stream.write(clean)
            self.stream.flush()


class TeeTextIO:
    """A small TextIO-compatible proxy used for both stdout and stderr."""

    def __init__(self, terminal: TextIO, sink: _LogSink) -> None:
        self.terminal = terminal
        self.sink = sink

    def write(self, text: str) -> int:
        written = self.terminal.write(text)
        self.terminal.flush()
        self.sink.write(text)
        return written

    def write_terminal_only(self, text: str) -> int:
        """Write transient UI (such as a countdown) without polluting the log."""
        written = self.terminal.write(text)
        self.terminal.flush()
        return written

    def flush(self) -> None:
        self.terminal.flush()
        self.sink.stream.flush()

    def isatty(self) -> bool:
        return bool(getattr(self.terminal, "isatty", lambda: False)())

    def fileno(self) -> int:
        return self.terminal.fileno()

    @property
    def encoding(self):
        return getattr(self.terminal, "encoding", None)

    @property
    def errors(self):
        return getattr(self.terminal, "errors", None)


def _valid_folder(value: object) -> str | None:
    """Return the value if it is safe to use as a task folder name, otherwise None."""
    if (not isinstance(value, str) or not value
            or not _FOLDER_RE.match(value) or value[0] in "-."):
        return None
    return value


def _folder_from_tasks(assent_dir: Path) -> str | None:
    """Best-effort derive the single ongoing task folder; any error is treated as unknown."""
    try:
        ongoing = []
        for folder in list_task_folders(assent_dir):
            plan = Plan.parse(assent_dir / folder)
            if any(task.status in ("TODO", "WIP") for task in plan.tasks):
                ongoing.append(folder)
        return ongoing[0] if len(ongoing) == 1 else None
    # Logging runs before real config is loaded; even unexpected bad files (e.g. encoding
    # errors) must not block the original command.
    except Exception:
        return None


def _folder_from_argv(argv: list[str]) -> str | None:
    """Find a run/verify folder argument, skipping known options and values."""
    if not argv or argv[0] not in ("run", "verify"):
        return None
    if "--all" in argv:
        return None
    idx = 1
    while idx < len(argv):
        arg = argv[idx]
        if arg in ("--config", "--task", "--jobs"):
            idx += 2
            continue
        if arg == "--once" or arg.startswith(("--config=", "--task=")):
            idx += 1
            continue
        if not arg.startswith("-"):
            return _valid_folder(arg)
        idx += 1
    return None


def _config_path_for_argv(argv: list[str]) -> Path:
    """Resolve the located management file without loading or validating it.

    An explicit ``--config PATH`` still selects another project's management
    directory; without it the log stays under this project's ``.assent``.
    """
    config = _DEFAULT_CONFIG
    for idx, arg in enumerate(argv):
        if arg == "--config" and idx + 1 < len(argv):
            config = argv[idx + 1]
        elif arg.startswith("--config="):
            config = arg.split("=", 1)[1]
    path = Path(config).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def log_path_for_argv(argv: list[str]) -> Path:
    """Best-effort determine the task folder log path, falling back to beside the config file on failure."""
    path = _config_path_for_argv(argv)
    folder = (None if "--all" in argv else
              _folder_from_argv(argv) or _folder_from_tasks(path.parent))
    parent = path.parent / folder if folder is not None else path.parent
    return parent / "_assent.log"


@contextmanager
def terminal_logging(argv: list[str]) -> Iterator[Path]:
    """Log run/verify terminal output; each invocation appends its own section.

    Appending rather than truncating is what makes the log usable as evidence: a
    run that was interrupted or force-terminated is diagnosed from the next
    invocation, and that next invocation used to erase the very section
    explaining what went wrong. Sections stay separable through the per-
    invocation ``ASSENT START`` header.
    """
    log_path = log_path_for_argv(argv)
    if not argv or argv[0] not in ("run", "verify"):
        yield log_path
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8", buffering=1, newline="\n") as log:
        sink = _LogSink(log)
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout = TeeTextIO(old_stdout, sink)
        sys.stderr = TeeTextIO(old_stderr, sink)
        command = "assent" + (" " + " ".join(argv) if argv else "")
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        sink.write(
            "\n\n============================================================\n"
            f"ASSENT START | {stamp}\n"
            f"COMMAND      | {command}\n"
            "============================================================\n"
        )
        try:
            yield log_path
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            sys.stdout, sys.stderr = old_stdout, old_stderr
