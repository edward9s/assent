"""Loading assent.toml, and enumerating and validating plans.

- Settings are layered: built-in defaults, then the user-wide
  ~/.assent/assent.toml plus its optional adapter.toml, then the optional
  project .assent/assent.toml plus its optional adapter.toml override.  Tables
  merge by key; scalars and arrays are replaced whole.
- The config path the caller supplies stays the project locator: the project
  root is the parent of the .assent directory that path lives in, whether or
  not the project file itself exists.
- The plan name is supplied by the caller; the git branch prefix is
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
from assent.agents import Ability, ResolvedRole, Role, resolve_role
from assent.lockfile import LOCK_NAME
from assent.modeling import (MODEL_TIERS, has_literal, literal_value,
                             parse_selection, split_selection)
from assent.ignored_dirs import MANIFEST_LOCK_NAME, MANIFEST_NAME
from assent.user_home import user_config_path

_TOP_LEVEL_KEYS = {
    "watchdog", "run", "adapter", "abilities", "roles", "workflow",
    "runtime_test",
}

# The ordered settings layers, lowest priority first.  The built-in layer contributes no
# document of its own: the models table is replaced whole rather than merged by key, so
# folding the built-in values into the merged document would resurrect defaults that a stated
# table means to drop.  The typed parsers below keep applying the built-in defaults,
# and BUILTIN_LAYER stays the provenance answer for every leaf no config file states.
BUILTIN_LAYER = "builtin"
USER_LAYER = "user"
PROJECT_LAYER = "project"
_ADAPTER_NAMES = {"claude", "codex", "antigravity"}

# A plan name becomes the first component of every Assent branch name.
# Keep this contract local and explicit instead of relying on a later Git command:
# the name must also remain usable as a Windows directory name.
_GIT_REF_FORBIDDEN_CHARS = frozenset("~^:?*[")
_WINDOWS_FORBIDDEN_CHARS = frozenset('<>"|')
_PLAN_FORBIDDEN_CHARS = (
    _GIT_REF_FORBIDDEN_CHARS | _WINDOWS_FORBIDDEN_CHARS | {"/", "\\"})
_WINDOWS_RESERVED_DEVICE_NAMES = frozenset({
    "con", "prn", "aux", "nul",
    "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
    "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
    # The superscript forms are also reserved device names on Windows.
    "com¹", "com²", "com³", "lpt¹", "lpt²", "lpt³",
})
_PLAN_NAME_RULE = (
    "must be non-empty, contain no whitespace, path separators, control characters, "
    "or Git-ref/Windows-forbidden characters, must not start with - or ., contain .. "
    "or @{, end with . or .lock, or use a reserved Windows device name; "
    "it also becomes the Git branch prefix")
_TASK_FILE_RE = re.compile(r"^t\d{3}_.+\.e\.toml$")

_DEFAULT_EXTRA_ARGS = ["--permission-mode", "acceptEdits"]
# There is deliberately no built-in tier -> model table.  A vendor model id names one
# release and is replaced whenever that vendor ships a new one, so shipping a default here
# would bake a value that expires into the code; every id lives in adapter.toml, where a
# human owns it.  An adapter that is actually selected must state all three tiers, and the
# refusal below names the missing ones while the config is still being read.

_DEFAULT_CODEX_EXTRA_ARGS = ["--sandbox", "danger-full-access"]

# A headless run cannot answer a permission prompt, and assent must not edit the user's own
# antigravity-cli settings.json, so unattended execution states the skip explicitly.
_DEFAULT_ANTIGRAVITY_EXTRA_ARGS = ["--dangerously-skip-permissions"]
_DEFAULT_ANTIGRAVITY_PRINT_TIMEOUT_MINUTES = 120


@dataclass(frozen=True)
class AdapterSettings:
    """One vendor adapter's resolved settings: command and its tier -> invocation map.

    A tier names one complete invocation, so resolution is a single lookup with nothing
    behind it: the mapped value already carries the exact vendor model and the exact vendor
    effort the CLI receives.  A literal selection bypasses the lookup and is read with the
    same ``model/effort`` grammar, which is what makes the two interchangeable at a call
    site without any caller branching on an adapter name.
    """

    name: str
    command: str
    extra_args: tuple[str, ...]
    models: dict[str, str]

    def resolve(self, model: str) -> tuple[str, str | None]:
        """Return the vendor model and effort for a portable tier or an exact literal.

        A ``None`` effort means the selection stated none, so the adapter deliberately
        omits its effort argument and inherits the vendor CLI's own default.
        """
        literal = literal_value(model)
        if literal is not None:
            return split_selection(literal, f"literal model {model!r}")
        alias = self.models.get(model)
        if alias is None:
            raise AssentError(
                f"model tier {model!r} is not in [adapter.{self.name}.models]; "
                f"check the plan file's suggested model or the config mapping")
        return split_selection(alias, f"[adapter.{self.name}.models] {model}")


@dataclass(frozen=True)
class WorkflowRoleStep:
    """One resolved plan, integration, or runtime-test role step."""

    role: str
    adapters: tuple[str, ...]
    resolved_role: ResolvedRole

    @property
    def model(self) -> str | None:
        return self.resolved_role.model

    @property
    def writes(self) -> bool:
        return self.resolved_role.writes


@dataclass(frozen=True)
class WorkflowTaskStep:
    """One parsed task-session role; execution is introduced by the next task."""

    role: str
    resolved_role: ResolvedRole
    adapters: tuple[str, ...] | None = None

    @property
    def writes(self) -> bool:
        return self.resolved_role.writes


@dataclass(frozen=True)
class WorkflowActionStep:
    """One narrowly supported scheduler-owned workflow action."""

    action: str


@dataclass(frozen=True)
class ConfigSource:
    """One layer that contributed to the effective settings."""

    layer: str          # BUILTIN_LAYER / USER_LAYER / PROJECT_LAYER
    path: Path | None   # None for the built-in defaults, which have no file


@dataclass
class Config:
    root: Path                     # Project root = parent of .assent
    assent_dir: Path               # .assent directory (= where the config file lives)
    tasks_dir: Path                # Plan directory (.assent/<tasks>)
    tasks_name: str                # Plan name (= git branch prefix stem)
    workflow_task: tuple[WorkflowTaskStep | WorkflowActionStep, ...]
    workflow_preflight: tuple[
        WorkflowRoleStep | WorkflowActionStep, ...] = ()
    stall_minutes: int = 0         # 0 = watchdog disabled
    quota_poll_minutes: int = 30
    rotation_poll_minutes: int = 1
    adapter_names: tuple[str, ...] = field(default_factory=tuple)
    claude_command: str = "claude"
    claude_extra_args: list[str] = field(
        default_factory=lambda: list(_DEFAULT_EXTRA_ARGS))
    claude_models: dict[str, str] = field(default_factory=dict)
    codex_command: str = "codex"
    codex_extra_args: list[str] = field(
        default_factory=lambda: list(_DEFAULT_CODEX_EXTRA_ARGS))
    codex_models: dict[str, str] = field(default_factory=dict)
    antigravity_command: str = "agy"
    antigravity_extra_args: list[str] = field(
        default_factory=lambda: list(_DEFAULT_ANTIGRAVITY_EXTRA_ARGS))
    antigravity_models: dict[str, str] = field(default_factory=dict)
    # Print mode has its own upstream wait limit, far shorter than a task session; the
    # adapter always states one instead of inheriting the CLI default.
    antigravity_print_timeout_minutes: int = _DEFAULT_ANTIGRAVITY_PRINT_TIMEOUT_MINUTES
    workflow_plan: tuple[WorkflowRoleStep | WorkflowActionStep, ...] = ()
    workflow_integration: tuple[WorkflowRoleStep | WorkflowActionStep, ...] = ()
    runtime_test_commands: tuple[str, ...] | None = None
    workflow_runtime_test: tuple[
        WorkflowRoleStep | WorkflowActionStep, ...] | None = None
    abilities: dict[str, Ability] = field(default_factory=dict)
    roles: dict[str, Role] = field(default_factory=dict)
    config_path: Path | None = None
    source_root: Path | None = None  # Original main worktree when running isolated; not from the config file
    # Where the effective settings came from: the layers that were present, lowest priority
    # first, and each stated leaf setting's dotted key mapped to the layer that stated it.
    sources: tuple[ConfigSource, ...] = ()
    provenance: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Normalize the one adapter-candidate sequence."""
        if not self.workflow_task:
            raise AssentError("Effective [workflow].task must not be empty")
        if not self.adapter_names:
            self.adapter_names = ("claude",)
        else:
            self.adapter_names = tuple(self.adapter_names)
        if not self.sources:
            self.sources = (ConfigSource(BUILTIN_LAYER, None),)

    def source_of(self, key: str) -> str:
        """Name the layer a leaf setting came from, by its dotted key.

        BUILTIN_LAYER is the answer for any key no config file states, which is exactly
        when the built-in default is the value in effect.
        """
        return self.provenance.get(key, BUILTIN_LAYER)

    def resolve_role(self, name: str) -> ResolvedRole:
        """Return one role with its ordered ability definitions and derived flags."""
        return resolve_role(name, self.roles, self.abilities)

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
                models=self.claude_models)
        if name == "codex":
            return AdapterSettings(
                name="codex", command=self.codex_command,
                extra_args=tuple(self.codex_extra_args),
                models=self.codex_models)
        if name == "antigravity":
            return AdapterSettings(
                name="antigravity", command=self.antigravity_command,
                extra_args=tuple(self.antigravity_extra_args),
                models=self.antigravity_models)
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
    def workflow_state_rel(self) -> str:
        """The plan-local, derived workflow execution cursor."""
        return self.git_rel(self.tasks_dir / "_workflow.toml")

    @property
    def selection_workflow_state_rel(self) -> str:
        """The project-level, derived exact-selection workflow cursor."""
        return self.git_rel(self.assent_dir / "_integration_workflow.toml")

    @property
    def runtime_test_workflow_state_rel(self) -> str:
        """The plan-owned runtime workflow cursor."""
        return self.git_rel(self.tasks_dir / "_runtime_test_workflow.toml")

    @property
    def main_runtime_test_workflow_state_rel(self) -> str:
        """The main-owned runtime workflow cursor."""
        return self.git_rel(self.assent_dir / "_runtime_test_workflow.toml")

    @property
    def plan_workflow_step_count(self) -> int:
        """Count the effective plan steps, including its implied final action."""
        if not self.workflow_plan:
            return 0
        return len(self.workflow_plan) + (
            0 if isinstance(self.workflow_plan[-1], WorkflowActionStep) else 1)

    @property
    def ignored_dirs_manifest_rel(self) -> str:
        """The local reviewed-ignored-directory cache; local memory, never project source."""
        return self.git_rel(self.assent_dir / MANIFEST_NAME)

    @property
    def ignored_dirs_lock_rel(self) -> str:
        return self.git_rel(self.assent_dir / MANIFEST_LOCK_NAME)

    @property
    def git_excludes(self) -> tuple[str, ...]:
        """Runtime artifacts: excluded from the clean check, scope check, and checkpoint commit."""
        return (self.runtime_log_rel, self.report_rel, self.lockfile_rel,
                self.verification_receipt_rel,
                self.workflow_state_rel, self.selection_workflow_state_rel,
                self.runtime_test_workflow_state_rel,
                self.main_runtime_test_workflow_state_rel,
                self.ignored_dirs_manifest_rel, self.ignored_dirs_lock_rel)


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


