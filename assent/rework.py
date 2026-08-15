"""Non-destructively reopen tasks through one guarded rework transaction.

The public command acquires the lock for a human request; the execution engine
may reuse the same transaction under its already-held lock when workflow repair
has authorized a bounded, reason-bearing repair.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from assent import AssentError, gitops, verification
from assent.config import Config
from assent.inspection import write_report
from assent.lockfile import LockBusy, LockMissing, probe_lock
from assent.plan import Plan, Task, append_entry, read_entries, set_status

_CASCADE_STATUSES = {"DONE", "WIP", "BLOCKED"}
_TASK_STATUSES = {"TODO", "WIP", "DONE", "BLOCKED", "SKIP"}
_CHECKPOINT_RE = re.compile(
    r"^(?:wip|auto)\((?P<folder>[^/()]+)/(?P<task>t[0-9]{3})\): .+$")
_REWORK_RE = re.compile(
    r"^rework\((?P<folder>[^/()]+)/(?P<task>t[0-9]{3})\): .+$")
_REWORK_METADATA_PREFIX = "assent-rework-v1:"
_HASH_RE = re.compile(r"^[0-9a-f]{40,64}$")


@dataclass(frozen=True)
class _RevertRecord:
    """Persistent data inside a revert checkpoint sufficient to safely resume the
    management plane."""

    checkpoint: str
    original_head: str
    target: str
    cascade: bool
    reason: str
    downstream: tuple[str, ...]
    statuses: tuple[tuple[str, str], ...]
    changed: tuple[str, ...]
    reverted: tuple[str, ...]


def rework_task(cfg: Config, task_id: str, cascade: bool = False,
                reason: str = "", revert_code: bool = False) -> int:
    """Safely reset the given task back to TODO; reopen downstream too when
    ``cascade`` is explicitly requested.

    By default only the Git scene is preserved and management state is adjusted;
    when ``revert_code`` is explicitly requested, only the checkpoints at the
    current branch tail whose ownership is provable are reverted. If any of the
    lock, task, or Git prechecks fail, the operation is refused with exit code 1.
    """
    name = cfg.tasks_name
    try:
        with probe_lock(cfg.tasks_dir, name):
            result = _rework_locked(
                cfg, task_id, cascade, reason, revert_code)
            if result != 0:
                return result
            try:
                write_report(cfg, Plan.parse(cfg.tasks_dir))
            except (AssentError, OSError, ValueError) as e:
                print(f"{name}: task reopened, but report update failed ({e})")
                return 1
            return 0
    except LockBusy as e:
        print(f"{name}: rework aborted (a run is in progress): {e}")
    except (LockMissing, AssentError, OSError, ValueError) as e:
        print(f"{name}: rework aborted ({e})")
    return 1


def rework_tasks_locked(cfg: Config, task_ids: list[str], reason: str) -> int:
    """Reopen an exact automatic finding set while the caller holds the folder lock.

    The existing single-task transaction remains the only mutation path.  This
    coordinator merely removes selected descendants whose required cascade is
    already covered by another selected task, then invokes that transaction in
    plan order with code preservation forced on.
    """
    if (not isinstance(task_ids, list) or not task_ids
            or not all(isinstance(item, str) and item for item in task_ids)):
        print(f"{cfg.tasks_name}: automatic rework aborted "
              "(task_ids must be a non-empty list of task ids)")
        return 1
    if len(task_ids) != len(set(task_ids)):
        print(f"{cfg.tasks_name}: automatic rework aborted "
              "(task_ids contains a duplicate)")
        return 1
    try:
        plan = Plan.parse(cfg.tasks_dir)
    except AssentError as e:
        print(f"{cfg.tasks_name}: automatic rework aborted "
              f"(task files could not be parsed: {e})")
        return 1
    selected = set(task_ids)
    unknown = sorted(selected - {task.id for task in plan.tasks})
    if unknown:
        print(f"{cfg.tasks_name}: automatic rework aborted "
              f"(exact task ids not found: {', '.join(unknown)})")
        return 1
    covered: set[str] = set()
    roots: list[str] = []
    for task in plan.tasks:
        if task.id not in selected or task.id in covered:
            continue
        roots.append(task.id)
        covered.update(item.id for item in _downstream_tasks(plan, task.id))

    for task_id in roots:
        current = Plan.parse(cfg.tasks_dir).get(task_id)
        if current is not None and current.status == "TODO":
            continue
        if _rework_locked(
                cfg, task_id, True, reason, False, automatic=True) != 0:
            return 1
    try:
        write_report(cfg, Plan.parse(cfg.tasks_dir))
    except (AssentError, OSError, ValueError) as e:
        print(f"{cfg.tasks_name}: tasks reopened, but report update failed ({e})")
        return 1
    return 0


def _downstream_tasks(plan: Plan, task_id: str) -> list[Task]:
    """Return all direct and indirect downstream tasks of the given task, in plan order."""
    reverse: dict[str, list[str]] = {task.id: [] for task in plan.tasks}
    for task in plan.tasks:
        for dependency in task.deps:
            reverse[dependency].append(task.id)

    found: set[str] = set()
    pending = list(reverse[task_id])
    while pending:
        current = pending.pop()
        if current in found:
            continue
        found.add(current)
        pending.extend(reverse[current])
    return [task for task in plan.tasks if task.id in found]


def _validate_request(task_id: object, cascade: object,
                      reason: object, revert_code: object) -> str | None:
    """Validate the domain entry parameters; return an error message, or ``None`` when valid."""
    if not isinstance(task_id, str) or not task_id:
        return "task_id must be a non-empty string"
    if not isinstance(cascade, bool):
        return "cascade must be a boolean"
    if not isinstance(reason, str):
        return "reason must be a string"
    if not isinstance(revert_code, bool):
        return "revert_code must be a boolean"
    return None


def _ensure_management_writable(tasks: list[Task]) -> None:
    """Conservatively check that task files and existing journals are writable before
    touching Git."""
    for task in tasks:
        if not os.access(task.path, os.W_OK):
            raise AssentError(f"task file is not writable: {task.path}")
        journal = task.journal_path
        target = journal if journal.exists() else journal.parent
        if not os.access(target, os.W_OK):
            raise AssentError(f"journal file is not writable: {journal}")


def _entry_values(task: Task, target: Task, head: str,
                  downstream: list[Task], cascade: bool,
                  reason: str, reverted: list[str] | None = None,
                  revert_checkpoint: str | None = None,
                  revert_scope: list[str] | None = None,
                  original_status: str | None = None,
                  automatic: bool = False) -> tuple[str, str]:
    """Build a verifiable summary and detail whose content stays stable across reruns."""
    status = original_status or task.status
    scope = ", ".join(item.id for item in downstream) if cascade else "disabled"
    if cascade and not scope:
        scope = "no downstream tasks"
    kind = "Automatic repair rework" if automatic else "Manual rework requested"
    summary = (f"{kind}; scheduler reset status {status} "
               "back to TODO")
    detail = (
        f"target id: {target.id}\n"
        f"original status: {status}\n"
        f"HEAD: {head}\n"
        f"cascade scope: {scope}\n"
        f"reason: {reason}"
    )
    if automatic:
        detail += "\nauthorization: configured workflow repair"
    if reverted is not None and revert_checkpoint is not None:
        reversed_scope = (", ".join(revert_scope or [])
                          if cascade else "disabled")
        detail += (
            f"\nHEAD before operation: {head}"
            f"\nrevert checkpoint: {revert_checkpoint}"
            f"\nreverted hashes: {', '.join(reverted)}"
            f"\nreverted cascade set: {reversed_scope}"
        )
    return summary, detail


def _revert_candidates(path: Path, name: str, task_ids: set[str],
                       ref: str = "HEAD") -> list[str]:
    """Find the continuous checkpoint tail, made up of allowed tasks, after the current
    rework boundary."""
    history = gitops.commit_history(path, ref)
    current: list[tuple[str, tuple[str, ...], str]] = []
    for record in history:
        subject = record[2]
        boundary = _REWORK_RE.fullmatch(subject)
        if boundary and boundary.group("folder") == name:
            break
        current.append(record)

    relevant_positions = [
        index for index, (_, _, subject) in enumerate(current)
        if (match := _CHECKPOINT_RE.fullmatch(subject))
        and match.group("folder") == name
        and match.group("task") in task_ids
    ]
    if not relevant_positions:
        raise AssentError("no code checkpoint available for automatic reversion")

    oldest = relevant_positions[-1]
    commits: list[str] = []
    for commit, parents, subject in current[:oldest + 1]:
        if len(parents) != 1:
            raise AssentError(f"checkpoint tail contains a merge commit: {commit}")
        match = _CHECKPOINT_RE.fullmatch(subject)
        if (match is None or match.group("folder") != name
                or match.group("task") not in task_ids):
            raise AssentError(
                f"checkpoint does not form a safely revertible continuous tail: "
                f"{commit} {subject}")
        commits.append(commit)
    return commits


def _revert_message(name: str, target: Task, cascade: bool, reason: str,
                    downstream: list[Task], changed: list[Task], head: str,
                    reverted: list[str]) -> str:
    """Build the revert checkpoint message carrying resume data while keeping a fixed
    first-line format."""
    statuses = [[task.id, task.status] for task in [target, *downstream]]
    payload = {
        "cascade": cascade,
        "changed": [task.id for task in changed],
        "downstream": [task.id for task in downstream],
        "original_head": head,
        "reason": reason,
        "reverted": reverted,
        "statuses": statuses,
        "target": target.id,
    }
    metadata = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return (
        f"rework({name}/{target.id}): revert task output\n\n"
        f"{_REWORK_METADATA_PREFIX}{metadata}"
    )


def _string_list(value: object, field: str) -> tuple[str, ...]:
    """Conservatively parse a string array from a checkpoint."""
    if (not isinstance(value, list)
            or any(not isinstance(item, str) or not item for item in value)):
        raise AssentError(f"revert checkpoint {field} has an unparseable format")
    result = tuple(value)
    if len(set(result)) != len(result):
        raise AssentError(f"revert checkpoint {field} contains duplicate items")
    return result


def _load_revert_record(path: Path, name: str,
                        target_id: str) -> _RevertRecord | None:
    """Parse the resume data of a new-style revert checkpoint when HEAD is one for this folder."""
    history = gitops.commit_history(path)
    if not history:
        return None
    checkpoint, parents, subject = history[0]
    match = _REWORK_RE.fullmatch(subject)
    if (match is None or match.group("folder") != name
            or match.group("task") != target_id):
        return None

    metadata_lines = [
        line[len(_REWORK_METADATA_PREFIX):]
        for line in gitops.commit_message(path).splitlines()
        if line.startswith(_REWORK_METADATA_PREFIX)
    ]
    if not metadata_lines:
        return None
    if len(metadata_lines) != 1:
        raise AssentError("revert checkpoint contains multiple resume-metadata records")
    try:
        payload = json.loads(metadata_lines[0])
    except json.JSONDecodeError as e:
        raise AssentError("revert checkpoint resume metadata is not valid JSON") from e
    expected = {
        "cascade", "changed", "downstream", "original_head", "reason",
        "reverted", "statuses", "target",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise AssentError("revert checkpoint resume fields are incomplete")

    target = payload["target"]
    original_head = payload["original_head"]
    reason = payload["reason"]
    cascade = payload["cascade"]
    if (not isinstance(target, str) or not _CHECKPOINT_RE.fullmatch(
            f"auto({name}/{target}): x")):
        raise AssentError("revert checkpoint target has an unparseable format")
    if target != match.group("task"):
        raise AssentError("revert checkpoint target is inconsistent with the subject")
    if (not isinstance(original_head, str)
            or _HASH_RE.fullmatch(original_head) is None):
        raise AssentError("revert checkpoint original_head has an unparseable format")
    if not isinstance(reason, str) or not isinstance(cascade, bool):
        raise AssentError("revert checkpoint request parameters have an unparseable format")
    if len(parents) != 1 or parents[0] != original_head:
        raise AssentError("revert checkpoint parent is inconsistent with original_head")

    changed = _string_list(payload["changed"], "changed")
    downstream = _string_list(payload["downstream"], "downstream")
    reverted = _string_list(payload["reverted"], "reverted")
    if any(_HASH_RE.fullmatch(item) is None for item in reverted):
        raise AssentError("revert checkpoint reverted hash has an unparseable format")

    raw_statuses = payload["statuses"]
    if not isinstance(raw_statuses, list):
        raise AssentError("revert checkpoint statuses have an unparseable format")
    statuses: list[tuple[str, str]] = []
    for item in raw_statuses:
        if (not isinstance(item, list) or len(item) != 2
                or not isinstance(item[0], str)
                or not isinstance(item[1], str)
                or item[1] not in _TASK_STATUSES):
            raise AssentError("revert checkpoint statuses have an unparseable format")
        statuses.append((item[0], item[1]))
    if len({task_id for task_id, _ in statuses}) != len(statuses):
        raise AssentError("revert checkpoint statuses contain duplicate tasks")

    return _RevertRecord(
        checkpoint=checkpoint, original_head=original_head, target=target,
        cascade=cascade, reason=reason, downstream=downstream,
        statuses=tuple(statuses), changed=changed, reverted=reverted)


def _validate_revert_record(record: _RevertRecord, plan: Plan,
                            target: Task, downstream: list[Task],
                            path: Path, name: str) -> list[Task]:
    """Confirm the revert checkpoint can be resumed unambiguously from the current plan."""
    expected_plan_ids = (target.id, *(task.id for task in downstream))
    if tuple(task_id for task_id, _ in record.statuses) != expected_plan_ids:
        raise AssentError("revert checkpoint is inconsistent with the current dependency scope")
    if record.downstream != expected_plan_ids[1:]:
        raise AssentError("revert checkpoint is inconsistent with the current downstream scope")

    original = dict(record.statuses)
    expected_changed = [target.id]
    if record.cascade:
        expected_changed.extend(
            task.id for task in downstream
            if original[task.id] in _CASCADE_STATUSES)
    if tuple(expected_changed) != record.changed or original[target.id] == "TODO":
        raise AssentError("revert checkpoint status-cascade scope is inconsistent")

    changed: list[Task] = []
    for task_id in record.changed:
        task = plan.get(task_id)
        if task is None:
            raise AssentError(f"revert checkpoint task no longer exists: {task_id}")
        changed.append(task)

    candidates = _revert_candidates(
        path, name, set(record.changed), ref=record.original_head)
    if tuple(candidates) != record.reverted:
        raise AssentError("revert checkpoint stored hashes are inconsistent with history")
    return changed


def _abort_failed_revert(path: Path, name: str, original_head: str,
                         error: AssentError) -> int:
    """Abort the failed reversion and verify the scene; when the abort also fails, demand
    explicit manual intervention."""
    try:
        gitops.abort_revert(path)
        if gitops.commit_of(path, "HEAD") != original_head:
            raise AssentError("HEAD did not return to its pre-operation position after abort")
        gitops.ensure_clean(path)
    except AssentError as abort_error:
        print(
            f"{name}: code reversion commit failed ({error}); git revert --abort "
            f"also failed ({abort_error}), manual intervention required: {path}")
        return 1
    print(f"{name}: code reversion commit failed ({error}); aborted and restored "
          "the pre-operation Git scene")
    return 1


def _has_entry(entries: list[dict], summary: str, detail: str) -> bool:
    """Recognize the same rework journal entry already written before a prior interruption."""
    return any(
        entry.get("by") == "scheduler"
        and entry.get("event") == "rework_requested"
        and entry.get("summary") == summary
        and isinstance(entry.get("detail"), str)
        and entry["detail"].rstrip("\r\n") == detail
        for entry in entries
    )


def _management_complete(tasks_dir: Path, task_ids: list[str],
                         log_values: dict[str, tuple[str, str]]) -> bool:
    """Re-read from disk to confirm both the reverted status and journal have fully landed."""
    current = Plan.parse(tasks_dir)
    for task_id in task_ids:
        task = current.get(task_id)
        if task is None or task.status != "TODO":
            return False
        if not _has_entry(read_entries(task.journal_path),
                          *log_values[task_id]):
            return False
    return True


@dataclass(frozen=True)
class _ReworkRequest:
    """The validated request together with the plan scope it resolves to."""

    cfg: Config
    name: str
    task_id: str
    cascade: bool
    reason: str
    revert_code: bool
    automatic: bool
    plan: Plan
    target: Task
    downstream: list[Task]
    blockers: list[str]
    path: Path


@dataclass
class _ReworkState:
    """Transaction state handed from one rework phase to the next."""

    changed: list[Task] = field(default_factory=list)
    journal_entries: dict[str, list[dict]] = field(default_factory=dict)
    log_values: dict[str, tuple[str, str]] = field(default_factory=dict)
    excludes: tuple[str, ...] = ()
    head: str = ""
    reverted: list[str] | None = None
    revert_checkpoint: str | None = None
    resuming: bool = False


def _ensure_folder_worktree(cfg: Config, path: Path) -> None:
    """Refuse a path that is not this repository's worktree, or one checked out on a
    branch outside this folder."""
    if not gitops.is_repo_worktree(cfg.root, path):
        raise AssentError(
            f"fixed path is not a valid worktree of this repo: {path}")
    branch = gitops.current_branch(path)
    if not branch.startswith(cfg.branch_prefix):
        shown = branch or "detached HEAD"
        raise AssentError(f"worktree is on a branch outside this folder: {shown}")


def _folder_worktree_path(cfg: Config) -> Path:
    """Use the caller's source worktree when execution already runs inside it."""
    try:
        if (gitops.is_repo_worktree(cfg.root, cfg.root)
                and gitops.current_branch(cfg.root).startswith(cfg.branch_prefix)):
            return cfg.root
    except AssentError:
        pass
    return gitops.worktree_path(cfg.root, cfg.tasks_name)


