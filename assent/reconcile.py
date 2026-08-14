"""Human-driven reconciliation of one finished folder's conflict with the target.

``reconcile`` prepares, preserves, continues, and aborts a single direct
source-versus-target merge conflict so a human can resolve it in a dedicated
worktree.  It is deliberately not an integration engine: it handles exactly one
folder against the current integration target, never combines speculative peer
folders, never runs a verifier, a focused test, or an AI adapter, never edits a
task status, and never merges anything into the integration target.  Once the
source really has advanced it deletes the derived receipts that were written
against the old source identity; proving the new source is a later, explicitly
human-started ``assent verify``, and approving it is a later ``assent accept``.

There is no state file and no "current folder" pointer.  Everything a later run
needs is a deterministic managed fact or a Git fact:

- worktree ``<project>.reconcile/<folder>``, a sibling of the main worktree,
- temporary branch ``<RECONCILE_BRANCH_PREFIX><folder>``,
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

from assent import AssentError, gitops, shared_paths, verification
from assent.config import Config
from assent.folder_source import COMPLETE_STATUSES, resolve_source_snapshot
from assent.lockfile import LockBusy, hold_integration_lock, hold_lock
from assent.plan import Plan

# gitops owns both temporary branch namespaces; this is the same object, kept
# under reconcile's own public name for the callers that read it here.
RECONCILE_BRANCH_PREFIX = gitops.RECONCILE_BRANCH_PREFIX
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


@dataclass(frozen=True)
class AutomaticReconcile:
    """Git-proven state handed to one scheduler-owned conflict editor."""

    worktree: Path | None
    source_branch: str
    source_tip: str
    target_tip: str
    conflict_paths: tuple[str, ...]
    needs_editing: bool


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

    ``resolve_source_snapshot`` is the same identity check verification and
    acceptance perform (sole ``<folder>/*`` branch, attached, clean).
    Reconciliation needs one fact more: the fast-forward at the end runs *in* the
    source worktree, so a branch without one cannot be advanced without rewriting
    a ref by hand.
    """
    branch, tip, worktree = resolve_source_snapshot(
        main, cfg.tasks_name, cfg.git_excludes, operation="reconcile")
    if worktree is None:
        raise AssentError(
            f"task folder {cfg.tasks_name} has no fixed source worktree; "
            "reconciliation fast-forwards the source branch inside its own "
            "worktree and never rewrites a branch ref by hand")
    return branch, tip, worktree


def _shared_contract(managed: _Managed, worktree: Path,
                     label: str, *,
                     manifest: shared_paths.Manifest | None = None
                     ) -> shared_paths.Contract | None:
    """Classify the finished source snapshot, or refuse before any managed resource.

    The reconciliation worktree is another consumer of the reviewed manifest, so
    the answer has to exist before ``git worktree add`` or the merge starts:
    UNKNOWN, STALE, a malformed or ambiguous manifest, an invalid profile, and
    source-link disagreement all stop here, with the actionable review or link
    remedy. Reconciliation never invokes AI and never guesses a profile of its
    own.
    """
    try:
        contract = shared_paths.classify(
            managed.main, worktree, manifest=manifest)
        if contract.settled:
            shared_paths.require_directory_link_agreement(
                managed.main, worktree, contract, folder=managed.folder)
    except AssentError as e:
        print(f"{label}: refused, {e}. Nothing was created.")
        return None
    if not contract.settled:
        print(f"{label}: refused, {shared_paths.closeout_refusal(contract)}. "
              "Nothing was created.")
        return None
    return contract


def _release_shared_paths(managed: _Managed) -> None:
    """Detach the reconciliation worktree's assent-created links and forget them."""
    detached = shared_paths.release(managed.main, managed.path)
    for relative in detached:
        print(f"  shared path detached: {relative}")


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
        # Ownership is proven above; only now are the link objects assent itself
        # recorded detached, because Git must never be handed a tree that still
        # contains a directory link.
        _release_shared_paths(managed)
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

    if not managed.path.exists():
        # A worktree that is gone cannot hold a link, so discarding its stale
        # application record costs one existence check and no traversal.
        _release_shared_paths(managed)


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


