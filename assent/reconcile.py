"""Human-driven reconciliation of one finished folder's conflict with the target.

``reconcile`` prepares, preserves, continues, and aborts a single direct
source-versus-target merge conflict so a human can resolve it in a dedicated
worktree.  It is deliberately not an integration engine: it handles exactly one
folder against the current integration target, never combines speculative peer
folders, never runs a verifier, a focused test, or an AI adapter, never edits a
task status, and never merges anything into the integration target.

There is no state file and no "current folder" pointer.  Everything a later run
needs is a deterministic managed fact or a Git fact:

- worktree ``<project>.reconcile/<folder>``, a sibling of the main worktree,
- temporary branch ``assent-reconcile/<folder>``,
- ``HEAD``, ``MERGE_HEAD``, branch ownership and the merge parents, which
  together say exactly how far an interrupted run got.

The merge is built source-first: the temporary branch starts at the exact source
tip and merges the captured target tip, so the merge commit's first parent is the
original source and the source branch can be *fast-forwarded* onto it -- the
source is never rewritten and the target is never touched.  A target that moves
after start does not rewrite that captured merge; the drift is reported, and a
later ``assent verify`` against the then-current target stays authoritative.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path

from assent import AssentError, gitops
from assent.accept import _COMPLETE_STATUSES, _source_snapshot
from assent.config import Config
from assent.lockfile import LockBusy, hold_integration_lock, hold_lock
from assent.plan import Plan

RECONCILE_BRANCH_PREFIX = "assent-reconcile/"
RECONCILE_TRAILER_FOLDER = "Assent-Reconcile-Folder"


def reconcile_commit_message(folder: str) -> str:
    """Compose the one canonical English reconciliation merge message.

    It depends only on the folder name, so any later run can recompute it and use
    it as ownership proof for a merge commit an interrupted run already created.
    """
    return (f"reconcile({folder}): merge the integration target into the source\n"
            f"\n{RECONCILE_TRAILER_FOLDER}: {folder}\n")


@dataclass(frozen=True)
class _Managed:
    """The deterministic managed resources of one folder's reconciliation."""

    folder: str
    main: Path
    path: Path
    branch: str


def _managed(cfg: Config) -> _Managed:
    main = gitops.main_worktree(cfg.root)
    folder = cfg.tasks_name
    return _Managed(folder, main,
                    gitops.reconcile_worktree_path(main, folder),
                    f"{RECONCILE_BRANCH_PREFIX}{folder}")


def _remove_empty_container(path: Path) -> None:
    """Drop the ``<repo>.reconcile`` container once it holds nothing.

    ``rmdir`` only succeeds on an empty directory, so this can never remove
    another folder's reconciliation; a failure is simply left alone.
    """
    with contextlib.suppress(OSError):
        path.parent.rmdir()


def _require_source(cfg: Config, main: Path) -> tuple[str, str, Path]:
    """Resolve the folder's exact source, requiring its fixed worktree.

    ``_source_snapshot`` is the same identity check verification and acceptance
    perform (sole ``<folder>/*`` branch, attached, clean).  Reconciliation needs
    one fact more: the fast-forward at the end runs *in* the source worktree, so a
    branch without one cannot be advanced without rewriting a ref by hand.
    """
    branch, tip, worktree = _source_snapshot(
        main, cfg.tasks_name, cfg.git_excludes, operation="reconcile")
    if worktree is None:
        raise AssentError(
            f"task folder {cfg.tasks_name} has no fixed source worktree; "
            "reconciliation fast-forwards the source branch inside its own "
            "worktree and never rewrites a branch ref by hand")
    return branch, tip, worktree