def _resolve_request(cfg: Config, task_id: object, cascade: object,
                     reason: object, revert_code: object,
                     automatic: bool = False) -> _ReworkRequest | None:
    """Phase: validate the entry parameters, parse the plan, and resolve the target and
    its downstream scope. Prints and returns ``None`` when the request is refused."""
    name = cfg.tasks_name
    request_error = _validate_request(task_id, cascade, reason, revert_code)
    if request_error:
        print(f"{name}: rework aborted ({request_error})")
        return None

    assert isinstance(task_id, str)
    assert isinstance(cascade, bool)
    assert isinstance(reason, str)
    assert isinstance(revert_code, bool)

    try:
        plan = Plan.parse(cfg.tasks_dir)
    except AssentError as e:
        print(f"{name}: rework aborted (task files could not be parsed: {e})")
        return None

    target = plan.get(task_id)
    if target is None:
        print(f"{name}: rework aborted (exact task id not found: {task_id})")
        return None

    downstream = _downstream_tasks(plan, task_id)
    blockers = [task.id for task in downstream
                if task.status in _CASCADE_STATUSES]
    if target.status != "TODO" and blockers and not cascade:
        print(f"{name}: {task_id} has downstream tasks that must be reopened "
              f"together; specify cascade: {', '.join(blockers)}")
        return None

    return _ReworkRequest(
        cfg=cfg, name=name, task_id=task_id, cascade=cascade,
        reason=reason.strip() or "manual rework requested",
        revert_code=revert_code, automatic=automatic, plan=plan, target=target,
        downstream=downstream, blockers=blockers,
        path=_folder_worktree_path(cfg))


