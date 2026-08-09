"""Loading assent.toml, and enumerating and validating task folders.

- Settings are layered: built-in defaults, then the user-wide
  ~/.assent/assent.toml plus its optional adapter.toml, then the optional
  project .assent/assent.toml plus its optional adapter.toml override.  Tables
  merge by key; scalars and arrays are replaced whole.
- The config path the caller supplies stays the project locator: the project
  root is the parent of the .assent directory that path lives in, whether or
  not the project file itself exists.
- The task folder name is supplied by the caller; the git branch prefix is
  that name plus "/".
- Fields not supplied fall back to defaults; an unknown top-level key is
  always an error (so a typo cannot fail silently).
- Absence is the only way to inherit.  TOML has no null, so a blank string is
  an explicit value, not a request to fall back: for settings that need useful
  text it is refused at load time, naming the dotted key and the file that
  stated it, rather than quietly reinstating a lower layer or a built-in.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

from assent import AssentError
from assent.agents import (Ability, Agent, ResolvedAgent,
                           resolve_agent as resolve_agent_role)
from assent.lockfile import LOCK_NAME
from assent.shared_paths import MANIFEST_LOCK_NAME, MANIFEST_NAME
from assent.user_home import user_config_path

_TOP_LEVEL_KEYS = {
    "watchdog", "run", "adapter", "verification", "auto_fix", "abilities",
    "agents", "workflow",
}

# The ordered settings layers, lowest priority first.  The built-in layer contributes no
# document of its own: several tables (models, efforts) are replaced whole rather than merged
# by key, so folding the built-in values into the merged document would resurrect defaults that
# a stated table means to drop.  The typed parsers below keep applying the built-in defaults,
# and BUILTIN_LAYER stays the provenance answer for every leaf no config file states.
BUILTIN_LAYER = "builtin"
USER_LAYER = "user"
PROJECT_LAYER = "project"
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

_DEFAULT_CODEX_EXTRA_ARGS = ["--sandbox", "danger-full-access"]
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


@dataclass(frozen=True)
class WorkflowPlanStep:
    """One resolved post-completion workflow step."""

    role: str
    adapter: str | None
    resolved_role: ResolvedAgent
    command: str | None = None
    extra_args: tuple[str, ...] = ()
    requested_model: str | None = None
    requested_effort: str | None = None

    @property
    def model(self) -> str | None:
        return self.resolved_role.model

    @property
    def effort(self) -> str | None:
        return self.resolved_role.effort

    @property
    def writes(self) -> bool:
        return self.resolved_role.writes

    @property
    def produces_verdict(self) -> bool:
        return self.resolved_role.produces_verdict

    @property
    def adapter_name(self) -> str:
        """Compatibility with call sites that name adapter selections explicitly."""
        return self.adapter


@dataclass(frozen=True)
class WorkflowTaskStep:
    """One parsed task-session role; execution is introduced by the next task."""

    role: str
    resolved_role: ResolvedAgent


@dataclass(frozen=True)
class ConfigSource:
    """One layer that contributed to the effective settings."""

    layer: str          # BUILTIN_LAYER / USER_LAYER / PROJECT_LAYER
    path: Path | None   # None for the built-in defaults, which have no file


@dataclass
class Config:
    root: Path                     # Project root = parent of .assent
    assent_dir: Path               # .assent directory (= where the config file lives)
    tasks_dir: Path                # Task folder (.assent/<tasks>)
    tasks_name: str                # Task folder name (= git branch prefix stem)
    stall_minutes: int = 0         # 0 = watchdog disabled
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
    receipt_refresh: str = "manual"  # "manual" = explicit verify only, "auto" = also at run closeout
    workflow_plan: tuple[WorkflowPlanStep, ...] = ()
    # None means today's implicit task session; an explicit empty tuple is distinct.
    workflow_task: tuple[WorkflowTaskStep, ...] | None = None
    abilities: dict[str, Ability] = field(default_factory=dict)
    agents: dict[str, Agent] = field(default_factory=dict)
    source_root: Path | None = None  # Original main worktree when running isolated; not from the config file
    # Where the effective settings came from: the layers that were present, lowest priority
    # first, and each stated leaf setting's dotted key mapped to the layer that stated it.
    sources: tuple[ConfigSource, ...] = ()
    provenance: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Keep the legacy adapter name and the normalized rotation list aligned."""
        if not self.adapter_names:
            self.adapter_names = (self.adapter_name,)
        else:
            self.adapter_names = tuple(self.adapter_names)
            self.adapter_name = self.adapter_names[0]
        if not self.sources:
            self.sources = (ConfigSource(BUILTIN_LAYER, None),)

    def source_of(self, key: str) -> str:
        """Name the layer a leaf setting came from, by its dotted key.

        BUILTIN_LAYER is the answer for any key no config file states, which is exactly
        when the built-in default is the value in effect.
        """
        return self.provenance.get(key, BUILTIN_LAYER)

    def resolve_agent(self, name: str) -> ResolvedAgent:
        """Return one role with its ordered ability definitions and derived flags."""
        return resolve_agent_role(name, self.agents, self.abilities)

    @property
    def auto_fix_review(self) -> tuple[WorkflowPlanStep, ...]:
        """Temporary internal compatibility alias for the renamed workflow plan."""
        return self.workflow_plan

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
    def auto_fix_state_rel(self) -> str:
        """The folder-local, derived auto-fix review state."""
        return self.git_rel(self.tasks_dir / "_auto_fix.toml")

    @property
    def workflow_state_rel(self) -> str:
        """The folder-local, derived workflow execution cursor."""
        return self.git_rel(self.tasks_dir / "_workflow.toml")

    @property
    def shared_paths_manifest_rel(self) -> str:
        """The local reviewed-shared-path cache; local memory, never project source."""
        return self.git_rel(self.assent_dir / MANIFEST_NAME)

    @property
    def shared_paths_lock_rel(self) -> str:
        return self.git_rel(self.assent_dir / MANIFEST_LOCK_NAME)

    @property
    def git_excludes(self) -> tuple[str, ...]:
        """Runtime artifacts: excluded from the clean check, scope check, and checkpoint commit."""
        return (self.runtime_log_rel, self.report_rel, self.lockfile_rel,
                self.verification_receipt_rel, self.auto_fix_state_rel,
                self.workflow_state_rel,
                self.shared_paths_manifest_rel, self.shared_paths_lock_rel)


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


