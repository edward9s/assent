"""Plan parsing and writeback (the format contract lives in templates/format.md).

- Task file: tNNN_name.e.toml, header fields strictly validated, unknown keys
  are an error; if a plan still has a legacy tNNN_name.toml, parsing
  is refused and the caller must move it.
- Journal file: tNNN_name.r.toml, append-only [[entry]] blocks.
- There are exactly two scheduler-owned task-file writes: set_status replaces
  the status line precisely, and add_scope_entries appends reviewed exact paths
  to the one-line scope array. Both leave every unrelated byte untouched and
  validate the result before it becomes authoritative.
"""
from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from assent import AssentError
from assent.modeling import parse_tier
from assent.verification_common import atomic_write_text

_FORMAL_FILENAME_RE = re.compile(r"^t(\d{3})_(.+)\.e\.toml$")
_RETIRED_FILENAME_RE = re.compile(r"^t\d{3}_.+\.toml$")
_ID_RE = re.compile(r"^t\d{3}$")
_STATUS_VALUES = {"TODO", "WIP", "DONE", "BLOCKED", "SKIP"}
_KNOWN_KEYS = {"title", "deps", "model", "status", "scope", "verify",
               "goal", "behavior", "acceptance", "notes", "workflow"}
# Journal identities a new write may claim.  Reading is deliberately unrestricted, so a
# journal written before an adapter existed (including the retired catch-all by = "ai")
# still parses and still reports.
_ENTRY_BY = {"antigravity", "codex", "claude", "scheduler"}
_ENTRY_AGENT = {"antigravity", "codex", "claude"}
# Status line: leading status = "VALUE" (tolerates leading whitespace and a trailing comment)
_STATUS_LINE_RE = re.compile(
    r'^(\s*status\s*=\s*")(TODO|WIP|DONE|BLOCKED|SKIP)("\s*(?:#.*)?)$')
_SCOPE_LINE_RE = re.compile(
    r'^(?P<prefix>\s*scope\s*=\s*)\[(?P<body>.*)\]'
    r'(?P<suffix>\s*(?:#.*)?)$')
# The project's own full verifier, in the spellings a verify command can reach it
# by.  A task's verify is the session's focused gate; the full verifier runs outside
# every AI session -- a human starts it, or the scheduler runs it once at the end of
# a whole run.  Naming it here would make every task re-run the whole suite, and on
# a slow project it outlives what a session can wait for at all.
_FULL_VERIFIER_RE = re.compile(r'\.assent[\\/]verify\.py\b')
WORKFLOW_STATE_NAME = "_workflow.toml"
INTEGRATION_WORKFLOW_STATE_NAME = "_integration_workflow.toml"
_WORKFLOW_ACTIONS = {"focused_test"}
_ACTION_STATUS_VALUES = {"PASSED", "FAILED", "STALE"}
_SELECTION_REPAIR_PHASES = {
    "NONE", "NEEDS_REPAIR", "REPAIRING", "MERGED", "RECHECK"}
_MAX_ACTION_EVIDENCE_ITEMS = 20
_MAX_ACTION_EVIDENCE_CHARS = 4096


@dataclass(frozen=True)
class TaskWorkflowAction:
    """One scheduler-owned action in a task-file workflow override."""

    action: str


class TaskWorkflowRole(str):
    """A tagged task-workflow role that remains compatible with role-name strings."""

    @property
    def role(self) -> str:
        return str(self)


@dataclass(frozen=True)
class WorkflowState:
    """Deletable cursor for the currently executing workflow accountability unit."""

    unit: str
    task_id: str
    step_index: int
    started: bool
    base_ref: str = ""
    focused_evidence: tuple[str, ...] = ()
    action: str = ""
    action_status: str = ""
    action_source_tree: str = ""
    action_exit_code: int = 0
    action_evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class SelectionWorkflowState:
    """Deletable cursor for one exact, source-bound plan selection."""

    plan_names: tuple[str, ...]
    target_ref: str
    target_commit: str
    source_commits: tuple[str, ...]
    step_index: int
    action: str = ""
    action_status: str = ""
    action_candidate_tree: str = ""
    action_exit_code: int = 0
    action_evidence: tuple[str, ...] = ()
    verification_script_sha256: str = ""
    shared_inputs_sha256: str = ""
    repair_phase: str = "NONE"


