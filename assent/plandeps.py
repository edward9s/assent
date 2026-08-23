"""Plan-level dependency parsing, completion inference, and cycle checks.

- ``_plan_deps.toml`` declares ordering with ``after`` and lineage with an optional
  ``base``; ``after`` never supplies a Git base.  A missing file means no plan
  prerequisites and no declared base.
- Plan completion is always inferred on the spot from the formal task
  files, with no separate state file.
- This module only provides the capability; wiring it into the run/check
  command gate is the caller's responsibility.
"""
from __future__ import annotations

import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from assent import AssentError, gitops
from assent.config import list_task_plans, validate_tasks_name
from assent.plan import Plan

_PLAN_DEPS_NAME = "_plan_deps.toml"
_KNOWN_KEYS = {"after", "base"}


@dataclass(frozen=True)
class PlanDependencies:
    """A plan's declared prerequisite plans."""

    name: str
    after: list[str]
    path: Path
    base: str | None = None


@dataclass(frozen=True)
class PlanCompletion:
    """A plan completion result and its reason, inferred from task files."""

    complete: bool
    reason: str


@dataclass(frozen=True)
class UnfinishedPrerequisite:
    """An unfinished prerequisite plan and its task status counts."""

    name: str
    counts: tuple[tuple[str, int], ...]

    @property
    def total(self) -> int:
        """Total number of unfinished tasks."""
        return sum(count for _, count in self.counts)

    def message(self) -> str:
        """Build the single-line reason shown when run refuses to start."""
        detail = ", ".join(f"{status} {count}" for status, count in self.counts)
        return (f"Prerequisite plan {self.name} still has {self.total} unfinished task(s)"
                f" ({detail})")


@dataclass(frozen=True)
class PlanBaseResolution:
    """Reproducible Git identity selected for one downstream plan."""

    target_snapshot: str
    speculative_upstream: gitops.PlanSourceSnapshot | None
    resolved_base: str


def archived_plan_names(assent_dir: str | Path) -> set[str]:
    """Plan names registered in the archive roster (empty when it is absent).

    This and :func:`live_upstreams` are the only roster readers the rest of the
    codebase may use; no ``after`` consumer reimplements roster lookup.

    Imported lazily because ``assent.archive`` depends on this module, so a
    top-level import here would be circular.  ``read_roster`` fails closed on a
    malformed roster, so any dependency parse (and thus ``check``) incidentally
    validates the roster format.
    """
    from assent.archive import read_roster
    return {entry["plan"] for entry in read_roster(assent_dir)}


def parse_plan_dependencies(tasks_dir: str | Path) -> PlanDependencies:
    """Parse and validate a plan's ``_plan_deps.toml``.

    A referenced plan must resolve to either a live plan with a formal
    task file under the same ``.assent`` directory, or an entry in the
    ``.assent/_archived.toml`` roster (an upstream already archived after being
    proven integrated).  A name present in neither is refused as a typo; a name
    present in both is a contradictory state and fails closed.  A missing
    ``_plan_deps.toml`` yields an empty ``after`` and no ``base``.
    """
    tasks_dir = Path(tasks_dir)
    if not tasks_dir.is_dir():
        raise AssentError(f"Plan directory not found: {tasks_dir}")

    name = tasks_dir.name
    validate_tasks_name(name, "Plan name")
    path = tasks_dir / _PLAN_DEPS_NAME
    if not path.is_file():
        return PlanDependencies(name=name, after=[], path=path.resolve())

    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except OSError as e:
        raise AssentError(f"Cannot read plan dependency file {path}: {e}") from e
    except tomllib.TOMLDecodeError as e:
        raise AssentError(
            f"Plan dependency file {path} is not valid TOML: {e}") from e

    unknown = sorted(set(data) - _KNOWN_KEYS)
    if unknown:
        raise AssentError(
            f"Plan dependency file {path} has unknown keys: {', '.join(unknown)}"
            f" (valid keys: {', '.join(sorted(_KNOWN_KEYS))})")
    if "after" not in data:
        raise AssentError(
            f"Plan dependency file {path} is missing after"
            " (write after = [] explicitly even with no prerequisite plans)")

    after = data["after"]
    if not isinstance(after, list) or not all(isinstance(item, str) for item in after):
        raise AssentError(f"Plan dependency file {path} field after must be an array of strings")

    available = set(list_task_plans(tasks_dir.parent))
    archived = archived_plan_names(tasks_dir.parent)
    for dependency in after:
        validate_tasks_name(dependency, f"Plan {name}'s after element")
        if dependency == name:
            raise AssentError(f"Plan {name}'s after must not depend on itself")
        in_live = dependency in available
        in_archive = dependency in archived
        if in_live and in_archive:
            raise AssentError(
                f"Plan {name}'s after references {dependency}, which exists both as a"
                " live plan and in the archive roster; this contradictory state"
                " needs manual resolution (a normal restore deregisters the plan)")
        if not in_live and not in_archive:
            raise AssentError(
                f"Plan {name}'s after references a plan that does not exist"
                f" or has no task files, and is not in the archive roster: {dependency}")

    base: str | None = None
    if "base" in data:
        base_value = data["base"]
        config_path = path.resolve()
        if not isinstance(base_value, str):
            raise AssentError(
                f"Plan dependency file {config_path} field base must be a string")
        if not base_value:
            raise AssentError(
                f"Plan dependency file {config_path} field base must not be empty")
        if base_value not in after:
            raise AssentError(
                f"Plan dependency file {config_path} field base {base_value!r}"
                f" is not an after member; after = {after!r}")
        base = base_value

    return PlanDependencies(
        name=name, after=list(after), path=path.resolve(), base=base)


