"""Folder-level dependency parsing, completion inference, and cycle checks.

- ``_folder.toml`` only declares ``after``; a missing file means no folder
  prerequisites.
- Folder completion is always inferred on the spot from the formal task
  files, with no separate state file.
- This module only provides the capability; wiring it into the run/check
  command gate is the caller's responsibility.
"""
from __future__ import annotations

import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from assent import AssentError, gitops
from assent.config import _validate_tasks_name, list_task_folders
from assent.plan import Plan

_FOLDER_CONFIG_NAME = "_folder.toml"
_KNOWN_KEYS = {"after"}


@dataclass(frozen=True)
class FolderDependencies:
    """A task folder's declared prerequisite folders."""

    name: str
    after: list[str]
    path: Path


@dataclass(frozen=True)
class FolderCompletion:
    """A folder completion result and its reason, inferred from task files."""

    complete: bool
    reason: str


@dataclass(frozen=True)
class UnfinishedPrerequisite:
    """An unfinished prerequisite folder and its task status counts."""

    name: str
    counts: tuple[tuple[str, int], ...]

    @property
    def total(self) -> int:
        """Total number of unfinished tasks."""
        return sum(count for _, count in self.counts)

    def message(self) -> str:
        """Build the single-line reason shown when run refuses to start."""
        detail = ", ".join(f"{status} {count}" for status, count in self.counts)
        return (f"Prerequisite folder {self.name} still has {self.total} unfinished task(s)"
                f" ({detail})")


@dataclass(frozen=True)
class FolderBaseResolution:
    """Reproducible Git identity selected for one downstream folder."""

    target_snapshot: str
    speculative_upstream: gitops.FolderSourceSnapshot | None
    resolved_base: str


def parse_folder_dependencies(tasks_dir: str | Path) -> FolderDependencies:
    """Parse and validate a task folder's ``_folder.toml``.

    A referenced task folder must be a folder with a formal task file under
    the same ``.assent`` directory. A missing ``_folder.toml`` yields an
    empty ``after``.
    """
    tasks_dir = Path(tasks_dir)
    if not tasks_dir.is_dir():
        raise AssentError(f"Task folder not found: {tasks_dir}")

    name = tasks_dir.name
    _validate_tasks_name(name, "Task folder name")
    path = tasks_dir / _FOLDER_CONFIG_NAME
    if not path.is_file():
        return FolderDependencies(name=name, after=[], path=path.resolve())

    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except OSError as e:
        raise AssentError(f"Cannot read folder dependency file {path}: {e}") from e
    except tomllib.TOMLDecodeError as e:
        raise AssentError(
            f"Folder dependency file {path} is not valid TOML: {e}") from e

    unknown = sorted(set(data) - _KNOWN_KEYS)
    if unknown:
        raise AssentError(
            f"Folder dependency file {path} has unknown keys: {', '.join(unknown)}"
            f" (valid keys: {', '.join(sorted(_KNOWN_KEYS))})")
    if "after" not in data:
        raise AssentError(
            f"Folder dependency file {path} is missing after"
            " (write after = [] explicitly even with no prerequisite folders)")

    after = data["after"]
    if not isinstance(after, list) or not all(isinstance(item, str) for item in after):
        raise AssentError(f"Folder dependency file {path} field after must be an array of strings")

    available = set(list_task_folders(tasks_dir.parent))
    for dependency in after:
        _validate_tasks_name(dependency, f"Folder {name}'s after element")
        if dependency == name:
            raise AssentError(f"Folder {name}'s after must not depend on itself")
        if dependency not in available:
            raise AssentError(
                f"Folder {name}'s after references a task folder that does not exist"
                f" or has no task files: {dependency}")

    return FolderDependencies(
        name=name, after=list(after), path=path.resolve())


def infer_folder_completion(tasks_dir: str | Path) -> FolderCompletion:
    """Parse the task files on the spot and infer whether the folder is entirely ``DONE`` or ``SKIP``."""
    try:
        plan = Plan.parse(Path(tasks_dir))
    except AssentError as e:
        return FolderCompletion(False, f"Cannot infer folder completion: {e}")

    unfinished = [
        f"{task.id}={task.status}"
        for task in plan.tasks
        if task.status not in ("DONE", "SKIP")
    ]
    if unfinished:
        return FolderCompletion(False, f"Unfinished tasks: {', '.join(unfinished)}")
    return FolderCompletion(True, "All tasks are DONE or SKIP")