@dataclass
class Task:
    id: str                        # Filename prefix, e.g. "t001" (id exists only in the filename)
    title: str
    deps: list[str]
    model: str                     # Portable tier: prime | core | lite -- nothing else
    status: str                    # TODO | WIP | DONE | BLOCKED | SKIP
    scope: list[str]               # Allowed path prefixes; fail-closed, must not be empty
    verify: str                    # Acceptance command
    goal: str
    behavior: str
    acceptance: str
    notes: str
    workflow: tuple[TaskWorkflowRole | TaskWorkflowAction, ...] | None
    path: Path                     # Absolute task file path
    journal_path: Path             # Absolute path of the matching .r.toml journal


def workflow_state_path(tasks_dir: Path) -> Path:
    return tasks_dir / WORKFLOW_STATE_NAME


def selection_workflow_state_path(assent_dir: Path) -> Path:
    return assent_dir / INTEGRATION_WORKFLOW_STATE_NAME


def _valid_action_evidence(evidence: object) -> bool:
    return (isinstance(evidence, list)
            and len(evidence) <= _MAX_ACTION_EVIDENCE_ITEMS
            and all(isinstance(item, str)
                    and len(item) <= _MAX_ACTION_EVIDENCE_CHARS
                    for item in evidence))


def _valid_action_result(action: object, status: object, identity: object,
                         exit_code: object, evidence: object,
                         *, allowed_action: str) -> bool:
    if (not isinstance(action, str) or not isinstance(status, str)
            or not isinstance(identity, str)
            or not isinstance(exit_code, int) or isinstance(exit_code, bool)
            or not _valid_action_evidence(evidence)):
        return False
    if not action:
        return not status and not identity and exit_code == 0 and not evidence
    if action != allowed_action:
        return False
    if not status:
        return not identity and exit_code == 0 and not evidence
    return status in _ACTION_STATUS_VALUES and bool(identity)


def read_workflow_state(tasks_dir: Path) -> WorkflowState | None:
    """Read the plan workflow cursor; absence means no unit is in flight."""
    path = workflow_state_path(tasks_dir)
    if not path.is_file():
        return None
    try:
        with open(path, "rb") as source:
            data = tomllib.load(source)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise AssentError(f"Workflow state {path.name} is unreadable: {error}") from error
    expected = {"version", "unit", "task_id", "step_index", "started",
                "base_ref", "focused_evidence", "action", "action_status",
                "action_source_tree", "action_exit_code", "action_evidence"}
    if set(data) != expected or data.get("version") != 2:
        raise AssentError(f"Workflow state {path.name} has an invalid schema")
    unit = data.get("unit")
    task_id = data.get("task_id")
    step_index = data.get("step_index")
    started = data.get("started")
    base_ref = data.get("base_ref")
    evidence = data.get("focused_evidence")
    action = data.get("action")
    action_status = data.get("action_status")
    action_source_tree = data.get("action_source_tree")
    action_exit_code = data.get("action_exit_code")
    action_evidence = data.get("action_evidence")
    if (unit not in {"task", "plan"} or not isinstance(task_id, str)
            or not isinstance(step_index, int) or isinstance(step_index, bool)
            or step_index < 0 or not isinstance(started, bool)
            or not isinstance(base_ref, str) or not isinstance(evidence, list)
            or not all(isinstance(item, str) for item in evidence)
            or (unit == "task" and not _ID_RE.fullmatch(task_id))
            or (unit == "plan" and task_id)
            or not _valid_action_result(
                action, action_status, action_source_tree, action_exit_code,
                action_evidence, allowed_action="focused_test")
            or (action and unit != "task")):
        raise AssentError(f"Workflow state {path.name} has invalid values")
    return WorkflowState(
        unit, task_id, step_index, started, base_ref, tuple(evidence), action,
        action_status, action_source_tree, action_exit_code,
        tuple(action_evidence))


