"""Pre-session decisions shared by task execution and read-only inspection.

Everything here answers a question that has to be settled *before* an AI session
exists, and that `run` and the query commands must answer identically:

- which selected effort a task gets, and which concrete CLI value that becomes;
- the full session identity (adapter, requested model, effort pair) of one run;
- whether the active adapter would accept every invocation the plan could still
  issue -- a zero-token gate `run` and `check` share verbatim;
- how the resolved assignment is rendered for a human;
- whether the project root has its own git marker and whether the `.assent`
  management surface is layered correctly against the worktrees;
- which commit a plan's worktree stacks on, and which upstream tip that
  reading is valid against.

The module deliberately knows nothing about sessions, checkpoints, or reports:
it is imported by ``assent.engine`` and ``assent.inspection`` and imports
neither.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from pathlib import Path

from assent import AssentError, gitops
from assent.adapters import Adapter, InvocationRequest, get_adapter
from assent.config import (Config, WorkflowActionStep, WorkflowPlanStep,
                           WorkflowTaskStep)
from assent.plandeps import PlanBaseResolution, resolve_plan_base
from assent.modeling import effort_identity, has_literal, inherited_effort
from assent.plan import Plan, Task, TaskWorkflowAction

GIT_REQUIRED_MESSAGE = "This project has no git repository yet; run git init first"

_ASSIGNMENT_NAME_WIDTH = 26
_ASSIGNMENT_ABSTRACT_WIDTH = 14
_ASSIGNMENT_LINE_WIDTH = 78


@dataclass(frozen=True)
class SessionIdentity:
    """The selected choices and actual CLI identity shared by one task run."""

    agent: str
    requested_model: str
    effort: str | None
    requested_effort: str | None


# --------------------------------------------------------------------------- #
# effort and session identity
# --------------------------------------------------------------------------- #
def resolve_effort(cfg: Config, task: Task,
                   adapter_name: str | None = None) -> str | None:
    """Selected effort: task annotation, portable-tier default, or vendor default.

    The current adapter's settings are looked up by name and fail closed for an unknown adapter,
    so a third adapter never inherits Claude's defaults."""
    return cfg.adapter_settings(adapter_name or cfg.adapter_name).resolve_effort(
        task.effort, task.model)


def resolve_requested_effort(cfg: Config, model: str,
                             effort: str | None,
                             adapter_name: str | None = None) -> str | None:
    """Resolve the selected effort to the actual CLI value for the current adapter.

    Literals bypass translation. Portable effort uses tier section, then flat
    mapping, then baseline; a literal model has no tier-specific section.
    """
    return cfg.adapter_settings(
        adapter_name or cfg.adapter_name).resolve_requested_effort(
        model, effort)


def resolve_session(cfg: Config, adapter: Adapter, task: Task,
                    adapter_name: str | None = None) -> SessionIdentity:
    """Resolve the identity before starting the adapter; the same result feeds the prompt,
    journal, and CLI command."""
    name = adapter_name or cfg.adapter_name
    effort = resolve_effort(cfg, task, name)
    return SessionIdentity(
        agent=name,
        requested_model=adapter.resolve_model(task.model),
        effort=effort,
        requested_effort=resolve_requested_effort(
            cfg, task.model, effort, name),
    )


