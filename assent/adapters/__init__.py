"""Adapter interface and shared data types.

Quota message detection and parsing are encapsulated inside each adapter; the main loop
stays unaware of vendor differences.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

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
    # Optional adapter-supplied classification of a failed session (for example "quota",
    # "permission", "unsupported_model", "timeout").  It is evidence for the scheduler's
    # journal and retry prompt only: the adapter never writes task status or checkpoints,
    # and a classification never turns a non-zero exit into a success.
    failure_kind: str | None = None


@dataclass(frozen=True)
class InvocationRequest:
    """One invocation a run may issue: the abstract choices plus the resolved CLI values.

    The engine resolves these before any session starts so an adapter can validate every
    model/effort combination the run could send without spending a token.
    """

    task_id: str
    model: str                     # abstract tier (prime / core / lite)
    effort: str | None             # abstract effort (heavy / normal / slight), None = unset
    requested_model: str           # the actual --model value
    requested_effort: str | None    # the actual effort value, None = no effort flag


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

    def preflight(self, requests: "Sequence[InvocationRequest]") -> list[str]:
        """Validate every invocation the run could issue; return stable English diagnostics.

        A non-empty result makes the caller refuse before an AI session, a task checkpoint or
        any status write, so a configuration that cannot be sent costs nothing.  The base
        implementation states no vendor capability restriction, which keeps adapters whose
        CLI has no capability catalog unaffected.
        """
        return []

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
    if name == "antigravity":
        from assent.adapters.antigravity import AntigravityAdapter
        return AntigravityAdapter(cfg)
    raise AssentError(
        f"unknown adapter: {name!r} (built in: 'antigravity' / 'claude' / 'codex')")