def write_workflow_state(tasks_dir: Path, state: WorkflowState) -> None:
    """Atomically persist the next workflow position and its start boundary."""
    text = "\n".join((
        "version = 2",
        f"unit = {json.dumps(state.unit)}",
        f"task_id = {json.dumps(state.task_id)}",
        f"step_index = {state.step_index}",
        f"started = {'true' if state.started else 'false'}",
        f"base_ref = {json.dumps(state.base_ref)}",
        "focused_evidence = [" + ", ".join(
            json.dumps(item, ensure_ascii=False)
            for item in state.focused_evidence) + "]",
        f"action = {json.dumps(state.action)}",
        f"action_status = {json.dumps(state.action_status)}",
        f"action_source_tree = {json.dumps(state.action_source_tree)}",
        f"action_exit_code = {state.action_exit_code}",
        "action_evidence = [" + ", ".join(
            json.dumps(item, ensure_ascii=False)
            for item in state.action_evidence) + "]",
        "",
    ))
    atomic_write_text(workflow_state_path(tasks_dir), text)


def read_selection_workflow_state(assent_dir: Path) -> SelectionWorkflowState | None:
    """Read the project-level exact-selection cursor."""
    path = selection_workflow_state_path(assent_dir)
    if not path.is_file():
        return None
    try:
        with open(path, "rb") as source:
            data = tomllib.load(source)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise AssentError(f"Integration workflow state {path.name} is unreadable: {error}") from error
    expected = {
        "version", "plans", "target_ref", "target_commit", "source_commits",
        "step_index", "action", "action_status", "action_candidate_tree",
        "action_exit_code", "action_evidence", "verification_script_sha256",
        "shared_inputs_sha256", "repair_phase",
    }
    if set(data) != expected or data.get("version") != 1:
        raise AssentError(f"Integration workflow state {path.name} has an invalid schema")
    plan_names = data.get("plans")
    target_ref = data.get("target_ref")
    target_commit = data.get("target_commit")
    source_commits = data.get("source_commits")
    step_index = data.get("step_index")
    action = data.get("action")
    action_status = data.get("action_status")
    action_candidate_tree = data.get("action_candidate_tree")
    action_exit_code = data.get("action_exit_code")
    action_evidence = data.get("action_evidence")
    verification_script_sha256 = data.get("verification_script_sha256")
    shared_inputs_sha256 = data.get("shared_inputs_sha256")
    repair_phase = data.get("repair_phase")
    action_valid = _valid_action_result(
        action, action_status, action_candidate_tree, action_exit_code,
        action_evidence, allowed_action="full_verify")
    if (not isinstance(plan_names, list) or not plan_names
            or not all(isinstance(item, str) and item for item in plan_names)
            or len(set(plan_names)) != len(plan_names)
            or not isinstance(target_ref, str) or not target_ref
            or not isinstance(target_commit, str) or not target_commit
            or not isinstance(source_commits, list)
            or len(source_commits) != len(plan_names)
            or not all(isinstance(item, str) and item for item in source_commits)
            or not isinstance(step_index, int) or isinstance(step_index, bool)
            or step_index < 0 or not action_valid
            or not isinstance(verification_script_sha256, str)
            or not isinstance(shared_inputs_sha256, str)
            or repair_phase not in _SELECTION_REPAIR_PHASES
            or (action_status and (not verification_script_sha256
                                   or not shared_inputs_sha256))):
        raise AssentError(f"Integration workflow state {path.name} has invalid values")
    return SelectionWorkflowState(
        tuple(plan_names), target_ref, target_commit, tuple(source_commits),
        step_index, action, action_status, action_candidate_tree,
        action_exit_code, tuple(action_evidence), verification_script_sha256,
        shared_inputs_sha256, repair_phase)