def literal_adapter_errors(cfg: Config, task: Task) -> list[str]:
    """Require every effective literal task/role profile to name one adapter."""
    profiles: list[tuple[str, str | None, str | None, tuple[str, ...]]] = []

    def add(label: str, role, adapters: tuple[str, ...]) -> None:
        model = role.model or task.model
        effort = inherited_effort(role.model, role.effort, task.effort)
        profiles.append((label, model, effort, adapters))

    workflow = task.workflow if task.workflow is not None else cfg.workflow_task
    if workflow is None:
        profiles.append(("task profile", task.model, task.effort,
                         cfg.adapter_names))
    elif not workflow:
        for index, step in enumerate(cfg.workflow_plan):
            if isinstance(step, WorkflowActionStep):
                continue
            add(f"workflow.plan[{index}] role {step.role!r}",
                step.resolved_role, step.adapters)
    elif task.workflow is None:
        for index, step in enumerate(workflow):
            if isinstance(step, WorkflowActionStep):
                continue
            add(f"workflow.task[{index}] role {step.role!r}",
                step.resolved_role, step.adapters or cfg.adapter_names)
    else:
        for index, entry in enumerate(workflow):
            if isinstance(entry, TaskWorkflowAction):
                continue
            try:
                role = cfg.resolve_role(str(entry))
            except AssentError as error:
                raise AssentError(
                    f"Task {task.id} workflow[{index}] names missing agent "
                    f"role {str(entry)!r}") from error
            add(f"task workflow[{index}] role {str(entry)!r}",
                role, cfg.adapter_names)

    errors = []
    for label, model, effort, adapters in profiles:
        if has_literal(model, effort) and len(adapters) != 1:
            errors.append(
                f"task {task.id} {label} uses a literal model or effort and "
                "must resolve to exactly one adapter"
            )
    return errors


# --------------------------------------------------------------------------- #
# adapter capability gate
# --------------------------------------------------------------------------- #
def _planned_invocations(cfg: Config, adapter: Adapter, plan: Plan,
                         task_id: str | None = None,
                         adapter_name: str | None = None) -> list[InvocationRequest]:
    """Resolve every invocation this run could still issue, without starting anything.

    Only tasks that can still run are resolved: a settled task will not open a session, and
    refusing a run because of a mapping a finished task once used would be noise.
    """
    name = adapter_name or cfg.adapter_name
    requests: list[InvocationRequest] = []
    for task in plan.tasks:
        if task_id is not None and task.id != task_id:
            continue
        if task.status not in ("TODO", "WIP"):
            continue
        literal_errors = literal_adapter_errors(cfg, task)
        if literal_errors:
            raise AssentError("; ".join(literal_errors))
        effort = resolve_effort(cfg, task, name)
        requests.append(InvocationRequest(
            task_id=task.id, model=task.model, effort=effort,
            requested_model=adapter.resolve_model(task.model),
            requested_effort=resolve_requested_effort(
                cfg, task.model, effort, name)))
    return requests


def capability_errors(cfg: Config, adapter: Adapter, plan: Plan,
                      task_id: str | None = None,
                      adapter_name: str | None = None) -> list[str]:
    """Ask the active adapter to prove every planned model/effort before anything starts.

    This is a zero-token gate: it runs before an AI session, a task checkpoint or any status
    write, so an invocation the vendor would refuse costs no quota and leaves no trace.  A
    resolution error (an unmapped tier, say) is itself a preflight failure.
    """
    try:
        requests = _planned_invocations(
            cfg, adapter, plan, task_id, adapter_name)
    except AssentError as e:
        return [str(e)]
    return adapter.preflight(requests)


def _review_round_session(cfg: Config, review, adapter: Adapter,
                          adapter_name: str) -> SessionIdentity:
    """Resolve one reviewer candidate through its adapter-specific mappings."""
    settings = cfg.adapter_settings(adapter_name)
    effort = settings.resolve_effort(review.effort, review.model)
    return SessionIdentity(
        agent=adapter_name,
        requested_model=adapter.resolve_model(review.model),
        effort=effort,
        requested_effort=settings.resolve_requested_effort(
            review.model, effort),
    )


def resolve_auto_fix_review_session(cfg: Config,
                                    adapter: Adapter) -> SessionIdentity:
    """Resolve the first verdict-producing plan review step."""
    steps = [step for step in cfg.workflow_plan
             if isinstance(step, WorkflowPlanStep) and step.produces_verdict]
    if not steps:
        raise AssentError("Auto-fix plan review is not configured")
    return _review_round_session(cfg, steps[0], adapter, steps[0].adapter)