def _model_map(section: dict, owner: str, guard: "_BlankGuard") -> dict[str, str]:
    """Parse one adapter's tier -> ``model/effort`` table, fail-closed on any bad entry.

    There is no built-in table to fall back to, so an absent one stays empty here and the
    refusal is raised later, against the adapters actually selected.  Both halves of every
    stated entry are checked now rather than at invocation time, so a malformed selection
    never surfaces as a rejected vendor command line mid-run.  The blank guard runs first so
    an explicitly empty value keeps its own dotted-key-and-layer refusal instead of being
    reported as a grammar error.
    """
    raw = _str_map(section, owner, "models", {})
    for tier, selection in raw.items():
        if tier not in MODEL_TIERS:
            raise AssentError(
                f"Config [{owner}.models] key {tier!r} is not a valid model tier"
                f" ({'/'.join(sorted(MODEL_TIERS))})")
        guard.text(selection, f"{owner}.models.{tier}")
        split_selection(selection, f"Config [{owner}.models] {tier}")
    return raw


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
    required = ("prompt", "writes")
    allowed = set(required)
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
        )
    return abilities


def _parse_roles(section: dict, abilities: dict[str, Ability]) -> dict[str, Role]:
    """Parse named roles and validate every ability reference."""
    roles: dict[str, Role] = {}
    for name, value in section.items():
        owner = f"roles.{name}"
        if not isinstance(value, dict):
            raise AssentError(f"Config [{owner}] must be a table, not a scalar")
        _known_keys(value, owner, {"ability", "model"})
        ability_names = _str_list(value, f"[{owner}]", "ability", [])
        if not ability_names:
            raise AssentError(f"Config [{owner}].ability must be a non-empty array")
        for ability_name in ability_names:
            if ability_name not in abilities:
                raise AssentError(
                    f"Config [{owner}].ability references missing ability"
                    f" {ability_name!r}")
        model = _typed(value, f"[{owner}]", "model", str, None)
        if model is not None:
            model = parse_selection(model, f"Config [{owner}]")
        roles[name] = Role(tuple(ability_names), model)
    return roles


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


