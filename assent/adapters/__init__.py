"""Adapter interface and shared data types.

Quota message detection and parsing are encapsulated inside each adapter; the main loop
stays unaware of vendor differences.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from assent import AssentError

if TYPE_CHECKING:
    from assent.config import Config


@dataclass
class TaskResult:
    exit_code: int
    output: str                    # Full subprocess output (verbatim, line by line)
    quota_exhausted: bool          # True = quota exhausted; this round doesn't count as a failure
    reset_at: datetime | None      # Parsed reset time; None if it couldn't be parsed


class Adapter:                     # Base class for each vendor's adapter
    def resolve_model(self, model: str) -> str:
        """Resolve the task's abstract tier into the ``--model`` value passed to the CLI."""
        return model

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
