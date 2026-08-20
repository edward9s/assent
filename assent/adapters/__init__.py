"""Adapter interface and shared data types.

Quota message detection and parsing are encapsulated inside each adapter; the main loop
stays unaware of vendor differences.  The one provider-neutral terminal control record is
recognized here so every adapter applies the same exact-match rule.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Mapping, Sequence

from assent import AssentError

if TYPE_CHECKING:
    from assent.config import AdapterSettings, Config


CHECKPOINT_RESUME_RECORD = '{"type":"assent.checkpoint_resume"}'
_AUTHENTICATION_FAILURE_RE = re.compile(
    r"\b(?:unauthorized|unauthenticated)\b"
    r"|authentication\s+(?:failed|required)"
    r"|no\s+authentication\s+methods?\s+available"
    r"|not\s+(?:logged|signed)\s+in(?:to)?"
    r"|(?:please\s+)?(?:log|sign)\s*in(?:\s+(?:required|to\s+continue))?"
    r"|(?:please\s+)?(?:re-)?run\s+(?:[^\s]+\s+){0,2}(?:/login|login|auth)\b"
    r"|(?:login|session)\s+(?:(?:is|has)\s+)?expired"
    r"|no\s+(?:\w+\s+){0,2}credentials?\s+(?:were\s+)?found"
    r"|credentials?\s+(?:(?:is|are)\s+)?incomplete"
    r"|no\s+(?:oauth\s+|access\s+|auth(?:entication)?\s+)?token\s+in\s+\w+"
    r"|(?:oauth\s+|access\s+|auth(?:entication)?\s+)?token\s+"
    r"(?:is\s+)?no\s+longer\s+valid"
    r"|(?:invalid|expired|missing)\s+(?:api\s+key|auth(?:entication)?\s+token"
    r"|oauth\s+token|access\s+token|credentials?)"
    r"|(?:api\s+key|auth(?:entication)?\s+token|oauth\s+token|access\s+token"
    r"|credentials?)\s+(?:(?:is|are|has|have)\s+)?(?:invalid|expired|missing)",
    re.IGNORECASE,
)


def is_authentication_failure_text(text: str) -> bool:
    """Return whether provider output says credentials or login are missing or invalid."""
    return _AUTHENTICATION_FAILURE_RE.search(text) is not None


def is_checkpoint_resume_line(raw_line: str) -> bool:
    """Return whether one streamed line is exactly the checkpoint-resume record."""
    return raw_line.rstrip("\r\n") == CHECKPOINT_RESUME_RECORD


def parse_checkpoint_resume_output(output: str, exit_code: int,
                                   stalled: bool) -> bool:
    """Recognize the control record only as a finished nonzero final non-empty line.

    Empty lines after the record are harmless transport formatting; any later non-empty
    line, extra character, zero exit, or watchdog stall makes the result ineligible.
    """
    if exit_code == 0 or stalled:
        return False
    nonempty = [line for line in output.splitlines() if line.strip()]
    return bool(nonempty and is_checkpoint_resume_line(nonempty[-1]))


@dataclass
class TokenUsage:
    """One provider-reported model bucket; absent counters stay absent."""

    provider_model: str | None = None
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_output_tokens: int | None = None


_TOKEN_COUNTER_KEYS = {
    "input_tokens": ("input_tokens", "inputTokens"),
    "cached_input_tokens": (
        "cached_input_tokens", "cache_read_input_tokens",
        "cache_read_tokens", "cachedInputTokens", "cacheReadInputTokens"),
    "cache_creation_input_tokens": (
        "cache_creation_input_tokens", "cache_creation_tokens",
        "cacheCreationInputTokens", "cacheCreationTokens"),
    "output_tokens": ("output_tokens", "outputTokens"),
    "reasoning_output_tokens": (
        "reasoning_output_tokens", "thinking_tokens",
        "reasoningOutputTokens", "thinkingTokens"),
}


def normalize_token_usage(
        raw: object, provider_model: str | None = None) -> TokenUsage | None:
    """Normalize one usage object without estimating or coercing counters."""
    if not isinstance(raw, Mapping):
        return None
    normalized_model = (
        provider_model.strip() if isinstance(provider_model, str)
        and provider_model.strip() else None)
    values: dict[str, int | str | None] = {
        "provider_model": normalized_model,
    }
    available = False
    for target, aliases in _TOKEN_COUNTER_KEYS.items():
        value = None
        for key in aliases:
            candidate = raw.get(key)
            if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 0:
                value = candidate
                available = True
                break
        values[target] = value
    if not available and normalized_model is None:
        return None
    return TokenUsage(**values)


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
    checkpoint_resume: bool = False  # True = exact terminal control record requested continuation
    # A structured invocation keeps the provider's complete stream in ``output`` and may
    # expose a separately captured final assistant response here.  ``None`` means that no
    # native structured boundary was supplied; the provider-neutral adapter default fills it
    # from ``output`` so existing adapters retain their strict terminal-record behavior.
    structured_output: str | bytes | None = None
    # A native structured boundary can fail after the subprocess exits (for example, the
    # last-message file is missing or is not UTF-8).  Callers must not fall back to ``output``
    # when this is set.
    structured_output_error: str | None = None
    # Provider-reported token accounting. ``None`` means unavailable; multiple entries
    # preserve distinct actual-model buckets from one invocation.
    usage: tuple[TokenUsage, ...] | None = None


@dataclass(frozen=True)
class InvocationRequest:
    """One invocation a run may issue: the selection plus the resolved CLI values.

    The engine resolves these before any session starts so an adapter can validate every
    model/effort combination the run could send without spending a token.
    """

    task_id: str
    model: str                     # selected portable tier or vendor selection
    requested_model: str           # the actual --model value
    requested_effort: str | None    # the actual effort value, None = no effort flag


class Adapter:                     # Base class for each vendor's adapter
    # Concrete adapters set this from ``cfg.adapter_settings(<name>)``; it owns the command
    # and the tier -> invocation map, so callers never branch on the adapter name.
    settings: "AdapterSettings | None" = None

    def resolve(self, model: str) -> tuple[str, str | None]:
        """Resolve a portable tier or literal into the CLI ``--model``/effort pair."""
        if self.settings is None:
            return model, None
        return self.settings.resolve(model)

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

    def run_structured_task(self, prompt: str, requested_model: str,
                            requested_effort: str | None,
                            cwd: Path) -> TaskResult:
        """Run a structured invocation through the provider-neutral fallback boundary.

        Adapters with a native final-response transport override this method.  The default
        deliberately invokes the ordinary worker path unchanged, then makes its complete raw
        output the structured response consumed by the existing strict parser.
        """
        result = self.run_task(prompt, requested_model, requested_effort, cwd)
        if (result.structured_output is None
                and result.structured_output_error is None):
            return replace(result, structured_output=result.output)
        return result


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
