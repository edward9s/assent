"""Implementation of manual-decision rejection for a task folder and its task statuses."""
from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from typing import Callable

from assent import AssentError, gitops, verification
from assent.config import Config
from assent.folder_source import COMPLETE_STATUSES, resolve_source_snapshot
from assent.folderdeps import direct_dependents, parse_folder_dependency_graph
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
            with ExitStack() as dependent_locks:
                return _reject_locked(cfg, path, dependent_locks, confirm)
    except LockBusy as e:
        print(f"{name}: reject aborted (a run is in progress): {e}")
        return 1
    except (LockMissing, AssentError) as e:
        print(f"{name}: reject aborted ({e})")
        return 1


def _confirm_destructive(name: str, path: Path, branches: list[str],
                         reset_candidates: list, stranded: list[tuple[str, str]],
                         confirm: Callable[[str], str] | None) -> bool:
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
    if stranded:
        print("  unaccepted dependent folders that would be stranded:")
        for dependent, reason in stranded:
            print(f"    {dependent}: {reason}")
        print("  Two ways forward: (a) reject each dependent first, bottom-up, "
              "then reject this folder; or (b) confirm below to accept stranding "
              "them.")

    ask = confirm if confirm is not None else input
    try:
        answer = ask("Continue? [y/N]: ")
    except EOFError:
        answer = ""
    return answer.strip().lower() == "y"


def _check_dependents(cfg: Config, stack: ExitStack
                      ) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Lock direct dependent folders and classify each as busy or unaccepted.

    Reads the dependent set from ``folderdeps.direct_dependents``, the same
    shared edge lookup ``clean`` uses, instead of a second implementation. Every
    direct dependent's ``assent.lock`` is held via ``stack`` for the rest of this
    reject, the same way ``clean.py`` ``_lock_and_check_dependents`` holds them
    for the rest of a cleanup. A
    dependent whose source tip is already an ancestor of the main worktree
    HEAD is accepted and never reported. Returns ``(busy, unaccepted)``:
    ``busy`` is non-empty only when some dependent's own run lock could not be
    probed (it is currently running), which must abort before any
    confirmation prompt; ``unaccepted`` lists every other dependent not yet
    provably accepted, paired with the reason.
    """
    name = cfg.tasks_name
    graph = parse_folder_dependency_graph(cfg.assent_dir)
    dependents = direct_dependents(graph, name)
    if not dependents:
        return [], []

    busy: list[tuple[str, str]] = []
    unaccepted: list[tuple[str, str]] = []
    for dependent in dependents:
        try:
            stack.enter_context(probe_lock(cfg.assent_dir / dependent, dependent))
        except LockBusy:
            busy.append((dependent, "its task folder is being changed by another run"))
        except LockMissing as e:
            unaccepted.append((dependent, str(e)))
        except AssentError as e:
            unaccepted.append((dependent, f"its task-folder lock is unavailable: {e}"))

    locked_graph = parse_folder_dependency_graph(cfg.assent_dir)
    locked_dependents = direct_dependents(locked_graph, name)
    if locked_dependents != dependents:
        raise AssentError(
            "direct dependents changed while reject was acquiring locks "
            f"({', '.join(dependents) or 'none'} -> "
            f"{', '.join(locked_dependents) or 'none'})")

    if busy:
        return busy, []

    main = gitops.main_worktree(cfg.root)
    head = gitops.head_ref(main)
    if head is None:
        raise AssentError("The main tree currently has no verifiable HEAD commit")

    already_unaccepted = {dependent for dependent, _ in unaccepted}
    for dependent in dependents:
        if dependent in already_unaccepted:
            continue
        plan = Plan.parse(cfg.assent_dir / dependent)
        unfinished = [f"{task.id}={task.status}" for task in plan.tasks
                      if task.status not in COMPLETE_STATUSES]
        if unfinished:
            unaccepted.append((dependent, f"unfinished tasks: {', '.join(unfinished)}"))
            continue
        try:
            _branch, source_tip, _worktree = resolve_source_snapshot(
                main, dependent, cfg.git_excludes, operation="reject dependency proof")
        except AssentError as e:
            unaccepted.append((dependent, str(e)))
            continue
        if not gitops.is_ancestor(main, source_tip, head):
            unaccepted.append((
                dependent,
                f"current source tip {source_tip[:12]} is not yet merged into "
                f"the main worktree HEAD {head[:12]}"))
    unaccepted.sort()
    return [], unaccepted


def _reject_locked(cfg: Config, path: Path, dependent_locks: ExitStack,
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

    busy, stranded = _check_dependents(cfg, dependent_locks)
    if busy:
        print(f"{name}: reject aborted (an unaccepted dependent folder is busy):")
        for dependent, reason in busy:
            print(f"  dependent {dependent}: {reason}")
        return 1

    branches = gitops.branches_with_prefix(root, cfg.branch_prefix)
    reset_candidates = [t for t in plan.tasks if t.status in RESETTABLE_STATUSES]
    if not _confirm_destructive(name, path, branches, reset_candidates, stranded, confirm):
        print(f"{name}: reject cancelled, no changes made")
        return 1

    # Invalidated before the first destructive step, so no window exists in which
    # a batch receipt still authorizes releasing a source this reject has already
    # deleted. A receipt is derived evidence; the only cost is one more
    # `assent verify --batch`.
    if verification.invalidate_batch_receipt(cfg.assent_dir):
        print(f"{name}: batch verification receipt invalidated; run "
              "`assent verify --batch` again before the next batch release")

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
    return _reset_rejected_tasks(cfg, plan, evidence, stranded)


def _reset_rejected_tasks(cfg: Config, plan: Plan, evidence: list[str],
                          stranded: list[tuple[str, str]] = ()) -> int:
    """Reset DONE/WIP/BLOCKED back to TODO, and preserve full Git evidence in the r file."""
    name = cfg.tasks_name
    reset = 0
    detail = "Git evidence before deletion:\n" + (
        "\n".join(evidence) if evidence else "no worktree or same-prefix branches")
    if stranded:
        detail += "\n\nConfirmed stranding unaccepted dependent folder(s):\n" + "\n".join(
            f"{dependent}: {reason}" for dependent, reason in stranded)
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
