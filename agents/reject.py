"""人工裁決駁回工作資料夾的實作與任務狀態。"""
from __future__ import annotations

from pathlib import Path

from agents import AgentsError, gitops
from agents.config import Config
from agents.lockfile import LockBusy, LockMissing, probe_lock
from agents.plan import Plan, append_entry, set_status


def _remove_empty_container(path: Path) -> None:
    """worktree 容器已空時移除；非空或移除失敗皆保留。"""
    try:
        path.parent.rmdir()
    except OSError:
        pass


def reject_folder(cfg: Config) -> int:
    """封存後強制清除 worktree 與同前綴分支，並把任務改回 TODO。

    鎖忙、鎖缺、任務檔預檢或任何 Git 步驟失敗一律回傳 1。任務檔必須在
    破壞性 Git 操作前完整解析；Git 現場全數清除成功後才重置任務狀態。
    """
    name = cfg.tasks_name
    path = gitops.worktree_path(cfg.root, name)
    _remove_empty_container(path)
    try:
        with probe_lock(cfg.tasks_dir, name):
            return _reject_locked(cfg, path)
    except LockBusy as e:
        print(f"{name}:駁回中止(run 進行中):{e}")
        return 1
    except (LockMissing, AgentsError) as e:
        print(f"{name}:駁回中止({e})")
        return 1


def _reject_locked(cfg: Config, path: Path) -> int:
    """持鎖後先預檢任務，再清 Git 現場，最後重置任務狀態。"""
    root = cfg.root
    name = cfg.tasks_name
    try:
        plan = Plan.parse(cfg.tasks_dir)
    except AgentsError as e:
        print(f"{name}:駁回中止(任務檔無法解析:{e}),Git 現場未變動")
        return 1

    evidence: list[str] = []
    try:
        branches = gitops.branches_with_prefix(root, cfg.branch_prefix)
        if path.exists():
            if not gitops.is_repo_worktree(root, path):
                print(f"{name}:駁回中止(固定路徑不是本 repo 的有效 worktree:"
                      f"{path})")
                return 1
            # 比照 engine 的 wip 檢查點哲學：先把未提交變更封存進 commit，
            # 之後的 worktree remove 就不需要 force。
            if gitops.commit_if_dirty(
                    path, f"wip({name}): 駁回封存,保全未提交變更",
                    cfg.git_excludes):
                print(f"{name}:未提交變更已封存為 wip commit")
            head = gitops.commit_of(path, "HEAD")
            evidence.append(f"worktree HEAD {head}")
            gitops.remove_worktree(root, path)
            _remove_empty_container(path)
            print(f"{name}:已除 worktree {path}(HEAD {head})")
        else:
            print(f"{name}:worktree 不存在,繼續駁回分支")

        # 非同前綴分支一概不碰；完整 tip hash 同時顯示於終端，並在成功後
        # 寫入每個被重置任務的 rejected 日誌供日後救援。
        for branch in branches:
            tip = gitops.commit_of(root, branch)
            gitops.delete_branch_force(root, branch)
            evidence.append(f"分支 {branch} tip {tip}")
            print(f"  分支 {branch}(tip {tip}):已刪(僅 gc 期限內可用 hash 救回)")
    except AgentsError as e:
        print(f"{name}:駁回中止(Git 步驟失敗:{e}),任務檔未重置")
        return 1
    return _reset_rejected_tasks(cfg, plan, evidence)


def _reset_rejected_tasks(cfg: Config, plan: Plan,
                          evidence: list[str]) -> int:
    """把 DONE/WIP/BLOCKED 改回 TODO，並以 r 檔保存完整 Git 存證。"""
    name = cfg.tasks_name
    reset = 0
    detail = "刪除前 Git 存證:\n" + (
        "\n".join(evidence) if evidence else "沒有 worktree 或同前綴分支")
    try:
        for task in plan.tasks:
            # SKIP 是人的明示放棄，TODO 本來就待做，駁回都不推翻。
            if task.status not in ("DONE", "WIP", "BLOCKED"):
                continue
            set_status(task.path, "TODO")
            append_entry(
                task.journal_path, by="scheduler", event="rejected",
                summary=f"人工裁決駁回,調度器把狀態 {task.status} 改回 TODO",
                detail=detail)
            print(f"  任務 {task.id}:{task.status} -> TODO")
            reset += 1
    except (AgentsError, OSError) as e:
        print(f"{name}:任務檔重置中斷({e}),請重跑 agents reject {name}")
        return 1
    print(f"{name}:駁回完成(重置 {reset} 個任務為 TODO)。"
          "視需要修訂任務檔後 agents run 重做;agents report 可更新報告。")
    return 0