def write_selection_workflow_state(
        assent_dir: Path, state: SelectionWorkflowState) -> None:
    """Atomically persist one exact selection and its recovery boundary."""
    text = "\n".join((
        "version = 1",
        "plans = [" + ", ".join(json.dumps(item) for item in state.plan_names) + "]",
        f"target_ref = {json.dumps(state.target_ref)}",
        f"target_commit = {json.dumps(state.target_commit)}",
        "source_commits = [" + ", ".join(
            json.dumps(item) for item in state.source_commits) + "]",
        f"step_index = {state.step_index}",
        f"action = {json.dumps(state.action)}",
        f"action_status = {json.dumps(state.action_status)}",
        f"action_candidate_tree = {json.dumps(state.action_candidate_tree)}",
        f"action_exit_code = {state.action_exit_code}",
        "action_evidence = [" + ", ".join(
            json.dumps(item, ensure_ascii=False)
            for item in state.action_evidence) + "]",
        f"verification_script_sha256 = {json.dumps(state.verification_script_sha256)}",
        f"shared_inputs_sha256 = {json.dumps(state.shared_inputs_sha256)}",
        f"repair_phase = {json.dumps(state.repair_phase)}",
        "",
    ))
    atomic_write_text(selection_workflow_state_path(assent_dir), text)


def _require_str(data: dict, path: Path, key: str, *, allow_empty: bool = False) -> str:
    if key not in data:
        raise AssentError(f"Task file {path.name} is missing required field: {key}")
    val = data[key]
    if not isinstance(val, str):
        raise AssentError(f"Task file {path.name} field {key} must be a string")
    if not allow_empty and not val.strip():
        raise AssentError(f"Task file {path.name} field {key} must not be empty")
    return val


def _optional_str(data: dict, path: Path, key: str) -> str:
    if key not in data:
        return ""
    val = data[key]
    if not isinstance(val, str):
        raise AssentError(f"Task file {path.name} field {key} must be a string")
    return val


def _str_list(data: dict, path: Path, key: str) -> list[str]:
    if key not in data:
        raise AssentError(f"Task file {path.name} is missing required field: {key}"
                          f" (write [] explicitly even when there is no "
                          f"{'dependency' if key == 'deps' else 'restriction'})")
    val = data[key]
    if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
        raise AssentError(f"Task file {path.name} field {key} must be an array of strings")
    return [x.strip() for x in val if x.strip()]


def _task_workflow(
        data: dict, path: Path
) -> tuple[TaskWorkflowRole | TaskWorkflowAction, ...] | None:
    """Parse an optional task-local role/action sequence without resolving settings."""
    if "workflow" not in data:
        return None
    raw = data["workflow"]
    if not isinstance(raw, list):
        raise AssentError(
            f"Task file {path.name} field workflow must be an array of inline tables")
    entries: list[TaskWorkflowRole | TaskWorkflowAction] = []
    for index, item in enumerate(raw):
        owner = f"workflow[{index}]"
        if not isinstance(item, dict):
            raise AssentError(f"Task file {path.name} {owner} must be an inline table")
        has_role = "role" in item
        has_action = "action" in item
        if has_role == has_action:
            raise AssentError(
                f"Task file {path.name} {owner} must contain exactly one of role or action")
        allowed = {"role"} if has_role else {"action"}
        unknown = sorted(set(item) - allowed)
        if unknown:
            raise AssentError(
                f"Task file {path.name} {owner} has undefined fields: "
                f"{', '.join(unknown)} (valid fields: {next(iter(allowed))})")
        if has_role:
            role = item.get("role")
            if not isinstance(role, str) or not role.strip():
                raise AssentError(
                    f"Task file {path.name} {owner}.role must be a non-empty string")
            entries.append(TaskWorkflowRole(role.strip()))
            continue
        action = item.get("action")
        if not isinstance(action, str) or not action.strip():
            raise AssentError(
                f"Task file {path.name} {owner}.action must be a non-empty string")
        action = action.strip()
        if action not in _WORKFLOW_ACTIONS:
            raise AssentError(
                f"Task file {path.name} {owner} has unknown action {action!r}")
        entries.append(TaskWorkflowAction(action))
    if entries and not any(isinstance(entry, str) for entry in entries):
        raise AssentError(
            f"Task file {path.name} workflow must include at least one role")
    return tuple(entries)


