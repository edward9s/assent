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
_EFFORT_LEVELS = {"heavy", "normal", "slight"}
_ADAPTER_NAMES = {"claude", "codex", "antigravity"}
# Abstract effort names intentionally differ from vendor names.  A missing translation must
# resolve through this settings-layer baseline rather than passing the abstract value through;
# this table is not vendor knowledge embedded in adapter code.
_EFFORT_BASELINE = {"heavy": "high", "normal": "medium", "slight": "low"}

# Who refreshes the folder verification receipt.  "manual" (the default) leaves it
# to an explicit `assent verify [--batch]`, so a batch workflow verifies once
# instead of once per folder at every run closeout; "auto" keeps run closeout
# refreshing a stale receipt itself.
_RECEIPT_REFRESH_MODES = {"manual", "auto"}

# A task folder name becomes the first component of every Assent branch name.
# Keep this contract local and explicit instead of relying on a later Git command:
# the name must also remain usable as a Windows directory name.
_GIT_REF_FORBIDDEN_CHARS = frozenset("~^:?*[")
_WINDOWS_FORBIDDEN_CHARS = frozenset('<>"|')
_FOLDER_FORBIDDEN_CHARS = (
    _GIT_REF_FORBIDDEN_CHARS | _WINDOWS_FORBIDDEN_CHARS | {"/", "\\"})
_WINDOWS_RESERVED_DEVICE_NAMES = frozenset({
    "con", "prn", "aux", "nul",
    "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
    "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
    # The superscript forms are also reserved device names on Windows.
    "com¹", "com²", "com³", "lpt¹", "lpt²", "lpt³",
})
_FOLDER_NAME_RULE = (
    "must be non-empty, contain no whitespace, path separators, control characters, "
    "or Git-ref/Windows-forbidden characters, must not start with - or ., contain .. "
    "or @{, end with . or .lock, or use a reserved Windows device name; "
    "it also becomes the Git branch prefix")
_TASK_FILE_RE = re.compile(r"^t\d{3}_.+\.e\.toml$")

_DEFAULT_EXTRA_ARGS = ["--permission-mode", "acceptEdits"]
# Abstract tier -> claude CLI --model argument
_DEFAULT_MODELS = {"prime": "fable", "core": "opus", "lite": "sonnet"}
_DEFAULT_EFFORT = {"prime": "heavy", "core": "heavy", "lite": "normal"}

_DEFAULT_CODEX_EXTRA_ARGS = ["--sandbox", "workspace-write"]
_DEFAULT_CODEX_MODELS = {
    "prime": "gpt-5.6-sol", "core": "gpt-5.6-terra", "lite": "gpt-5.6-luna",
}
_DEFAULT_CODEX_EFFORT = {"prime": "heavy", "core": "normal", "lite": "slight"}

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
_DEFAULT_ANTIGRAVITY_EFFORT = {"prime": "heavy", "core": "heavy", "lite": "heavy"}
# Vendor effort translation lives here, never in adapter code: Gemini 3.1 Pro has no normal
# (quality-first, so normal goes up to high), and Gemini 3.5 Flash has no heavy (so the lite
# tier's heavy lands on that family's ceiling instead of being sent and refused).
_DEFAULT_ANTIGRAVITY_TIER_EFFORTS = {
    "prime": {"normal": "high"},
    "lite": {"heavy": "medium"},
}
_DEFAULT_ANTIGRAVITY_PRINT_TIMEOUT_MINUTES = 120


