"""Loading assent.toml, and enumerating and validating task folders.

- assent.toml lives inside the project's .assent/; the project root is the
  parent directory of .assent.
- The task folder name is supplied by the caller; the git branch prefix is
  that name plus "/".
- Fields not supplied fall back to defaults; an unknown top-level key is
  always an error (so a typo cannot fail silently).
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

from assent import AssentError
from assent.lockfile import LOCK_NAME

_TOP_LEVEL_KEYS = {"watchdog", "run", "adapter", "prompt", "verification"}
_MODEL_TIERS = {"prime", "core", "lite"}
_EFFORT_LEVELS = {"low", "medium", "high"}

# Who refreshes the folder verification receipt.  "manual" (the default) leaves it
# to an explicit `assent verify [--batch]`, so a batch workflow verifies once
# instead of once per folder at every run closeout; "auto" keeps run closeout
# refreshing a stale receipt itself.
_RECEIPT_REFRESH_MODES = {"manual", "auto"}

# Task folder name: no whitespace or path separators, must not start with
# - or . (it becomes the git branch prefix)
_FOLDER_RE = re.compile(r"^[^\s/\\]+$")
_TASK_FILE_RE = re.compile(r"^t\d{3}_.+\.e\.toml$")

_DEFAULT_EXTRA_ARGS = ["--permission-mode", "acceptEdits"]
# Abstract tier -> claude CLI --model argument
_DEFAULT_MODELS = {"prime": "fable", "core": "opus", "lite": "sonnet"}
_DEFAULT_EFFORT = {"prime": "high", "core": "high", "lite": "medium"}

_DEFAULT_CODEX_EXTRA_ARGS = ["--sandbox", "workspace-write"]
_DEFAULT_CODEX_MODELS = {
    "prime": "gpt-5.6-sol", "core": "gpt-5.6-terra", "lite": "gpt-5.6-luna",
}
_DEFAULT_CODEX_EFFORT = {"prime": "high", "core": "medium", "lite": "low"}

# Antigravity defaults.  The slugs and the effort translations below are the base family
# names AGY 1.1.5 proved it accepts; the reasoning behind each one, and the recorded probe
# it came from, are documented in assent/adapters/antigravity.py.
# A headless run cannot answer a permission prompt, and assent must not edit the user's own
# antigravity-cli settings.json, so unattended execution states the skip explicitly.
_DEFAULT_ANTIGRAVITY_EXTRA_ARGS = ["--dangerously-skip-permissions"]
_DEFAULT_ANTIGRAVITY_MODELS = {
    "prime": "gemini-3.1-pro",     # low/high only
    "core": "gemini-3.6-flash",    # low/medium/high
    "lite": "gemini-3.5-flash",    # low/medium; AGY exposes no Flash Lite at all
}
_DEFAULT_ANTIGRAVITY_EFFORT = {"prime": "high", "core": "high", "lite": "high"}
# Vendor effort translation lives here, never in adapter code: Gemini 3.1 Pro has no medium
# (quality-first, so medium goes up to high), and Gemini 3.5 Flash has no high (so the lite
# tier's high lands on that family's ceiling instead of being sent and refused).
_DEFAULT_ANTIGRAVITY_TIER_EFFORTS = {
    "prime": {"medium": "high"},
    "lite": {"high": "medium"},
}
_DEFAULT_ANTIGRAVITY_PRINT_TIMEOUT_MINUTES = 120


@dataclass(frozen=True)
class AdapterSettings:
    """One vendor adapter's resolved settings: command, tier -> model map, and effort contract.

    Both resolution orders are fixed and live here so no caller has to branch on an adapter
    name to resolve an invocation:
    - abstract effort selection: task annotation > this adapter's tier default > unset;
    - vendor effort translation: tier-specific > flat > identity.
    An empty ``default_effort`` for a tier means no abstract effort is chosen, so no effort flag
    is passed and no CLI default is invented.
    """

    name: str
    command: str
    extra_args: tuple[str, ...]
    models: dict[str, str]
    default_effort: dict[str, str]
    efforts: dict[str, str]
    tier_efforts: dict[str, dict[str, str]]

    def resolve_model(self, model: str) -> str:
        """Resolve the abstract tier into the concrete ``--model`` argument for this adapter."""
        alias = self.models.get(model)
        if alias is None:
            raise AssentError(
                f"model tier {model!r} is not in [adapter.{self.name}.models]; "
                f"check the plan file's suggested model or the config mapping")
        return alias

    def resolve_effort(self, task_effort: str | None, model: str) -> str | None:
        """Choose the abstract effort: the task annotation wins, else this tier's default, else None."""
        if task_effort:
            return task_effort
        return self.default_effort.get(model)

    def resolve_requested_effort(self, model: str,
                                 effort: str | None) -> str | None:
        """Translate the abstract effort to the actual CLI value by tier-specific > flat > identity."""
        if effort is None:
            return None
        return self.tier_efforts.get(model, {}).get(
            effort, self.efforts.get(effort, effort))