def _runtime_test_commands(
        section: dict, guard: _BlankGuard) -> tuple[str, ...] | None:
    """Normalize the external singular ``command`` key to ordered commands."""
    if "command" not in section:
        return None
    raw = section["command"]
    if isinstance(raw, str):
        commands = (raw,)
    elif isinstance(raw, list):
        if not raw:
            raise AssentError("Config runtime_test.command must not be empty")
        if not all(isinstance(item, str) for item in raw):
            raise AssentError(
                "Config runtime_test.command array must contain only strings")
        commands = tuple(raw)
    else:
        raise AssentError(
            "Config [runtime_test] command must be a string or an array of strings")
    for command in commands:
        guard.text(command, "runtime_test.command")
    return commands


def _workflow_adapter_candidates(value: dict, owner: str, guard: "_BlankGuard"
                                 ) -> tuple[str, ...] | None:
    """Normalize an optional workflow adapter string/list without choosing defaults."""
    configured = value.get("adapter")
    if configured is None:
        return None
    if isinstance(configured, str):
        adapters = (guard.text(configured, f"{owner}.adapter"),)
    elif isinstance(configured, list):
        if not configured:
            raise AssentError(
                f"Config {owner}.adapter must be a non-empty string or list")
        if any(not isinstance(name, str) for name in configured):
            raise AssentError(
                f"Config {owner}.adapter list entries must be strings")
        adapters = tuple(
            guard.text(name, f"{owner}.adapter") for name in configured)
        if len(set(adapters)) != len(adapters):
            raise AssentError(
                f"Config {owner}.adapter list must not contain duplicates")
    else:
        raise AssentError(
            f"Config {owner}.adapter must be a string or non-empty list")
    unknown = [name for name in adapters if name not in _ADAPTER_NAMES]
    if unknown:
        raise AssentError(
            f"Config {owner}.adapter contains {unknown[0]!r}, which is not a "
            f"registered adapter ({'/'.join(sorted(_ADAPTER_NAMES))})")
    return adapters


