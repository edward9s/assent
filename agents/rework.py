"""Non-destructively reopen a single task, cascading to downstream tasks when
explicitly requested by a human."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from agents import AgentsError, gitops
from agents.config import Config
from agents.engine import write_report
from agents.lockfile import LockBusy, LockMissing, probe_lock
from agents.plan import Plan, Task, append_entry, read_entries, set_status

_CASCADE_STATUSES = {"DONE", "WIP", "BLOCKED"}
_TASK_STATUSES = {"TODO", "WIP", "DONE", "BLOCKED", "SKIP"}
_CHECKPOINT_RE = re.compile(
    r"^(?:wip|auto)\((?P<folder>[^/()]+)/(?P<task>t[0-9]{3})\): .+$")
_REWORK_RE = re.compile(
    r"^rework\((?P<folder>[^/()]+)/(?P<task>t[0-9]{3})\): .+$")
_REWORK_METADATA_PREFIX = "agents-rework-v1:"
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
            except (AgentsError, OSError, ValueError) as e:
                print(f"{name}: task reopened, but report update failed ({e})")
                return 1
            return 0
    except LockBusy as e:
        print(f"{name}: rework aborted (a run is in progress): {e}")
    except (LockMissing, AgentsError, OSError, ValueError) as e:
        print(f"{name}: rework aborted ({e})")
    return 1


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
            raise AgentsError(f"task file is not writable: {task.path}")
        journal = task.journal_path
        target = journal if journal.exists() else journal.parent
        if not os.access(target, os.W_OK):
            raise AgentsError(f"journal file is not writable: {journal}")


def _entry_values(task: Task, target: Task, head: str,
                  downstream: list[Task], cascade: bool,
                  reason: str, reverted: list[str] | None = None,
                  revert_checkpoint: str | None = None,
                  revert_scope: list[str] | None = None,
                  original_status: str | None = None) -> tuple[str, str]:
    """Build a verifiable summary and detail whose content stays stable across reruns."""
    status = original_status or task.status
    scope = ", ".join(item.id for item in downstream) if cascade else "disabled"
    if cascade and not scope:
        scope = "no downstream tasks"
    summary = (f"Manual rework requested; scheduler reset status {status} "
               "back to TODO")
    detail = (
        f"target id: {target.id}\n"
        f"original status: {status}\n"
        f"HEAD: {head}\n"
        f"cascade scope: {scope}\n"
        f"reason: {reason}"
    )
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
        raise AgentsError("no code checkpoint available for automatic reversion")

    oldest = relevant_positions[-1]
    commits: list[str] = []
    for commit, parents, subject in current[:oldest + 1]:
        if len(parents) != 1:
            raise AgentsError(f"checkpoint tail contains a merge commit: {commit}")
        match = _CHECKPOINT_RE.fullmatch(subject)
        if (match is None or match.group("folder") != name
                or match.group("task") not in task_ids):
            raise AgentsError(
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
        raise AgentsError(f"revert checkpoint {field} has an unparseable format")
    result = tuple(value)
    if len(set(result)) != len(result):
        raise AgentsError(f"revert checkpoint {field} contains duplicate items")
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
        raise AgentsError("revert checkpoint contains multiple resume-metadata records")
    try:
        payload = json.loads(metadata_lines[0])
    except json.JSONDecodeError as e:
        raise AgentsError("revert checkpoint resume metadata is not valid JSON") from e
    expected = {
        "cascade", "changed", "downstream", "original_head", "reason",
        "reverted", "statuses", "target",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise AgentsError("revert checkpoint resume fields are incomplete")

    target = payload["target"]
    original_head = payload["original_head"]
    reason = payload["reason"]
    cascade = payload["cascade"]
    if (not isinstance(target, str) or not _CHECKPOINT_RE.fullmatch(
            f"auto({name}/{target}): x")):
        raise AgentsError("revert checkpoint target has an unparseable format")
    if target != match.group("task"):
        raise AgentsError("revert checkpoint target is inconsistent with the subject")
    if (not isinstance(original_head, str)
            or _HASH_RE.fullmatch(original_head) is None):
        raise AgentsError("revert checkpoint original_head has an unparseable format")
    if not isinstance(reason, str) or not isinstance(cascade, bool):
        raise AgentsError("revert checkpoint request parameters have an unparseable format")
    if len(parents) != 1 or parents[0] != original_head:
        raise AgentsError("revert checkpoint parent is inconsistent with original_head")

    changed = _string_list(payload["changed"], "changed")
    downstream = _string_list(payload["downstream"], "downstream")
    reverted = _string_list(payload["reverted"], "reverted")
    if any(_HASH_RE.fullmatch(item) is None for item in reverted):
        raise AgentsError("revert checkpoint reverted hash has an unparseable format")

    raw_statuses = payload["statuses"]
    if not isinstance(raw_statuses, list):
        raise AgentsError("revert checkpoint statuses have an unparseable format")
    statuses: list[tuple[str, str]] = []
    for item in raw_statuses:
        if (not isinstance(item, list) or len(item) != 2
                or not isinstance(item[0], str)
                or not isinstance(item[1], str)
                or item[1] not in _TASK_STATUSES):
            raise AgentsError("revert checkpoint statuses have an unparseable format")
        statuses.append((item[0], item[1]))
    if len({task_id for task_id, _ in statuses}) != len(statuses):
        raise AgentsError("revert checkpoint statuses contain duplicate tasks")

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
        raise AgentsError("revert checkpoint is inconsistent with the current dependency scope")
    if record.downstream != expected_plan_ids[1:]:
        raise AgentsError("revert checkpoint is inconsistent with the current downstream scope")

    original = dict(record.statuses)
    expected_changed = [target.id]
    if record.cascade:
        expected_changed.extend(
            task.id for task in downstream
            if original[task.id] in _CASCADE_STATUSES)
    if tuple(expected_changed) != record.changed or original[target.id] == "TODO":
        raise AgentsError("revert checkpoint status-cascade scope is inconsistent")

    changed: list[Task] = []
    for task_id in record.changed:
        task = plan.get(task_id)
        if task is None:
            raise AgentsError(f"revert checkpoint task no longer exists: {task_id}")
        changed.append(task)

    candidates = _revert_candidates(
        path, name, set(record.changed), ref=record.original_head)
    if tuple(candidates) != record.reverted:
        raise AgentsError("revert checkpoint stored hashes are inconsistent with history")
    return changed


def _abort_failed_revert(path: Path, name: str, original_head: str,
                         error: AgentsError) -> int:
    """Abort the failed reversion and verify the scene; when the abort also fails, demand
    explicit manual intervention."""
    try:
        gitops.abort_revert(path)
        if gitops.commit_of(path, "HEAD") != original_head:
            raise AgentsError("HEAD did not return to its pre-operation position after abort")
        gitops.ensure_clean(path)
    except AgentsError as abort_error:
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


def _rework_locked(cfg: Config, task_id: object, cascade: object,
                   reason: object, revert_code: object) -> int:
    """After acquiring the lock, run every precheck, preserve the Git scene, then write
    journal and status."""
    name = cfg.tasks_name
    request_error = _validate_request(task_id, cascade, reason, revert_code)
    if request_error:
        print(f"{name}: rework aborted ({request_error})")
        return 1

    assert isinstance(task_id, str)
    assert isinstance(cascade, bool)
    assert isinstance(reason, str)
    assert isinstance(revert_code, bool)
    effective_reason = reason.strip() or "manual rework requested"

    try:
        plan = Plan.parse(cfg.tasks_dir)
    except AgentsError as e:
        print(f"{name}: rework aborted (task files could not be parsed: {e})")
        return 1

    target = plan.get(task_id)
    if target is None:
        print(f"{name}: rework aborted (exact task id not found: {task_id})")
        return 1

    downstream = _downstream_tasks(plan, task_id)
    blockers = [task.id for task in downstream
                if task.status in _CASCADE_STATUSES]
    if target.status != "TODO" and blockers and not cascade:
        print(f"{name}: {task_id} has downstream tasks that must be reopened "
              f"together; specify cascade: {', '.join(blockers)}")
        return 1

    path = gitops.worktree_path(cfg.root, name)
    changed: list[Task] = []
    journal_entries: dict[str, list[dict]] = {}
    log_values: dict[str, tuple[str, str]] = {}
    reverted: list[str] | None = None
    revert_checkpoint: str | None = None
    resuming = False

    # The revert checkpoint is the only fact that lands before the management files;
    # when HEAD is the same not-yet-finished checkpoint, its commit body rebuilds the
    # original parameters and statuses, and no code is reverted again.
    if revert_code:
        try:
            if not path.exists():
                raise AgentsError(f"worktree does not exist: {path}")
            if not gitops.is_repo_worktree(cfg.root, path):
                raise AgentsError(
                    f"fixed path is not a valid worktree of this repo: {path}")
            branch = gitops.current_branch(path)
            if not branch.startswith(cfg.branch_prefix):
                shown = branch or "detached HEAD"
                raise AgentsError(f"worktree is on a branch outside this folder: {shown}")
            gitops.ensure_clean(path)
            head_value = gitops.head_ref(path)
            if head_value is None:
                raise AgentsError("worktree has no HEAD")
            head = head_value

            record = _load_revert_record(path, name, task_id)
            if record is not None:
                record_changed = _validate_revert_record(
                    record, plan, target, downstream, path, name)
                original = dict(record.statuses)
                record_log_values = {
                    task.id: _entry_values(
                        task, target, record.original_head, downstream,
                        record.cascade, record.reason, list(record.reverted),
                        record.checkpoint, list(record.changed),
                        original_status=original[task.id])
                    for task in record_changed
                }
                record_entries = {
                    task.id: read_entries(task.journal_path)
                    for task in record_changed
                }
                logs_complete = all(
                    _has_entry(record_entries[task.id],
                               *record_log_values[task.id])
                    for task in record_changed
                )
                statuses_complete = all(
                    task.status == "TODO" for task in record_changed)
                same_request = (
                    record.cascade == cascade
                    and record.reason == effective_reason
                )
                if not logs_complete:
                    if record.cascade != cascade or record.reason != effective_reason:
                        raise AgentsError(
                            "an incomplete revert checkpoint already exists; "
                            "rerun with exactly the same parameters")
                    for task in record_changed:
                        if task.status not in {original[task.id], "TODO"}:
                            raise AgentsError(
                                f"task status changed after the revert checkpoint: {task.id}")
                if ((not logs_complete and same_request)
                        or (logs_complete and statuses_complete
                            and same_request)):
                    if not logs_complete:
                        _ensure_management_writable(record_changed)
                    changed = record_changed
                    journal_entries = record_entries
                    log_values = record_log_values
                    head = record.original_head
                    reverted = list(record.reverted)
                    revert_checkpoint = record.checkpoint
                    resuming = True
                    if logs_complete:
                        print(
                            f"{name}: {task_id} revert checkpoint management data "
                            f"already persisted, continuing to update the report: "
                            f"{record.checkpoint}")
                    else:
                        print(
                            f"{name}: {task_id} resuming an incomplete revert "
                            f"checkpoint: {record.checkpoint}")
        except (AgentsError, OSError, ValueError) as e:
            print(f"{name}: rework aborted (Git or resume precheck failed: {e}), "
                  "status unchanged")
            return 1

    if not resuming:
        if target.status == "TODO":
            print(f"{name}: {task_id} is already TODO, no rework needed")
            return 1
        if blockers and not cascade:
            print(f"{name}: {task_id} has downstream tasks that must be reopened "
                  f"together; specify cascade: {', '.join(blockers)}")
            return 1

        changed = [target]
        if cascade:
            changed.extend(task for task in downstream
                           if task.status in _CASCADE_STATUSES)
        try:
            journal_entries = {
                task.id: read_entries(task.journal_path) for task in changed
            }
            _ensure_management_writable(changed)
            excludes = cfg.git_excludes
        except (AgentsError, OSError, ValueError) as e:
            print(f"{name}: rework aborted (management-plane precheck failed: {e})")
            return 1

    try:
        if revert_code and not resuming:
            reverted = _revert_candidates(
                path, name, {task.id for task in changed})
        elif not revert_code and path.exists():
            if not gitops.is_repo_worktree(cfg.root, path):
                raise AgentsError(
                    f"fixed path is not a valid worktree of this repo: {path}")
            branch = gitops.current_branch(path)
            if not branch.startswith(cfg.branch_prefix):
                shown = branch or "detached HEAD"
                raise AgentsError(
                    f"worktree is on a branch outside this folder: {shown}")
            if gitops.commit_if_dirty(
                    path, f"wip({name}/{target.id}): manual rework pre-archive",
                    excludes):
                print(f"{name}: {target.id} uncommitted changes archived as a wip checkpoint")
            head = gitops.commit_of(path, "HEAD")
        elif not revert_code:
            head = gitops.commit_of(cfg.root, "HEAD")
            print(f"{name}: worktree does not exist, only reopening management state")
    except AgentsError as e:
        print(f"{name}: rework aborted (Git precheck or archive failed: {e}), "
              "status unchanged")
        return 1

    if revert_code and not resuming:
        assert reverted is not None
        message = _revert_message(
            name, target, cascade, effective_reason, downstream, changed,
            head, reverted)
        print(f"{name}: {target.id} will revert the following code checkpoints:")
        for commit in reverted:
            print(f"  - {commit}")
        try:
            gitops.revert_no_commit(path, reverted)
            gitops.commit_all(path, message)
            revert_checkpoint = gitops.commit_of(path, "HEAD")
        except AgentsError as e:
            return _abort_failed_revert(path, name, head, e)

    if not log_values:
        log_values = {
            task.id: _entry_values(
                task, target, head, downstream, cascade, effective_reason,
                reverted, revert_checkpoint, [item.id for item in changed])
            for task in changed
        }

    ordered = [task for task in changed if task.id != target.id] + [target]
    if revert_code:
        # Status is written first, journal last; a complete same-checkpoint journal
        # therefore serves as a durable completion marker, letting an already-finished
        # old rework and this management-plane-interrupted rework be told apart unambiguously.
        try:
            for task in ordered:
                if task.status != "TODO":
                    set_status(task.path, "TODO")
                    print(f"  task {task.id}: {task.status} -> TODO")
        except (AgentsError, OSError) as e:
            try:
                complete = _management_complete(
                    cfg.tasks_dir, [task.id for task in changed], log_values)
            except (AgentsError, OSError, ValueError) as verify_error:
                print(
                    f"{name}: task-status write interrupted ({e}), and re-reading "
                    f"disk failed ({verify_error}); rerun with the same parameters")
                return 1
            if complete:
                print(f"{name}: status write reported failure, but management data "
                      "is fully persisted")
            else:
                print(f"{name}: task-status write interrupted ({e}); rerun with "
                      "the same parameters")
                return 1
        try:
            for task in changed:
                summary, detail = log_values[task.id]
                if _has_entry(journal_entries[task.id], summary, detail):
                    continue
                append_entry(
                    task.journal_path, by="scheduler",
                    event="rework_requested", summary=summary, detail=detail)
        except (AgentsError, OSError) as e:
            try:
                complete = _management_complete(
                    cfg.tasks_dir, [task.id for task in changed], log_values)
            except (AgentsError, OSError, ValueError) as verify_error:
                print(
                    f"{name}: rework journal write interrupted ({e}), and re-reading "
                    f"disk failed ({verify_error}); rerun with the same parameters")
                return 1
            if complete:
                print(f"{name}: journal write reported failure, but management data "
                      "is fully persisted")
            else:
                print(f"{name}: rework journal write interrupted ({e}); rerun with "
                      "the same parameters")
                return 1
    else:
        try:
            # The default path reuses t001's transaction order: status is touched only
            # after the journal is complete.
            for task in changed:
                summary, detail = log_values[task.id]
                if _has_entry(journal_entries[task.id], summary, detail):
                    continue
                append_entry(
                    task.journal_path, by="scheduler",
                    event="rework_requested", summary=summary, detail=detail)
        except (AgentsError, OSError) as e:
            print(f"{name}: rework journal write interrupted ({e}), task status "
                  "unchanged; rerun")
            return 1
        # Downstream is written first, target last. While the target is still not TODO,
        # the same command can resume.
        try:
            for task in ordered:
                set_status(task.path, "TODO")
                print(f"  task {task.id}: {task.status} -> TODO")
        except (AgentsError, OSError) as e:
            print(f"{name}: task-status write interrupted ({e}); rerun with the "
                  "same parameters")
            return 1

    print(f"{name}: {target.id} rework complete ({len(changed)} task(s) reset to TODO)")
    return 0