@dataclass
class Config:
    root: Path                     # Project root = parent of .assent
    assent_dir: Path               # .assent directory (= where the config file lives)
    tasks_dir: Path                # Task folder (.assent/<tasks>)
    tasks_name: str                # Task folder name (= git branch prefix stem)
    stall_minutes: int = 30        # 0 = watchdog disabled
    retry_per_task: int = 1
    quota_poll_minutes: int = 30
    adapter_name: str = "claude"
    claude_command: str = "claude"
    claude_extra_args: list[str] = field(
        default_factory=lambda: list(_DEFAULT_EXTRA_ARGS))
    claude_models: dict[str, str] = field(
        default_factory=lambda: dict(_DEFAULT_MODELS))
    claude_default_effort: dict[str, str] = field(
        default_factory=lambda: dict(_DEFAULT_EFFORT))
    claude_efforts: dict[str, str] = field(default_factory=dict)
    claude_tier_efforts: dict[str, dict[str, str]] = field(default_factory=dict)
    codex_command: str = "codex"
    codex_extra_args: list[str] = field(
        default_factory=lambda: list(_DEFAULT_CODEX_EXTRA_ARGS))
    codex_models: dict[str, str] = field(
        default_factory=lambda: dict(_DEFAULT_CODEX_MODELS))
    codex_default_effort: dict[str, str] = field(
        default_factory=lambda: dict(_DEFAULT_CODEX_EFFORT))
    codex_efforts: dict[str, str] = field(default_factory=dict)
    codex_tier_efforts: dict[str, dict[str, str]] = field(default_factory=dict)
    antigravity_command: str = "agy"
    antigravity_extra_args: list[str] = field(
        default_factory=lambda: list(_DEFAULT_ANTIGRAVITY_EXTRA_ARGS))
    antigravity_models: dict[str, str] = field(
        default_factory=lambda: dict(_DEFAULT_ANTIGRAVITY_MODELS))
    antigravity_default_effort: dict[str, str] = field(
        default_factory=lambda: dict(_DEFAULT_ANTIGRAVITY_EFFORT))
    antigravity_efforts: dict[str, str] = field(default_factory=dict)
    antigravity_tier_efforts: dict[str, dict[str, str]] = field(
        default_factory=lambda: {tier: dict(values) for tier, values
                                 in _DEFAULT_ANTIGRAVITY_TIER_EFFORTS.items()})
    # Print mode has its own upstream wait limit, far shorter than a task session; the
    # adapter always states one instead of inheriting the CLI default.
    antigravity_print_timeout_minutes: int = _DEFAULT_ANTIGRAVITY_PRINT_TIMEOUT_MINUTES
    prompt_template: str | None = None
    receipt_refresh: str = "manual"  # "manual" = explicit verify only, "auto" = also at run closeout
    source_root: Path | None = None  # Original main worktree when running isolated; not from the config file

    @property
    def branch_prefix(self) -> str:
        return f"{self.tasks_name}/"

    def adapter_settings(self, name: str) -> AdapterSettings:
        """Return one adapter's typed settings, fail-closed on an unknown name.

        This is the single place a vendor name maps to its settings; the engine and the adapters
        both go through it, so an unknown third adapter is rejected here rather than silently
        inheriting Claude's mapping.
        """
        if name == "claude":
            return AdapterSettings(
                name="claude", command=self.claude_command,
                extra_args=tuple(self.claude_extra_args),
                models=self.claude_models,
                default_effort=self.claude_default_effort,
                efforts=self.claude_efforts,
                tier_efforts=self.claude_tier_efforts)
        if name == "codex":
            return AdapterSettings(
                name="codex", command=self.codex_command,
                extra_args=tuple(self.codex_extra_args),
                models=self.codex_models,
                default_effort=self.codex_default_effort,
                efforts=self.codex_efforts,
                tier_efforts=self.codex_tier_efforts)
        if name == "antigravity":
            return AdapterSettings(
                name="antigravity", command=self.antigravity_command,
                extra_args=tuple(self.antigravity_extra_args),
                models=self.antigravity_models,
                default_effort=self.antigravity_default_effort,
                efforts=self.antigravity_efforts,
                tier_efforts=self.antigravity_tier_efforts)
        raise AssentError(
            f"unknown adapter: {name!r} (built in: 'antigravity' / 'claude' / 'codex')")

    def rel(self, path: Path) -> str:
        """Path for use in prompts; relative inside the project, absolute for an external source of truth."""
        resolved = path.resolve()
        try:
            return resolved.relative_to(self.root.resolve()).as_posix()
        except ValueError:
            if self.source_root is not None:
                resolved.relative_to(self.source_root.resolve())
                return str(resolved)
            raise

    def git_rel(self, path: Path) -> str:
        """Convert a path in the main tree or a worktree to a repo-relative path for git pathspecs."""
        resolved = path.resolve()
        roots = (self.root, self.source_root) if self.source_root else (self.root,)
        for root in roots:
            try:
                return resolved.relative_to(root.resolve()).as_posix()
            except ValueError:
                continue
        raise ValueError(f"Path is outside the project worktree: {resolved}")

    def for_worktree(self, root: Path) -> "Config":
        """Derive an equivalent config that only moves the code/git root into a worktree."""
        return replace(self, root=root.resolve(), source_root=self.root.resolve())

    @property
    def runtime_log_rel(self) -> str:
        return self.git_rel(self.tasks_dir / "_assent.log")

    @property
    def report_rel(self) -> str:
        return self.git_rel(self.tasks_dir / "_report.md")

    @property
    def lockfile_rel(self) -> str:
        return self.git_rel(self.tasks_dir / LOCK_NAME)

    @property
    def verification_receipt_rel(self) -> str:
        return self.git_rel(self.tasks_dir / "_verification.toml")

    @property
    def git_excludes(self) -> tuple[str, ...]:
        """Runtime artifacts: excluded from the clean check, scope check, and checkpoint commit."""
        return (self.runtime_log_rel, self.report_rel, self.lockfile_rel,
                self.verification_receipt_rel)