def _known_keys(section: dict, owner: str, allowed: set[str]) -> None:
    """Refuse schema drift in a table whose complete key set is owned here."""
    unknown = sorted(set(section) - allowed)
    if unknown:
        raise AssentError(
            f"Config [{owner}] has unknown keys: {', '.join(unknown)}"
            f" (valid keys: {', '.join(sorted(allowed))})")


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


def _parse_abilities(section: dict) -> dict[str, Ability]:
    """Parse atomic ability definitions from the effective config layer."""
    abilities: dict[str, Ability] = {}
    required = ("prompt", "writes", "gate")
    allowed = {*required, "produces_verdict"}
    for name, value in section.items():
        owner = f"abilities.{name}"
        if not isinstance(value, dict):
            raise AssentError(f"Config [{owner}] must be a table, not a scalar")
        _known_keys(value, owner, allowed)
        missing = [key for key in required if key not in value]
        if missing:
            raise AssentError(
                f"Config [{owner}] is missing required keys: {', '.join(missing)}")
        abilities[name] = Ability(
            prompt=_typed(value, f"[{owner}]", "prompt", str, None),
            writes=_typed(value, f"[{owner}]", "writes", bool, None),
            gate=_typed(value, f"[{owner}]", "gate", bool, None),
            produces_verdict=_typed(
                value, f"[{owner}]", "produces_verdict", bool, False),
        )
    return abilities