@dataclass(frozen=True)
class AdapterSettings:
    """One vendor adapter's resolved settings: command, tier -> model map, and effort contract.

    Both resolution orders are fixed and live here so no caller has to branch on an adapter
    name to resolve an invocation:
    - abstract effort selection: task annotation > this adapter's tier default;
    - vendor effort translation: tier-specific > flat > built-in baseline.
    A loaded config always carries a default for every known tier (a stated
    ``default_effort`` table overrides per tier rather than replacing the built-in one), so a
    known tier resolves to an explicit portable effort instead of a vendor CLI default.
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
        """Choose the abstract effort: the task annotation wins, else this tier's default.

        None is returned only for a tier this adapter states no default for, which a loaded
        config never leaves open for prime/core/lite.
        """
        if task_effort:
            return task_effort
        return self.default_effort.get(model)

    def resolve_requested_effort(self, model: str,
                                 effort: str | None) -> str | None:
        """Translate the abstract effort by tier-specific > flat > built-in baseline."""
        if effort is None:
            return None
        return self.tier_efforts.get(model, {}).get(
            effort, self.efforts.get(effort, _EFFORT_BASELINE.get(effort, effort)))


@dataclass
class Config:
    root: Path                     # Project root = parent of .assent
    assent_dir: Path               # .assent directory (= where the config file lives)
    tasks_dir: Path                # Task folder (.assent/<tasks>)
    tasks_name: str                # Task folder name (= git branch prefix stem)
    stall_minutes: int = 30        # 0 = watchdog disabled
    retry_per_task: int = 1
    quota_poll_minutes: int = 30
    rotation_poll_minutes: int = 1
    adapter_name: str = "claude"
    adapter_names: tuple[str, ...] = field(default_factory=tuple)
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

    def __post_init__(self) -> None:
        """Keep the legacy adapter name and the normalized rotation list aligned."""
        if not self.adapter_names:
            self.adapter_names = (self.adapter_name,)
        else:
            self.adapter_names = tuple(self.adapter_names)
            self.adapter_name = self.adapter_names[0]

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


def _default_effort_map(section: dict, owner: str,
                        builtin: dict[str, str]) -> dict[str, str]:
    """Merge a stated ``default_effort`` table over the complete built-in tier defaults.

    Unlike ``models`` and ``efforts``, which a stated table replaces whole, this table states
    per-tier overrides: an absent, empty, or partial table keeps the built-in value for every
    omitted tier.  A tier therefore always selects an explicit portable effort, instead of an
    omission silently handing the decision to the vendor CLI's own default.
    """
    raw = _typed(section, owner, "default_effort", dict, None)
    merged = dict(builtin)
    if raw is None:
        return merged
    for model, eff in raw.items():
        if model not in _MODEL_TIERS:
            raise AssentError(
                f"[{owner}.default_effort] key {model!r} is not a valid model tier"
                f" ({'/'.join(sorted(_MODEL_TIERS))})")
        if eff not in _EFFORT_LEVELS:
            raise AssentError(
                f"[{owner}.default_effort] {model} = {eff!r} is not a valid effort"
                f" ({'/'.join(sorted(_EFFORT_LEVELS))})")
        merged[model] = eff
    return merged


def _parse_adapter_names(section: dict) -> tuple[str, ...]:
    """Parse the configured adapter name or ordered rotation list."""
    if "name" not in section:
        raw = "claude"
        names = (raw,)
    else:
        raw = section["name"]
        if isinstance(raw, str):
            # Preserve the legacy scalar path; adapter_settings() remains the
            # fail-closed validator for an unknown single adapter name.
            names = (raw,)
        elif isinstance(raw, list):
            if not raw:
                raise AssentError(
                    "Config [adapter].name must be a non-empty list of adapter names")
            for index, name in enumerate(raw):
                if not isinstance(name, str):
                    raise AssentError(
                        f"Config [adapter].name[{index}] must be a string")
            names = tuple(raw)
        else:
            raise AssentError(
                "Config [adapter].name has the wrong type: expected a string or"
                " a list of strings")

    if isinstance(raw, str):
        return names

    unknown = [name for name in names if name not in _ADAPTER_NAMES]
    if unknown:
        raise AssentError(
            f"Config [adapter].name contains unknown adapter name(s): {', '.join(repr(name) for name in unknown)}"
            f" (built in: {', '.join(sorted(_ADAPTER_NAMES))})")

    seen: set[str] = set()
    duplicates: list[str] = []
    for name in names:
        if name in seen and name not in duplicates:
            duplicates.append(name)
        seen.add(name)
    if duplicates:
        raise AssentError(
            f"Config [adapter].name contains duplicate adapter name(s): "
            f"{', '.join(repr(name) for name in duplicates)}")
    return names


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
    valid = isinstance(tasks_name, str) and bool(tasks_name)
    if valid:
        valid = (
            not any(char.isspace() for char in tasks_name)
            and not any(ord(char) < 0x20 or ord(char) == 0x7F
                        for char in tasks_name)
            and not any(char in _FOLDER_FORBIDDEN_CHARS for char in tasks_name)
            and tasks_name[0] not in "-."
            and ".." not in tasks_name
            and "@{" not in tasks_name
            and not tasks_name.endswith(".")
            and not tasks_name.casefold().endswith(".lock")
            and tasks_name.split(".", 1)[0].casefold()
            not in _WINDOWS_RESERVED_DEVICE_NAMES)
    if not valid:
        raise AssentError(
            f"{owner} = {tasks_name!r} is not a valid task folder name"
            f" ({_FOLDER_NAME_RULE})")


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
            _validate_tasks_name(entry.name, "Live task folder")
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
    adapter_names = _parse_adapter_names(adapter)
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
        rotation_poll_minutes=_typed(run, "[run]", "rotation_poll_minutes", int, 1),
        adapter_name=adapter_names[0],
        adapter_names=adapter_names,
        claude_command=_typed(claude, "[adapter.claude]", "command", str, "claude"),
        claude_extra_args=_str_list(claude, "[adapter.claude]", "extra_args",
                                    _DEFAULT_EXTRA_ARGS),
        claude_models=_str_map(claude, "adapter.claude", "models", _DEFAULT_MODELS),
        claude_default_effort=_default_effort_map(claude, "adapter.claude",
                                                  _DEFAULT_EFFORT),
        claude_efforts=claude_efforts,
        claude_tier_efforts=claude_tier_efforts,
        codex_command=_typed(codex, "[adapter.codex]", "command", str, "codex"),
        codex_extra_args=_str_list(codex, "[adapter.codex]", "extra_args",
                                   _DEFAULT_CODEX_EXTRA_ARGS),
        codex_models=_str_map(codex, "adapter.codex", "models",
                              _DEFAULT_CODEX_MODELS),
        codex_default_effort=_default_effort_map(codex, "adapter.codex",
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
        antigravity_default_effort=_default_effort_map(
            antigravity, "adapter.antigravity", _DEFAULT_ANTIGRAVITY_EFFORT),
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
    if cfg.rotation_poll_minutes < 1:
        raise AssentError("[run] rotation_poll_minutes must be at least 1")
    if cfg.receipt_refresh not in _RECEIPT_REFRESH_MODES:
        raise AssentError(
            f"[verification] receipt_refresh = {cfg.receipt_refresh!r} is not valid"
            f" ({'/'.join(sorted(_RECEIPT_REFRESH_MODES))})")
    if cfg.antigravity_print_timeout_minutes < 1:
        raise AssentError(
            "[adapter.antigravity] print_timeout_minutes must be at least 1")
    return cfg
