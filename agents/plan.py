"""Task folder parsing and writeback (the format contract lives in templates/format.md).

- Task file: tNNN_name.e.toml, header fields strictly validated, unknown keys
  are an error; if a task folder still has a legacy tNNN_name.toml, parsing
  is refused and the caller must move it.
- Journal file: tNNN_name.r.toml, append-only [[entry]] blocks.
- There is exactly one machine write to a task file: set_status replaces the
  status line precisely, leaving every other byte untouched; after writing it
  is re-parsed with tomllib to guard against matching a fake status line
  inside a multi-line string.
"""
from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agents import AgentsError

_FORMAL_FILENAME_RE = re.compile(r"^t(\d{3})_(.+)\.e\.toml$")
_RETIRED_FILENAME_RE = re.compile(r"^t\d{3}_.+\.toml$")
_ID_RE = re.compile(r"^t\d{3}$")
_STATUS_VALUES = {"TODO", "WIP", "DONE", "BLOCKED", "SKIP"}
_MODEL_TIERS = {"prime", "core", "lite"}
_EFFORT_LEVELS = {"low", "medium", "high"}
_KNOWN_KEYS = {"title", "deps", "model", "effort", "status", "scope", "verify",
               "goal", "behavior", "acceptance", "notes"}
_ENTRY_BY = {"codex", "claude", "scheduler"}
_ENTRY_AGENT = {"codex", "claude"}
# Status line: leading status = "VALUE" (tolerates leading whitespace and a trailing comment)
_STATUS_LINE_RE = re.compile(
    r'^(\s*status\s*=\s*")(TODO|WIP|DONE|BLOCKED|SKIP)("\s*(?:#.*)?)$')


@dataclass
class Task:
    id: str                        # Filename prefix, e.g. "t001" (id exists only in the filename)
    title: str
    deps: list[str]
    model: str                     # prime | core | lite
    effort: str | None             # low | medium | high; omitted means the engine applies its default
    status: str                    # TODO | WIP | DONE | BLOCKED | SKIP
    scope: list[str]               # Allowed path prefixes; fail-closed, must not be empty
    verify: str                    # Acceptance command
    goal: str
    behavior: str
    acceptance: str
    notes: str
    path: Path                     # Absolute task file path
    journal_path: Path             # Absolute path of the matching .r.toml journal


def _require_str(data: dict, path: Path, key: str, *, allow_empty: bool = False) -> str:
    if key not in data:
        raise AgentsError(f"Task file {path.name} is missing required field: {key}")
    val = data[key]
    if not isinstance(val, str):
        raise AgentsError(f"Task file {path.name} field {key} must be a string")
    if not allow_empty and not val.strip():
        raise AgentsError(f"Task file {path.name} field {key} must not be empty")
    return val


def _optional_str(data: dict, path: Path, key: str) -> str:
    if key not in data:
        return ""
    val = data[key]
    if not isinstance(val, str):
        raise AgentsError(f"Task file {path.name} field {key} must be a string")
    return val


def _str_list(data: dict, path: Path, key: str) -> list[str]:
    if key not in data:
        raise AgentsError(f"Task file {path.name} is missing required field: {key}"
                          f" (write [] explicitly even when there is no "
                          f"{'dependency' if key == 'deps' else 'restriction'})")
    val = data[key]
    if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
        raise AgentsError(f"Task file {path.name} field {key} must be an array of strings")
    return [x.strip() for x in val if x.strip()]


