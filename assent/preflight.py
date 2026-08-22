"""Pre-session decisions shared by task execution and read-only inspection.

Everything here answers a question that has to be settled *before* an AI session
exists, and that `run` and the query commands must answer identically:

- which concrete CLI model and effort a task's tier becomes;
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
from assent.config import Config, WorkflowActionStep, WorkflowTaskStep
from assent.plandeps import PlanBaseResolution, resolve_plan_base
from assent.modeling import effort_identity, has_literal
from assent.plan import Plan, Task, TaskWorkflowAction

GIT_REQUIRED_MESSAGE = "This project has no git repository yet; run git init first"

_ASSIGNMENT_NAME_WIDTH = 26
_ASSIGNMENT_ABSTRACT_WIDTH = 14
_ASSIGNMENT_LINE_WIDTH = 78


@dataclass(frozen=True)
class SessionIdentity:
    """The actual CLI identity shared by one task run."""

    agent: str
    requested_model: str
    requested_effort: str | None


# --------------------------------------------------------------------------- #
# selection and session identity
# --------------------------------------------------------------------------- #
def resolve_selection(cfg: Config, model: str,
                      adapter_name: str | None = None) -> tuple[str, str | None]:
    """Resolve one tier or literal into this adapter's CLI model and effort.

    The current adapter's settings are looked up by name and fail closed for an unknown
    adapter, so a third adapter never inherits Claude's defaults.  A ``None`` effort means
    the selection stated none and the vendor CLI default applies.
    """
    return cfg.adapter_settings(
        adapter_name or cfg.adapter_names[0]).resolve(model)


def resolve_session(cfg: Config, adapter: Adapter, task: Task,
                    adapter_name: str | None = None) -> SessionIdentity:
    """Resolve the identity before starting the adapter; the same result feeds the prompt,
    journal, and CLI command."""
    name = adapter_name or cfg.adapter_names[0]
    requested_model, requested_effort = resolve_selection(cfg, task.model, name)
    return SessionIdentity(
        agent=name,
        requested_model=requested_model,
        requested_effort=requested_effort,
    )


def literal_adapter_errors(cfg: Config, task: Task) -> list[str]:
    """Require every effective literal task/role profile to name one adapter."""
    profiles: list[tuple[str, str | None, tuple[str, ...]]] = []

    def add(label: str, role, adapters: tuple[str, ...]) -> None:
        profiles.append((label, role.model or task.model, adapters))

    workflow = task.workflow if task.workflow is not None else cfg.workflow_task
    if task.workflow is None:
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
    for label, model, adapters in profiles:
        if has_literal(model) and len(adapters) != 1:
            errors.append(
                f"task {task.id} {label} uses a literal model and "
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
    name = adapter_name or cfg.adapter_names[0]
    requests: list[InvocationRequest] = []
    for task in plan.tasks:
        if task_id is not None and task.id != task_id:
            continue
        if task.status not in ("TODO", "WIP"):
            continue
        literal_errors = literal_adapter_errors(cfg, task)
        if literal_errors:
            raise AssentError("; ".join(literal_errors))
        workflow = (cfg.workflow_task if task.workflow is None
                    else task.workflow)
        profiles: list[tuple[str, str, tuple[str, ...]]] = []
        if task.workflow is None:
            profiles.extend(
                (f"{task.id} workflow.task[{index}]",
                 step.resolved_role.model or task.model,
                 step.adapters or cfg.adapter_names)
                for index, step in enumerate(workflow)
                if not isinstance(step, WorkflowActionStep))
        else:
            for index, entry in enumerate(workflow):
                if isinstance(entry, TaskWorkflowAction):
                    continue
                role = cfg.resolve_role(str(entry))
                profiles.append((
                    f"{task.id} workflow[{index}]",
                    role.model or task.model, cfg.adapter_names))
        for label, model, candidates in profiles:
            if name not in candidates:
                continue
            requested_model, requested_effort = resolve_selection(
                cfg, model, name)
            requests.append(InvocationRequest(
                task_id=label, model=model,
                requested_model=requested_model,
                requested_effort=requested_effort))
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
        for task, session in assignments:
            print(_task_assignment_line(task, session))


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