def _remove_managed(managed: _Managed, expected_head: str) -> None:
    """Remove the managed worktree and branch, proving ownership of each first.

    Every fact is re-read here rather than remembered from earlier in the run:
    the path is a worktree of this repository, it is attached to the managed
    branch, its ``HEAD`` is exactly the commit the caller proved, it is clean,
    and the branch points at that same commit.  Anything else keeps the resource
    as recovery evidence instead of widening the deletion.
    """
    if managed.path.exists():
        if not gitops.is_repo_worktree(managed.main, managed.path):
            raise AssentError(
                f"refusing to remove {managed.path}: it is not a worktree of "
                "this repository")
        attached = gitops.current_branch(managed.path)
        if attached != managed.branch:
            raise AssentError(
                f"refusing to remove {managed.path}: it is on "
                f"{attached or 'a detached HEAD'}, not {managed.branch}")
        head = gitops.commit_of(managed.path, "HEAD")
        if head != expected_head:
            raise AssentError(
                f"refusing to remove {managed.path}: its HEAD is {head}, not the "
                f"proven {expected_head}")
        if not gitops.working_tree_status(managed.path).is_clean:
            raise AssentError(
                f"refusing to remove {managed.path}: it still holds uncommitted "
                "changes")
        gitops.remove_worktree(managed.main, managed.path)
        _remove_empty_container(managed.path)
        print(f"  reconciliation worktree removed: {managed.path}")

    if gitops.branch_exists(managed.main, managed.branch):
        tip = gitops.branch_tip(managed.main, managed.branch)
        if tip != expected_head:
            raise AssentError(
                f"refusing to delete {managed.branch}: its tip is {tip}, not the "
                f"proven {expected_head}")
        # The managed branch is temporary by construction and its tip is proven
        # above, so a forced delete is the ordinary path here; the hash is
        # printed first so the ref stays recoverable.
        gitops.delete_branch_force(managed.main, managed.branch)
        print(f"  temporary branch removed: {managed.branch} (was {tip})")


def _stage_resolution(worktree: Path) -> str | None:
    """Stage the human's conflict resolution, or say why it is not acceptable.

    The originally unmerged paths are exactly the paths Git still reports as
    unmerged, so they are read from the index rather than remembered across runs.
    """
    unmerged = gitops.conflict_paths(worktree)
    gitops.stage_paths(worktree, unmerged)

    remaining = gitops.conflict_paths(worktree)
    if remaining:
        return ("path(s) are still unmerged after staging: "
                + ", ".join(remaining))
    problem = gitops.diff_cached_check(worktree)
    if problem is not None:
        return ("`git diff --cached --check` refused the staged resolution "
                f"(a leftover conflict marker or whitespace error):\n{problem}")
    status = gitops.working_tree_status(worktree)
    unexpected = sorted(set(status.unstaged) | set(status.untracked))
    if unexpected:
        return ("edit(s) outside the conflict-resolution scene: "
                + ", ".join(unexpected))
    return None


def _fast_forward_source(cfg: Config, managed: _Managed, merge_commit: str,
                         source_parent: str, source_branch: str,
                         source_tip: str, source_worktree: Path) -> int:
    """Advance the source onto a proven merge commit, then clean up.

    Both supported resumptions land here: the source still at the merge's first
    parent (advance it), and a source already fast-forwarded by an interrupted
    run (advance nothing).  Anything else is independent source movement, so the
    reconciliation worktree, branch, and every human edit are preserved.
    """
    label = f"reconcile continue {managed.folder}"
    if source_tip == merge_commit:
        print(f"{label}: source branch {source_branch} was already "
              "fast-forwarded by an earlier interrupted run; not moved again.")
    elif source_tip == source_parent:
        gitops.fast_forward(source_worktree, merge_commit)
        moved = gitops.branch_tip(managed.main, source_branch)
        if moved != merge_commit:
            raise AssentError(
                f"source branch {source_branch} is at {moved} after the "
                f"fast-forward, not the merge commit {merge_commit}")
        print(f"  source branch fast-forwarded: {source_branch} "
              f"{source_parent} -> {merge_commit}")
    else:
        print(f"{label}: refused, source branch {source_branch} moved "
              f"independently to {source_tip} (the reconciliation merges "
              f"{source_parent}). The reconciliation worktree "
              f"{managed.path}, its branch, and every edit were preserved; "
              f"run `assent reconcile --abort {managed.folder}` and start "
              "again once the source is settled.")
        return 1

    if not gitops.working_tree_status(source_worktree, cfg.git_excludes).is_clean:
        raise AssentError(
            f"source worktree {source_worktree} is not clean after the "
            "fast-forward; the managed resources were kept")
    _remove_managed(managed, merge_commit)
    return 0