def journal_path_for(task_path: Path) -> Path:
    """Formal task file path -> the matching .r.toml journal path with the same stem."""
    name = task_path.name
    if _FORMAL_FILENAME_RE.match(name) is None:
        raise AgentsError(
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
        raise AgentsError(
            f"Journal file {path.name} must not be parsed as a task file"
            " (journal files use the tNNN_name.r.toml format)")
    if _is_retired_task_filename(path.name):
        raise AgentsError(
            f"Legacy task file {path.name} is retired; move it to tNNN_name.e.toml")
    m = _FORMAL_FILENAME_RE.match(path.name)
    if m is None:
        raise AgentsError(
            f"Task filename does not follow the naming rule: {path.name}"
            " (must be tNNN_name.e.toml, NNN a three-digit number)")
    task_id = f"t{m.group(1)}"

    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except OSError as e:
        raise AgentsError(f"Cannot read task file {path}: {e}") from e
    except tomllib.TOMLDecodeError as e:
        raise AgentsError(f"Task file {path.name} is not valid TOML: {e}") from e

    unknown = sorted(set(data) - _KNOWN_KEYS)
    if unknown:
        raise AgentsError(
            f"Task file {path.name} has undefined fields: {', '.join(unknown)}"
            f" (valid fields: {', '.join(sorted(_KNOWN_KEYS))})")

    status = _require_str(data, path, "status").strip()
    if status not in _STATUS_VALUES:
        raise AgentsError(
            f"Task file {path.name} has status = {status!r}, which is invalid"
            f" ({' / '.join(sorted(_STATUS_VALUES))})")

    model = _require_str(data, path, "model").strip().lower()
    if model not in _MODEL_TIERS:
        raise AgentsError(
            f"Task file {path.name} has model = {model!r}, not a valid tier"
            " (prime / core / lite; do not write a vendor model name, the mapping"
            " lives in agents.toml)")

    effort_raw = _optional_str(data, path, "effort").strip().lower()
    if effort_raw and effort_raw not in _EFFORT_LEVELS:
        raise AgentsError(
            f"Task file {path.name} has effort = {effort_raw!r}, which is invalid"
            " (low / medium / high, or omit it to use the agents.toml default)")

    deps = _str_list(data, path, "deps")
    for dep in deps:
        if not _ID_RE.match(dep):
            raise AgentsError(
                f"Task file {path.name} has an invalid task id in deps: {dep!r} (must be tNNN)")
        if dep == task_id:
            raise AgentsError(f"Task file {path.name} must not depend on itself in deps")

    scope = _str_list(data, path, "scope")
    if not scope:
        raise AgentsError(
            f"Task file {path.name} has an empty scope: the scope check is fail-closed"
            " (undeclared = every change counts as out of scope), list the allowed"
            " paths explicitly")

    return Task(
        id=task_id,
        title=_require_str(data, path, "title").strip(),
        deps=deps,
        model=model,
        effort=effort_raw or None,
        status=status,
        scope=scope,
        verify=_require_str(data, path, "verify").strip(),
        goal=_require_str(data, path, "goal"),
        behavior=_optional_str(data, path, "behavior"),
        acceptance=_require_str(data, path, "acceptance"),
        notes=_optional_str(data, path, "notes"),
        path=path.resolve(),
        journal_path=journal_path_for(path.resolve()),
    )


class Plan:
    """All tasks in a task folder (sorted by filename)."""

    def __init__(self, tasks: list[Task], tasks_dir: Path) -> None:
        self.tasks = tasks
        self.dir = tasks_dir

    @classmethod
    def parse(cls, tasks_dir: Path) -> "Plan":
        tasks_dir = Path(tasks_dir)
        if not tasks_dir.is_dir():
            raise AgentsError(
                f"Task folder not found: {tasks_dir}"
                " (wrong command-line argument, or did the folder change"
                " after auto-derivation?)")
        entries = [p for p in tasks_dir.iterdir() if p.is_file()]
        retired = sorted(p.name for p in entries
                         if _is_retired_task_filename(p.name))
        if retired:
            raise AgentsError(
                "Task folder still has retired legacy task files: "
                f"{', '.join(retired)}; move them to tNNN_name.e.toml first")
        files = sorted(p for p in entries
                       if _FORMAL_FILENAME_RE.match(p.name))
        if not files:
            raise AgentsError(
                f"Task folder {tasks_dir} has no task files (tNNN_name.e.toml);"
                " run an AI planning session first to produce a plan")

        tasks: list[Task] = []
        seen: dict[str, str] = {}
        for path in files:
            task = parse_task_file(path)
            if task.id in seen:
                raise AgentsError(
                    f"Duplicate task id: {task.id} ({seen[task.id]} and {path.name})")
            seen[task.id] = path.name
            tasks.append(task)

        ids = {t.id for t in tasks}
        for task in tasks:
            for dep in task.deps:
                if dep not in ids:
                    raise AgentsError(
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
                raise AgentsError(f"Task dependencies form a cycle: {cycle}")
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
        raise AgentsError(f"Invalid status: {new_status!r}")
    try:
        with open(path, encoding="utf-8", newline="") as f:
            text = f.read()
    except OSError as e:
        raise AgentsError(f"Cannot read task file {path}: {e}") from e

    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        body = line.rstrip("\r\n")
        eol = line[len(body):]
        m = _STATUS_LINE_RE.match(body)
        if m:
            lines[i] = f"{m.group(1)}{new_status}{m.group(3)}{eol}"
            break
    else:
        raise AgentsError(f"Task file {path.name} has no status line to write back")

    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("".join(lines))

    # Re-parse and validate: catches an inconsistency if we just matched a fake
    # status line inside a multi-line string.
    with open(path, "rb") as f:
        data = tomllib.load(f)
    if data.get("status") != new_status:
        raise AgentsError(
            f"Task file {path.name} failed validation after the status writeback"
            " (a disguised status line?); inspect the file manually")


def same_except_status(a: Task, b: Task) -> list[str]:
    """Return the fields (other than status) where two task snapshots differ
    (empty list = identical).

    Used by the scheduler's acceptance check: the only change an executing AI
    may legitimately make to its own task file is the status line; a change to
    any other field (deps/scope/verify/prose) counts as overreach (guards
    against loosening one's own acceptance criteria).
    """
    diff = []
    for name in ("title", "deps", "model", "effort", "scope", "verify",
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
        raise AgentsError(
            f"Journal field by is invalid: {by!r} (codex / claude / scheduler)")
    if agent is not None and agent not in _ENTRY_AGENT:
        raise AgentsError(f"Journal field agent is invalid: {agent!r} (codex / claude)")
    if requested_model is not None and not requested_model.strip():
        raise AgentsError("Journal field requested_model must not be an empty string")
    if requested_effort is not None and not requested_effort.strip():
        raise AgentsError("Journal field requested_effort must not be an empty string")
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
            raise AgentsError(
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
            raise AgentsError(f"Journal file {journal.name} is not valid TOML: {e}") from e
    entries = data.get("entry", [])
    return entries if isinstance(entries, list) else []
