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
        print(f"Python: OK ({version} >= {required})")
        return True
    print(f"Python: FAIL ({version} < {required} required)")
    return False


def _check_git() -> bool:
    try:
        result = subprocess.run(
            ["git", "--version"],
            capture_output=True, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        print("git: FAIL (executable not found: 'git')")
        return False
    if result.returncode == 0:
        print(f"git: OK ({result.stdout.strip()})")
        return True
    print(f"git: FAIL (--version exit code {result.returncode})")
    return False


def _check_adapter(name: str, cfg: Config) -> bool:
    adapter = get_adapter(name, cfg)
    ok, message = adapter.probe_cli()
    label = f"{name}: {'OK' if ok else 'FAIL'} ({message})"
    print(label)
    return ok


def _check_temp_dir() -> bool:
    temp_dir = Path(tempfile.gettempdir())
    probe = temp_dir / f"assent-doctor-{uuid.uuid4().hex}.tmp"
    try:
        probe.write_text("assent doctor probe\n", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        print(f"temp directory: FAIL ({temp_dir}: {e})")
        return False
    print(f"temp directory: OK ({temp_dir} is writable)")
    return True


def doctor() -> int:
    python_ok = _check_python()
    git_ok = _check_git()

    cfg = _placeholder_config()
    adapter_results = [_check_adapter(name, cfg) for name in _ADAPTER_NAMES]
    any_adapter_ok = any(adapter_results)

    temp_ok = _check_temp_dir()

    return 0 if (python_ok and git_ok and any_adapter_ok and temp_ok) else 1