def _section(data: dict, name: str) -> dict:
    val = data.get(name, {})
    if not isinstance(val, dict):
        raise AssentError(f"Config [{name}] must be a table, not a scalar")
    return val


def _typed(section: dict, owner: str, key: str, typ: type, default):
    if key not in section:
        return default
    val = section[key]
    if not isinstance(val, typ) or (typ is not bool and isinstance(val, bool)):
        raise AssentError(f"Config {owner}.{key} has the wrong type: expected {typ.__name__}")
    return val


def _str_list(section: dict, owner: str, key: str, default: list[str]) -> list[str]:
    val = _typed(section, owner, key, list, None)
    if val is None:
        return list(default)
    if not all(isinstance(x, str) for x in val):
        raise AssentError(f"Config {owner}.{key} must have all-string elements")
    return list(val)


def _str_map(section: dict, owner: str, key: str, default: dict[str, str]) -> dict[str, str]:
    val = _typed(section, owner, key, dict, None)
    if val is None:
        return dict(default)
    if not all(isinstance(v, str) for v in val.values()):
        raise AssentError(f"Config [{owner}.{key}] must have all-string values")
    return dict(val)


def _effort_maps(section: dict, owner: str,
                 default_flat: dict[str, str] | None = None,
                 default_tier: dict[str, dict[str, str]] | None = None
                 ) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """Parse the flat and per-tier ``efforts`` maps, fail-closed on any bad structure.

    Like ``models``, a stated ``efforts`` table replaces the built-in one whole rather than
    merging into it: a vendor translation is a single coherent decision, and a half-merged
    one would hide which value is actually being sent.
    """
    raw = _typed(section, f"[{owner}]", "efforts", dict, None)
    if raw is None:
        return (dict(default_flat or {}),
                {tier: dict(values) for tier, values in (default_tier or {}).items()})

    flat: dict[str, str] = {}
    by_tier: dict[str, dict[str, str]] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            if key not in _MODEL_TIERS:
                raise AssentError(
                    f"Config [{owner}.efforts] section {key!r} is invalid"
                    f" ({'/'.join(sorted(_MODEL_TIERS))})")
            tier_values: dict[str, str] = {}
            block = f"[{owner}.efforts.{key}]"
            for effort, requested in value.items():
                if effort not in _EFFORT_LEVELS:
                    raise AssentError(
                        f"Config {block} key {effort!r} is not a valid effort"
                        f" ({'/'.join(sorted(_EFFORT_LEVELS))})")
                if not isinstance(requested, str) or not requested.strip():
                    raise AssentError(
                        f"Config {block} {effort} must be a non-empty string")
                tier_values[effort] = requested
            by_tier[key] = tier_values
            continue

        block = f"[{owner}.efforts]"
        if key not in _EFFORT_LEVELS:
            raise AssentError(
                f"Config {block} key {key!r} is not a valid effort"
                f" ({'/'.join(sorted(_EFFORT_LEVELS))})")
        if not isinstance(value, str) or not value.strip():
            raise AssentError(f"Config {block} {key} must be a non-empty string")
        flat[key] = value
    return flat, by_tier


