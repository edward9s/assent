"""安全清除可證明冗餘的工作資料夾 worktree 與已併入分支。"""
from __future__ import annotations

from pathlib import Path

from agents import AgentsError, gitops
from agents.config import Config
from agents.lockfile import LockBusy, LockMissing, probe_lock


def _has_cleanup_target(cfg: Config) -> bool:
    """快速判斷是否有固定 worktree 路徑或同前綴分支。"""
    return (gitops.worktree_path(cfg.root, cfg.tasks_name).exists()
            or bool(gitops.branches_with_prefix(cfg.root, cfg.branch_prefix)))


def _unmerged_branches(root: Path, branches: list[str], head: str) -> list[str]:
    """列出 tip 不是指定主樹 HEAD 祖先的分支。"""
    return [branch for branch in branches
            if not gitops.is_ancestor(root, branch, head)]


def _remove_empty_container(path: Path) -> None:
    """容器目錄(<repo>.worktrees)已空就順手刪掉。

    worktree 剛移除後與每次清理入口都會各嘗試一次;入口那次負責補刪
    前回殘留的空容器(當時 rmdir 失敗或 worktree 由其他途徑移除)。
    rmdir 只在空目錄成功,天然避免誤刪其他工作資料夾的 worktree;
    非空或移除失敗(如其他行程佔用)都靜默保留,不影響清理結果。
    """
    try:
        path.parent.rmdir()
    except OSError:
        pass


def _print_retained_branches(branches: list[str], unmerged: set[str]) -> None:
    for branch in branches:
        if branch in unmerged:
            print(f"  分支 {branch}:跳過(尚未併入,保留)")
        else:
            print(f"  分支 {branch}:跳過(同前綴仍有尚未併入分支,保留)")


def clean_folder(cfg: Config) -> int:
    """清理一個工作資料夾；只有 Git 查詢或實際刪除失敗時回傳 1。"""
    name = cfg.tasks_name
    path = gitops.worktree_path(cfg.root, name)
    _remove_empty_container(path)
    try:
        if not _has_cleanup_target(cfg):
            print(f"{name}:跳過(沒有可清理的 worktree 或分支)")
            return 0
    except AgentsError as e:
        print(f"{name}:跳過(Git 查詢失敗:{e})")
        return 1

    try:
        with probe_lock(cfg.tasks_dir, name):
            return _clean_locked(cfg, path)
    except LockBusy:
        print(f"{name}:跳過(run 進行中,拒絕清理)")
        return 0
    except LockMissing as e:
        print(f"{name}:跳過({e})")
        return 0
    except AgentsError as e:
        print(f"{name}:跳過(鎖檔無法安全取得:{e})")
        return 0


def _clean_locked(cfg: Config, path: Path) -> int:
    """已持有工作資料夾鎖後重新取證並執行清理。"""
    root = cfg.root
    name = cfg.tasks_name
    try:
        branches = gitops.branches_with_prefix(root, cfg.branch_prefix)
        head = gitops.head_ref(root)
        if head is None:
            raise AgentsError("主樹目前沒有可驗證的 HEAD commit")

        if path.exists():
            if not gitops.is_repo_worktree(root, path):
                print(f"{name}:跳過(固定路徑不是本 repo 的有效 worktree:{path})")
                return 0
            try:
                gitops.ensure_clean(path)
            except AgentsError as e:
                if "工作樹不乾淨" in str(e):
                    print(f"{name}:跳過(worktree 不乾淨,保留)\n{e}")
                    return 0
                raise

            branch = gitops.current_branch(path)
            if branch and not branch.startswith(cfg.branch_prefix):
                print(f"{name}:跳過(worktree 位於非本資料夾分支 {branch},保留)")
                return 0
            # 附著於本資料夾分支時由下方「全部同前綴分支」統一判定並逐條
            # 回報；detached HEAD 沒有分支可保護，需額外證明游離 tip 已併入。
            if not branch:
                worktree_head = gitops.head_ref(path)
                if worktree_head is None or not gitops.is_ancestor(
                        root, worktree_head, head):
                    print(f"{name}:跳過(worktree HEAD 尚未併入,保留)")
                    return 0

        unmerged = _unmerged_branches(root, branches, head)
    except AgentsError as e:
        print(f"{name}:跳過(Git 取證失敗:{e})")
        return 1

    if unmerged:
        retained = "worktree 與分支皆保留" if path.exists() else "分支保留"
        print(f"{name}:跳過(同前綴分支未全部併入,{retained})")
        _print_retained_branches(branches, set(unmerged))
        return 0

    failed = False
    if path.exists():
        try:
            gitops.remove_worktree(root, path)
            _remove_empty_container(path)
            print(f"{name}:已清(worktree {path})")
        except AgentsError as e:
            print(f"{name}:失敗(worktree 移除失敗:{e})")
            return 1
    else:
        print(f"{name}:worktree 不存在,繼續清理已併入分支")

    for branch in branches:
        try:
            gitops.delete_branch(root, branch)
            print(f"  分支 {branch}:已清")
        except AgentsError as e:
            failed = True
            print(f"  分支 {branch}:失敗({e})")
    return 1 if failed else 0


def clean_folders(configs: list[Config]) -> int:
    """依序清理全部設定，單一項目失敗不妨礙後續項目。"""
    failed = False
    for index, cfg in enumerate(configs):
        if index:
            print()
        if clean_folder(cfg) != 0:
            failed = True
    return 1 if failed else 0