def journal_path_for(task_path: Path) -> Path:
    """Formal task file path -> the matching .r.toml journal path with the same stem."""
    name = task_path.name
    if _FORMAL_FILENAME_RE.match(name) is None:
        raise AssentError(
            f"Cannot derive a journal path from a non-task-file path: {name}"
            " (must be tNNN_name.e.toml)")
    journal_name = name[:-len(".e.toml")] + ".r.toml"
    return task_path.with_name(journal_name)


def _is_retired_task_filename(name: str) -> bool:
    """Identify a retired tNNN_name.toml, without misclassifying a formal task or journal file."""
    return bool(_RETIRED_FILENAME_RE.match(name)
                and _FORMAL_FILENAME_RE.match(name) is None
                and not name.endswith(".r.toml"))


def parse_task_file(path: Path) -> Task:
    """Parse a single task file; any format problem raises a clear error (fail-closed)."""
    if path.name.endswith(".r.toml"):
        raise AssentError(
            f"Journal file {path.name} must not be parsed as a task file"
            " (journal files use the tNNN_name.r.toml format)")
    if _is_retired_task_filename(path.name):
        raise AssentError(
            f"Legacy task file {path.name} is retired; move it to tNNN_name.e.toml")
    m = _FORMAL_FILENAME_RE.match(path.name)
    if m is None:
        raise AssentError(
            f"Task filename does not follow the naming rule: {path.name}"
            " (must be tNNN_name.e.toml, NNN a three-digit number)")
    task_id = f"t{m.group(1)}"

    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except OSError as e:
        raise AssentError(f"Cannot read task file {path}: {e}") from e
    except tomllib.TOMLDecodeError as e:
        raise AssentError(f"Task file {path.name} is not valid TOML: {e}") from e

    unknown = sorted(set(data) - _KNOWN_KEYS)
    if unknown:
        raise AssentError(
            f"Task file {path.name} has undefined fields: {', '.join(unknown)}"
            f" (valid fields: {', '.join(sorted(_KNOWN_KEYS))})")

    status = _require_str(data, path, "status").strip()
    if status not in _STATUS_VALUES:
        raise AssentError(
            f"Task file {path.name} has status = {status!r}, which is invalid"
            f" ({' / '.join(sorted(_STATUS_VALUES))})")

    model = parse_tier(
        _require_str(data, path, "model"), f"Task file {path.name}")

    deps = _str_list(data, path, "deps")
    for dep in deps:
        if not _ID_RE.match(dep):
            raise AssentError(
                f"Task file {path.name} has an invalid task id in deps: {dep!r} (must be tNNN)")
        if dep == task_id:
            raise AssentError(f"Task file {path.name} must not depend on itself in deps")

    scope = _str_list(data, path, "scope")
    if not scope:
        raise AssentError(
            f"Task file {path.name} has an empty scope: the scope check is fail-closed"
            " (undeclared = every change counts as out of scope), list the allowed"
            " paths explicitly")

    verify = _require_str(data, path, "verify").strip()
    if _FULL_VERIFIER_RE.search(verify):
        raise AssentError(
            f"Task file {path.name} names the full verifier in verify: {verify!r}."
            " A task's verify is the session's focused gate; the full verifier runs"
            " outside every AI session, on a human's command or once at the end of a"
            " whole run. Name the narrow command that proves this task's own"
            " acceptance instead")

    return Task(
        id=task_id,
        title=_require_str(data, path, "title").strip(),
        deps=deps,
        model=model,
        status=status,
        scope=scope,
        verify=verify,
        goal=_require_str(data, path, "goal"),
        behavior=_optional_str(data, path, "behavior"),
        acceptance=_require_str(data, path, "acceptance"),
        notes=_optional_str(data, path, "notes"),
        workflow=_task_workflow(data, path),
        path=path.resolve(),
        journal_path=journal_path_for(path.resolve()),
    )