def infer_plan_completion(tasks_dir: str | Path) -> PlanCompletion:
    """Parse the task files on the spot and infer whether the plan is entirely ``DONE`` or ``SKIP``."""
    try:
        plan = Plan.parse(Path(tasks_dir))
    except AssentError as e:
        return PlanCompletion(False, f"Cannot infer plan completion: {e}")

    unfinished = [
        f"{task.id}={task.status}"
        for task in plan.tasks
        if task.status not in ("DONE", "SKIP")
    ]
    if unfinished:
        return PlanCompletion(False, f"Unfinished tasks: {', '.join(unfinished)}")
    return PlanCompletion(True, "All tasks are DONE or SKIP")


def is_upstream_complete(
        name: str, plans: Mapping[str, Plan], archived: set[str]) -> bool:
    """Whether an upstream plan counts as complete for unlocking a downstream.

    ``plans`` holds the freshly reparsed *live* plans and ``archived`` is
    the roster set for the same ``.assent`` directory (from
    ``archived_plan_names``).  A live plan is judged on the spot from its
    task files (all DONE/SKIP); an archived plan is proven complete by roster
    membership alone (never by any stored hash); a name in neither fails closed
    as an unresolved reference.  ``parse_plan_dependency_graph`` already
    rejects such a name, so this last branch is a defensive guard -- callers
    must not silently skip it -- rather than an expected path.

    This is the single ``after``-completion predicate shared by every scheduler
    call site, so none of them grows its own roster-reading logic.
    """
    plan = plans.get(name)
    if plan is not None:
        return all(task.status in ("DONE", "SKIP") for task in plan.tasks)
    if name in archived:
        return True
    raise AssentError(
        f"upstream plan {name} is neither live nor in the "
        f"archive roster")


def live_upstreams(assent_dir: str | Path,
                   dependencies: PlanDependencies) -> list[str]:
    """The direct ``after`` upstreams that still have a live plan, in order.

    This is the single filter every consumer of ``after`` applies before it
    looks for an upstream's task files or its ``<plan>/*`` Git identity.  An
    archived upstream has neither: archival requires ``clean``'s mechanical
    proof that the plan's content is already merged into the integration
    target, and it then deletes the branch and the live directory.  So an
    archived name is complete, already integrated, and contributes no source
    tip, inherited lineage, or ancestry check -- a downstream base cut from the
    target already contains its content.

    The judgement is roster membership alone, never a hash recorded in the
    roster, because the project may rewrite Git history.
    ``parse_plan_dependencies`` has already refused any name that is neither
    live nor archived, so what this returns is exactly the live upstreams.
    """
    archived = archived_plan_names(assent_dir)
    return [name for name in dependencies.after if name not in archived]


def find_unfinished_prerequisites(
        tasks_dir: str | Path) -> list[UnfinishedPrerequisite]:
    """Check direct ``after`` prerequisites, returning any not entirely ``DONE/SKIP``.

    If any dependency file or prerequisite task file fails to parse, the error
    propagates directly so the caller stays fail-closed.
    """
    tasks_dir = Path(tasks_dir)
    dependencies = parse_plan_dependencies(tasks_dir)
    unfinished: list[UnfinishedPrerequisite] = []
    status_order = ("TODO", "WIP", "BLOCKED")
    # An archived upstream is proven complete and integrated; its live directory
    # is gone, so there is nothing left to be unfinished.
    for name in live_upstreams(tasks_dir.parent, dependencies):
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


def parse_plan_dependency_graph(
        assent_dir: str | Path) -> dict[str, PlanDependencies]:
    """Parse the ``after`` graph for every plan and check for cycles."""
    assent_dir = Path(assent_dir)
    dependencies = {
        name: parse_plan_dependencies(assent_dir / name)
        for name in list_task_plans(assent_dir)
    }
    _ensure_acyclic(dependencies)
    return dependencies


def direct_dependents(graph: Mapping[str, PlanDependencies],
                      target: str) -> list[str]:
    """Return plans that directly name ``target`` in their ``after`` list.

    Clean and reject both have to lock and inspect exactly this set before they
    remove a source, so the edge direction is read here rather than in either
    command; a difference between the two would silently change which plans a
    destructive step protects.
    """
    return sorted(name for name, dependencies in graph.items()
                  if target in dependencies.after)


