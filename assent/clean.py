"""Safely clean up worktrees and merged branches that are provably redundant for a task folder."""
from __future__ import annotations

from pathlib import Path

from assent import AssentError, gitops
from assent.config import Config
from assent.lockfile import LockBusy, LockMissing, probe_lock


def _has_cleanup_target(cfg: Config) -> bool:
    """Quickly check whether a fixed worktree path or same-prefix branches exist."""
    return (gitops.worktree_path(cfg.root, cfg.tasks_name).exists()
            or bool(gitops.branches_with_prefix(cfg.root, cfg.branch_prefix)))


def _unmerged_branches(root: Path, branches: list[str], head: str) -> list[str]:
    """List branches whose tip is not an ancestor of the given main-tree HEAD."""
    return [branch for branch in branches
            if not gitops.is_ancestor(root, branch, head)]


def _remove_empty_container(path: Path) -> None:
    """Remove the container directory (<repo>.worktrees) once it is empty.

    This is attempted once right after a worktree is removed, and once again at every
    cleanup entry point; the entry-point attempt clears an empty container left over
    from a previous run (where rmdir failed at the time, or the worktree was removed by
    some other means). rmdir only succeeds on an empty directory, which naturally avoids
    removing another task folder's worktree by mistake; a non-empty directory or a
    failed removal (e.g. held open by another process) is silently retained and does
    not affect the cleanup result.
    """
    try:
        path.parent.rmdir()
    except OSError:
        pass


def _print_retained_branches(branches: list[str], unmerged: set[str]) -> None:
    for branch in branches:
        if branch in unmerged:
            print(f"  branch {branch}: skipped (not yet merged, retained)")
        else:
            print(f"  branch {branch}: skipped (another same-prefix branch is not "
                  "yet merged, retained)")


def clean_folder(cfg: Config) -> int:
    """Clean up one task folder; returns 1 only when a Git query or actual removal fails."""
    name = cfg.tasks_name
    path = gitops.worktree_path(cfg.root, name)
    _remove_empty_container(path)
    try:
        if not _has_cleanup_target(cfg):
            print(f"{name}: skipped (no worktree or branch to clean up)")
            return 0
    except AssentError as e:
        print(f"{name}: skipped (Git query failed: {e})")
        return 1

    try:
        with probe_lock(cfg.tasks_dir, name):
            return _clean_locked(cfg, path)
    except LockBusy:
        print(f"{name}: skipped (a run is in progress, refusing cleanup)")
        return 0
    except LockMissing as e:
        print(f"{name}: skipped ({e})")
        return 0
    except AssentError as e:
        print(f"{name}: skipped (lock file could not be safely acquired: {e})")
        return 0


def _clean_locked(cfg: Config, path: Path) -> int:
    """With the task-folder lock already held, re-gather evidence and perform cleanup."""
    root = cfg.root
    name = cfg.tasks_name
    try:
        branches = gitops.branches_with_prefix(root, cfg.branch_prefix)
        head = gitops.head_ref(root)
        if head is None:
            raise AssentError("The main tree currently has no verifiable HEAD commit")

        if path.exists():
            if not gitops.is_repo_worktree(root, path):
                print(f"{name}: skipped (fixed path is not a valid worktree of "
                      f"this repo: {path})")
                return 0
            try:
                gitops.ensure_clean(path)
            except AssentError as e:
                # Match the English substring of gitops's (still bilingual, out-of-scope
                # for this task) message, so no Han characters are needed here.
                if "Working tree is not clean" in str(e):
                    print(f"{name}: skipped (worktree not clean, retained)\n{e}")
                    return 0
                raise

            branch = gitops.current_branch(path)
            if branch and not branch.startswith(cfg.branch_prefix):
                print(f"{name}: skipped (worktree is on branch {branch}, which "
                      "does not belong to this folder, retained)")
                return 0
            # When attached to this folder's branch, the "all same-prefix branches"
            # check below decides and reports on it uniformly; a detached HEAD has no
            # branch to protect it, so we must separately prove the detached tip has
            # been merged.
            if not branch:
                worktree_head = gitops.head_ref(path)
                if worktree_head is None or not gitops.is_ancestor(
                        root, worktree_head, head):
                    print(f"{name}: skipped (worktree HEAD not yet merged, retained)")
                    return 0

        unmerged = _unmerged_branches(root, branches, head)
    except AssentError as e:
        print(f"{name}: skipped (Git evidence gathering failed: {e})")
        return 1

    if unmerged:
        retained = ("both worktree and branches retained" if path.exists()
                    else "branches retained")
        print(f"{name}: skipped (not all same-prefix branches are merged, "
              f"{retained})")
        _print_retained_branches(branches, set(unmerged))
        return 0

    failed = False
    if path.exists():
        try:
            gitops.remove_worktree(root, path)
            _remove_empty_container(path)
            print(f"{name}: cleaned (worktree {path})")
        except AssentError as e:
            print(f"{name}: failed (worktree removal failed: {e})")
            return 1
    else:
        print(f"{name}: worktree does not exist, continuing to clean up merged branches")

    for branch in branches:
        try:
            gitops.delete_branch(root, branch)
            print(f"  branch {branch}: cleaned")
        except AssentError as e:
            failed = True
            print(f"  branch {branch}: failed ({e})")
    return 1 if failed else 0


def clean_folders(configs: list[Config]) -> int:
    """Clean up all configs in order; one item's failure does not block later items."""
    failed = False
    for index, cfg in enumerate(configs):
        if index:
            print()
        if clean_folder(cfg) != 0:
            failed = True
    return 1 if failed else 0