def _report_target_drift(managed: _Managed, captured: str,
                         current: str, target_branch: str) -> None:
    if captured == current:
        return
    print(f"  note: {target_branch} advanced from {captured} to {current} "
          "after this reconciliation started. The captured merge was not "
          f"rewritten; run `assent verify {managed.folder}` against the "
          "current target, which stays authoritative.")


def _start(cfg: Config) -> int:
    """Prepare a reconciliation worktree holding the real conflict."""
    folder = cfg.tasks_name
    label = f"reconcile start {folder}"

    plan = Plan.parse(cfg.tasks_dir)
    unfinished = [f"{task.id}={task.status}" for task in plan.tasks
                  if task.status not in _COMPLETE_STATUSES]
    if unfinished:
        print(f"{label}: refused, the folder is not finished "
              f"({', '.join(unfinished)}); every task must be DONE or SKIP")
        return 1

    managed = _managed(cfg)
    target_branch = gitops.require_current_branch(managed.main)
    if not gitops.working_tree_status(managed.main, cfg.git_excludes).is_clean:
        print(f"{label}: refused, the main worktree {managed.main} is not clean; "
              "commit or set aside its changes first")
        return 1
    target_tip = gitops.commit_of(managed.main, "HEAD")

    try:
        source_branch, source_tip, _worktree = _require_source(cfg, managed.main)
    except AssentError as e:
        print(f"{label}: refused, {e}")
        return 1

    if gitops.is_ancestor(managed.main, source_tip, target_tip):
        print(f"{label}: nothing to reconcile; source {source_branch} "
              f"({source_tip[:12]}) is already contained in {target_branch}.")
        return 0

    if managed.path.exists() or gitops.branch_exists(managed.main, managed.branch):
        print(f"{label}: refused, the managed reconciliation resources are "
              "already occupied; nothing was deleted or overwritten:")
        if managed.path.exists():
            print(f"  path:   {managed.path}")
        if gitops.branch_exists(managed.main, managed.branch):
            print(f"  branch: {managed.branch} "
                  f"({gitops.branch_tip(managed.main, managed.branch)[:12]})")
        print(f"Run `assent reconcile --continue {folder}` to finish it, or "
              f"`assent reconcile --abort {folder}` to discard it.")
        return 1

    gitops.add_worktree_branch(
        managed.main, managed.branch, managed.path, source_tip)
    try:
        outcome = gitops.merge_no_commit(managed.path, target_tip)
    except AssentError as e:
        raise AssentError(
            f"{e}. The reconciliation worktree {managed.path} was kept; run "
            f"`assent reconcile --abort {folder}` to discard it") from e

    if outcome.ok:
        # An automatic merge is not a reconciliation: undo it and take the
        # managed resources back out, leaving the source exactly as it was.
        if gitops.merge_head(managed.path) is not None:
            gitops.abort_merge(managed.path)
        print(f"{label}: not needed; {source_branch} ({source_tip[:12]}) and "
              f"{target_branch} ({target_tip[:12]}) merge without conflict. "
              "The source was not changed.")
        _remove_managed(managed, source_tip)
        return 0

    print(f"{label}: conflict prepared. Resolve it in this worktree only; the "
          f"main worktree and {source_branch} stay clean and unchanged.")
    print(f"  worktree:      {managed.path}")
    print(f"  branch:        {managed.branch}")
    print(f"  source tip:    {source_tip}")
    print(f"  target tip:    {target_tip} ({target_branch})")
    print("  conflicting file(s):")
    for path in outcome.conflicts:
        print(f"    - {path}")
    print(f"Edit only these files, then run `assent reconcile --continue "
          f"{folder}`; `assent reconcile --abort {folder}` discards the attempt.")
    return 0