def auto_fix_review_capability_errors(
        cfg: Config, adapter: Adapter) -> tuple[SessionIdentity | None, list[str]]:
    """Preflight every distinct configured reviewer adapter, not only the first round's.

    A later round naming an adapter this machine cannot invoke is then caught before the
    run starts, instead of only when that round is finally reached.
    """
    rounds = [step for step in cfg.workflow_plan
              if isinstance(step, WorkflowPlanStep) and step.produces_verdict]
    if not rounds:
        return None, []
    try:
        by_identity: dict[tuple[str, str, str], tuple[object, Adapter]] = {}
        for review in rounds:
            for name in review.adapters:
                round_adapter = (adapter if name == rounds[0].adapter
                                 else get_adapter(name, cfg))
                identity = _review_round_session(
                    cfg, review, round_adapter, name)
                by_identity.setdefault(
                    (name, identity.requested_model,
                     identity.requested_effort or ""),
                    (review, round_adapter))
        errors: list[str] = []
        session = resolve_auto_fix_review_session(cfg, adapter)
        for (name, _model, _effort), (review, round_adapter) in by_identity.items():
            identity = _review_round_session(
                cfg, review, round_adapter, name)
            request = InvocationRequest(
                task_id=f"{cfg.tasks_name}/plan-review",
                model=review.model,
                effort=identity.effort,
                requested_model=identity.requested_model,
                requested_effort=identity.requested_effort,
            )
            for message in round_adapter.preflight([request]):
                errors.append(message if len(by_identity) == 1
                              else f"{name}: {message}")
    except AssentError as e:
        return None, [str(e)]
    return session, errors


def resolve_auto_fix_fixer_session(
        cfg: Config, adapter: Adapter, adapter_name: str,
        model: str, effort: str | None) -> SessionIdentity:
    """Resolve one bounded fixer profile through the normal settings tables."""
    settings = cfg.adapter_settings(adapter_name)
    return SessionIdentity(
        agent=adapter_name,
        requested_model=adapter.resolve_model(model),
        effort=effort,
        requested_effort=settings.resolve_requested_effort(model, effort),
    )


def auto_fix_fixer_capability_errors(
        cfg: Config, adapter: Adapter, adapter_name: str,
        model: str, effort: str | None
        ) -> tuple[SessionIdentity | None, list[str]]:
    """Resolve and preflight one write-capable bounded repair invocation."""
    try:
        session = resolve_auto_fix_fixer_session(
            cfg, adapter, adapter_name, model, effort)
        request = InvocationRequest(
            task_id=f"{cfg.tasks_name}/auto-fix",
            model=model,
            effort=effort,
            requested_model=session.requested_model,
            requested_effort=session.requested_effort,
        )
    except AssentError as e:
        return None, [str(e)]
    return session, adapter.preflight([request])


# --------------------------------------------------------------------------- #
# assignment rendering
# --------------------------------------------------------------------------- #
def _display_width(text: str) -> int:
    """Return terminal columns using the project's W/F CJK width rule."""
    return sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
               for char in text)


def _truncate_display(text: str, width: int) -> str:
    """Truncate a display field to ``width`` columns with one ellipsis."""
    if width <= 0:
        return ""
    if _display_width(text) <= width:
        return text
    ellipsis = "…"
    remaining = width - _display_width(ellipsis)
    if remaining <= 0:
        return ellipsis if width >= _display_width(ellipsis) else ""
    kept: list[str] = []
    used = 0
    for char in text:
        char_width = _display_width(char)
        if used + char_width > remaining:
            break
        kept.append(char)
        used += char_width
    return "".join(kept) + ellipsis


def _pad_display(text: str, width: int) -> str:
    """Left-align a field to a fixed terminal-column width."""
    text = _truncate_display(text, width)
    return text + " " * max(0, width - _display_width(text))


