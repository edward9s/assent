"""Antigravity (``agy``) CLI adapter: headless stream mode and capability preflight.

Capability evidence, probed on 2026-07-23 against the installed CLI.  No probe below ever
reached a model: each one stops inside ``--model``/``--effort`` validation or at the
transport, so the whole contract was recorded without spending a token.

- ``agy --version`` prints ``1.1.5``.
- ``agy models`` lists one tab-separated expanded slug and description per line
  (verbatim copy in ``tests/fixtures/agy_models_1.1.5.txt``).
- ``--model`` and ``--effort`` are validated as a pair before the request is sent
  (transcript in ``tests/fixtures/agy_selection_1.1.5.toml``):
  a family base slug requires an ``--effort`` from its own set (``gemini-3.1-pro`` ->
  low, high; ``gemini-3.6-flash`` -> low, medium, high; ``gemini-3.5-flash`` -> low, medium);
  an expanded slug already carries its effort, so a matching ``--effort`` is accepted and a
  different one is refused as a conflict; ``gemini-3-flash`` exposes no variants and refuses
  ``--effort`` outright; and an unlisted slug such as ``gemini-3.5-flash-lite`` or
  ``gemini-3.1-pro-preview`` is refused with the available list instead of being downgraded
  to a neighbour.

Two facts from that evidence shape the shipped mapping in ``assent/config.py``:

- Gemini 3.1 Pro has no ``medium``, so ``prime`` maps the abstract medium up to ``high``;
  quality-first, and never a silent send of an unsupported value.
- AGY exposes no Flash Lite at all, so ``lite`` uses the explicitly written Gemini 3.5 Flash
  fallback, whose ceiling is ``medium`` -- the catalog's "Gemini 3.5 Flash (High)" entry is
  not reachable through ``--effort``.  ``lite`` high therefore maps to that ceiling in the
  configuration table, where it is visible, rather than in adapter code.

1.1.8 is the shipped minimum because it adds the typed ``stream-json`` result and usage
object required for provider-reported accounting, on top of the model, effort, and
headless contracts established by 1.1.5. Anything older is refused rather than run with
an output contract it does not support.
"""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from assent import AssentError
from assent.adapters import (Adapter, InvocationRequest, TaskResult, TokenUsage,
                             is_authentication_failure_text,
                             is_checkpoint_resume_line, normalize_token_usage,
                             parse_checkpoint_resume_output)
from assent.adapters.process import run_subprocess
from assent.modeling import has_literal

if TYPE_CHECKING:
    from assent.config import Config

NAME = "antigravity"
MINIMUM_VERSION = (1, 1, 8)
# AGY model slug suffixes and vendor effort values; unrelated to abstract task levels.
_EFFORT_ORDER = ("low", "medium", "high")
# AGY 1.1.5 lists this exact slug, but its argument validator does not accept the
# corresponding base-model/effort pair.  Keep that recorded selection contract separate
# from the exact slugs the catalog reports.
_BASE_EFFORT_OVERRIDES = {
    "gemini-3.5-flash": ("low", "medium"),
}
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")

# Flags this adapter owns.  Repeating one in extra_args can only contradict the resolved
# invocation or break the headless contract, so the value is refused before the CLI starts.
_RESERVED_ARGS = {
    "--print": "the adapter always runs exactly one non-interactive print session",
    "-p": "the adapter always runs exactly one non-interactive print session",
    "--prompt": "the adapter always runs exactly one non-interactive print session",
    "--prompt-interactive": "a scheduled task session must not be interactive",
    "-i": "a scheduled task session must not be interactive",
    "--model": f"the model comes from [adapter.{NAME}.models]",
    "--effort": (f"the effort comes from [adapter.{NAME}.default_effort] and "
                 f"[adapter.{NAME}.efforts]"),
    "--mode": "the adapter always runs --mode accept-edits",
    "--output-format": "the adapter always consumes stream-json output",
    "--print-timeout": (f"the adapter sets the print timeout from [adapter.{NAME}] "
                        "print_timeout_minutes"),
    "--log-file": "the adapter keeps the CLI log out of the isolated worktree",
    "--add-dir": "the adapter derives the workspace directories a task session needs",
    "--continue": "a task session must not resume an earlier conversation",
    "-c": "a task session must not resume an earlier conversation",
    "--conversation": "a task session must not resume an earlier conversation",
    "--agent": "a task session must not hand the task to a custom agent",
}