def _batch_source_is_current(main: Path,
                             source: verification.BatchSource) -> bool:
    """True while the batch receipt's recorded identity for one folder holds."""
    try:
        branch = gitops.unique_folder_branch(main, source.folder)
    except AssentError:
        return False  # an ambiguous branch set is no longer a proven identity
    return (branch is not None
            and gitops.branch_tip(main, branch) == source.source_tip)


def _invalidate_derived_receipts(cfg: Config, main: Path) -> None:
    """Delete the evidence the just-advanced source has made obsolete.

    A receipt is a derived artifact, so deleting it costs one ``assent verify``
    and never destroys a source of truth -- while keeping it would let an
    ``accept`` publish a tree that was proven before the conflict resolution
    existed.  The batch receipt is all-or-nothing by construction, so any one
    recorded source identity that is no longer current expires the whole file.
    """
    if verification.invalidate_folder_receipt(cfg):
        print("  stale verification receipt deleted: "
              f"{verification.receipt_path(cfg)}")

    batch_path = verification.batch_receipt_path(cfg.assent_dir)
    if not batch_path.exists():
        return
    try:
        receipt = verification.read_batch_receipt(batch_path, main)
    except AssentError as e:
        # A malformed receipt is evidence of an unsafe state, not permission to
        # erase it; every consumer already refuses it fail-closed.
        print(f"  note: the batch verification receipt {batch_path} cannot be "
              f"read ({e}); it was left in place for inspection")
        return
    drifted = [source.folder for source in receipt.sources
               if not _batch_source_is_current(main, source)]
    if drifted:
        verification.invalidate_batch_receipt(cfg.assent_dir)
        print(f"  stale batch verification receipt deleted: {batch_path} "
              f"(recorded source identity for {', '.join(drifted)} is no "
              "longer current)")


def _finish(cfg: Config, managed: _Managed, source_branch: str,
            merge_commit: str) -> None:
    """Report the resolved source, drop stale receipts, and stop there."""
    folder = managed.folder
    print(f"reconcile continue {folder}: done. The resolved source is "
          f"{source_branch} ({merge_commit}); the integration target was not "
          "touched.")
    _invalidate_derived_receipts(cfg, managed.main)
    print(f"reconcile continue {folder}: no verification has run -- neither "
          "the focused task tests nor the complete verification were executed "
          "here.")
    print(f"Run `assent verify {folder}` when you want the complete "
          "verification of the resolved source against the current target (the "
          f"expensive step), and `assent accept {folder}` afterwards as the "
          "explicit approval that integrates it.")


def _is_expected_automatic_merge(managed: _Managed, commit: str,
                                 source_tip: str, target_tip: str) -> bool:
    """Recognize only this folder's exact source-first reconcile merge."""
    try:
        return (gitops.commit_parents(managed.main, commit)
                == (source_tip, target_tip)
                and gitops.commit_message(managed.main, commit).strip()
                == reconcile_commit_message(managed.folder).strip())
    except AssentError:
        return False