def _continue_without_worktree(cfg: Config, managed: _Managed,
                               source_branch: str, source_tip: str,
                               source_worktree: Path) -> int:
    """Finish a reconciliation whose worktree is already gone.

    The only recoverable shape is a leftover managed branch: either its merge is
    already in the source (cleanup was all that remained), or the source still
    sits on that merge's first parent and the fast-forward is what was missed.
    """
    label = f"reconcile continue {managed.folder}"
    if not gitops.branch_exists(managed.main, managed.branch):
        print(f"{label}: refused, no reconciliation is in progress "
              f"(no {managed.path} and no {managed.branch}). Run "
              f"`assent reconcile {managed.folder}` to start one.")
        return 1

    tip = gitops.branch_tip(managed.main, managed.branch)
    if gitops.is_ancestor(managed.main, tip, source_tip):
        print(f"{label}: only cleanup remained; the reconciliation merge is "
              f"already contained in {source_branch}.")
        _remove_managed(managed, tip)
        return 0

    parents = gitops.commit_parents(managed.main, tip)
    if (len(parents) == 2
            and gitops.commit_message(managed.main, tip).strip()
            == reconcile_commit_message(managed.folder).strip()):
        return _fast_forward_source(
            cfg, managed, tip, parents[0], source_branch, source_tip,
            source_worktree)

    print(f"{label}: refused, {managed.branch} ({tip[:12]}) is not a "
          "recognizable reconciliation merge and its worktree is gone; nothing "
          "was deleted. Inspect it by hand.")
    return 1


def _continue(cfg: Config) -> int:
    """Turn the human's resolution into the merge commit and advance the source."""
    folder = cfg.tasks_name
    label = f"reconcile continue {folder}"

    managed = _managed(cfg)
    target_branch = gitops.require_current_branch(managed.main)
    target_tip = gitops.commit_of(managed.main, "HEAD")

    try:
        source_branch, source_tip, source_worktree = _require_source(
            cfg, managed.main)
    except AssentError as e:
        print(f"{label}: refused, {e}. Nothing was deleted; every edit in "
              f"{managed.path} was preserved.")
        return 1

    if not managed.path.exists():
        return _continue_without_worktree(
            cfg, managed, source_branch, source_tip, source_worktree)

    if not gitops.is_repo_worktree(managed.main, managed.path):
        print(f"{label}: refused, {managed.path} exists but is not a worktree "
              "of this repository; nothing was deleted.")
        return 1
    attached = gitops.current_branch(managed.path)
    if attached != managed.branch:
        print(f"{label}: refused, {managed.path} is on "
              f"{attached or 'a detached HEAD'}, not the managed branch "
              f"{managed.branch}; nothing was deleted.")
        return 1

    pending = gitops.merge_head(managed.path)
    head = gitops.commit_of(managed.path, "HEAD")

    if pending is not None:
        if source_tip != head:
            print(f"{label}: refused, source branch {source_branch} moved "
                  f"independently to {source_tip} while the reconciliation "
                  f"merges {head}. The reconciliation worktree {managed.path} "
                  "and every edit were preserved.")
            return 1
        problem = _stage_resolution(managed.path)
        if problem is not None:
            print(f"{label}: refused, {problem}")
            print(f"Nothing was committed and nothing was deleted; fix it in "
                  f"{managed.path} and run continue again.")
            return 1
        merge_commit = gitops.commit_merge(
            managed.path, reconcile_commit_message(folder))
        parents = gitops.commit_parents(managed.path, merge_commit)
        if parents != (head, pending):
            raise AssentError(
                f"the reconciliation merge {merge_commit} has parents "
                f"{', '.join(parents) or 'none'}, not the expected "
                f"{head} and {pending}")
        source_parent, captured_target = head, pending
        print(f"{label}: merge commit created {merge_commit}")
    else:
        parents = gitops.commit_parents(managed.path, head)
        if (len(parents) != 2
                or gitops.commit_message(managed.path, "HEAD").strip()
                != reconcile_commit_message(folder).strip()):
            print(f"{label}: refused, no merge is in progress in "
                  f"{managed.path} and its HEAD ({head[:12]}) is not a "
                  f"reconciliation merge. Run `assent reconcile --abort "
                  f"{folder}` and start again.")
            return 1
        if not gitops.working_tree_status(managed.path).is_clean:
            print(f"{label}: refused, the reconciliation merge was already "
                  f"committed but {managed.path} has further uncommitted "
                  "changes; nothing was deleted.")
            return 1
        merge_commit = head
        source_parent, captured_target = parents
        print(f"{label}: reusing the merge commit an earlier interrupted run "
              f"already created ({merge_commit}); no duplicate was made.")

    result = _fast_forward_source(
        cfg, managed, merge_commit, source_parent, source_branch, source_tip,
        source_worktree)
    if result == 0:
        print(f"{label}: done. The resolved source is {source_branch} "
              f"({merge_commit}); the integration target was not touched.")
        _report_target_drift(managed, captured_target, target_tip, target_branch)
        print(f"Run `assent verify {folder}` to prove the resolved source "
              "against the current target.")
    return result