def _parse_agents(section: dict, abilities: dict[str, Ability]) -> dict[str, Agent]:
    """Parse named roles and validate every ability reference."""
    agents: dict[str, Agent] = {}
    for name, value in section.items():
        owner = f"agents.{name}"
        if not isinstance(value, dict):
            raise AssentError(f"Config [{owner}] must be a table, not a scalar")
        _known_keys(value, owner, {"ability", "model", "effort"})
        ability_names = _str_list(value, f"[{owner}]", "ability", [])
        if not ability_names:
            raise AssentError(f"Config [{owner}].ability must be a non-empty array")
        for ability_name in ability_names:
            if ability_name not in abilities:
                raise AssentError(
                    f"Config [{owner}].ability references missing ability"
                    f" {ability_name!r}")
        model = _typed(value, f"[{owner}]", "model", str, None)
        effort = _typed(value, f"[{owner}]", "effort", str, None)
        if model is not None and model not in _MODEL_TIERS:
            raise AssentError(
                f"Config [{owner}].model = {model!r} is not a valid model tier"
                f" ({'/'.join(sorted(_MODEL_TIERS))})")
        if effort is not None and effort not in _EFFORT_LEVELS:
            raise AssentError(
                f"Config [{owner}].effort = {effort!r} is not a valid effort"
                f" ({'/'.join(sorted(_EFFORT_LEVELS))})")
        agents[name] = Agent(tuple(ability_names), model, effort)
    return agents


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


class _BlankGuard:
    """Refuse blank operational strings, naming the dotted key and the offending file.

    A higher layer that states ``key = ""`` must never look like an omission: it would
    otherwise hide a valid user value or silently select a built-in fallback, and the
    breakage would surface much later as an unusable adapter command line.  Enumerated
    settings keep their own domain checks; this guard only covers settings whose contract
    is "some useful text".
    """

    def __init__(self, provenance: dict[str, str],
                 sources: tuple[ConfigSource, ...]) -> None:
        self._provenance = provenance
        self._paths = {source.layer: source.path for source in sources}

    def text(self, value, dotted: str):
        if isinstance(value, str) and not value.strip():
            layer = self._provenance.get(dotted, BUILTIN_LAYER)
            path = self._paths.get(layer)
            where = f"the {layer} config file ({path})" if path else f"the {layer} defaults"
            raise AssentError(
                f"Config {dotted} is blank in {where}: a blank value is an explicit"
                " value, not a request to inherit; delete the key to fall back to the"
                " lower layer")
        return value

    def values(self, mapping: dict[str, str], prefix: str) -> dict[str, str]:
        """Apply the same rule to every value of a stated table."""
        for key, value in mapping.items():
            self.text(value, f"{prefix}.{key}")
        return mapping


def _parse_workflow_entries(section: dict, key: str, guard: "_BlankGuard",
                            agents: dict[str, Agent], abilities: dict[str, Ability]):
    """Parse one workflow array without inventing defaults for an omitted key."""
    if key not in section:
        return None
    raw = _typed(section, "[workflow]", key, list, None)
    entries = []
    for index, value in enumerate(raw):
        owner = f"workflow.{key}[{index}]"
        if not isinstance(value, dict):
            raise AssentError(f"Config {owner} must be an inline table")
        allowed = {"role", "adapter"} if key == "plan" else {"role"}
        _known_keys(value, owner, allowed)
        if "role" not in value:
            raise AssentError(f"Config {owner} is missing required key: role")
        role = guard.text(_typed(value, f"[{owner}]", "role", str, None),
                          f"workflow.{key}.{index}.role")
        resolved = resolve_agent_role(role, agents, abilities)
        if key == "task":
            entries.append(WorkflowTaskStep(role, resolved))
            continue
        adapter = value.get("adapter")
        if resolved.produces_verdict:
            if adapter is None:
                raise AssentError(
                    f"Config {owner} role {role!r} produces a verdict and requires adapter")
            adapter = guard.text(
                _typed(value, f"[{owner}]", "adapter", str, None),
                f"workflow.plan.{index}.adapter")
            if adapter not in _ADAPTER_NAMES:
                raise AssentError(
                    f"Config {owner}.adapter = {adapter!r} is not a registered adapter"
                    f" ({'/'.join(sorted(_ADAPTER_NAMES))})")
            if resolved.model is None or resolved.effort is None:
                raise AssentError(
                    f"Config {owner} verdict-producing role {role!r} must state model and effort")
            entries.append((role, adapter, resolved))
        else:
            if "adapter" in value:
                raise AssentError(
                    f"Config {owner} role {role!r} produces_verdict = false and must not state adapter")
            entries.append((role, None, resolved))
    return entries