class Plan:
    """All tasks in a plan (sorted by filename)."""

    def __init__(self, tasks: list[Task], tasks_dir: Path) -> None:
        self.tasks = tasks
        self.dir = tasks_dir

    @classmethod
    def parse(cls, tasks_dir: Path) -> "Plan":
        tasks_dir = Path(tasks_dir)
        if not tasks_dir.is_dir():
            raise AssentError(
                f"Plan directory not found: {tasks_dir}"
                " (wrong command-line argument, or did the plan directory change"
                " after auto-derivation?)")
        entries = [p for p in tasks_dir.iterdir() if p.is_file()]
        retired = sorted(p.name for p in entries
                         if _is_retired_task_filename(p.name))
        if retired:
            raise AssentError(
                "Plan directory still has retired legacy task files: "
                f"{', '.join(retired)}; move them to tNNN_name.e.toml first")
        files = sorted(p for p in entries
                       if _FORMAL_FILENAME_RE.match(p.name))
        if not files:
            raise AssentError(
                f"Plan directory {tasks_dir} has no task files (tNNN_name.e.toml);"
                " run an AI planning session first to produce a plan")

        tasks: list[Task] = []
        seen: dict[str, str] = {}
        for path in files:
            task = parse_task_file(path)
            if task.id in seen:
                raise AssentError(
                    f"Duplicate task id: {task.id} ({seen[task.id]} and {path.name})")
            seen[task.id] = path.name
            tasks.append(task)

        ids = {t.id for t in tasks}
        for task in tasks:
            for dep in task.deps:
                if dep not in ids:
                    raise AssentError(
                        f"Task {task.id} depends on a task that does not exist: {dep}"
                        " (was the file renamed or deleted? deps refer to the"
                        " filename prefix)")
        cls._ensure_acyclic(tasks)
        return cls(tasks, tasks_dir)

    @staticmethod
    def _ensure_acyclic(tasks: list[Task]) -> None:
        deps_by_id = {t.id: t.deps for t in tasks}
        state: dict[str, int] = {}  # 0=unvisited 1=visiting 2=done

        def visit(node: str, chain: list[str]) -> None:
            if state.get(node) == 2:
                return
            if state.get(node) == 1:
                cycle = " -> ".join(chain[chain.index(node):] + [node])
                raise AssentError(f"Task dependencies form a cycle: {cycle}")
            state[node] = 1
            for dep in deps_by_id.get(node, []):
                visit(dep, chain + [node])
            state[node] = 2

        for task in tasks:
            visit(task.id, [])

    def get(self, task_id: str) -> Task | None:
        return next((t for t in self.tasks if t.id == task_id), None)

    def next_task(self) -> tuple[Task, bool] | None:
        """(task, whether this resumes an interruption). WIP takes priority (the
        last-interrupted task, rerun with a resume hint); otherwise the first
        TODO task in order whose deps are all DONE/SKIP; None if there is none."""
        for task in self.tasks:
            if task.status == "WIP":
                return task, True
        status_by_id = {t.id: t.status for t in self.tasks}
        for task in self.tasks:
            if task.status != "TODO":
                continue
            if all(status_by_id.get(dep) in ("DONE", "SKIP") for dep in task.deps):
                return task, False
        return None


def set_status(path: Path, new_status: str) -> None:
    """Precisely replace the status line in a task file, leaving other bytes untouched;
    re-parse and validate after writing."""
    if new_status not in _STATUS_VALUES:
        raise AssentError(f"Invalid status: {new_status!r}")
    try:
        with open(path, encoding="utf-8", newline="") as f:
            text = f.read()
    except OSError as e:
        raise AssentError(f"Cannot read task file {path}: {e}") from e

    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        body = line.rstrip("\r\n")
        eol = line[len(body):]
        m = _STATUS_LINE_RE.match(body)
        if m:
            lines[i] = f"{m.group(1)}{new_status}{m.group(3)}{eol}"
            break
    else:
        raise AssentError(f"Task file {path.name} has no status line to write back")

    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("".join(lines))

    # Re-parse and validate: catches an inconsistency if we just matched a fake
    # status line inside a multi-line string.
    with open(path, "rb") as f:
        data = tomllib.load(f)
    if data.get("status") != new_status:
        raise AssentError(
            f"Task file {path.name} failed validation after the status writeback"
            " (a disguised status line?); inspect the file manually")