def _require_single_literal_adapter(model: str | None,
                                    adapters: tuple[str, ...], owner: str) -> None:
    """A vendor literal is meaningful only for one exact adapter."""
    if has_literal(model) and len(adapters) != 1:
        raise AssentError(
            f"Config {owner} uses a literal model and must resolve "
            "to exactly one adapter"
        )


def _parse_workflow_entries(section: dict, key: str, guard: "_BlankGuard",
                            roles: dict[str, Role], abilities: dict[str, Ability]):
    """Parse one workflow array without inventing defaults for an omitted key."""
    if key not in section:
        return None
    raw = _typed(section, "[workflow]", key, list, None)
    entries = []
    for index, value in enumerate(raw):
        owner = f"workflow.{key}[{index}]"
        if not isinstance(value, dict):
            raise AssentError(f"Config {owner} must be an inline table")
        has_role = "role" in value
        has_action = "action" in value
        if has_role == has_action:
            raise AssentError(
                f"Config {owner} must contain exactly one of role or action")
        if has_action:
            _known_keys(value, owner, {"action"})
            action = guard.text(
                _typed(value, f"[{owner}]", "action", str, None),
                f"workflow.{key}.{index}.action")
            allowed_actions = {"preflight": {"check"},
                               "task": {"focused_test"},
                               "plan": {"focused_sweep"},
                               "integration": {"full_verify"},
                               "runtime_test": {"runtime_test"}}[key]
            if action not in {"focused_test", "focused_sweep", "full_verify",
                              "runtime_test", "check"}:
                raise AssentError(f"Config {owner} has unknown action {action!r}")
            if action not in allowed_actions:
                valid = "/".join(sorted(allowed_actions))
                raise AssentError(
                    f"Config {owner} action {action!r} is not valid under"
                    f" [workflow].{key} (valid action: {valid})")
            entries.append(WorkflowActionStep(action))
            continue
        allowed = {"role", "adapter", "model"}
        _known_keys(value, owner, allowed)
        role = guard.text(_typed(value, f"[{owner}]", "role", str, None),
                          f"workflow.{key}.{index}.role")
        resolved = resolve_role(role, roles, abilities)
        model = _typed(value, f"[{owner}]", "model", str, None)
        if model is not None:
            resolved = replace(
                resolved, model=parse_selection(model, f"Config [{owner}]"))
        adapters = _workflow_adapter_candidates(value, owner, guard)
        if key == "task":
            entries.append(WorkflowTaskStep(role, resolved, adapters))
            continue
        # A non-task role answers for a whole unit, not for one task, so it has
        # nothing to inherit a model from: an adapter only translates a tier it
        # is given.  Requiring the model here keeps the omission a config error
        # `check` reports rather than a silent inheritance from a task.
        if resolved.model is None:
            raise AssentError(
                f"Config {owner} role {role!r} must state model")
        entries.append((role, adapters, resolved))
    return entries