def order_plans_by_dependency(
        graph: dict[str, PlanDependencies],
        selected: set[str]) -> list[str]:
    """Topologically sort a subset of plans, breaking ties lexicographically.

    ``graph`` keys are already lexicographically sorted (parsed from
    ``list_task_plans``), so repeatedly picking the smallest ready name
    reproduces whole-project ``run`` dependency-then-lexicographic ordering, just
    serialized instead of concurrency-scheduled.  Edges to plans outside
    ``selected`` are dropped: a prerequisite that is not part of this subset is
    either already integrated or deliberately excluded, and either way it
    imposes no order among the plans that remain.

    This is the single ordering used by every command that walks several
    plans in one pass (``accept --all``, ``verify --batch``), so their orders
    cannot drift apart.
    """
    edges = {name: {dep for dep in graph[name].after if dep in selected}
             for name in selected}
    ordered: list[str] = []
    resolved: set[str] = set()
    remaining = set(selected)
    while remaining:
        ready = sorted(name for name in remaining if edges[name] <= resolved)
        if not ready:
            raise AssentError(
                "Plan dependencies among the selected plans form a cycle; "
                "this should be unreachable because the full graph is "
                "already checked acyclic")
        picked = ready[0]
        ordered.append(picked)
        resolved.add(picked)
        remaining.discard(picked)
    return ordered


def resolve_plan_base(
        root: str | Path, tasks_dir: str | Path, *,
        excludes: Sequence[str] = (),
        downstream_tip: str | None = None) -> PlanBaseResolution:
    """Resolve the immutable base for a downstream plan, failing closed.

    Every live direct prerequisite must be complete because ``after`` declares
    scheduler ordering only.  Git lineage comes exclusively from an explicit
    ``base``: its unaccepted tip becomes the lineage source, while a missing,
    archived, or already accepted base resolves to the current target ``HEAD``.

    When ``downstream_tip`` is supplied, it must descend from the newly resolved
    base.  This turns an upstream that advanced after downstream creation into an
    actionable stale-stack error without rewriting either branch.
    """
    root = Path(root).resolve()
    tasks_dir = Path(tasks_dir).resolve()
    graph = parse_plan_dependency_graph(tasks_dir.parent)
    dependencies = graph.get(tasks_dir.name)
    if dependencies is None or dependencies.path.parent != tasks_dir:
        raise AssentError(
            f"downstream plan is not part of the parsed dependency graph: "
            f"{tasks_dir}")

    target = gitops.main_worktree(root)
    target_snapshot = gitops.commit_of(target, "HEAD")
    live = live_upstreams(tasks_dir.parent, dependencies)
    for plan_name in live:
        completion = infer_plan_completion(tasks_dir.parent / plan_name)
        if not completion.complete:
            raise AssentError(
                f"upstream plan {plan_name} is incomplete: {completion.reason}")

    upstream: gitops.PlanSourceSnapshot | None = None
    if dependencies.base is not None and dependencies.base in live:
        source = gitops.resolve_plan_source(
            root, dependencies.base, excludes)
        if not gitops.is_ancestor(target, source.tip, target_snapshot):
            upstream = source

    resolved_base = upstream.tip if upstream is not None else target_snapshot
    if downstream_tip is not None:
        downstream_snapshot = gitops.commit_of(target, downstream_tip)
        if not gitops.is_ancestor(target, resolved_base, downstream_snapshot):
            old_tip = gitops.merge_base(target, resolved_base, downstream_snapshot)
            if upstream is not None:
                raise AssentError(
                    f"stale speculative stack for {tasks_dir.name}: downstream tip "
                    f"{downstream_snapshot} descends from old upstream tip {old_tip}, "
                    f"not current upstream {upstream.plan} tip {upstream.tip}; "
                    f"run `assent rework {tasks_dir.name}` to rebuild on the new tip, "
                    "or replan the dependency")
            raise AssentError(
                f"stale downstream {tasks_dir.name}: downstream tip "
                f"{downstream_snapshot} descends from old target tip {old_tip}, not "
                f"current target tip {target_snapshot}; run `assent rework "
                f"{tasks_dir.name}` or replan before continuing")

    return PlanBaseResolution(target_snapshot, upstream, resolved_base)


def _ensure_acyclic(dependencies: dict[str, PlanDependencies]) -> None:
    """Check the plan dependency graph; a cycle message includes the full closed path."""
    state: dict[str, int] = {}  # 0=unvisited 1=visiting 2=done

    def visit(node: str, chain: list[str]) -> None:
        if state.get(node) == 2:
            return
        if state.get(node) == 1:
            cycle = " -> ".join(chain[chain.index(node):] + [node])
            raise AssentError(f"Plan dependencies form a cycle: {cycle}")
        node_deps = dependencies.get(node)
        if node_deps is None:
            # An archived upstream is not a live graph node; it is a resolved,
            # integrated leaf with no outgoing edges and cannot join a cycle.
            state[node] = 2
            return
        state[node] = 1
        for dependency in node_deps.after:
            visit(dependency, chain + [node])
        state[node] = 2

    for name in dependencies:
        visit(name, [])