def _abort(cfg: Config) -> int:
    """Discard only the managed merge, worktree, and temporary branch."""
    folder = cfg.tasks_name
    label = f"reconcile abort {folder}"
    managed = _managed(cfg)

    if not managed.path.exists() and not gitops.branch_exists(
            managed.main, managed.branch):
        print(f"{label}: nothing to abort; neither {managed.path} nor "
              f"{managed.branch} exists.")
        return 0

    if managed.path.exists():
        if not gitops.is_repo_worktree(managed.main, managed.path):
            print(f"{label}: refused, {managed.path} exists but is not a "
                  "worktree of this repository; nothing was deleted.")
            return 1
        attached = gitops.current_branch(managed.path)
        if attached != managed.branch:
            print(f"{label}: refused, {managed.path} is on "
                  f"{attached or 'a detached HEAD'}, not the managed branch "
                  f"{managed.branch}; nothing was deleted.")
            return 1
        if gitops.merge_head(managed.path) is not None:
            gitops.abort_merge(managed.path)
        status = gitops.working_tree_status(managed.path)
        if not status.is_clean:
            leftover = sorted(
                set(status.staged) | set(status.unstaged) | set(status.untracked))
            print(f"{label}: refused, {managed.path} still holds uncommitted "
                  f"content ({', '.join(leftover)}); it was retained in full "
                  "rather than force-removed.")
            return 1
        head = gitops.commit_of(managed.path, "HEAD")
        gitops.remove_worktree(managed.main, managed.path)
        _remove_empty_container(managed.path)
        print(f"{label}: reconciliation worktree removed: {managed.path} "
              f"(was at {head})")

    if gitops.branch_exists(managed.main, managed.branch):
        tip = gitops.branch_tip(managed.main, managed.branch)
        # Printed before the delete: the hash is what makes the discarded merge
        # recoverable, and abort never rewrites the source or the target, so a
        # merge already fast-forwarded into the source survives this untouched.
        gitops.delete_branch_force(managed.main, managed.branch)
        print(f"{label}: temporary branch removed: {managed.branch} "
              f"(was {tip}; recoverable by that hash)")

    print(f"{label}: done. The source and the integration target were not "
          "changed.")
    return 0


def _run(cfg: Config, operation: str, body) -> int:
    """Hold the integration lock then the folder lock for one invocation.

    The lock order matches accept's and must stay fixed.  Both locks are released
    when the call returns, so a human edits the conflict with no assent lock held.
    """
    folder = cfg.tasks_name
    try:
        with hold_integration_lock(cfg.assent_dir):
            with hold_lock(cfg.tasks_dir, folder):
                return body(cfg)
    except LockBusy as e:
        print(f"reconcile {operation} {folder}: refused ({e})")
        return 1
    except AssentError as e:
        print(f"reconcile {operation} {folder}: failed ({e})")
        return 1


def reconcile_start(cfg: Config) -> int:
    """Start reconciling ``cfg.tasks_name`` against the current integration target."""
    return _run(cfg, "start", _start)


def reconcile_continue(cfg: Config) -> int:
    """Turn a resolved reconciliation into the source's merge commit."""
    return _run(cfg, "continue", _continue)


def reconcile_abort(cfg: Config) -> int:
    """Discard a reconciliation attempt without touching the source or target."""
    return _run(cfg, "abort", _abort)