def _adopt_revert_record(request: _ReworkRequest, state: _ReworkState,
                         record: _RevertRecord) -> None:
    """Decide whether an existing revert checkpoint may be resumed, and load its
    management data into the transaction state when it may."""
    record_changed = _validate_revert_record(
        record, request.plan, request.target, request.downstream,
        request.path, request.name)
    original = dict(record.statuses)
    log_values = {
        task.id: _entry_values(
            task, request.target, record.original_head, request.downstream,
            record.cascade, record.reason, list(record.reverted),
            record.checkpoint, list(record.changed),
            original_status=original[task.id], automatic=request.automatic)
        for task in record_changed
    }
    entries = {
        task.id: read_entries(task.journal_path) for task in record_changed
    }
    logs_complete = all(
        _has_entry(entries[task.id], *log_values[task.id])
        for task in record_changed
    )
    statuses_complete = all(task.status == "TODO" for task in record_changed)
    same_request = (record.cascade == request.cascade
                    and record.reason == request.reason)
    if not logs_complete:
        if record.cascade != request.cascade or record.reason != request.reason:
            raise AssentError(
                "an incomplete revert checkpoint already exists; "
                "rerun with exactly the same parameters")
        for task in record_changed:
            if task.status not in {original[task.id], "TODO"}:
                raise AssentError(
                    f"task status changed after the revert checkpoint: {task.id}")
    if not same_request or (logs_complete and not statuses_complete):
        return

    if not logs_complete:
        _ensure_management_writable(record_changed)
    state.changed = record_changed
    state.journal_entries = entries
    state.log_values = log_values
    state.head = record.original_head
    state.reverted = list(record.reverted)
    state.revert_checkpoint = record.checkpoint
    state.resuming = True
    if logs_complete:
        print(f"{request.name}: {request.task_id} revert checkpoint management data "
              f"already persisted, continuing to update the report: "
              f"{record.checkpoint}")
    else:
        print(f"{request.name}: {request.task_id} resuming an incomplete revert "
              f"checkpoint: {record.checkpoint}")


