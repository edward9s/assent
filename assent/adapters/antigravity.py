"""Antigravity (``agy``) CLI adapter: headless print mode, capability preflight, plain text.

Capability evidence, probed on 2026-07-23 against the installed CLI.  No probe below ever
reached a model: each one stops inside ``--model``/``--effort`` validation or at the
transport, so the whole contract was recorded without spending a token.

- ``agy --version`` prints ``1.1.5``.
- ``agy models`` lists expanded slugs only (verbatim copy in
  ``tests/fixtures/agy_models_1.1.5.txt``): ``gemini-3.6-flash-{high,medium,low}``,
  ``gemini-3.5-flash-medium``, ``gemini-3.5-flash``, ``gemini-3.5-flash-low``,
  ``gemini-3.1-pro-{low,high}``, ``gemini-3-flash``.
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

1.1.5 is the shipped minimum because this probe shows it is the version that carries
``--effort``, the stable ``--model`` slugs accepted above, and the headless fixes this
scheduler depends on (``-p`` honouring persisted permission policy, a real non-zero exit
code on server-side failure, and usable Windows non-TTY output).  Anything older is refused
rather than run with a guess.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from assent import AssentError
from assent.adapters import Adapter, InvocationRequest, TaskResult
from assent.adapters.claude import run_subprocess

if TYPE_CHECKING:
    from assent.config import Config

NAME = "antigravity"
MINIMUM_VERSION = (1, 1, 5)
_EFFORT_ORDER = ("low", "medium", "high")
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
_PERMISSION_TEXT_RE = re.compile(
    r"permission\s+denied|permission\s+request|soft-denied|not\s+authorized"
    r"|unauthorized|unauthenticated|forbidden|sign\s*in|log\s*in\s+required"
    r"|api\s+has\s+not\s+been\s+used|is\s+disabled|eligibilit|allow-rule",
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

    Every slug ending in a known effort suffix contributes that effort to its base family;
    a slug that is itself a family base (``gemini-3.5-flash``) is therefore a base slug and
    not a standalone model, which is exactly how the CLI resolves it.
    """
    listed = tuple(line.strip() for line in text.splitlines() if line.strip())
    if not listed:
        raise AssentError("the AGY model catalog is empty")

    collected: dict[str, set[str]] = {}
    variants: dict[str, str] = {}
    for slug in listed:
        base, _, suffix = slug.rpartition("-")
        if base and suffix in _EFFORT_ORDER:
            collected.setdefault(base, set()).add(suffix)
            variants[slug] = suffix
    families = {base: tuple(e for e in _EFFORT_ORDER if e in efforts)
                for base, efforts in collected.items()}
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
    """Quality-first suggestion: the lowest supported effort at or above the requested one.

    When the family's ceiling is below the request there is nothing above it, so the ceiling
    itself is suggested rather than a value the CLI would refuse.
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
    """Build the headless ``agy`` command; model and effort are already resolved by the engine.

    The argument array is handed to the process untouched -- no shell is involved -- and any
    reserved-flag conflict is refused here as the hard floor, in addition to the preflight.
    """
    settings = cfg.adapter_settings(NAME)
    conflicts = reserved_argument_errors(settings.extra_args)
    if conflicts:
        raise AssentError("; ".join(conflicts))

    cmd = [settings.command, "--print", prompt, "--model", requested_model]
    if requested_effort:
        cmd += ["--effort", requested_effort]
    cmd += ["--mode", "accept-edits",
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
    """Render one print-mode line for the terminal.

    Print mode emits prose on stdout and diagnostics on stderr, so the text is shown as it
    arrived.  No JSON event, token count, tool call or server-selected model is inferred:
    a line that merely looks like JSON is still just a line of output.
    """
    text = raw_line.rstrip("\r\n")
    if not text.strip():
        return None
    if text.lstrip().lower().startswith("error"):
        return f"  !| {text.strip()}"
    return f"  AI| {text}"


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
    if _PERMISSION_TEXT_RE.search(output):
        return "permission"
    if _TIMEOUT_TEXT_RE.search(output):
        return "timeout"
    return "nonzero"


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
                "version carrying --effort, the stable --model slugs and the headless "
                "print-mode fixes this scheduler depends on")
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
        head = (f"task {request.task_id}: model tier {request.model!r} resolves to "
                f"{sent}, but {reason}")
        supported = catalog.families.get(request.requested_model)
        if supported and request.effort:
            suggestion = recommended_effort(supported, request.effort)
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
        returncode, output, stalled = run_subprocess(
            command, cwd, stall_seconds, echo=self._echo_line)
        kind = classify_output(returncode, stalled, output)
        return TaskResult(exit_code=returncode, output=output,
                          quota_exhausted=kind == "quota",
                          reset_at=None,        # print mode states no reset time; none is invented
                          stalled=stalled, failure_kind=kind)

    @staticmethod
    def _echo_line(raw_line: str) -> None:
        text = format_output_line(raw_line)
        if text:
            print(text, flush=True)