def find_unfinished_prerequisites(
        tasks_dir: str | Path) -> list[UnfinishedPrerequisite]:
    """Check direct ``after`` prerequisites, returning any not entirely ``DONE/SKIP``.

    If any dependency file or prerequisite task file fails to parse, the error
    propagates directly so the caller stays fail-closed.
    """
    tasks_dir = Path(tasks_dir)
    dependencies = parse_folder_dependencies(tasks_dir)
    unfinished: list[UnfinishedPrerequisite] = []
    status_order = ("TODO", "WIP", "BLOCKED")
    for name in dependencies.after:
        plan = Plan.parse(tasks_dir.parent / name)
        counts = Counter(
            task.status for task in plan.tasks
            if task.status not in ("DONE", "SKIP"))
        if counts:
            ordered = tuple(
                (status, counts[status])
                for status in status_order
                if counts.get(status, 0))
            unfinished.append(UnfinishedPrerequisite(name, ordered))
    return unfinished


def parse_folder_dependency_graph(
        assent_dir: str | Path) -> dict[str, FolderDependencies]:
    """Parse the ``after`` graph for every task folder and check for cycles."""
    assent_dir = Path(assent_dir)
    dependencies = {
        name: parse_folder_dependencies(assent_dir / name)
        for name in list_task_folders(assent_dir)
    }
    _ensure_acyclic(dependencies)
    return dependencies


def resolve_folder_base(
        root: str | Path, tasks_dir: str | Path, *,
        excludes: Sequence[str] = (),
        downstream_tip: str | None = None) -> FolderBaseResolution:
    """Resolve the immutable base for a downstream folder, failing closed.

    Every direct prerequisite is reconstructed from its current task files and
    sole clean, attached source.  A source tip already reachable from the current
    target ``HEAD`` is accepted.  At most one other tip may become the speculative
    base; choosing among multiple unaccepted sources would be an implicit
    integration policy, so it is refused.

    When ``downstream_tip`` is supplied, it must descend from the newly resolved
    base.  This turns an upstream that advanced after downstream creation into an
    actionable stale-stack error without rewriting either branch.
    """
    root = Path(root).resolve()
    tasks_dir = Path(tasks_dir).resolve()
    graph = parse_folder_dependency_graph(tasks_dir.parent)
    dependencies = graph.get(tasks_dir.name)
    if dependencies is None or dependencies.path.parent != tasks_dir:
        raise AssentError(
            f"downstream task folder is not part of the parsed dependency graph: "
            f"{tasks_dir}")

    target = gitops.main_worktree(root)
    target_snapshot = gitops.commit_of(target, "HEAD")
    candidates: list[gitops.FolderSourceSnapshot] = []
    for folder in dependencies.after:
        completion = infer_folder_completion(tasks_dir.parent / folder)
        if not completion.complete:
            raise AssentError(
                f"upstream folder {folder} is incomplete: {completion.reason}")
        source = gitops.resolve_folder_source(root, folder, excludes)
        if not gitops.is_ancestor(target, source.tip, target_snapshot):
            candidates.append(source)

    if len(candidates) > 1:
        detail = "\n".join(
            f"  - folder {source.folder}, tip {source.tip}: accept this upstream "
            "into the target, or replan the downstream dependency"
            for source in candidates)
        raise AssentError(
            "multiple unaccepted upstream folders cannot form one speculative "
            f"base:\n{detail}\nResolve all but at most one before starting the "
            "downstream task")

    upstream = candidates[0] if candidates else None
    resolved_base = upstream.tip if upstream is not None else target_snapshot
    if downstream_tip is not None:
        downstream_snapshot = gitops.commit_of(target, downstream_tip)
        if not gitops.is_ancestor(target, resolved_base, downstream_snapshot):
            old_tip = gitops.merge_base(target, resolved_base, downstream_snapshot)
            if upstream is not None:
                raise AssentError(
                    f"stale speculative stack for {tasks_dir.name}: downstream tip "
                    f"{downstream_snapshot} descends from old upstream tip {old_tip}, "
                    f"not current upstream {upstream.folder} tip {upstream.tip}; "
                    f"run `assent rework {tasks_dir.name}` to rebuild on the new tip, "
                    "or replan the dependency")
            raise AssentError(
                f"stale downstream {tasks_dir.name}: downstream tip "
                f"{downstream_snapshot} descends from old target tip {old_tip}, not "
                f"current target tip {target_snapshot}; run `assent rework "
                f"{tasks_dir.name}` or replan before continuing")

    return FolderBaseResolution(target_snapshot, upstream, resolved_base)


def _ensure_acyclic(dependencies: dict[str, FolderDependencies]) -> None:
    """Check the folder dependency graph; a cycle message includes the full closed path."""
    state: dict[str, int] = {}  # 0=unvisited 1=visiting 2=done

    def visit(node: str, chain: list[str]) -> None:
        if state.get(node) == 2:
            return
        if state.get(node) == 1:
            cycle = " -> ".join(chain[chain.index(node):] + [node])
            raise AssentError(f"Folder dependencies form a cycle: {cycle}")
        state[node] = 1
        for dependency in dependencies[node].after:
            visit(dependency, chain + [node])
        state[node] = 2

    for name in dependencies:
        visit(name, [])
