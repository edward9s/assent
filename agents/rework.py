"""非破壞性重開單一任務，並依人工明示連動下游任務。"""
from __future__ import annotations

import os
import re
from pathlib import Path

from agents import AgentsError, gitops
from agents.config import Config
from agents.lockfile import LockBusy, LockMissing, probe_lock
from agents.plan import Plan, Task, append_entry, read_entries, set_status

_CASCADE_STATUSES = {"DONE", "WIP", "BLOCKED"}
_CHECKPOINT_RE = re.compile(
    r"^(?:wip|auto)\((?P<folder>[^/()]+)/(?P<task>t[0-9]{3})\): .+$")
_REWORK_RE = re.compile(
    r"^rework\((?P<folder>[^/()]+)/t[0-9]{3}\): .+$")


def rework_task(cfg: Config, task_id: str, cascade: bool = False,
                reason: str = "", revert_code: bool = False) -> int:
    """把指定任務安全改回 TODO；明示 ``cascade`` 時一併重開下游。

    預設只保存 Git 現場並調整管理狀態；明示 ``revert_code`` 時，只反向目前
    分支尾端可確定歸屬的檢查點。鎖定、任務與 Git 預檢任一步驟失敗時，均以
    退出碼 1 拒絕操作。
    """
    name = cfg.tasks_name
    try:
        with probe_lock(cfg.tasks_dir, name):
            return _rework_locked(
                cfg, task_id, cascade, reason, revert_code)
    except LockBusy as e:
        print(f"{name}:任務重開中止(run 進行中):{e}")
    except (LockMissing, AgentsError, OSError, ValueError) as e:
        print(f"{name}:任務重開中止({e})")
    return 1


def _downstream_tasks(plan: Plan, task_id: str) -> list[Task]:
    """依計畫檔順序回傳指定任務的全部直接與間接下游。"""
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
    """驗證領域入口參數；回傳錯誤訊息，合法時回傳 ``None``。"""
    if not isinstance(task_id, str) or not task_id:
        return "task_id 必須是非空字串"
    if not isinstance(cascade, bool):
        return "cascade 必須是布林值"
    if not isinstance(reason, str):
        return "reason 必須是字串"
    if not isinstance(revert_code, bool):
        return "revert_code 必須是布林值"
    return None


def _ensure_management_writable(tasks: list[Task]) -> None:
    """在 Git 封存前保守檢查任務檔與既有日誌是否可寫。"""
    for task in tasks:
        if not os.access(task.path, os.W_OK):
            raise AgentsError(f"任務檔不可寫:{task.path}")
        journal = task.journal_path
        target = journal if journal.exists() else journal.parent
        if not os.access(target, os.W_OK):
            raise AgentsError(f"日誌檔不可寫:{journal}")


def _entry_values(task: Task, target: Task, head: str,
                  downstream: list[Task], cascade: bool,
                  reason: str, reverted: list[str] | None = None,
                  revert_checkpoint: str | None = None,
                  revert_scope: list[str] | None = None) -> tuple[str, str]:
    """建立可驗證且重跑時內容穩定的摘要與明細。"""
    scope = ", ".join(item.id for item in downstream) if cascade else "未啟用"
    if cascade and not scope:
        scope = "無下游任務"
    summary = f"人工要求重開，調度器把狀態 {task.status} 改回 TODO"
    detail = (
        f"目標 id: {target.id}\n"
        f"原狀態: {task.status}\n"
        f"HEAD: {head}\n"
        f"cascade 範圍: {scope}\n"
        f"reason: {reason}"
    )
    if reverted is not None and revert_checkpoint is not None:
        reversed_scope = (", ".join(revert_scope or [])
                          if cascade else "未啟用")
        detail += (
            f"\n操作前 HEAD: {head}"
            f"\n反向 checkpoint: {revert_checkpoint}"
            f"\n被撤銷 hashes: {', '.join(reverted)}"
            f"\n反向 cascade 集合: {reversed_scope}"
        )
    return summary, detail


def _revert_candidates(path: Path, name: str,
                       task_ids: set[str]) -> list[str]:
    """找出目前 rework 邊界後、由允許任務組成的連續 checkpoint 尾段。"""
    history = gitops.commit_history(path)
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
        raise AgentsError("沒有可自動撤銷的程式碼檢查點")

    oldest = relevant_positions[-1]
    commits: list[str] = []
    for commit, parents, subject in current[:oldest + 1]:
        if len(parents) != 1:
            raise AgentsError(f"checkpoint 尾段含 merge commit:{commit}")
        match = _CHECKPOINT_RE.fullmatch(subject)
        if (match is None or match.group("folder") != name
                or match.group("task") not in task_ids):
            raise AgentsError(
                f"checkpoint 未形成可安全撤銷的連續尾段:{commit} {subject}")
        commits.append(commit)
    return commits


def _abort_failed_revert(path: Path, name: str, original_head: str,
                         error: AgentsError) -> int:
    """中止失敗的反向操作並驗證現場；中止失敗時明確要求人工處理。"""
    try:
        gitops.abort_revert(path)
        if gitops.commit_of(path, "HEAD") != original_head:
            raise AgentsError("abort 後 HEAD 未回到操作前位置")
        gitops.ensure_clean(path)
    except AgentsError as abort_error:
        print(
            f"{name}:程式碼反向提交失敗({error})；git revert --abort 亦失敗"
            f"({abort_error})，必須人工處理:{path}")
        return 1
    print(f"{name}:程式碼反向提交失敗({error})；已中止並還原操作前 Git 現場")
    return 1


