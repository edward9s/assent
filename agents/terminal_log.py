"""把 ``agents run`` 的終端輸出同步寫入當次工作資料夾。

終端保留原生輸出(含色彩與歸位更新),工作資料夾內的 ``_agents.log`` 則保存
可攜、即時 flush 的純文字。設定載入前也可能發生錯誤,因此本模組自行以
best-effort 判讀設定,不依賴 config.py 且不向外拋錯。
"""
from __future__ import annotations

import re
import sys
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, TextIO

from agents.config import list_task_folders
from agents.plan import Plan

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
    """回傳可安全作為工作資料夾名稱的字串,否則回傳 None。"""
    if (not isinstance(value, str) or not value
            or not _FOLDER_RE.match(value) or value[0] in "-."):
        return None
    return value


def _folder_from_tasks(agents_dir: Path) -> str | None:
    """Best-effort 推導唯一進行中的工作資料夾;任何錯誤皆視為未知。"""
    try:
        ongoing = []
        for folder in list_task_folders(agents_dir):
            plan = Plan.parse(agents_dir / folder)
            if any(task.status in ("TODO", "WIP") for task in plan.tasks):
                ongoing.append(folder)
        return ongoing[0] if len(ongoing) == 1 else None
    # logging 早於正式設定載入;連編碼錯誤等非預期壞檔也不可阻斷原命令。
    except Exception:
        return None


def _folder_from_argv(argv: list[str]) -> str | None:
    """從 run 的位置參數找工作資料夾,略過已知選項及其值。"""
    if not argv or argv[0] != "run":
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


def log_path_for_argv(argv: list[str]) -> Path:
    """Best-effort 決定工作資料夾日誌路徑,失敗時退回設定檔旁。"""
    config = _DEFAULT_CONFIG
    for idx, arg in enumerate(argv):
        if arg == "--config" and idx + 1 < len(argv):
            config = argv[idx + 1]
        elif arg.startswith("--config="):
            config = arg.split("=", 1)[1]
    path = Path(config).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()
    folder = (None if "--all" in argv else
              _folder_from_argv(argv) or _folder_from_tasks(path.parent))
    parent = path.parent / folder if folder is not None else path.parent
    return parent / "_agents.log"


@contextmanager
def terminal_logging(argv: list[str]) -> Iterator[Path]:
    """只把 run 的完整終端輸出寫入日誌;每次 run 起點截斷舊現場。"""
    log_path = log_path_for_argv(argv)
    if not argv or argv[0] != "run":
        yield log_path
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w", encoding="utf-8", buffering=1, newline="\n") as log:
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