def _parse_adapter_names(section: dict, guard: "_BlankGuard") -> tuple[str, ...]:
    """Parse the configured adapter name or ordered rotation list."""
    if "name" not in section:
        raw = "claude"
        names = (raw,)
    else:
        raw = section["name"]
        if isinstance(raw, str):
            # Preserve the legacy scalar path; adapter_settings() remains the
            # fail-closed validator for an unknown single adapter name.
            names = (guard.text(raw, "adapter.name"),)
        elif isinstance(raw, list):
            if not raw:
                raise AssentError(
                    "Config [adapter].name must be a non-empty list of adapter names")
            for index, name in enumerate(raw):
                if not isinstance(name, str):
                    raise AssentError(
                        f"Config [adapter].name[{index}] must be a string")
                guard.text(name, "adapter.name")
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


def _effort_maps(section: dict, owner: str, guard: "_BlankGuard",
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
                if not isinstance(requested, str):
                    raise AssentError(
                        f"Config {block} {effort} must be a non-empty string")
                guard.text(requested, f"{owner}.efforts.{key}.{effort}")
                tier_values[effort] = requested
            by_tier[key] = tier_values
            continue

        block = f"[{owner}.efforts]"
        if key not in _EFFORT_LEVELS:
            raise AssentError(
                f"Config {block} key {key!r} is not a valid effort"
                f" ({'/'.join(sorted(_EFFORT_LEVELS))})")
        if not isinstance(value, str):
            raise AssentError(f"Config {block} {key} must be a non-empty string")
        guard.text(value, f"{owner}.efforts.{key}")
        flat[key] = value
    return flat, by_tier


def validate_tasks_name(tasks_name: str, owner: str) -> None:
    """Validate a task folder name so it is safe to use as a git branch prefix.

    Public because folder-dependency parsing and receipt reading validate names
    this module never sees; ``owner`` names the caller's field so the refusal
    says which input was rejected.
    """
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


def _read_layer(path: Path, label: str) -> dict:
    """Parse and shallow-validate one config file, naming it in every refusal.

    Each layer is checked on its own so a broken file is reported with its own path
    instead of the other layer masking or inheriting the blame.
    """
    with open(path, "rb") as f:
        try:
            data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise AssentError(
                f"{label} config file is not valid TOML ({path}): {e}") from e

    unknown = sorted(set(data) - _TOP_LEVEL_KEYS)
    if unknown:
        raise AssentError(
            f"{label} config file ({path}) has unknown top-level keys:"
            f" {', '.join(unknown)}"
            f" (valid keys: {', '.join(sorted(_TOP_LEVEL_KEYS))})")
    auto_fix = data.get("auto_fix")
    if isinstance(auto_fix, dict) and "review" in auto_fix:
        raise AssentError(
            f"Config table [auto_fix.review] was removed; edit the layer file"
            f" that states it ({path}) and use [workflow].plan")
    return data


def _read_layer_with_adapter(path: Path, label: str) -> dict:
    """Read one assent layer and overlay its optional sibling adapter file."""
    data = _read_layer(path, label)
    adapter_path = path.with_name("adapter.toml")
    if adapter_path.is_file():
        data = _merge_layer(
            data, _read_layer(adapter_path, label), path, adapter_path)
    return data


def _shape(value: object) -> str:
    return "a table" if isinstance(value, dict) else "a value"


def _merge_layer(base: dict, overlay: dict, base_path: Path | None,
                 overlay_path: Path, prefix: str = "") -> dict:
    """Merge one higher layer over a lower one: tables by key, everything else replaced.

    A scalar or array is a leaf, so the higher layer replaces it whole rather than
    extending it.  A key that is a table in one file and a leaf in the other is refused
    with both file names, because no merge of the two can be the author's intent.
    ``base_path`` is only None while ``base`` is still empty, where no clash is possible.
    """
    merged = dict(base)
    for key, value in overlay.items():
        dotted = f"{prefix}{key}"
        if key in merged and isinstance(merged[key], dict) != isinstance(value, dict):
            raise AssentError(
                f"Config {dotted} has incompatible structures across config layers:"
                f" {_shape(merged[key])} in {base_path} but {_shape(value)} in"
                f" {overlay_path}")
        if isinstance(value, dict):
            merged[key] = _merge_layer(merged.get(key, {}), value, base_path,
                                       overlay_path, f"{dotted}.")
        else:
            merged[key] = value
    return merged


def _flatten(data: dict, prefix: str = "") -> dict[str, object]:
    """Map every leaf setting to its dotted key; scalars and arrays are leaves."""
    flat: dict[str, object] = {}
    for key, value in data.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{dotted}."))
        else:
            flat[dotted] = value
    return flat


def _provenance(merged: dict,
                layers: list[tuple[str, Path, dict]]) -> dict[str, str]:
    """Record, per effective leaf, the highest-priority layer that states it."""
    stated = [(layer, _flatten(data)) for layer, _path, data in layers]
    provenance: dict[str, str] = {}
    for key in _flatten(merged):
        for layer, flat in reversed(stated):
            if key in flat:
                provenance[key] = layer
                break
    return provenance


def _load_layers(path: str | Path
                 ) -> tuple[Path, dict, tuple[ConfigSource, ...], dict[str, str]]:
    """Assemble the effective config document from the layers that are present.

    The supplied path stays the project locator even when the project file is absent:
    an absent project file means "no project override", not "the project is
    uninitialized", so only an absent user config as well is a refusal.
    """
    project_path = Path(path).resolve()
    user_path = user_config_path().resolve()

    layers: list[tuple[str, Path, dict]] = []
    # The same file cannot be two layers; a user home pointed at this project keeps
    # its higher-priority project role.
    if user_path.is_file() and user_path != project_path:
        layers.append((USER_LAYER, user_path,
                       _read_layer_with_adapter(user_path, "User")))
    if project_path.is_file():
        layers.append((PROJECT_LAYER, project_path,
                       _read_layer_with_adapter(project_path, "Project")))
    if not layers:
        raise AssentError(
            f"Config file not found: neither the user config {user_path} nor the"
            f" project config {project_path} exists"
            " (not initialized yet? run assent init in the project root)")

    merged: dict = {}
    lower: Path | None = None
    for _layer, layer_path, data in layers:
        merged = _merge_layer(merged, data, lower, layer_path)
        lower = layer_path

    sources = (ConfigSource(BUILTIN_LAYER, None),
               *(ConfigSource(layer, layer_path)
                 for layer, layer_path, _data in layers))
    return project_path, merged, sources, _provenance(merged, layers)


def validate_config(path: str | Path) -> Path:
    """Validate the layered config and return the project ``.assent`` directory."""
    project_path, _data, _sources, _provenance_map = _load_layers(path)
    return project_path.parent


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
            validate_tasks_name(entry.name, "Live task folder")
            folders.append(entry.name)
    return sorted(folders)


def load_config(path: str | Path, folder: str) -> Config:
    """Load the config and build derived paths from the caller-supplied task folder name."""
    project_path, data, sources, provenance = _load_layers(path)
    validate_tasks_name(folder, "Command-line task folder")

    assent_dir = project_path.parent
    root = assent_dir.parent

    tasks_name = folder

    watchdog = _section(data, "watchdog")
    run = _section(data, "run")
    adapter = _section(data, "adapter")
    claude = _section(adapter, "claude") if "claude" in adapter else {}
    codex = _section(adapter, "codex") if "codex" in adapter else {}
    antigravity = (_section(adapter, "antigravity")
                   if "antigravity" in adapter else {})
    verification_section = _section(data, "verification")
    abilities = _parse_abilities(_section(data, "abilities"))
    agents = _parse_agents(_section(data, "agents"), abilities)
    auto_fix = _section(data, "auto_fix")
    _known_keys(auto_fix, "auto_fix", set())
    workflow = _section(data, "workflow")
    _known_keys(workflow, "workflow", {"plan", "task"})
    guard = _BlankGuard(provenance, sources)
    adapter_names = _parse_adapter_names(adapter, guard)
    raw_workflow_plan = _parse_workflow_entries(
        workflow, "plan", guard, agents, abilities)
    raw_workflow_task = _parse_workflow_entries(
        workflow, "task", guard, agents, abilities)
    claude_efforts, claude_tier_efforts = _effort_maps(
        claude, "adapter.claude", guard)
    codex_efforts, codex_tier_efforts = _effort_maps(
        codex, "adapter.codex", guard)
    antigravity_efforts, antigravity_tier_efforts = _effort_maps(
        antigravity, "adapter.antigravity", guard,
        default_tier=_DEFAULT_ANTIGRAVITY_TIER_EFFORTS)

    cfg = Config(
        root=root,
        assent_dir=assent_dir,
        tasks_dir=assent_dir / tasks_name,
        tasks_name=tasks_name,
        stall_minutes=_typed(watchdog, "[watchdog]", "stall_minutes", int, 0),
        retry_per_task=_typed(run, "[run]", "retry_per_task", int, 1),
        quota_poll_minutes=_typed(run, "[run]", "quota_poll_minutes", int, 30),
        rotation_poll_minutes=_typed(run, "[run]", "rotation_poll_minutes", int, 1),
        adapter_name=adapter_names[0],
        adapter_names=adapter_names,
        claude_command=guard.text(
            _typed(claude, "[adapter.claude]", "command", str, "claude"),
            "adapter.claude.command"),
        claude_extra_args=_str_list(claude, "[adapter.claude]", "extra_args",
                                    _DEFAULT_EXTRA_ARGS),
        claude_models=guard.values(
            _str_map(claude, "adapter.claude", "models", _DEFAULT_MODELS),
            "adapter.claude.models"),
        claude_default_effort=_default_effort_map(claude, "adapter.claude",
                                                  _DEFAULT_EFFORT),
        claude_efforts=claude_efforts,
        claude_tier_efforts=claude_tier_efforts,
        codex_command=guard.text(
            _typed(codex, "[adapter.codex]", "command", str, "codex"),
            "adapter.codex.command"),
        codex_extra_args=_str_list(codex, "[adapter.codex]", "extra_args",
                                   _DEFAULT_CODEX_EXTRA_ARGS),
        codex_models=guard.values(
            _str_map(codex, "adapter.codex", "models", _DEFAULT_CODEX_MODELS),
            "adapter.codex.models"),
        codex_default_effort=_default_effort_map(codex, "adapter.codex",
                                                 _DEFAULT_CODEX_EFFORT),
        codex_efforts=codex_efforts,
        codex_tier_efforts=codex_tier_efforts,
        antigravity_command=guard.text(
            _typed(antigravity, "[adapter.antigravity]", "command", str, "agy"),
            "adapter.antigravity.command"),
        antigravity_extra_args=_str_list(antigravity, "[adapter.antigravity]",
                                         "extra_args",
                                         _DEFAULT_ANTIGRAVITY_EXTRA_ARGS),
        antigravity_models=guard.values(
            _str_map(antigravity, "adapter.antigravity", "models",
                     _DEFAULT_ANTIGRAVITY_MODELS),
            "adapter.antigravity.models"),
        antigravity_default_effort=_default_effort_map(
            antigravity, "adapter.antigravity", _DEFAULT_ANTIGRAVITY_EFFORT),
        antigravity_efforts=antigravity_efforts,
        antigravity_tier_efforts=antigravity_tier_efforts,
        antigravity_print_timeout_minutes=_typed(
            antigravity, "[adapter.antigravity]", "print_timeout_minutes", int,
            _DEFAULT_ANTIGRAVITY_PRINT_TIMEOUT_MINUTES),
        receipt_refresh=_typed(verification_section, "[verification]",
                               "receipt_refresh", str, "manual"),
        abilities=abilities,
        agents=agents,
        sources=sources,
        provenance=provenance,
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
    steps: list[WorkflowPlanStep] = []
    for role, name, resolved in raw_workflow_plan or ():
        if name is None:
            steps.append(WorkflowPlanStep(role, None, resolved))
            continue
        adapter_settings = cfg.adapter_settings(name)
        assert resolved.model is not None and resolved.effort is not None
        requested_effort = adapter_settings.resolve_requested_effort(
            resolved.model, resolved.effort)
        if requested_effort is None:
            raise AssentError(
                f"[workflow].plan role {role!r} effort did not resolve to a requested value")
        steps.append(WorkflowPlanStep(
            role, name, resolved, adapter_settings.command,
            adapter_settings.extra_args,
            adapter_settings.resolve_model(resolved.model), requested_effort))
    if (steps and not any(step.produces_verdict for step in steps)
            and raw_workflow_task != []):
        raise AssentError(
            "Config [workflow].plan cannot open any session: no step's role produces a verdict")
    cfg.workflow_plan = tuple(steps)
    cfg.workflow_task = (None if raw_workflow_task is None
                         else tuple(raw_workflow_task))
    return cfg
