"""Implementation of manual-decision rejection for a task folder and its task statuses."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from assent import AssentError, gitops
from assent.config import Config
from assent.lockfile import LockBusy, LockMissing, probe_lock
from assent.plan import Plan, append_entry, set_status

RESETTABLE_STATUSES = ("DONE", "WIP", "BLOCKED")


def _remove_empty_container(path: Path) -> None:
    """Remove the worktree container once empty; retained if non-empty or removal fails."""
    try:
        path.parent.rmdir()
    except OSError:
        pass


def reject_folder(cfg: Config, confirm: Callable[[str], str] | None = None) -> int:
    """Archive, then force-remove the worktree and same-prefix branches, and reset tasks to TODO.

    Returns 1 for a busy lock, missing lock, task-file precheck failure, a declined
    confirmation, or any Git step failure. Task files must parse completely before
    any destructive Git operation; task statuses are reset only after the entire
    Git scene has been cleared successfully.
    """
    name = cfg.tasks_name
    path = gitops.worktree_path(cfg.root, name)
    _remove_empty_container(path)
    try:
        with probe_lock(cfg.tasks_dir, name):
            return _reject_locked(cfg, path, confirm)
    except LockBusy as e:
        print(f"{name}: reject aborted (a run is in progress): {e}")
        return 1
    except (LockMissing, AssentError) as e:
        print(f"{name}: reject aborted ({e})")
        return 1


def _confirm_destructive(name: str, path: Path, branches: list[str],
                         reset_candidates: list, confirm: Callable[[str], str] | None) -> bool:
    """Print a preview of what reject is about to destroy and ask for interactive
    confirmation. Anything other than exactly "y"/"Y", including EOF, declines."""
    print(f"{name}: about to reject (destructive, cannot be undone):")
    print(f"  worktree: {path}")
    if branches:
        print("  branches to delete:")
        for branch in branches:
            print(f"    {branch}")
    else:
        print("  branches to delete: none")
    if reset_candidates:
        print("  tasks to reset to TODO:")
        for task in reset_candidates:
            print(f"    {task.id}: {task.status}")
    else:
        print("  tasks to reset to TODO: none")

    ask = confirm if confirm is not None else input
    try:
        answer = ask("Continue? [y/N]: ")
    except EOFError:
        answer = ""
    return answer.strip().lower() == "y"


def _reject_locked(cfg: Config, path: Path,
                   confirm: Callable[[str], str] | None = None) -> int:
    """With the lock held, precheck the task files first, confirm interactively,
    then clear the Git scene, and finally reset task statuses."""
    root = cfg.root
    name = cfg.tasks_name
    try:
        plan = Plan.parse(cfg.tasks_dir)
    except AssentError as e:
        print(f"{name}: reject aborted (task files could not be parsed: {e}), "
              "Git scene unchanged")
        return 1

    branches = gitops.branches_with_prefix(root, cfg.branch_prefix)
    reset_candidates = [t for t in plan.tasks if t.status in RESETTABLE_STATUSES]
    if not _confirm_destructive(name, path, branches, reset_candidates, confirm):
        print(f"{name}: reject cancelled, no changes made")
        return 1

    evidence: list[str] = []
    try:
        if path.exists():
            if not gitops.is_repo_worktree(root, path):
                print(f"{name}: reject aborted (fixed path is not a valid "
                      f"worktree of this repo: {path})")
                return 1
            # Following the same wip-checkpoint philosophy as the engine: archive
            # uncommitted changes into a commit first, so the later worktree remove
            # does not need force.
            if gitops.commit_if_dirty(
                    path, f"wip({name}): reject archive, preserving uncommitted changes",
                    cfg.git_excludes):
                print(f"{name}: uncommitted changes archived as a wip commit")
            head = gitops.commit_of(path, "HEAD")
            evidence.append(f"worktree HEAD {head}")
            gitops.remove_worktree(root, path)
            _remove_empty_container(path)
            print(f"{name}: removed worktree {path} (HEAD {head})")
        else:
            print(f"{name}: worktree does not exist, continuing to reject branches")

        # Branches without this prefix are never touched; the full tip hash is shown
        # in the terminal and, after success, written into each reset task's rejected
        # journal entry for later recovery.
        for branch in branches:
            tip = gitops.commit_of(root, branch)
            gitops.delete_branch_force(root, branch)
            evidence.append(f"branch {branch} tip {tip}")
            print(f"  branch {branch} (tip {tip}): deleted (recoverable by hash "
                  "only within the gc grace period)")
    except AssentError as e:
        print(f"{name}: reject aborted (Git step failed: {e}), task files not reset")
        return 1
    return _reset_rejected_tasks(cfg, plan, evidence)


def _reset_rejected_tasks(cfg: Config, plan: Plan,
                          evidence: list[str]) -> int:
    """Reset DONE/WIP/BLOCKED back to TODO, and preserve full Git evidence in the r file."""
    name = cfg.tasks_name
    reset = 0
    detail = "Git evidence before deletion:\n" + (
        "\n".join(evidence) if evidence else "no worktree or same-prefix branches")
    try:
        for task in plan.tasks:
            # SKIP is an explicit human decision to abandon; TODO is already
            # pending — reject does not override either.
            if task.status not in RESETTABLE_STATUSES:
                continue
            set_status(task.path, "TODO")
            append_entry(
                task.journal_path, by="scheduler", event="rejected",
                summary=f"Manual-decision reject; scheduler reset status "
                        f"{task.status} back to TODO",
                detail=detail)
            print(f"  task {task.id}: {task.status} -> TODO")
            reset += 1
    except (AssentError, OSError) as e:
        print(f"{name}: task-file reset interrupted ({e}), rerun assent reject {name}")
        return 1
    print(f"{name}: reject complete ({reset} task(s) reset to TODO). "
          "Revise task files as needed then rerun with assent run; assent report "
          "can refresh the report.")
    return 0
