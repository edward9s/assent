"""Adapter interface and shared data types.

Quota message detection and parsing are encapsulated inside each adapter; the main loop
stays unaware of vendor differences.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from assent import AssentError

if TYPE_CHECKING:
    from assent.config import AdapterSettings, Config


@dataclass
class TaskResult:
    exit_code: int
    output: str                    # Full subprocess output (verbatim, line by line)
    quota_exhausted: bool          # True = quota exhausted; this round doesn't count as a failure
    reset_at: datetime | None      # Parsed reset time; None if it couldn't be parsed
    stalled: bool = False          # True = watchdog terminated the subprocess; never quota


class Adapter:                     # Base class for each vendor's adapter
    # Concrete adapters set this from ``cfg.adapter_settings(<name>)``; it owns the command,
    # the tier -> model map, and the effort contract so callers never branch on the adapter name.
    settings: "AdapterSettings | None" = None

    def resolve_model(self, model: str) -> str:
        """Resolve the task's abstract tier into the ``--model`` value passed to the CLI."""
        if self.settings is None:
            return model
        return self.settings.resolve_model(model)

    def probe_cli(self) -> tuple[bool, str]:
        """Probe this adapter's CLI for doctor/check; returns (ok, a stable English diagnostic).

        The probe uses this adapter's own command rather than a hardcoded vendor name, so a new
        adapter is checkable without touching the engine.  FileNotFound, a non-zero exit, and the
        version banner each map to a stable message.
        """
        if self.settings is None:
            return False, "adapter has no CLI settings to probe"
        command = self.settings.command
        try:
            result = subprocess.run(
                [command, "--version"],
                capture_output=True, encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return False, f"executable not found {command!r}"
        if result.returncode == 0:
            return True, result.stdout.strip() or "runnable"
        return False, f"--version exit code {result.returncode}"

    def run_task(self, prompt: str, requested_model: str,
                 requested_effort: str | None,
                 cwd: Path) -> TaskResult:
        """Run the task using the concrete CLI model and effort already resolved by the engine."""
        raise NotImplementedError


def get_adapter(name: str, cfg: "Config") -> Adapter:
    """Get an adapter instance by name; cfg is injected here (includes the tier -> model mapping)."""
    if name == "claude":
        from assent.adapters.claude import ClaudeAdapter  # Deferred import to avoid a circular import
        return ClaudeAdapter(cfg)
    if name == "codex":
        from assent.adapters.codex import CodexAdapter
        return CodexAdapter(cfg)
    raise AssentError(f"unknown adapter: {name!r} (built in: 'claude' / 'codex')")