def task_text_sha256(text: str) -> str:
    """Return the digest used to bind one exact UTF-8 task contract."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _scope_line_edit(text: str, entries: list[str], *, remove: bool) -> str:
    """Render the reversible exact-suffix edit used by scope amendment recovery."""
    if not entries:
        return text
    try:
        document_scope = tomllib.loads(text).get("scope")
    except tomllib.TOMLDecodeError as e:
        raise AssentError(f"Task contract is not valid TOML: {e}") from e
    lines = text.splitlines(keepends=True)
    matches: list[tuple[int, re.Match[str], str]] = []
    for index, line in enumerate(lines):
        body = line.rstrip("\r\n")
        match = _SCOPE_LINE_RE.match(body)
        if match is None:
            continue
        try:
            parsed = tomllib.loads(body).get("scope")
        except tomllib.TOMLDecodeError:
            continue
        if (parsed == document_scope and isinstance(parsed, list)
                and all(isinstance(item, str) for item in parsed)):
            matches.append((index, match, line[len(body):]))
    if len(matches) != 1:
        raise AssentError(
            "Task contract must contain one writable single-line scope array")

    index, match, eol = matches[0]
    body = match.group("body")
    core = body.rstrip()
    spacing = body[len(core):]
    encoded = ", ".join(json.dumps(item, ensure_ascii=False) for item in entries)
    if remove:
        addition = (f" {encoded}," if core.endswith(",")
                    else f", {encoded}")
        if not core.endswith(addition):
            raise AssentError(
                "Task scope does not contain the scheduler amendment as its exact suffix")
        core = core[:-len(addition)]
    else:
        addition = (f" {encoded}," if core.endswith(",")
                    else f", {encoded}")
        core += addition
    lines[index] = (
        f"{match.group('prefix')}[{core}{spacing}]"
        f"{match.group('suffix')}{eol}")
    return "".join(lines)


def scope_text_without_entries(text: str, entries: list[str]) -> str:
    """Reconstruct exact pre-amendment bytes from a scheduler-written suffix."""
    return _scope_line_edit(text, entries, remove=True)


def scope_text_with_entries(text: str, entries: list[str]) -> str:
    """Precompute the exact scheduler amendment without writing a task file."""
    if not isinstance(entries, list) or not entries or not all(
            isinstance(item, str) and item for item in entries):
        raise AssentError("Scope amendment entries must be a non-empty string list")
    if len(entries) != len(set(entries)):
        raise AssentError("Scope amendment entries contain a duplicate")
    return _scope_line_edit(text, entries, remove=False)


def add_scope_entries(path: Path, entries: list[str], *,
                      expected_sha256: str | None = None) -> tuple[str, str]:
    """Atomically append exact paths without changing any unrelated task bytes.

    Filesystem/path policy is owned by ``auto_fix``. This lower-level writer
    enforces compare-and-swap bytes, addition-only semantics, and a valid task
    contract before replacing the file.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise AssentError(f"Cannot read task file {path}: {e}") from e
    before = task_text_sha256(text)
    if expected_sha256 is not None and before != expected_sha256:
        raise AssentError(
            f"Task contract changed before the scope amendment: {path.name}")
    original = parse_task_file(path)
    if any(item in original.scope for item in entries):
        raise AssentError("Scope amendment would duplicate an existing entry")

    amended = scope_text_with_entries(text, entries)
    try:
        old_data = tomllib.loads(text)
        new_data = tomllib.loads(amended)
    except tomllib.TOMLDecodeError as e:
        raise AssentError(
            f"Task scope amendment did not produce valid TOML: {e}") from e
    expected_data = dict(old_data)
    expected_data["scope"] = list(original.scope) + entries
    if new_data != expected_data:
        raise AssentError(
            "Task scope amendment would alter fields other than appending scope")

    atomic_write_text(path, amended)
    fresh = parse_task_file(path)
    if same_except_status(original, fresh) != ["scope"]:
        raise AssentError(
            f"Task file {path.name} failed validation after scope amendment")
    if fresh.status != original.status or fresh.scope != original.scope + entries:
        raise AssentError(
            f"Task file {path.name} has an unexpected scope amendment result")
    return before, task_text_sha256(amended)