def _resume_interrupted_revert(request: _ReworkRequest,
                               state: _ReworkState) -> bool:
    """Phase: check the Git scene for a revert rework and, when HEAD is this folder's
    not-yet-finished revert checkpoint, rebuild the original parameters and statuses
    from its commit body so no code is reverted again."""
    try:
        if not request.path.exists():
            raise AssentError(f"worktree does not exist: {request.path}")
        _ensure_folder_worktree(request.cfg, request.path)
        gitops.ensure_clean(request.path)
        head_value = gitops.head_ref(request.path)
        if head_value is None:
            raise AssentError("worktree has no HEAD")
        state.head = head_value

        record = _load_revert_record(
            request.path, request.name, request.task_id)
        if record is not None:
            _adopt_revert_record(request, state, record)
    except (AssentError, OSError, ValueError) as e:
        print(f"{request.name}: rework aborted (Git or resume precheck failed: {e}), "
              "status unchanged")
        return False
    return True


def _prepare_management_plane(request: _ReworkRequest,
                              state: _ReworkState) -> bool:
    """Phase: for a fresh rework, resolve the status-cascade set and confirm the
    management files can be written before Git is touched."""
    name = request.name
    target = request.target
    if target.status == "TODO":
        print(f"{name}: {request.task_id} is already TODO, no rework needed")
        return False
    if request.blockers and not request.cascade:
        print(f"{name}: {request.task_id} has downstream tasks that must be reopened "
              f"together; specify cascade: {', '.join(request.blockers)}")
        return False

    changed = [target]
    if request.cascade:
        changed.extend(task for task in request.downstream
                       if task.status in _CASCADE_STATUSES)
    try:
        state.journal_entries = {
            task.id: read_entries(task.journal_path) for task in changed
        }
        _ensure_management_writable(changed)
        state.excludes = request.cfg.git_excludes
    except (AssentError, OSError, ValueError) as e:
        print(f"{name}: rework aborted (management-plane precheck failed: {e})")
        return False
    state.changed = changed
    return True


