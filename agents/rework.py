"""非破壞性重開單一任務，並依人工明示連動下游任務。"""
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
    """反向 checkpoint 內足以安全續作管理面的持久資料。"""

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
    """把指定任務安全改回 TODO；明示 ``cascade`` 時一併重開下游。

    預設只保存 Git 現場並調整管理狀態；明示 ``revert_code`` 時，只反向目前
    分支尾端可確定歸屬的檢查點。鎖定、任務與 Git 預檢任一步驟失敗時，均以
    退出碼 1 拒絕操作。
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
                print(f"{name}:任務已重開，但報告更新失敗({e})")
                return 1
            return 0
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
                  revert_scope: list[str] | None = None,
                  original_status: str | None = None) -> tuple[str, str]:
    """建立可驗證且重跑時內容穩定的摘要與明細。"""
    status = original_status or task.status
    scope = ", ".join(item.id for item in downstream) if cascade else "未啟用"
    if cascade and not scope:
        scope = "無下游任務"
    summary = f"人工要求重開，調度器把狀態 {status} 改回 TODO"
    detail = (
        f"目標 id: {target.id}\n"
        f"原狀態: {status}\n"
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


def _revert_candidates(path: Path, name: str, task_ids: set[str],
                       ref: str = "HEAD") -> list[str]:
    """找出目前 rework 邊界後、由允許任務組成的連續 checkpoint 尾段。"""
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


def _revert_message(name: str, target: Task, cascade: bool, reason: str,
                    downstream: list[Task], changed: list[Task], head: str,
                    reverted: list[str]) -> str:
    """建立含續作資料、但維持固定首行格式的反向 checkpoint 訊息。"""
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
        f"rework({name}/{target.id}): 撤銷 task 成果\n\n"
        f"{_REWORK_METADATA_PREFIX}{metadata}"
    )


def _string_list(value: object, field: str) -> tuple[str, ...]:
    """保守解析 checkpoint 中的字串陣列。"""
    if (not isinstance(value, list)
            or any(not isinstance(item, str) or not item for item in value)):
        raise AgentsError(f"反向 checkpoint 的 {field} 格式無法解析")
    result = tuple(value)
    if len(set(result)) != len(result):
        raise AgentsError(f"反向 checkpoint 的 {field} 含重複項目")
    return result


def _load_revert_record(path: Path, name: str,
                        target_id: str) -> _RevertRecord | None:
    """HEAD 是本資料夾的新式反向 checkpoint 時，解析其續作資料。"""
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
        raise AgentsError("反向 checkpoint 含多筆續作資料")
    try:
        payload = json.loads(metadata_lines[0])
    except json.JSONDecodeError as e:
        raise AgentsError("反向 checkpoint 的續作資料不是有效 JSON") from e
    expected = {
        "cascade", "changed", "downstream", "original_head", "reason",
        "reverted", "statuses", "target",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise AgentsError("反向 checkpoint 的續作欄位不完整")

    target = payload["target"]
    original_head = payload["original_head"]
    reason = payload["reason"]
    cascade = payload["cascade"]
    if (not isinstance(target, str) or not _CHECKPOINT_RE.fullmatch(
            f"auto({name}/{target}): x")):
        raise AgentsError("反向 checkpoint 的 target 格式無法解析")
    if target != match.group("task"):
        raise AgentsError("反向 checkpoint 的 target 與主旨不一致")
    if (not isinstance(original_head, str)
            or _HASH_RE.fullmatch(original_head) is None):
        raise AgentsError("反向 checkpoint 的 original_head 格式無法解析")
    if not isinstance(reason, str) or not isinstance(cascade, bool):
        raise AgentsError("反向 checkpoint 的請求參數格式無法解析")
    if len(parents) != 1 or parents[0] != original_head:
        raise AgentsError("反向 checkpoint 的親代與 original_head 不一致")

    changed = _string_list(payload["changed"], "changed")
    downstream = _string_list(payload["downstream"], "downstream")
    reverted = _string_list(payload["reverted"], "reverted")
    if any(_HASH_RE.fullmatch(item) is None for item in reverted):
        raise AgentsError("反向 checkpoint 的 reverted hash 格式無法解析")

    raw_statuses = payload["statuses"]
    if not isinstance(raw_statuses, list):
        raise AgentsError("反向 checkpoint 的 statuses 格式無法解析")
    statuses: list[tuple[str, str]] = []
    for item in raw_statuses:
        if (not isinstance(item, list) or len(item) != 2
                or not isinstance(item[0], str)
                or not isinstance(item[1], str)
                or item[1] not in _TASK_STATUSES):
            raise AgentsError("反向 checkpoint 的 statuses 格式無法解析")
        statuses.append((item[0], item[1]))
    if len({task_id for task_id, _ in statuses}) != len(statuses):
        raise AgentsError("反向 checkpoint 的 statuses 含重複任務")

    return _RevertRecord(
        checkpoint=checkpoint, original_head=original_head, target=target,
        cascade=cascade, reason=reason, downstream=downstream,
        statuses=tuple(statuses), changed=changed, reverted=reverted)


def _validate_revert_record(record: _RevertRecord, plan: Plan,
                            target: Task, downstream: list[Task],
                            path: Path, name: str) -> list[Task]:
    """確認反向 checkpoint 可由目前計畫無歧義地續作。"""
    expected_plan_ids = (target.id, *(task.id for task in downstream))
    if tuple(task_id for task_id, _ in record.statuses) != expected_plan_ids:
        raise AgentsError("反向 checkpoint 與目前相依範圍不一致")
    if record.downstream != expected_plan_ids[1:]:
        raise AgentsError("反向 checkpoint 與目前下游範圍不一致")

    original = dict(record.statuses)
    expected_changed = [target.id]
    if record.cascade:
        expected_changed.extend(
            task.id for task in downstream
            if original[task.id] in _CASCADE_STATUSES)
    if tuple(expected_changed) != record.changed or original[target.id] == "TODO":
        raise AgentsError("反向 checkpoint 的狀態連動範圍不一致")

    changed: list[Task] = []
    for task_id in record.changed:
        task = plan.get(task_id)
        if task is None:
            raise AgentsError(f"反向 checkpoint 的任務已不存在:{task_id}")
        changed.append(task)

    candidates = _revert_candidates(
        path, name, set(record.changed), ref=record.original_head)
    if tuple(candidates) != record.reverted:
        raise AgentsError("反向 checkpoint 保存的 hashes 與歷史不一致")
    return changed


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

    downstream = _downstream_tasks(plan, task_id)
    blockers = [task.id for task in downstream
                if task.status in _CASCADE_STATUSES]
    if target.status != "TODO" and blockers and not cascade:
        print(f"{name}:{task_id} 有下游任務必須一併重開；"
              f"請明示 cascade:{', '.join(blockers)}")
        return 1

    path = gitops.worktree_path(cfg.root, name)
    changed: list[Task] = []
    journal_entries: dict[str, list[dict]] = {}
    log_values: dict[str, tuple[str, str]] = {}
    reverted: list[str] | None = None
    revert_checkpoint: str | None = None
    resuming = False

    # 反向 checkpoint 是唯一先於管理檔落地的事實；HEAD 若是同一筆尚未完成的
    # checkpoint，就由其 commit body 重建原始參數與狀態，不再反向任何程式碼。
    if revert_code:
        try:
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
                complete = all(
                    _has_entry(record_entries[task.id],
                               *record_log_values[task.id])
                    for task in record_changed
                )
                if not complete:
                    if record.cascade != cascade or record.reason != effective_reason:
                        raise AgentsError(
                            "已有未完成的反向 checkpoint；請用完全相同參數重跑")
                    for task in record_changed:
                        if task.status not in {original[task.id], "TODO"}:
                            raise AgentsError(
                                f"反向 checkpoint 後任務狀態另有變動:{task.id}")
                    _ensure_management_writable(record_changed)
                    changed = record_changed
                    journal_entries = record_entries
                    log_values = record_log_values
                    head = record.original_head
                    reverted = list(record.reverted)
                    revert_checkpoint = record.checkpoint
                    resuming = True
                    print(
                        f"{name}:{task_id} 續作未完成的反向 checkpoint:"
                        f"{record.checkpoint}")
        except (AgentsError, OSError, ValueError) as e:
            print(f"{name}:任務重開中止(Git 或續作預檢失敗:{e})，狀態未變更")
            return 1

    if not resuming:
        if target.status == "TODO":
            print(f"{name}:{task_id} 已是 TODO，無須重開")
            return 1
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

    try:
        if revert_code and not resuming:
            reverted = _revert_candidates(
                path, name, {task.id for task in changed})
        elif not revert_code and path.exists():
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
        elif not revert_code:
            head = gitops.commit_of(cfg.root, "HEAD")
            print(f"{name}:worktree 不存在，只重開管理狀態")
    except AgentsError as e:
        print(f"{name}:任務重開中止(Git 預檢或封存失敗:{e})，狀態未變更")
        return 1

    if revert_code and not resuming:
        assert reverted is not None
        message = _revert_message(
            name, target, cascade, effective_reason, downstream, changed,
            head, reverted)
        print(f"{name}:{target.id} 將反向下列程式碼檢查點:")
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
        # 狀態先寫、日誌最後寫；完整的同 checkpoint 日誌因此可作為耐久的完成
        # 標記，讓已完成舊 rework 與管理面中斷的本次 rework 能無歧義區分。
        try:
            for task in ordered:
                if task.status != "TODO":
                    set_status(task.path, "TODO")
                    print(f"  任務 {task.id}:{task.status} -> TODO")
        except (AgentsError, OSError) as e:
            print(f"{name}:任務狀態寫入中斷({e})；請用相同參數重跑")
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
            print(f"{name}:重開日誌寫入中斷({e})；請用相同參數重跑")
            return 1
    else:
        try:
            # 預設路徑沿用 t001 的交易順序：日誌完成後才動狀態。
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
        # 下游先寫、目標最後寫。目標仍非 TODO 時，相同命令可續作。
        try:
            for task in ordered:
                set_status(task.path, "TODO")
                print(f"  任務 {task.id}:{task.status} -> TODO")
        except (AgentsError, OSError) as e:
            print(f"{name}:任務狀態寫入中斷({e})；請用相同參數重跑")
            return 1

    print(f"{name}:{target.id} 重開完成(共 {len(changed)} 個任務改為 TODO)")
    return 0