def same_except_status(a: Task, b: Task) -> list[str]:
    """Return the fields (other than status) where two task snapshots differ
    (empty list = identical).

    Used by the scheduler's acceptance check: the only change an executing AI
    may legitimately make to its own task file is the status line; a change to
    any other field (deps/scope/verify/prose) counts as overreach (guards
    against loosening one's own acceptance criteria).
    """
    diff = []
    for name in ("title", "deps", "model", "workflow", "scope", "verify",
                 "goal", "behavior", "acceptance", "notes"):
        if getattr(a, name) != getattr(b, name):
            diff.append(name)
    return diff


def _toml_str(value: str) -> str:
    """Single-line TOML basic string (JSON string escaping is compatible with TOML)."""
    return json.dumps(value, ensure_ascii=False)


def _toml_multiline(value: str) -> str:
    """Multi-line TOML literal string; ''' cannot be represented inside it, so
    substitute a near-identical sequence."""
    safe = value.replace("'''", "'' '")
    return f"'''\n{safe}\n'''"


def append_entry(journal: Path, *, by: str, event: str, summary: str,
                 detail: str = "", time_str: str | None = None,
                 agent: str | None = None,
                 requested_model: str | None = None,
                 requested_effort: str | None = None) -> None:
    """Append one [[entry]] block to the end of a .r.toml journal; create it if
    absent. Re-parses and validates after writing.

    ``agent``, ``requested_model``, and ``requested_effort`` are newer optional
    fields; ``read_entries`` still reads legacy journals as-is, but a new write
    no longer accepts the old catch-all ``by = "ai"`` for an unidentified
    adapter.
    """
    if by not in _ENTRY_BY:
        raise AssentError(
            f"Journal field by is invalid: {by!r}"
            f" ({' / '.join(sorted(_ENTRY_BY))})")
    if agent is not None and agent not in _ENTRY_AGENT:
        raise AssentError(
            f"Journal field agent is invalid: {agent!r}"
            f" ({' / '.join(sorted(_ENTRY_AGENT))})")
    if requested_model is not None and not requested_model.strip():
        raise AssentError("Journal field requested_model must not be an empty string")
    if requested_effort is not None and not requested_effort.strip():
        raise AssentError("Journal field requested_effort must not be an empty string")
    if time_str is None:
        time_str = datetime.now(timezone.utc).isoformat(timespec="seconds")

    block_lines = [
        "[[entry]]",
        f"time = {_toml_str(time_str)}",
        f"by = {_toml_str(by)}",
    ]
    if agent is not None:
        block_lines.append(f"agent = {_toml_str(agent)}")
    if requested_model is not None:
        block_lines.append(
            f"requested_model = {_toml_str(requested_model)}")
    if requested_effort is not None:
        block_lines.append(
            f"requested_effort = {_toml_str(requested_effort)}")
    block_lines += [
        f"event = {_toml_str(event)}",
        f"summary = {_toml_str(summary)}",
    ]
    if detail:
        block_lines.append(f"detail = {_toml_multiline(detail)}")
    block = "\n".join(block_lines) + "\n"

    existing = journal.read_text(encoding="utf-8") if journal.is_file() else ""
    with open(journal, "a", encoding="utf-8", newline="") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        if existing:
            f.write("\n")
        f.write(block)

    with open(journal, "rb") as f:
        try:
            tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise AssentError(
                f"Journal file {journal.name} is not valid TOML after appending: {e}") from e


def read_entries(journal: Path) -> list[dict]:
    """Read all [[entry]] blocks from a .r.toml journal; a missing file returns an
    empty list, a broken file raises an error. Used by report."""
    if not journal.is_file():
        return []
    with open(journal, "rb") as f:
        try:
            data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise AssentError(f"Journal file {journal.name} is not valid TOML: {e}") from e
    entries = data.get("entry", [])
    return entries if isinstance(entries, list) else []