def _automatic_reconcile_context(
        cfg: Config, expected_target: str, expected_source: str,
        expected_paths: tuple[str, ...], *, may_prepare: bool
        ) -> AutomaticReconcile:
    """Reconstruct one automatic reconciliation from Git facts only."""
    managed = _managed(cfg)
    target_branch = gitops.require_current_branch(managed.main)
    current_target = gitops.commit_of(managed.main, target_branch)
    if current_target != expected_target:
        raise AssentError(
            f"automatic reconcile target drifted from {expected_target} to "
            f"{current_target}")
    source_branch, current_source, source_worktree = _require_source(
        cfg, managed.main)
    expected = tuple(sorted(dict.fromkeys(expected_paths)))
    if not expected:
        raise AssentError("automatic reconcile has no exact conflict paths")

    resources_exist = (managed.path.exists()
                       or gitops.branch_exists(managed.main, managed.branch))
    source_completed = _is_expected_automatic_merge(
        managed, current_source, expected_source, expected_target)
    if current_source not in {expected_source} and not source_completed:
        raise AssentError(
            f"automatic reconcile source drifted from {expected_source} to "
            f"{current_source}")

    if not resources_exist:
        if source_completed:
            return AutomaticReconcile(
                None, source_branch, current_source, expected_target,
                expected, False)
        if not may_prepare:
            raise AssentError(
                "automatic reconciliation resources disappeared before "
                "the source transition was proven")
        if _start(cfg) != 0:
            raise AssentError("automatic reconcile preparation was refused")
        return _automatic_reconcile_context(
            cfg, expected_target, expected_source, expected,
            may_prepare=False)

    if not managed.path.exists():
        if not gitops.branch_exists(managed.main, managed.branch):
            raise AssentError("automatic reconcile resources are incomplete")
        branch_tip = gitops.branch_tip(managed.main, managed.branch)
        if not _is_expected_automatic_merge(
                managed, branch_tip, expected_source, expected_target):
            raise AssentError(
                "automatic reconcile branch without a worktree is not the "
                "expected merge")
        return AutomaticReconcile(
            None, source_branch, current_source, expected_target,
            expected, False)

    if (not gitops.is_repo_worktree(managed.main, managed.path)
            or gitops.current_branch(managed.path) != managed.branch):
        raise AssentError(
            "automatic reconcile worktree ownership cannot be proven")
    problem = shared_paths.application_problem(managed.main, managed.path)
    if problem:
        raise AssentError(
            f"automatic reconcile shared-path evidence is invalid: {problem}")
    head = gitops.commit_of(managed.path, "HEAD")
    pending = gitops.merge_head(managed.path)
    if pending is None:
        if not _is_expected_automatic_merge(
                managed, head, expected_source, expected_target):
            raise AssentError(
                "automatic reconcile worktree has neither the expected "
                "in-progress merge nor its completed merge commit")
        return AutomaticReconcile(
            managed.path, source_branch, current_source, expected_target,
            expected, False)
    if (head != expected_source or pending != expected_target
            or current_source != expected_source):
        superseded = (
            may_prepare
            and current_source == expected_source
            and gitops.is_ancestor(managed.main, head, expected_source)
            and gitops.is_ancestor(managed.main, pending, expected_target))
        if superseded:
            if not gitops.merge_scene_is_unedited(
                    managed.path, head, pending):
                raise AssentError(
                    "superseded automatic reconciliation contains edits; "
                    "it was retained instead of being reset")
            gitops.abort_merge(managed.path)
            _remove_managed(managed, head)
            print("  superseded unedited reconciliation removed; rebuilding "
                  "it for the current source and target")
            return _automatic_reconcile_context(
                cfg, expected_target, expected_source, expected,
                may_prepare=True)
        raise AssentError(
            "automatic reconcile source/target/parent identity drifted")
    current_conflicts = tuple(sorted(gitops.conflict_paths(managed.path)))
    if current_conflicts != expected:
        raise AssentError(
            "automatic reconcile conflict paths changed (expected "
            + ", ".join(expected) + "; found "
            + (", ".join(current_conflicts) or "none") + ")")
    status = gitops.working_tree_status(managed.path)
    touched = set(status.unstaged) | set(status.untracked)
    outside = sorted(touched - set(expected))
    if outside:
        raise AssentError(
            "automatic reconcile contains edits outside the exact conflict "
            "scene: " + ", ".join(outside))
    if not gitops.merge_index_matches_generated(
            managed.path, expected_source, expected_target):
        raise AssentError(
            "automatic reconcile index changed outside the exact conflict scene")
    return AutomaticReconcile(
        managed.path, source_branch, current_source, expected_target,
        expected, True)


def automatic_reconcile_prepare_locked(
        cfg: Config, expected_target: str, expected_source: str,
        expected_paths: tuple[str, ...]) -> AutomaticReconcile:
    """Prepare or resume the exact reconcile merge while caller holds locks."""
    return _automatic_reconcile_context(
        cfg, expected_target, expected_source, expected_paths,
        may_prepare=True)