def _task_assignment_line(task: Task, session: SessionIdentity) -> str:
    """Render one assignment without allowing a long configured value to wrap."""
    task_name = task.path.name[:-len(".e.toml")]
    abstract = task.model
    if session.effort is not None:
        abstract += f"/{session.effort}"
        if task.effort is None:
            abstract += "*"
    else:
        abstract += f"/{effort_identity(None)}"
    actual = session.requested_model
    if session.requested_effort is not None:
        actual += f"/{session.requested_effort}"
    else:
        actual += f"/{effort_identity(None)}"

    prefix = (f"  {_pad_display(task_name, _ASSIGNMENT_NAME_WIDTH)} "
              f"{_pad_display(abstract, _ASSIGNMENT_ABSTRACT_WIDTH)} -> ")
    remaining = max(0, _ASSIGNMENT_LINE_WIDTH - _display_width(prefix))
    return prefix + _truncate_display(actual, remaining)


def resolve_task_assignments(
        cfg: Config, plan: Plan
        ) -> list[tuple[str, list[tuple[Task, SessionIdentity]]]]:
    """Resolve every task through the same identity path used by ``run``."""
    literal_errors = [error for task in plan.tasks
                      for error in literal_adapter_errors(cfg, task)]
    if literal_errors:
        raise AssentError("; ".join(literal_errors))
    blocks: list[tuple[str, list[tuple[Task, SessionIdentity]]]] = []
    for adapter_name in cfg.adapter_names:
        adapter = get_adapter(adapter_name, cfg)
        assignments = [
            (task, resolve_session(cfg, adapter, task, adapter_name))
            for task in plan.tasks
        ]
        blocks.append((adapter_name, assignments))
    return blocks


def print_task_assignments(
        blocks: list[tuple[str, list[tuple[Task, SessionIdentity]]]]) -> None:
    """Print one complete assignment block per configured adapter."""
    for adapter_name, assignments in blocks:
        print(f"Task assignment (adapter = {adapter_name}):")
        used_default = False
        for task, session in assignments:
            used_default = used_default or (
                task.effort is None and session.effort is not None)
            print(_task_assignment_line(task, session))
        if used_default:
            print("  (* effort filled from default_effort)")


# --------------------------------------------------------------------------- #
# git and worktree layering
# --------------------------------------------------------------------------- #
def has_git_marker(root: Path) -> bool:
    """The project root must initialize its own git; it may not borrow a parent directory's repo."""
    return (root / ".git").exists()


def worktree_configuration_errors(cfg: Config) -> list[str]:
    """The .assent management surface must stay in the main tree; it must not produce a
    second real copy inside a worktree."""
    errors: list[str] = []
    assent_path = cfg.git_rel(cfg.assent_dir)
    tracked = sorted(set(gitops.tracked_paths(cfg.root, assent_path))
                     | set(gitops.tracked_paths(cfg.root, assent_path,
                                                ref="HEAD")))
    if tracked:
        shown = ", ".join(tracked[:5]) + (" ..." if len(tracked) > 5 else "")
        errors.append(f".assent already has Git-tracked files: {shown}"
                      " (with Git enabled the whole .assent must stay in the main working tree)")
    return errors


# --------------------------------------------------------------------------- #
# stack state
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class StackState:
    """Resolved base plus the declared base identity used to verify races."""

    base: PlanBaseResolution
    sources: tuple[gitops.PlanSourceSnapshot, ...]


def resolve_stack_state(cfg: Config) -> StackState:
    """Resolve a reproducible base and snapshot only its live source tip.

    A plan without an unaccepted declared base has no source to snapshot:
    ordering-only ``after`` entries do not provide Git lineage or race evidence.
    """
    base = resolve_plan_base(
        cfg.root, cfg.tasks_dir, excludes=cfg.git_excludes)
    if base.speculative_upstream is None:
        sources = ()
    else:
        source = gitops.resolve_plan_source(
            cfg.root, base.speculative_upstream.plan, cfg.git_excludes)
        sources = (source,)
        if (source.plan != base.speculative_upstream.plan
                or source.tip != base.speculative_upstream.tip):
            raise AssentError(
                "upstream source changed while the stack base was being resolved")
    return StackState(base, sources)