def _prepare_git_scene(request: _ReworkRequest, state: _ReworkState) -> bool:
    """Phase: pick the checkpoint tail a reversion would undo, or -- for the
    code-preserving default -- archive uncommitted work and record HEAD."""
    name = request.name
    try:
        if request.revert_code:
            if not state.resuming:
                state.reverted = _revert_candidates(
                    request.path, name, {task.id for task in state.changed})
        elif request.path.exists():
            _ensure_folder_worktree(request.cfg, request.path)
            if gitops.commit_if_dirty(
                    request.path,
                    f"wip({name}/{request.target.id}): "
                    f"{'automatic repair' if request.automatic else 'manual rework'} "
                    "pre-archive",
                    state.excludes):
                print(f"{name}: {request.target.id} uncommitted changes archived "
                      "as a wip checkpoint")
            state.head = gitops.commit_of(request.path, "HEAD")
        else:
            state.head = gitops.commit_of(request.cfg.root, "HEAD")
            print(f"{name}: worktree does not exist, only reopening management state")
    except AssentError as e:
        print(f"{name}: rework aborted (Git precheck or archive failed: {e}), "
              "status unchanged")
        return False
    return True


def _apply_code_revert(request: _ReworkRequest, state: _ReworkState) -> bool:
    """Phase: the only mutation landing before the management files -- revert the proven
    checkpoint tail and commit it together with its resume metadata."""
    if not request.revert_code or state.resuming:
        return True
    assert state.reverted is not None
    message = _revert_message(
        request.name, request.target, request.cascade, request.reason,
        request.downstream, state.changed, state.head, state.reverted)
    print(f"{request.name}: {request.target.id} will revert the following code checkpoints:")
    for commit in state.reverted:
        print(f"  - {commit}")
    try:
        gitops.revert_no_commit(request.path, state.reverted)
        gitops.commit_all(request.path, message)
        state.revert_checkpoint = gitops.commit_of(request.path, "HEAD")
    except gitops.CommitPostconditionError as e:
        print(
            f"{request.name}: code reversion commit failed Assent's "
            f"postcondition ({e}); no revert abort was attempted. "
            "Task status and journal state are unchanged; inspect the repository "
            "hook or Git configuration before recovery.")
        return False
    except AssentError as e:
        _abort_failed_revert(request.path, request.name, state.head, e)
        return False
    return True