def automatic_reconcile_continue_locked(
        cfg: Config, expected_target: str, expected_source: str,
        expected_paths: tuple[str, ...]) -> str:
    """Continue one AI-edited merge through the ordinary reconcile lifecycle."""
    context = _automatic_reconcile_context(
        cfg, expected_target, expected_source, expected_paths,
        may_prepare=False)
    if context.worktree is not None or gitops.branch_exists(
            _managed(cfg).main, _managed(cfg).branch):
        if _continue(cfg) != 0:
            raise AssentError("automatic reconcile continue was refused")
    managed = _managed(cfg)
    target_now = gitops.commit_of(
        managed.main, gitops.require_current_branch(managed.main))
    if target_now != expected_target:
        raise AssentError("automatic reconcile changed the integration target")
    _branch, source_now, _worktree = _require_source(cfg, managed.main)
    if not _is_expected_automatic_merge(
            managed, source_now, expected_source, expected_target):
        raise AssentError(
            "automatic reconcile did not produce the exact source-first merge")
    return source_now


def _start(cfg: Config) -> int:
    """Prepare a reconciliation worktree holding the real conflict."""
    folder = cfg.tasks_name
    label = f"reconcile start {folder}"

    plan = Plan.parse(cfg.tasks_dir)
    unfinished = [f"{task.id}={task.status}" for task in plan.tasks
                  if task.status not in COMPLETE_STATUSES]
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
        source_branch, source_tip, worktree = _require_source(cfg, managed.main)
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

    # Classification and provisioning share the manifest lock.  A concurrent
    # review can therefore only run before this snapshot or after the managed
    # worktree has been provisioned; it can never replace the profile between
    # the refusal gate and link creation.
    try:
        with shared_paths.hold_manifest_lock(managed.main):
            manifest = shared_paths.read_manifest(managed.main)
            contract = _shared_contract(
                managed, worktree, label, manifest=manifest)
            if contract is None:
                return 1
            gitops.add_worktree_branch(
                managed.main, managed.branch, managed.path, source_tip)
            # The declared shared inputs exist before the merge does: a
            # resolution is edited and later verified in this worktree, so it
            # must look like a real source worktree. REVIEWED-NONE creates
            # nothing at all.
            created, _detached = shared_paths.reconcile(
                managed.main, managed.path, contract, manifest=manifest)
    except AssentError as e:
        raise AssentError(
            f"{e}. The reconciliation worktree {managed.path} was kept; run "
            f"`assent reconcile --abort {folder}` to discard it") from e
    for relative in created:
        print(f"  shared path provisioned: {relative}")
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
        _finish(cfg, managed, source_branch, tip)
        return 0

    parents = gitops.commit_parents(managed.main, tip)
    if (len(parents) == 2
            and gitops.commit_message(managed.main, tip).strip()
            == reconcile_commit_message(managed.folder).strip()):
        result = _fast_forward_source(
            cfg, managed, tip, parents[0], source_branch, source_tip,
            source_worktree)
        if result == 0:
            _finish(cfg, managed, source_branch, tip)
        return result

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

    try:
        contract = shared_paths.classify(managed.main, source_worktree)
        if contract.settled:
            shared_paths.require_directory_link_agreement(
                managed.main, source_worktree, contract, folder=folder)
    except AssentError as e:
        print(f"{label}: refused, {e}. The reconciliation worktree "
              f"{managed.path} and every edit were preserved.")
        return 1
    if not contract.settled:
        print(f"{label}: refused, {shared_paths.closeout_refusal(contract)}. "
              f"The reconciliation worktree {managed.path} and every edit "
              "were preserved.")
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
    # A resumed reconciliation is revalidated, never silently repaired: the
    # profile it was provisioned from must still exist and every recorded link
    # must still point at the primary worktree's same relative directory.  A
    # mismatch refuses with the conflict and every human edit preserved.
    problem = shared_paths.application_problem(managed.main, managed.path)
    if problem:
        print(f"{label}: refused, {problem}. The reconciliation worktree "
              f"{managed.path} and every edit were preserved; assent does not "
              "repair a shared-path link behind your back.")
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
        _report_target_drift(managed, captured_target, target_tip, target_branch)
        _finish(cfg, managed, source_branch, merge_commit)
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
        _release_shared_paths(managed)
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

    _release_shared_paths(managed)
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