def _validate_tasks_name(tasks_name: str, owner: str) -> None:
    """Validate a task folder name so it is safe to use as a git branch prefix."""
    if not _FOLDER_RE.match(tasks_name) or tasks_name[0] in "-.":
        raise AssentError(
            f"{owner} = {tasks_name!r} is not a valid task folder name"
            " (no whitespace or path separators, must not start with - or .;"
            " it also becomes the git branch prefix)")


def _load_data(path: str | Path) -> tuple[Path, dict]:
    """Read and validate the config content that does not depend on a task folder."""
    path = Path(path).resolve()
    if not path.is_file():
        raise AssentError(
            f"Config file not found: {path}"
            " (not initialized yet? run assent init in the project root)")
    with open(path, "rb") as f:
        try:
            data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise AssentError(f"Config file is not valid TOML ({path}): {e}") from e

    unknown = sorted(set(data) - _TOP_LEVEL_KEYS)
    if unknown:
        raise AssentError(
            f"Config file has unknown top-level keys: {', '.join(unknown)}"
            f" (valid keys: {', '.join(sorted(_TOP_LEVEL_KEYS))})")

    return path.resolve(), data


def validate_config(path: str | Path) -> Path:
    """Validate the config file and return the ``.assent`` directory it lives in."""
    resolved, _ = _load_data(path)
    return resolved.parent


def list_task_folders(assent_dir: str | Path) -> list[str]:
    """List task folders that contain a formal task file, sorted lexicographically."""
    assent_dir = Path(assent_dir)
    if not assent_dir.is_dir():
        return []
    folders = []
    for entry in assent_dir.iterdir():
        if (not entry.is_dir() or entry.name == "__pycache__"
                or entry.name.startswith("_")):
            continue
        if any(child.is_file() and _TASK_FILE_RE.match(child.name)
               for child in entry.iterdir()):
            folders.append(entry.name)
    return sorted(folders)