# Plain-text classification of a failed session.  AGY print mode emits prose, not events, so
# these patterns are matched against the human-readable transcript and nothing is invented.
_MODEL_TEXT_RE = re.compile(
    r"invalid model selection|is not recognized as a known model"
    r"|has no \"[a-z]+\" effort|--effort is not supported"
    r"|requires --effort|conflicts with --effort",
    re.IGNORECASE)
_QUOTA_TEXT_RE = re.compile(
    r"resource\s+has\s+been\s+exhausted|resource_exhausted"
    r"|quota\s+(?:exceeded|exhausted)|usage\s+limit|rate\s+limit|session\s+limit"
    r"|limit\s+reached|hit\s+your\s+[\w'’ ]{0,40}limit|too\s+many\s+requests",
    re.IGNORECASE)
# Account-level billing/insufficient-balance: distinct from quota because it never resets on
# its own (a prepaid balance does not refill), so the scheduler must fail fast rather than wait.
_BILLING_TEXT_RE = re.compile(
    r"credit\s+balance|balance\s+is\s+too\s+low"
    r"|insufficient\s+(?:credit|funds|balance)|payment\s+required",
    re.IGNORECASE)
_PERMISSION_TEXT_RE = re.compile(
    r"permission\s+denied|permission\s+request|soft-denied|not\s+authorized"
    r"|forbidden|api\s+has\s+not\s+been\s+used|is\s+disabled|eligibilit|allow-rule",
    re.IGNORECASE)