def _validate_runtime_test_entries(entries) -> None:
    """Require a runtime-test action before and after every writable role."""
    if entries is None:
        return
    if not entries:
        raise AssentError(
            "Config [workflow].runtime_test must not be empty")
    if not isinstance(entries[0], WorkflowActionStep):
        raise AssentError(
            "Config [workflow].runtime_test must start with an action")
    if not isinstance(entries[-1], WorkflowActionStep):
        raise AssentError(
            "Config [workflow].runtime_test must end with an action")
    for index, entry in enumerate(entries):
        if index % 2 == 0:
            if not isinstance(entry, WorkflowActionStep):
                raise AssentError(
                    "Config [workflow].runtime_test must strictly alternate"
                    " runtime_test actions and writable roles")
            continue
        if isinstance(entry, WorkflowActionStep):
            raise AssentError(
                "Config [workflow].runtime_test must strictly alternate"
                " runtime_test actions and writable roles")
        if not entry[2].writes:
            raise AssentError(
                f"Config [workflow].runtime_test[{index}] role {entry[0]!r}"
                " must be writable")


def _validate_preflight_entries(entries) -> None:
    """Require checks around every writable preflight repair role."""
    if entries is None:
        return
    if not entries:
        raise AssentError(
            "Config [workflow].preflight must not be empty")
    if not isinstance(entries[0], WorkflowActionStep):
        raise AssentError(
            "Config [workflow].preflight must start with an action")
    if not isinstance(entries[-1], WorkflowActionStep):
        raise AssentError(
            "Config [workflow].preflight must end with an action")
    for index, entry in enumerate(entries):
        if index % 2 == 0:
            if not isinstance(entry, WorkflowActionStep):
                raise AssentError(
                    "Config [workflow].preflight must strictly alternate"
                    " check actions and writable roles")
            continue
        if isinstance(entry, WorkflowActionStep):
            raise AssentError(
                "Config [workflow].preflight must strictly alternate"
                " check actions and writable roles")
        if not entry[2].writes:
            raise AssentError(
                f"Config [workflow].preflight[{index}] role {entry[0]!r}"
                " must be writable")


