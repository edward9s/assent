"""Safely clean up worktrees and merged branches that are provably redundant for a task folder."""
from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path

from assent import AssentError, gitops
from assent.accept import _source_snapshot
from assent.config import Config
from assent.folderdeps import parse_folder_dependency_graph
from assent.lockfile import (LockBusy, LockMissing, hold_integration_lock,
                             probe_lock)
from assent.plan import Plan


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
    """Clean one folder; return 1 when evidence is invalid or an action fails."""
    name = cfg.tasks_name
    path = gitops.worktree_path(cfg.root, name)
    try:
        # Keep the same integration-then-folder lock order as accept.  The
        # dependency proof and every destructive action happen inside both.
        with hold_integration_lock(cfg.assent_dir):
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
    except LockBusy:
        print(f"{name}: skipped (repository integration is in progress, refusing cleanup)")
        return 0
    except AssentError as e:
        print(f"{name}: failed (integration lock could not be safely acquired: {e})")
        return 1


def _direct_dependents(graph, target: str) -> list[str]:
    """Return folders that directly name ``target`` in their ``after`` list."""
    return sorted(name for name, dependencies in graph.items()
                  if target in dependencies.after)


def _print_dependency_retention(name: str, problems: list[tuple[str, str]]) -> None:
    print(f"{name}: skipped (dependent source evidence is still required; "
          "worktree and branches retained)")
    for dependent, reason in problems:
        print(f"  dependent {dependent}: {reason}")
    print("  Complete and verify each dependent, accept it, then clean in "
          "upstream-first order; run clean again for the dependent afterward.")


def _lock_and_check_dependents(cfg: Config, target_head: str,
                               stack: ExitStack) -> tuple[bool, int]:
    """Lock direct dependents and prove their source no longer needs ``target``.

    The graph is parsed once to discover the locks and again after every
    available dependent lock is held.  A changed set is refused rather than
    allowing an unprotected dependent into the proof.
    """
    name = cfg.tasks_name
    try:
        graph = parse_folder_dependency_graph(cfg.assent_dir)
    except AssentError as e:
        print(f"{name}: failed (folder dependency graph is invalid: {e})")
        return False, 1

    dependents = _direct_dependents(graph, name)
    lock_problems: dict[str, str] = {}
    for dependent in dependents:
        try:
            stack.enter_context(probe_lock(
                cfg.assent_dir / dependent, dependent))
        except LockBusy:
            lock_problems[dependent] = (
                "its task folder is being changed by another run")
        except LockMissing as e:
            lock_problems[dependent] = str(e)
        except AssentError as e:
            lock_problems[dependent] = f"its task-folder lock is unavailable: {e}"

    try:
        locked_graph = parse_folder_dependency_graph(cfg.assent_dir)
        locked_dependents = _direct_dependents(locked_graph, name)
        if locked_dependents != dependents:
            raise AssentError(
                "direct dependents changed while cleanup was acquiring locks "
                f"({', '.join(dependents) or 'none'} -> "
                f"{', '.join(locked_dependents) or 'none'})")
        plans = {
            folder: Plan.parse(cfg.assent_dir / folder)
            for folder in locked_graph
        }
    except AssentError as e:
        print(f"{name}: failed (dependency evidence could not be parsed: {e})")
        return False, 1

    problems: list[tuple[str, str]] = []
    for dependent in dependents:
        if dependent in lock_problems:
            problems.append((dependent, lock_problems[dependent]))
            continue
        unfinished = [
            f"{task.id}={task.status}" for task in plans[dependent].tasks
            if task.status not in ("DONE", "SKIP")
        ]
        if unfinished:
            problems.append((
                dependent, f"unfinished tasks: {', '.join(unfinished)}"))
            continue
        try:
            _branch, source_tip, _worktree = _source_snapshot(
                gitops.main_worktree(cfg.root), dependent, cfg.git_excludes,
                operation="cleanup dependency proof")
        except AssentError as e:
            problems.append((dependent, str(e)))
            continue
        if not gitops.is_ancestor(cfg.root, source_tip, target_head):
            problems.append((
                dependent,
                f"current source tip {source_tip[:12]} is not integrated into "
                f"the current target {target_head[:12]}"))

    if problems:
        _print_dependency_retention(name, problems)
        return False, 0
    return True, 0


def _clean_locked(cfg: Config, path: Path) -> int:
    """With the task-folder lock already held, re-gather evidence and perform cleanup."""
    root = cfg.root
    name = cfg.tasks_name
    try:
        head = gitops.head_ref(root)
        if head is None:
            raise AssentError("The main tree currently has no verifiable HEAD commit")

        with ExitStack() as dependent_locks:
            safe, result = _lock_and_check_dependents(
                cfg, head, dependent_locks)
            if not safe:
                return result

            # Even removing a leftover empty container waits until the complete
            # dependency proof above has succeeded.
            _remove_empty_container(path)
            if not _has_cleanup_target(cfg):
                print(f"{name}: skipped (no worktree or branch to clean up)")
                return 0

            branches = gitops.branches_with_prefix(root, cfg.branch_prefix)

            if path.exists():
                if not gitops.is_repo_worktree(root, path):
                    print(f"{name}: skipped (fixed path is not a valid worktree of "
                          f"this repo: {path})")
                    return 0
                try:
                    gitops.ensure_clean(path)
                except AssentError as e:
                    # Match the stable English prefix emitted by gitops.
                    if "Working tree is not clean" in str(e):
                        print(f"{name}: skipped (worktree not clean, retained)\n{e}")
                        return 0
                    raise

                branch = gitops.current_branch(path)
                if branch and not branch.startswith(cfg.branch_prefix):
                    print(f"{name}: skipped (worktree is on branch {branch}, which "
                          "does not belong to this folder, retained)")
                    return 0
                # When attached to this folder's branch, the all-prefix check
                # below decides uniformly.  A detached tip needs its own proof.
                if not branch:
                    worktree_head = gitops.head_ref(path)
                    if worktree_head is None or not gitops.is_ancestor(
                            root, worktree_head, head):
                        print(f"{name}: skipped (worktree HEAD not yet merged, retained)")
                        return 0

            unmerged = _unmerged_branches(root, branches, head)

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
    except AssentError as e:
        print(f"{name}: failed (Git evidence gathering failed: {e})")
        return 1


def clean_folders(configs: list[Config]) -> int:
    """Clean folders upstream-first; one item's result does not block unrelated items."""
    if not configs:
        return 0
    try:
        graph = parse_folder_dependency_graph(configs[0].assent_dir)
    except AssentError as e:
        print(f"clean failed (folder dependency graph is invalid: {e})")
        return 1

    by_name = {cfg.tasks_name: cfg for cfg in configs}
    ordered: list[Config] = []
    visited: set[str] = set()

    def add_with_prerequisites(folder: str) -> None:
        if folder in visited:
            return
        visited.add(folder)
        for prerequisite in graph[folder].after:
            if prerequisite in by_name:
                add_with_prerequisites(prerequisite)
        ordered.append(by_name[folder])

    for folder in sorted(by_name):
        add_with_prerequisites(folder)

    failed = False
    for index, cfg in enumerate(ordered):
        if index:
            print()
        if clean_folder(cfg) != 0:
            failed = True
    return 1 if failed else 0