_TIMEOUT_TEXT_RE = re.compile(
    r"print[-\s]?timeout|timed\s+out|timeout\s+waiting|deadline\s+exceeded",
    re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Capability catalog
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ModelCatalog:
    """What ``agy models`` proves this installation can be asked for.

    ``families`` are base slugs that require an ``--effort`` chosen from their own variants;
    ``variants`` are the expanded slugs the catalog lists, mapped to the effort each already
    carries; ``standalone`` slugs take no effort at all.  A base slug is derived rather than
    listed, because AGY prints only expanded names while accepting the base form.
    """

    listed: tuple[str, ...]                  # verbatim catalog order, for diagnostics
    families: dict[str, tuple[str, ...]]
    variants: dict[str, str]
    standalone: tuple[str, ...]

    def check(self, model: str, effort: str | None) -> str | None:
        """Return None when this ``--model``/``--effort`` pair is sendable, else the reason."""
        supported = self.families.get(model)
        if supported is not None:
            if effort is None:
                return (f"model {model!r} requires an effort "
                        f"(available: {', '.join(supported)})")
            if effort not in supported:
                return (f"model {model!r} has no {effort!r} effort "
                        f"(available: {', '.join(supported)})")
            return None
        carried = self.variants.get(model)
        if carried is not None:
            if effort is None or effort == carried:
                return None
            return (f"model {model!r} already selects the {carried!r} effort and "
                    f"conflicts with --effort={effort}")
        if model in self.standalone:
            if effort is None:
                return None
            return f"model {model!r} supports no --effort at all"
        return (f"model {model!r} is not in this installation's AGY catalog "
                f"(available: {', '.join(self.listed)})")


def parse_models_catalog(text: str) -> ModelCatalog:
    """Derive the capability matrix from ``agy models`` output.

    Every slug ending in a known effort suffix contributes that effort to its base family,
    subject to recorded base-model selection contracts.  An exact expanded slug remains a
    separately selectable variant even when its suffix is not accepted for the base model.
    """
    listed = []
    for line in text.splitlines():
        record = line.strip()
        if not record:
            continue
        slug = record.split("\t", 1)[0].strip()
        if not slug or any(character.isspace() for character in slug):
            continue
        listed.append(slug)
    listed = tuple(listed)
    if not listed:
        raise AssentError("the AGY model catalog is empty")

    collected: dict[str, set[str]] = {}
    variants: dict[str, str] = {}
    for slug in listed:
        base, _, suffix = slug.rpartition("-")
        if base and suffix in _EFFORT_ORDER:
            collected.setdefault(base, set()).add(suffix)
            variants[slug] = suffix
    families = {}
    for base, efforts in collected.items():
        allowed = _BASE_EFFORT_OVERRIDES.get(base, _EFFORT_ORDER)
        families[base] = tuple(e for e in allowed if e in efforts)
    standalone = tuple(slug for slug in listed
                       if slug not in variants and slug not in families)
    return ModelCatalog(listed=listed, families=families, variants=variants,
                        standalone=standalone)


def load_catalog(command: str) -> ModelCatalog:
    """Read the live catalog from the CLI; listing models sends no prompt and costs nothing."""
    try:
        result = subprocess.run(
            [command, "models"],
            capture_output=True, encoding="utf-8", errors="replace")
    except OSError as e:
        raise AssentError(f"cannot run {command!r} models: {e}") from e
    if result.returncode != 0:
        detail = " ".join((result.stderr or result.stdout or "").split())[:160]
        raise AssentError(
            f"{command!r} models exited {result.returncode}: {detail or 'no output'}")
    return parse_models_catalog(result.stdout)


def parse_version(banner: str) -> tuple[int, int, int] | None:
    """Extract the semantic version from the ``agy --version`` banner."""
    match = _VERSION_RE.search(banner)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def recommended_effort(supported: Sequence[str], effort: str) -> str:
    """Suggest the lowest supported vendor effort at or above the requested vendor effort.

    Both arguments use vendor vocabulary.  When the family's ceiling is below the request
    there is nothing above it, so the ceiling itself is suggested rather than a value the CLI
    would refuse; that existing behavior is intentionally unchanged.
    """
    order = [level for level in _EFFORT_ORDER if level in supported]
    if not order:
        return ""
    if effort in _EFFORT_ORDER:
        wanted = _EFFORT_ORDER.index(effort)
        for candidate in order:
            if _EFFORT_ORDER.index(candidate) >= wanted:
                return candidate
    return order[-1]


# --------------------------------------------------------------------------- #
# Command construction
# --------------------------------------------------------------------------- #
def reserved_argument_errors(extra_args: Sequence[str]) -> list[str]:
    """Refuse extra args that would contradict an adapter-owned flag or the headless contract."""
    errors = []
    for arg in extra_args:
        flag = arg.split("=", 1)[0]
        reason = _RESERVED_ARGS.get(flag)
        if reason is not None:
            errors.append(
                f"[adapter.{NAME}] extra_args must not set {flag}: {reason}")
    return errors


def workspace_dirs(cfg: "Config") -> tuple[str, ...]:
    """Directories a task session must reach outside its worktree cwd.

    The main working tree's task folder holds the t/r files the session has to update, and
    the system temp directory is where tooling writes scratch files; AGY does not
    auto-approve writes outside the workspace, so both are added explicitly.
    """
    return (str(Path(cfg.tasks_dir).resolve()), tempfile.gettempdir())


def log_file(cfg: "Config") -> Path:
    """Keep the CLI log in the system temp directory so it never dirties the worktree."""
    return Path(tempfile.gettempdir()) / f"assent-{NAME}-{cfg.tasks_name}.log"


def build_command(cfg: "Config", prompt: str, requested_model: str,
                  requested_effort: str | None) -> list[str]:
    """Build the headless ``agy`` command whose prompt is supplied through stdin.

    Model and effort are already resolved by the engine.  The prompt is delivered via
    stdin (run_subprocess's input_text) instead of ``--print prompt`` to avoid exceeding
    Windows' CreateProcessW command-line length limit ([WinError 206]).  Without
    ``--print``, ``agy`` auto-detects a piped stdin and runs in non-interactive mode.

    The argument array is handed to the process untouched -- no shell is involved -- and any
    reserved-flag conflict is refused here as the hard floor, in addition to the preflight.
    """
    settings = cfg.adapter_settings(NAME)
    conflicts = reserved_argument_errors(settings.extra_args)
    if conflicts:
        raise AssentError("; ".join(conflicts))

    cmd = [settings.command, "--model", requested_model]
    if requested_effort:
        cmd += ["--effort", requested_effort]
    cmd += ["--mode", "accept-edits",
            "--output-format", "stream-json",
            "--print-timeout", f"{cfg.antigravity_print_timeout_minutes}m",
            "--log-file", str(log_file(cfg))]
    for directory in workspace_dirs(cfg):
        cmd += ["--add-dir", directory]
    cmd += list(settings.extra_args)
    return cmd


# --------------------------------------------------------------------------- #
# Output handling
# --------------------------------------------------------------------------- #
def format_output_line(raw_line: str) -> str | None:
    """Render one AGY stream-json event or stderr diagnostic."""
    if is_checkpoint_resume_line(raw_line):
        return None
    text = raw_line.rstrip("\r\n")
    if not text.strip():
        return None
    try:
        event = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        event = None
    if isinstance(event, dict):
        kind = event.get("event")
        if kind == "init":
            model = event.get("model") or "?"
            return f"  --| session started (model={model})"
        if kind == "step_update":
            message = event.get("message") or event.get("text")
            return f"  AI| {message}" if isinstance(message, str) and message.strip() else None
        if kind == "result":
            result = event.get("result")
            result = result if isinstance(result, dict) else {}
            usage = result.get("usage")
            usage = usage if isinstance(usage, dict) else {}
            parts = [f"session ended ({result.get('status', '?')})"]
            if isinstance(usage.get("output_tokens"), int):
                parts.append(f"output {usage['output_tokens']} tokens")
            if isinstance(usage.get("thinking_tokens"), int):
                parts.append(f"reasoning {usage['thinking_tokens']} tokens")
            return "  --| " + ",".join(parts)
        return None
    if text.lstrip().lower().startswith("error"):
        return f"  !| {text.strip()}"
    return f"  AI| {text}"


def parse_output_for_usage(output: str) -> tuple[TokenUsage, ...] | None:
    """Read the terminal AGY stream result's provider-reported usage."""
    provider_model: str | None = None
    terminal: dict | None = None
    for raw in output.splitlines():
        try:
            event = json.loads(raw.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        if event.get("event") == "init":
            model = event.get("model")
            if isinstance(model, str) and model.strip():
                provider_model = model.strip()
        elif event.get("event") == "result" and isinstance(event.get("result"), dict):
            terminal = event["result"]
    if terminal is None:
        return None
    model = terminal.get("model")
    if isinstance(model, str) and model.strip():
        provider_model = model.strip()
    raw_usage = terminal.get("usage")
    if isinstance(raw_usage, dict):
        model_usage = raw_usage.get("model_usage") or raw_usage.get("modelUsage")
        buckets: list[TokenUsage] = []
        if isinstance(model_usage, dict):
            for bucket_model, counters in model_usage.items():
                if isinstance(bucket_model, str) and bucket_model.strip():
                    normalized = normalize_token_usage(counters, bucket_model)
                    if normalized is not None:
                        buckets.append(normalized)
        if buckets:
            return tuple(buckets)
    normalized = normalize_token_usage(raw_usage, provider_model)
    return (normalized,) if normalized is not None else None


def _extract_result_text(output: str) -> tuple[str | None, str | None]:
    """Extract AGY's final response from the typed terminal result event."""
    response: str | None = None
    for raw in output.splitlines():
        try:
            event = json.loads(raw.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        if (isinstance(event, dict) and event.get("event") == "result"
                and isinstance(event.get("result"), dict)):
            candidate = event["result"].get("response")
            if isinstance(candidate, str):
                response = candidate
    if response is None:
        return None, "Antigravity structured output is missing a terminal result response"
    if not response.strip():
        return None, "Antigravity structured output is empty"
    return response, None


def classify_output(exit_code: int, stalled: bool, output: str) -> str | None:
    """Classify a finished session for the scheduler; None means it succeeded.

    The classification is evidence only.  The scheduler still owns the status, the checkpoint
    and the retry decision, and a non-zero exit stays a failure whatever the text says.
    """
    if stalled:
        return "stall"          # watchdog kill: a task failure, never quota
    if exit_code == 0:
        return None
    if _MODEL_TEXT_RE.search(output):
        return "unsupported_model"
    if _QUOTA_TEXT_RE.search(output):
        return "quota"
    if _BILLING_TEXT_RE.search(output):
        return "billing"
    if is_authentication_failure_text(output):
        return "authentication"
    if _PERMISSION_TEXT_RE.search(output):
        return "permission"
    if _TIMEOUT_TEXT_RE.search(output):
        return "timeout"
    return "nonzero"


def _has_quota_evidence(exit_code: int, stalled: bool, output: str) -> bool:
    """Return whether a failed, non-stalled transcript contains quota evidence.

    Quota is kept as an independent fact because another diagnostic classifier may also match
    the same prose.  The scheduler needs the quota path to win over those diagnostics when the
    exact checkpoint-resume control record is present.
    """
    return (exit_code != 0 and not stalled
            and _QUOTA_TEXT_RE.search(output) is not None)


# --------------------------------------------------------------------------- #
# Adapter
# --------------------------------------------------------------------------- #
class AntigravityAdapter(Adapter):
    """``agy`` CLI adapter; config is injected by get_adapter.

    ``catalog`` is injectable so the capability rules can be tested against a recorded
    version fixture without an installed CLI, a login or a model call.
    """

    def __init__(self, cfg: "Config", catalog: ModelCatalog | None = None) -> None:
        self.cfg = cfg
        # Model resolution and the effort contract come from the shared typed settings so this
        # adapter and the engine resolve invocations identically (base Adapter.resolve_model).
        self.settings = cfg.adapter_settings(NAME)
        self._catalog = catalog

    def catalog(self) -> ModelCatalog:
        if self._catalog is None:
            self._catalog = load_catalog(self.settings.command)
        return self._catalog

    def probe_cli(self) -> tuple[bool, str]:
        """Probe the CLI and enforce the shipped minimum version."""
        ok, message = super().probe_cli()
        if not ok:
            return ok, message
        version = parse_version(message)
        minimum = ".".join(str(part) for part in MINIMUM_VERSION)
        if version is None:
            return False, (f"cannot read an agy version from {message!r}; "
                           f"{minimum} or newer is required")
        if version < MINIMUM_VERSION:
            found = ".".join(str(part) for part in version)
            return False, (
                f"agy {found} is older than the required {minimum}, which is the first "
                "version carrying the structured result usage this scheduler consumes")
        return True, message

    def preflight(self, requests: Sequence[InvocationRequest]) -> list[str]:
        """Prove every resolved model/effort against the catalog before any session starts."""
        errors = reserved_argument_errors(self.settings.extra_args)
        try:
            catalog = self.catalog()
        except AssentError as e:
            errors.append(
                "the AGY capability catalog is unavailable, so no resolved model can be "
                f"proven: {e}")
            return errors
        seen = set(errors)
        for request in requests:
            reason = catalog.check(request.requested_model,
                                   request.requested_effort)
            if reason is None:
                continue
            message = self._diagnostic(catalog, request, reason)
            if message not in seen:
                seen.add(message)
                errors.append(message)
        return errors

    @staticmethod
    def _diagnostic(catalog: ModelCatalog, request: InvocationRequest,
                    reason: str) -> str:
        """Name the exact configuration owner, its current value, and the workable value."""
        sent = (f"--model {request.requested_model} --effort {request.requested_effort}"
                if request.requested_effort
                else f"--model {request.requested_model} with no --effort")
        head = (f"task {request.task_id}: model selection {request.model!r} resolves to "
                f"{sent}, but {reason}")
        if has_literal(request.model, request.effort):
            return (f"{head}; change the bracketed literal or bind this step "
                    "to an adapter that accepts it")
        supported = catalog.families.get(request.requested_model)
        if supported and request.effort and request.requested_effort:
            suggestion = recommended_effort(supported, request.requested_effort)
            return (f"{head}; set [adapter.{NAME}.efforts.{request.model}] "
                    f"{request.effort} = \"{suggestion}\" "
                    f"(current value {request.requested_effort!r})")
        if supported:
            return (f"{head}; set [adapter.{NAME}.default_effort] {request.model} to one "
                    f"of {', '.join(supported)}")
        return (f"{head}; fix [adapter.{NAME}.models] {request.model} "
                f"(current value {request.requested_model!r})")

    def run_task(self, prompt: str, requested_model: str,
                 requested_effort: str | None,
                 cwd: Path) -> TaskResult:
        command = build_command(
            self.cfg, prompt, requested_model, requested_effort)
        stall_seconds = self.cfg.stall_minutes * 60 if self.cfg.stall_minutes else 0
        log_path = log_file(self.cfg)
        try:
            returncode, output, stalled = run_subprocess(
                command, cwd, stall_seconds, echo=self._echo_line,
                heartbeat_path=log_path, input_text=prompt)
        finally:
            # Success, failure, stall-kill and interrupt all take this path: the per-run log
            # may hold internal detail that must not linger on disk, and it is never read here.
            try:
                log_path.unlink()
            except OSError:
                pass
        quota_evidence = _has_quota_evidence(returncode, stalled, output)
        kind = "quota" if quota_evidence else classify_output(
            returncode, stalled, output)
        response, _response_error = _extract_result_text(output)
        terminal_record = (
            parse_checkpoint_resume_output(output, returncode, stalled)
            or (response is not None and parse_checkpoint_resume_output(
                response, returncode, stalled)))
        # The exact final control record is authoritative over every non-quota prose
        # classifier.  Independently detected quota evidence remains the stronger outcome.
        checkpoint_resume = terminal_record and not quota_evidence
        return TaskResult(exit_code=returncode, output=output,
                          quota_exhausted=quota_evidence,
                          reset_at=None,        # print mode states no reset time; none is invented
                          stalled=stalled,
                          checkpoint_resume=checkpoint_resume,
                          failure_kind=None if checkpoint_resume else kind,
                          usage=parse_output_for_usage(output))

    def run_structured_task(self, prompt: str, requested_model: str,
                            requested_effort: str | None,
                            cwd: Path) -> TaskResult:
        result = self.run_task(prompt, requested_model, requested_effort, cwd)
        structured_output, error = _extract_result_text(result.output)
        return replace(result, structured_output=structured_output,
                       structured_output_error=error)

    @staticmethod
    def _echo_line(raw_line: str) -> None:
        text = format_output_line(raw_line)
        if text:
            print(text, flush=True)