def _resolve_plan_steps(
        cfg: Config, raw_steps, owner: str
) -> tuple[WorkflowRoleStep | WorkflowActionStep, ...]:
    """Resolve one plan, integration, or runtime-test role layer."""
    steps: list[WorkflowRoleStep | WorkflowActionStep] = []
    for raw in raw_steps or ():
        if isinstance(raw, WorkflowActionStep):
            steps.append(raw)
            continue
        role, configured_names, resolved = raw
        names = configured_names or cfg.adapter_names
        _require_single_literal_adapter(
            resolved.model, names, f"[workflow].{owner} role {role!r}")
        steps.append(WorkflowRoleStep(role, names, resolved))
    return tuple(steps)


def _workflow_bound_adapters(*raw_entry_lists) -> set[str]:
    """Every adapter a workflow entry binds itself to, across all layers.

    Entries arrive in the two shapes ``_parse_workflow_entries`` produces: a task step
    object, or a ``(role, adapters, resolved)`` tuple for non-task layers.  Scheduler
    actions bind nothing.
    """
    names: set[str] = set()
    for entries in raw_entry_lists:
        for entry in entries or ():
            if isinstance(entry, WorkflowActionStep):
                continue
            adapters = (entry.adapters if isinstance(entry, WorkflowTaskStep)
                        else entry[1])
            names.update(adapters or ())
    return names


def _require_complete_models(cfg: Config, bound: set[str]) -> None:
    """Refuse a reachable adapter whose tier -> invocation table is absent or partial.

    Nothing supplies these values but the config file, so an omission cannot silently
    resolve to something plausible.  Required of the rotation and of every adapter a
    workflow entry binds itself to -- not the rotation alone: a task-layer role may
    inherit its tier from the task, so its selection is resolved during the run rather
    than while the config is read, and an unmapped adapter bound there would otherwise
    pass ``check`` and fail only after a worktree and a checkpoint already existed.  An
    adapter nothing reaches stays optional, so a project is never forced to name models
    for a vendor it does not use.
    """
    for name in sorted(set(cfg.adapter_names) | bound):
        if name not in _ADAPTER_NAMES:
            continue          # an unknown name has its own refusal, at its own layer
        models = cfg.adapter_settings(name).models
        missing = sorted(MODEL_TIERS - set(models))
        if missing:
            raise AssentError(
                f"Config [adapter.{name}.models] must state every model tier"
                f" ({'/'.join(sorted(MODEL_TIERS))}); missing:"
                f" {', '.join(missing)}. Assent ships no built-in model ids because a"
                " vendor model id names one release; state them in adapter.toml")


def _validate_task_literal_adapters(
        cfg: Config,
        steps: list[WorkflowTaskStep | WorkflowActionStep] | None) -> None:
    """Validate adapter binding for literal values stated by task roles."""
    for index, step in enumerate(steps or ()):
        if isinstance(step, WorkflowActionStep):
            continue
        _require_single_literal_adapter(
            step.resolved_role.model,
            step.adapters or cfg.adapter_names,
            f"[workflow].task[{index}] role {step.role!r}")


def _parse_adapter_names(section: dict, guard: "_BlankGuard") -> tuple[str, ...]:
    """Parse the configured adapter name or ordered rotation list."""
    if "name" not in section:
        raw = "claude"
        names = (raw,)
    else:
        raw = section["name"]
        if isinstance(raw, str):
            # A scalar is the concise spelling of a one-adapter rotation.
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


