"""Terminal output tee for ``agents``.

The terminal keeps its native output (including colours and carriage-return
updates), while ``.agents/agents.log`` receives portable, immediately-flushed
text.
"""
from __future__ import annotations

import re
import sys
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, TextIO

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

_DEFAULT_CONFIG = ".agents/agents.toml"


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


def log_path_for_argv(argv: list[str]) -> Path:
    """Put the log beside the selected config (i.e. inside .agents/)."""
    config = _DEFAULT_CONFIG
    for idx, arg in enumerate(argv):
        if arg == "--config" and idx + 1 < len(argv):
            config = argv[idx + 1]
        elif arg.startswith("--config="):
            config = arg.split("=", 1)[1]
    path = Path(config).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve().parent / "agents.log"


@contextmanager
def terminal_logging(argv: list[str]) -> Iterator[Path]:
    """Tee this invocation's complete terminal output to ``agents.log``."""
    log_path = log_path_for_argv(argv)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8", buffering=1, newline="\n") as log:
        sink = _LogSink(log)
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout = TeeTextIO(old_stdout, sink)
        sys.stderr = TeeTextIO(old_stderr, sink)
        command = "agents" + (" " + " ".join(argv) if argv else "")
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        sink.write(
            "\n\n============================================================\n"
            f"AGENTS START | {stamp}\n"
            f"COMMAND      | {command}\n"
            "============================================================\n"
        )
        try:
            yield log_path
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            sys.stdout, sys.stderr = old_stdout, old_stderr