def _build_log_values(request: _ReworkRequest, state: _ReworkState) -> None:
    """Phase: derive the journal text once; a resumed checkpoint keeps the text it
    already wrote, so an entry from before the interruption stays recognizable."""
    if state.log_values:
        return
    state.log_values = {
        task.id: _entry_values(
            task, request.target, state.head, request.downstream,
            request.cascade, request.reason, state.reverted,
            state.revert_checkpoint, [item.id for item in state.changed],
            automatic=request.automatic)
        for task in state.changed
    }


def _status_order(request: _ReworkRequest, state: _ReworkState) -> list[Task]:
    """Downstream first, target last: while the target is still not TODO, the same
    command can resume."""
    return ([task for task in state.changed if task.id != request.target.id]
            + [request.target])


def _write_statuses(tasks: list[Task], skip_todo: bool) -> None:
    """Reset each task file to TODO in the given order."""
    for task in tasks:
        if skip_todo and task.status == "TODO":
            continue
        set_status(task.path, "TODO")
        print(f"  task {task.id}: {task.status} -> TODO")


def _write_journals(state: _ReworkState) -> None:
    """Append the rework entry to each journal, skipping one a prior run already wrote."""
    for task in state.changed:
        summary, detail = state.log_values[task.id]
        if _has_entry(state.journal_entries[task.id], summary, detail):
            continue
        append_entry(
            task.journal_path, by="scheduler", event="rework_requested",
            summary=summary, detail=detail)