def validate_tasks_name(tasks_name: str, owner: str) -> None:
    """Validate a plan name so it is safe to use as a git branch prefix.

    Public because plan-dependency parsing and receipt reading validate names
    this module never sees; ``owner`` names the caller's field so the refusal
    says which input was rejected.
    """
    valid = isinstance(tasks_name, str) and bool(tasks_name)
    if valid:
        valid = (
            not any(char.isspace() for char in tasks_name)
            and not any(ord(char) < 0x20 or ord(char) == 0x7F
                        for char in tasks_name)
            and not any(char in _PLAN_FORBIDDEN_CHARS for char in tasks_name)
            and tasks_name[0] not in "-."
            and ".." not in tasks_name
            and "@{" not in tasks_name
            and not tasks_name.endswith(".")
            and not tasks_name.casefold().endswith(".lock")
            and tasks_name.split(".", 1)[0].casefold()
            not in _WINDOWS_RESERVED_DEVICE_NAMES)
    if not valid:
        raise AssentError(
            f"{owner} = {tasks_name!r} is not a valid plan name"
            f" ({_PLAN_NAME_RULE})")


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


def list_task_plans(assent_dir: str | Path) -> list[str]:
    """List plans that contain a formal task file, sorted lexicographically."""
    assent_dir = Path(assent_dir)
    if not assent_dir.is_dir():
        return []
    plan_names = []
    for entry in assent_dir.iterdir():
        if (not entry.is_dir() or entry.name == "__pycache__"
                or entry.name.startswith("_")):
            continue
        if any(child.is_file() and _TASK_FILE_RE.match(child.name)
               for child in entry.iterdir()):
            validate_tasks_name(entry.name, "Live plan")
            plan_names.append(entry.name)
    return sorted(plan_names)