def _has_entry(entries: list[dict], summary: str, detail: str) -> bool:
    """辨識先前中斷時已寫入的同一筆重開日誌。"""
    return any(
        entry.get("by") == "scheduler"
        and entry.get("event") == "rework_requested"
        and entry.get("summary") == summary
        and isinstance(entry.get("detail"), str)
        and entry["detail"].rstrip("\r\n") == detail
        for entry in entries
    )


def _rework_locked(cfg: Config, task_id: object, cascade: object,
                   reason: object, revert_code: object) -> int:
    """持鎖後完成所有預檢、保存 Git 現場，再寫入日誌與狀態。"""
    name = cfg.tasks_name
    request_error = _validate_request(task_id, cascade, reason, revert_code)
    if request_error:
        print(f"{name}:任務重開中止({request_error})")
        return 1

    assert isinstance(task_id, str)
    assert isinstance(cascade, bool)
    assert isinstance(reason, str)
    assert isinstance(revert_code, bool)
    effective_reason = reason.strip() or "人工要求重做"

    try:
        plan = Plan.parse(cfg.tasks_dir)
    except AgentsError as e:
        print(f"{name}:任務重開中止(任務檔無法解析:{e})")
        return 1

    target = plan.get(task_id)
    if target is None:
        print(f"{name}:任務重開中止(找不到精確任務 id:{task_id})")
        return 1
    if target.status == "TODO":
        print(f"{name}:{task_id} 已是 TODO，無須重開")
        return 1

    downstream = _downstream_tasks(plan, task_id)
    blockers = [task.id for task in downstream
                if task.status in _CASCADE_STATUSES]
    if blockers and not cascade:
        print(f"{name}:{task_id} 有下游任務必須一併重開；"
              f"請明示 cascade:{', '.join(blockers)}")
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
        print(f"{name}:任務重開中止(管理面預檢失敗:{e})")
        return 1

    path = gitops.worktree_path(cfg.root, name)
    reverted: list[str] | None = None
    revert_checkpoint: str | None = None
    try:
        if revert_code:
            if not path.exists():
                raise AgentsError(f"worktree 不存在:{path}")
            if not gitops.is_repo_worktree(cfg.root, path):
                raise AgentsError(
                    f"固定路徑不是本 repo 的有效 worktree:{path}")
            branch = gitops.current_branch(path)
            if not branch.startswith(cfg.branch_prefix):
                shown = branch or "detached HEAD"
                raise AgentsError(f"worktree 位於非本資料夾分支:{shown}")
            gitops.ensure_clean(path)
            head_value = gitops.head_ref(path)
            if head_value is None:
                raise AgentsError("worktree 沒有 HEAD")
            head = head_value
            reverted = _revert_candidates(
                path, name, {task.id for task in changed})
        elif path.exists():
            if not gitops.is_repo_worktree(cfg.root, path):
                raise AgentsError(
                    f"固定路徑不是本 repo 的有效 worktree:{path}")
            branch = gitops.current_branch(path)
            if not branch.startswith(cfg.branch_prefix):
                shown = branch or "detached HEAD"
                raise AgentsError(
                    f"worktree 位於非本資料夾分支:{shown}")
            if gitops.commit_if_dirty(
                    path, f"wip({name}/{target.id}): 人工 rework 前封存",
                    excludes):
                print(f"{name}:{target.id} 未提交變更已封存為 wip checkpoint")
            head = gitops.commit_of(path, "HEAD")
        else:
            head = gitops.commit_of(cfg.root, "HEAD")
            print(f"{name}:worktree 不存在，只重開管理狀態")
    except AgentsError as e:
        print(f"{name}:任務重開中止(Git 預檢或封存失敗:{e})，狀態未變更")
        return 1

    if revert_code:
        assert reverted is not None
        message = f"rework({name}/{target.id}): 撤銷 task 成果"
        print(f"{name}:{target.id} 將反向下列程式碼檢查點:")
        for commit in reverted:
            print(f"  - {commit}")
        try:
            gitops.revert_no_commit(path, reverted)
            gitops.commit_all(path, message)
            revert_checkpoint = gitops.commit_of(path, "HEAD")
        except AgentsError as e:
            return _abort_failed_revert(path, name, head, e)

    log_values = {
        task.id: _entry_values(
            task, target, head, downstream, cascade, effective_reason,
            reverted, revert_checkpoint, [item.id for item in changed])
        for task in changed
    }
    try:
        # 日誌全數完成後才動狀態；中斷重跑時略過內容完全相同的既有條目。
        for task in changed:
            summary, detail = log_values[task.id]
            if _has_entry(journal_entries[task.id], summary, detail):
                continue
            append_entry(
                task.journal_path, by="scheduler",
                event="rework_requested", summary=summary, detail=detail)
    except (AgentsError, OSError) as e:
        print(f"{name}:重開日誌寫入中斷({e})，任務狀態未變更；請重跑")
        return 1

    # 下游先寫、目標最後寫。若中途失敗，目標仍非 TODO，可用相同命令續作；
    # 一旦目標成為 TODO，這次操作所需的下游狀態與全部日誌必然已先完成。
    ordered = [task for task in changed if task.id != target.id] + [target]
    try:
        for task in ordered:
            set_status(task.path, "TODO")
            print(f"  任務 {task.id}:{task.status} -> TODO")
    except (AgentsError, OSError) as e:
        print(f"{name}:任務狀態寫入中斷({e})；請用相同參數重跑")
        return 1

    print(f"{name}:{target.id} 重開完成(共 {len(changed)} 個任務改為 TODO)")
    return 0
