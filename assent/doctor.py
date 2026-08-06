"""assent doctor: machine-environment diagnosis that needs no existing .assent/
project.

Distinct from ``inspection.check()``: check validates one project's own config and
task files; doctor validates the machine underneath any project (Python
version, git, adapter CLIs, temp directory writability).
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from assent.adapters import get_adapter
from assent.config import Config

_MIN_PYTHON = (3, 11)
_ADAPTER_NAMES = ("claude", "codex", "antigravity")

PASS = "pass"
WARN = "warn"
FAIL = "fail"

# Preferred glyphs deliberately avoid U+2600-U+27BF (checkmarks and crosses live
# there): terminal_log.py strips that block out of captured logs, so a tick from
# it would vanish from every captured run.  U+221A and U+00D7 survive capture and
# still fall back to ASCII on a console whose code page cannot encode them.
_GLYPHS = {PASS: "√", WARN: "!", FAIL: "×"}
_ASCII_GLYPHS = {PASS: "OK", WARN: "!", FAIL: "X"}
_COLORS = {PASS: "\x1b[32m", WARN: "\x1b[33m", FAIL: "\x1b[31m"}
_RESET = "\x1b[0m"


def _stream_can_encode(stream, text: str) -> bool:
    """Whether the destination can really represent ``text``.

    A Windows console whose code page is not UTF-8 corrupts non-encodable output
    rather than refusing it, so the glyphs are probed against the stream's own
    encoding instead of being assumed.  A stream that declares no encoding (a
    StringIO capture, a pipe wrapper) counts as incapable.
    """
    encoding = getattr(stream, "encoding", None)
    if not encoding:
        return False
    try:
        text.encode(encoding)
    except (LookupError, UnicodeError):
        return False
    return True


def _print_check(state: str, name: str, detail: str, stream=None) -> None:
    """Print one check as a leading status marker plus its name and detail.

    Glyph and colour degrade independently: the glyph set follows the stream's
    encoding, the colour follows whether the stream is a TTY.  A non-TTY stream
    that cannot encode the glyphs therefore receives plain ASCII with no escape
    sequences at all.
    """
    stream = sys.stdout if stream is None else stream
    glyphs = _GLYPHS if _stream_can_encode(
        stream, "".join(_GLYPHS.values())) else _ASCII_GLYPHS
    marker = f"[{glyphs[state]}]"
    if hasattr(stream, "isatty") and stream.isatty():
        marker = f"{_COLORS[state]}{marker}{_RESET}"
    print(f"{marker} {name}: {detail}", file=stream)


def _placeholder_config() -> Config:
    """A minimal Config for adapter construction; doctor probes CLIs only, so
    the paths inside it are never read from disk."""
    root = Path.cwd()
    assent_dir = root / ".assent"
    return Config(
        root=root,
        assent_dir=assent_dir,
        tasks_dir=assent_dir / "doctor",
        tasks_name="doctor",
    )


def _check_python() -> bool:
    version = ".".join(str(part) for part in sys.version_info[:3])
    required = ".".join(str(part) for part in _MIN_PYTHON)
    if sys.version_info >= _MIN_PYTHON:
        _print_check(PASS, "Python", f"{version} >= {required}")
        return True
    _print_check(FAIL, "Python", f"{version} < {required} required")
    return False


def _check_git() -> bool:
    try:
        result = subprocess.run(
            ["git", "--version"],
            capture_output=True, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        _print_check(FAIL, "git", "executable not found: 'git'")
        return False
    if result.returncode == 0:
        _print_check(PASS, "git", result.stdout.strip())
        return True
    _print_check(FAIL, "git", f"--version exit code {result.returncode}")
    return False


def _check_adapter(name: str, cfg: Config) -> bool:
    adapter = get_adapter(name, cfg)
    ok, message = adapter.probe_cli()
    _print_check(PASS if ok else FAIL, name, message)
    return ok


def _check_temp_dir() -> bool:
    temp_dir = Path(tempfile.gettempdir())
    probe = temp_dir / f"assent-doctor-{uuid.uuid4().hex}.tmp"
    try:
        probe.write_text("assent doctor probe\n", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        _print_check(FAIL, "temp directory", f"{temp_dir}: {e}")
        return False
    _print_check(PASS, "temp directory", f"{temp_dir} is writable")
    return True


def doctor() -> int:
    python_ok = _check_python()
    git_ok = _check_git()

    cfg = _placeholder_config()
    adapter_results = [_check_adapter(name, cfg) for name in _ADAPTER_NAMES]
    any_adapter_ok = any(adapter_results)

    temp_ok = _check_temp_dir()

    return 0 if (python_ok and git_ok and any_adapter_ok and temp_ok) else 1