def load_config(path: str | Path, plan_name: str) -> Config:
    """Load the config and build derived paths from the caller-supplied plan name."""
    project_path, data, sources, provenance = _load_layers(path)
    validate_tasks_name(plan_name, "Command-line plan")

    assent_dir = project_path.parent
    root = assent_dir.parent

    tasks_name = plan_name

    watchdog = _section(data, "watchdog")
    run = _section(data, "run")
    _known_keys(run, "run", {"quota_poll_minutes", "rotation_poll_minutes"})
    adapter = _section(data, "adapter")
    claude = _section(adapter, "claude") if "claude" in adapter else {}
    codex = _section(adapter, "codex") if "codex" in adapter else {}
    antigravity = (_section(adapter, "antigravity")
                   if "antigravity" in adapter else {})
    abilities = _parse_abilities(_section(data, "abilities"))
    roles = _parse_roles(_section(data, "roles"), abilities)
    workflow = _section(data, "workflow")
    _known_keys(workflow, "workflow",
                {"preflight", "plan", "integration", "task", "runtime_test"})
    runtime_test = _section(data, "runtime_test")
    _known_keys(runtime_test, "runtime_test", {"command"})
    guard = _BlankGuard(provenance, sources)
    adapter_names = _parse_adapter_names(adapter, guard)
    raw_workflow_preflight = _parse_workflow_entries(
        workflow, "preflight", guard, roles, abilities)
    raw_workflow_plan = _parse_workflow_entries(
        workflow, "plan", guard, roles, abilities)
    raw_workflow_task = _parse_workflow_entries(
        workflow, "task", guard, roles, abilities)
    raw_workflow_integration = _parse_workflow_entries(
        workflow, "integration", guard, roles, abilities)
    raw_workflow_runtime_test = _parse_workflow_entries(
        workflow, "runtime_test", guard, roles, abilities)
    _validate_preflight_entries(raw_workflow_preflight)
    _validate_runtime_test_entries(raw_workflow_runtime_test)
    if raw_workflow_task is None:
        raise AssentError(
            "Config [workflow].task is required in the effective settings; "
            "an omitted project override may inherit it only when a lower "
            "configuration layer defines it")
    if not raw_workflow_task:
        raise AssentError(
            "Config [workflow].task must not be empty; state at least one "
            "role or { action = \"focused_test\" }")
    cfg = Config(
        root=root,
        assent_dir=assent_dir,
        tasks_dir=assent_dir / tasks_name,
        tasks_name=tasks_name,
        workflow_task=tuple(raw_workflow_task),
        stall_minutes=_typed(watchdog, "[watchdog]", "stall_minutes", int, 0),
        quota_poll_minutes=_typed(run, "[run]", "quota_poll_minutes", int, 30),
        rotation_poll_minutes=_typed(run, "[run]", "rotation_poll_minutes", int, 1),
        adapter_names=adapter_names,
        claude_command=guard.text(
            _typed(claude, "[adapter.claude]", "command", str, "claude"),
            "adapter.claude.command"),
        claude_extra_args=_str_list(claude, "[adapter.claude]", "extra_args",
                                    _DEFAULT_EXTRA_ARGS),
        claude_models=_model_map(claude, "adapter.claude", guard),
        codex_command=guard.text(
            _typed(codex, "[adapter.codex]", "command", str, "codex"),
            "adapter.codex.command"),
        codex_extra_args=_str_list(codex, "[adapter.codex]", "extra_args",
                                   _DEFAULT_CODEX_EXTRA_ARGS),
        codex_models=_model_map(codex, "adapter.codex", guard),
        antigravity_command=guard.text(
            _typed(antigravity, "[adapter.antigravity]", "command", str, "agy"),
            "adapter.antigravity.command"),
        antigravity_extra_args=_str_list(antigravity, "[adapter.antigravity]",
                                         "extra_args",
                                         _DEFAULT_ANTIGRAVITY_EXTRA_ARGS),
        antigravity_models=_model_map(
            antigravity, "adapter.antigravity", guard),
        antigravity_print_timeout_minutes=_typed(
            antigravity, "[adapter.antigravity]", "print_timeout_minutes", int,
            _DEFAULT_ANTIGRAVITY_PRINT_TIMEOUT_MINUTES),
        runtime_test_commands=_runtime_test_commands(runtime_test, guard),
        abilities=abilities,
        roles=roles,
        config_path=project_path,
        sources=sources,
        provenance=provenance,
    )

    if cfg.stall_minutes < 0:
        raise AssentError("[watchdog] stall_minutes must not be negative (0 = disabled)")
    if cfg.quota_poll_minutes < 1:
        raise AssentError("[run] quota_poll_minutes must be at least 1")
    if cfg.rotation_poll_minutes < 1:
        raise AssentError("[run] rotation_poll_minutes must be at least 1")
    if cfg.antigravity_print_timeout_minutes < 1:
        raise AssentError(
            "[adapter.antigravity] print_timeout_minutes must be at least 1")
    _require_complete_models(cfg, _workflow_bound_adapters(
        raw_workflow_preflight, raw_workflow_task, raw_workflow_plan,
        raw_workflow_integration,
        raw_workflow_runtime_test))
    cfg.workflow_preflight = _resolve_plan_steps(
        cfg, raw_workflow_preflight, "preflight")
    plan_steps = _resolve_plan_steps(cfg, raw_workflow_plan, "plan")
    integration_steps = _resolve_plan_steps(
        cfg, raw_workflow_integration, "integration")
    cfg.workflow_plan = plan_steps
    _validate_task_literal_adapters(cfg, raw_workflow_task)
    cfg.workflow_integration = integration_steps
    cfg.workflow_runtime_test = (
        None if raw_workflow_runtime_test is None else
        _resolve_plan_steps(cfg, raw_workflow_runtime_test, "runtime_test"))
    return cfg


def load_main_runtime_config(path: str | Path) -> Config:
    """Load project settings for the main runtime-test candidate."""
    cfg = load_config(path, "main")
    cfg.tasks_dir = cfg.assent_dir
    return cfg