def _tolerate_partial_write(request: _ReworkRequest, state: _ReworkState,
                            error: Exception, stage: str, settled: str) -> bool:
    """Re-read from disk after a reported write failure: management data that fully
    landed still counts as complete, anything else demands a rerun."""
    name = request.name
    try:
        complete = _management_complete(
            request.cfg.tasks_dir, [task.id for task in state.changed],
            state.log_values)
    except (AssentError, OSError, ValueError) as verify_error:
        print(f"{name}: {stage} interrupted ({error}), and re-reading disk failed "
              f"({verify_error}); rerun with the same parameters")
        return False
    if not complete:
        print(f"{name}: {stage} interrupted ({error}); rerun with the same parameters")
        return False
    print(f"{name}: {settled} reported failure, but management data is fully persisted")
    return True


def _persist_status_first(request: _ReworkRequest, state: _ReworkState) -> bool:
    """Phase: status is written first, journal last; a complete same-checkpoint journal
    therefore serves as a durable completion marker, letting an already-finished old
    rework and this management-plane-interrupted rework be told apart unambiguously."""
    try:
        _write_statuses(_status_order(request, state), skip_todo=True)
    except (AssentError, OSError) as e:
        if not _tolerate_partial_write(
                request, state, e, "task-status write", "status write"):
            return False
    try:
        _write_journals(state)
    except (AssentError, OSError) as e:
        if not _tolerate_partial_write(
                request, state, e, "rework journal write", "journal write"):
            return False
    return True