def load_config(path: str | Path, folder: str) -> Config:
    """Load the config and build derived paths from the caller-supplied task folder name."""
    resolved, data = _load_data(path)
    _validate_tasks_name(folder, "Command-line task folder")

    assent_dir = resolved.parent
    root = assent_dir.parent

    tasks_name = folder

    watchdog = _section(data, "watchdog")
    run = _section(data, "run")
    adapter = _section(data, "adapter")
    claude = _section(adapter, "claude") if "claude" in adapter else {}
    codex = _section(adapter, "codex") if "codex" in adapter else {}
    antigravity = (_section(adapter, "antigravity")
                   if "antigravity" in adapter else {})
    prompt = _section(data, "prompt")
    verification_section = _section(data, "verification")
    claude_efforts, claude_tier_efforts = _effort_maps(
        claude, "adapter.claude")
    codex_efforts, codex_tier_efforts = _effort_maps(
        codex, "adapter.codex")
    antigravity_efforts, antigravity_tier_efforts = _effort_maps(
        antigravity, "adapter.antigravity",
        default_tier=_DEFAULT_ANTIGRAVITY_TIER_EFFORTS)

    cfg = Config(
        root=root,
        assent_dir=assent_dir,
        tasks_dir=assent_dir / tasks_name,
        tasks_name=tasks_name,
        stall_minutes=_typed(watchdog, "[watchdog]", "stall_minutes", int, 30),
        retry_per_task=_typed(run, "[run]", "retry_per_task", int, 1),
        quota_poll_minutes=_typed(run, "[run]", "quota_poll_minutes", int, 30),
        adapter_name=_typed(adapter, "[adapter]", "name", str, "claude"),
        claude_command=_typed(claude, "[adapter.claude]", "command", str, "claude"),
        claude_extra_args=_str_list(claude, "[adapter.claude]", "extra_args",
                                    _DEFAULT_EXTRA_ARGS),
        claude_models=_str_map(claude, "adapter.claude", "models", _DEFAULT_MODELS),
        claude_default_effort=_str_map(claude, "adapter.claude", "default_effort",
                                       _DEFAULT_EFFORT),
        claude_efforts=claude_efforts,
        claude_tier_efforts=claude_tier_efforts,
        codex_command=_typed(codex, "[adapter.codex]", "command", str, "codex"),
        codex_extra_args=_str_list(codex, "[adapter.codex]", "extra_args",
                                   _DEFAULT_CODEX_EXTRA_ARGS),
        codex_models=_str_map(codex, "adapter.codex", "models",
                              _DEFAULT_CODEX_MODELS),
        codex_default_effort=_str_map(codex, "adapter.codex", "default_effort",
                                      _DEFAULT_CODEX_EFFORT),
        codex_efforts=codex_efforts,
        codex_tier_efforts=codex_tier_efforts,
        antigravity_command=_typed(antigravity, "[adapter.antigravity]",
                                   "command", str, "agy"),
        antigravity_extra_args=_str_list(antigravity, "[adapter.antigravity]",
                                         "extra_args",
                                         _DEFAULT_ANTIGRAVITY_EXTRA_ARGS),
        antigravity_models=_str_map(antigravity, "adapter.antigravity", "models",
                                    _DEFAULT_ANTIGRAVITY_MODELS),
        antigravity_default_effort=_str_map(antigravity, "adapter.antigravity",
                                            "default_effort",
                                            _DEFAULT_ANTIGRAVITY_EFFORT),
        antigravity_efforts=antigravity_efforts,
        antigravity_tier_efforts=antigravity_tier_efforts,
        antigravity_print_timeout_minutes=_typed(
            antigravity, "[adapter.antigravity]", "print_timeout_minutes", int,
            _DEFAULT_ANTIGRAVITY_PRINT_TIMEOUT_MINUTES),
        prompt_template=_typed(prompt, "[prompt]", "template", str, None),
        receipt_refresh=_typed(verification_section, "[verification]",
                               "receipt_refresh", str, "manual"),
    )

    if cfg.stall_minutes < 0:
        raise AssentError("[watchdog] stall_minutes must not be negative (0 = disabled)")
    if cfg.retry_per_task < 0:
        raise AssentError("[run] retry_per_task must not be negative")
    if cfg.quota_poll_minutes < 1:
        raise AssentError("[run] quota_poll_minutes must be at least 1")
    if cfg.receipt_refresh not in _RECEIPT_REFRESH_MODES:
        raise AssentError(
            f"[verification] receipt_refresh = {cfg.receipt_refresh!r} is not valid"
            f" ({'/'.join(sorted(_RECEIPT_REFRESH_MODES))})")
    if cfg.antigravity_print_timeout_minutes < 1:
        raise AssentError(
            "[adapter.antigravity] print_timeout_minutes must be at least 1")
    for owner, efforts in (
            ("adapter.claude", cfg.claude_default_effort),
            ("adapter.codex", cfg.codex_default_effort),
            ("adapter.antigravity", cfg.antigravity_default_effort)):
        for model, eff in efforts.items():
            if eff not in _EFFORT_LEVELS:
                raise AssentError(
                    f"[{owner}.default_effort] {model} = {eff!r} is not a valid effort"
                    f" ({'/'.join(sorted(_EFFORT_LEVELS))})")
    return cfg