def _persist_journal_first(request: _ReworkRequest, state: _ReworkState) -> bool:
    """Phase: the code-preserving default reuses t001's transaction order -- status is
    touched only after the journal is complete."""
    name = request.name
    try:
        _write_journals(state)
    except (AssentError, OSError) as e:
        print(f"{name}: rework journal write interrupted ({e}), task status "
              "unchanged; rerun")
        return False
    try:
        _write_statuses(_status_order(request, state), skip_todo=False)
    except (AssentError, OSError) as e:
        print(f"{name}: task-status write interrupted ({e}); rerun with the "
              "same parameters")
        return False
    return True


def _rework_locked(cfg: Config, task_id: object, cascade: object,
                   reason: object, revert_code: object,
                   automatic: bool = False) -> int:
    """After acquiring the lock, run the rework phases in the order that keeps every
    precheck ahead of the Git scene and the journal a durable completion marker."""
    request = _resolve_request(
        cfg, task_id, cascade, reason, revert_code, automatic)
    if request is None:
        return 1

    # Reopening a task takes the folder back out of the finished set the batch
    # candidate was built from, so the batch receipt stops describing publishable
    # work here -- before any status, journal, or revert checkpoint is written.
    if verification.invalidate_batch_receipt(cfg.assent_dir):
        print(f"{request.name}: batch verification receipt invalidated; run "
              "`assent verify --batch` again before the next batch release")

    state = _ReworkState()
    if request.revert_code and not _resume_interrupted_revert(request, state):
        return 1
    if not state.resuming and not _prepare_management_plane(request, state):
        return 1
    if not _prepare_git_scene(request, state):
        return 1
    if not _apply_code_revert(request, state):
        return 1

    _build_log_values(request, state)
    persist = (_persist_status_first if request.revert_code
               else _persist_journal_first)
    if not persist(request, state):
        return 1

    print(f"{request.name}: {request.target.id} rework complete "
          f"({len(state.changed)} task(s) reset to TODO)")
    return 0
