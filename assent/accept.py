"""Fast transactional publication of one independently verified work folder.

``assent accept FOLDER`` never runs a verifier.  It reconstructs the current
integration candidate and publishes it only when its exact tree matches a
PASSED verification receipt for the current source and verifier.
"""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

from assent import AssentError, gitops, verification
from assent.config import Config
from assent.folderdeps import infer_folder_completion, parse_folder_dependencies
from assent.lockfile import LockBusy, hold_integration_lock, hold_lock
from assent.plan import Plan

_COMPLETE_STATUSES = ("DONE", "SKIP")


def _source_snapshot(main: Path, folder: str,
                     excludes: Sequence[str], *,
                     operation: str = "accept") -> tuple[str, str, Path | None]:
    """Resolve the only current source branch and require a clean attachment."""
    worktree = gitops.folder_worktree(main, folder)
    if worktree is not None:
        branch = gitops.current_branch(worktree)
        if not branch:
            raise AssentError(
                f"source worktree {worktree} is in detached HEAD state, so there "
                f"is no current source branch for {operation}")
        if not branch.startswith(f"{folder}/") or branch == f"{folder}/":
            raise AssentError(
                f"source worktree {worktree} is on branch {branch}, which is not "
                f"a {folder}/* branch")
        if not gitops.working_tree_status(worktree, excludes).is_clean:
            raise AssentError(f"source worktree {worktree} is not clean")
    else:
        branches = gitops.folder_branches(main, folder)
        if len(branches) > 1:
            if operation != "accept":
                raise AssentError(
                    f"current source is ambiguous ({', '.join(branches)})")
            raise AssentError(
                "multiple candidate source branches exist "
                f"({', '.join(branches)}); accept does not guess which one is current")
        if not branches:
            if operation != "accept":
                raise AssentError(
                    f"no source worktree or {folder}/* branch exists")
            raise AssentError(
                f"no source worktree or {folder}/* branch exists; accept does not "
                "infer authorization from old commit messages")
        branch = branches[0]
    return branch, gitops.branch_tip(main, branch), worktree


def _dependency_tip(main: Path, folder: str,
                    excludes: Sequence[str] = ()) -> tuple[str | None, str | None]:
    """Resolve a direct prerequisite's current tip without parsing its task plan."""
    worktree = gitops.folder_worktree(main, folder)
    if worktree is not None:
        branch = gitops.current_branch(worktree)
        if not branch:
            return None, f"its source worktree {worktree} is detached"
        if not branch.startswith(f"{folder}/") or branch == f"{folder}/":
            return None, f"its source worktree is on non-{folder} branch {branch}"
        if not gitops.working_tree_status(worktree, excludes).is_clean:
            return None, "its source worktree is dirty"
        tip = gitops.branch_tip(main, branch)
        if (gitops.commit_of(worktree, "HEAD") != tip
                or gitops.branch_tip(main, branch) != tip):
            return None, "its source changed while its tip was being resolved"
        return tip, None
    branches = gitops.folder_branches(main, folder)
    if not branches:
        return None, "its source was cleaned before the dependent was accepted"
    if len(branches) > 1:
        return None, f"its current source is ambiguous ({', '.join(branches)})"
    return gitops.branch_tip(main, branches[0]), None


def _refresh_message(folder: str, reason: str) -> str:
    return (f"accept {folder}: refused, {reason}. Run `assent verify {folder}` "
            "to refresh the verification receipt unattended; accept never runs "
            "the full verifier")


def _cleanup_warning(path: Path, branch: str, error: AssentError) -> None:
    print(f"warning: {error}")
    print("warning: acceptance cleanup may require manual recovery:")
    print(f"  temporary ref:  refs/heads/{branch}")
    print(f"  temporary path: {path}")


def accept_folder(cfg: Config) -> int:
    """Accept exactly ``cfg.tasks_name``; return zero only on proven success."""
    folder = cfg.tasks_name
    try:
        # This order is part of the concurrency contract and must remain fixed.
        with hold_integration_lock(cfg.assent_dir):
            with hold_lock(cfg.tasks_dir, folder):
                return _accept_locked(cfg)
    except LockBusy as e:
        print(f"accept {folder}: refused ({e})")
        return 1
    except AssentError as e:
        print(f"accept {folder}: failed ({e})")
        return 1


def _accept_locked(cfg: Config) -> int:
    """Rebuild and publish an exact receipt-backed integration candidate."""
    folder = cfg.tasks_name

    plan = Plan.parse(cfg.tasks_dir)
    unfinished = [f"{task.id}={task.status}" for task in plan.tasks
                  if task.status not in _COMPLETE_STATUSES]
    if unfinished:
        print(f"accept {folder}: refused, the folder is not finished "
              f"({', '.join(unfinished)}); every task must be DONE or SKIP")
        return 1
    dependencies = parse_folder_dependencies(cfg.tasks_dir)

    main = gitops.main_worktree(cfg.root)
    target_branch = gitops.require_current_branch(main)
    if not gitops.working_tree_status(main, cfg.git_excludes).is_clean:
        print(f"accept {folder}: refused, the main worktree {main} is not clean; "
              "commit or set aside its changes before accepting")
        return 1
    target_before = gitops.commit_of(main, "HEAD")

    pending: list[str] = []
    dependency_tips: list[tuple[str, str]] = []
    for dependency in dependencies.after:
        completion = infer_folder_completion(cfg.assent_dir / dependency)
        if not completion.complete:
            pending.append(f"{dependency} ({completion.reason})")
            continue
        tip, problem = _dependency_tip(main, dependency, cfg.git_excludes)
        if problem is not None:
            pending.append(f"{dependency} ({problem})")
        elif not gitops.is_ancestor(main, tip, target_before):
            pending.append(f"{dependency} (current tip {tip[:12]} is not in target)")
        else:
            dependency_tips.append((dependency, tip))
    if pending:
        print(f"accept {folder}: refused, prerequisite folder(s) cannot be proven "
              f"accepted into {target_branch}: {', '.join(pending)}. Do not clean "
              "an upstream source before its dependents are accepted")
        return 1

    try:
        source_branch, source_tip, source_worktree = _source_snapshot(
            main, folder, cfg.git_excludes)
    except AssentError as e:
        print(f"accept {folder}: refused, {e}")
        return 1

    stale_dependencies = [
        f"{dependency} ({tip[:12]})"
        for dependency, tip in dependency_tips
        if not gitops.is_ancestor(main, tip, source_tip)
    ]
    if stale_dependencies:
        print(f"accept {folder}: refused, the downstream source does not contain "
              f"the current accepted prerequisite tip(s): "
              f"{', '.join(stale_dependencies)}. The target, source, and receipt "
              f"were preserved; run `assent rework {folder}` or replan the "
              "dependency")
        return 1

    try:
        receipt = verification.read_receipt(
            verification.receipt_path(cfg), main)
    except AssentError as e:
        print(_refresh_message(folder, str(e)))
        return 1
    if receipt.status != "PASSED":
        print(_refresh_message(
            folder, f"verification receipt status is {receipt.status}"))
        return 1
    if receipt.source_tip != source_tip:
        print(_refresh_message(
            folder, "source tip changed since verification "
            f"({receipt.source_tip} -> {source_tip})"))
        return 1
    try:
        current_digest = verification.verifier_digest(cfg)
    except AssentError as e:
        print(_refresh_message(folder, str(e)))
        return 1
    if receipt.verify_script_sha256 != current_digest:
        print(_refresh_message(folder, "the verification script changed"))
        return 1

    already_merged = gitops.is_ancestor(main, source_tip, target_before)
    subject = f"accept({folder}): integrate into {target_branch}"
    message = gitops.accept_commit_message(
        subject, folder, source_branch, source_tip,
        receipt.integration_tree, receipt.verify_script_sha256)

    integration: Path | None = None
    integration_branch: str | None = None
    integration_commit: str | None = None
    conflict_paths: tuple[str, ...] = ()
    gate_problem: str | None = None
    body_completed = False
    published = False
    try:
        with gitops.temporary_integration_worktree(
                main, folder, target_before) as (candidate, candidate_branch):
            integration = candidate
            integration_branch = candidate_branch
            outcome = gitops.merge_no_ff(candidate, source_tip, message)
            if not outcome.ok:
                conflict_paths = outcome.conflicts
            else:
                integration_commit = gitops.commit_of(main, candidate_branch)
                candidate_tree = gitops.tree_of(candidate, "HEAD")
                if candidate_tree != receipt.integration_tree:
                    gate_problem = (
                        "reconstructed candidate tree differs from the PASSED receipt "
                        f"({candidate_tree} != {receipt.integration_tree})")
                elif not already_merged:
                    parents = gitops.commit_parents(candidate, "HEAD")
                    if parents != (target_before, source_tip):
                        gate_problem = (
                            "temporary integration did not produce the expected "
                            "two-parent no-ff merge")
                    elif (gitops.current_branch(main) != target_branch):
                        gate_problem = (
                            f"the main worktree {main} is no longer on {target_branch} "
                            "(concurrent branch switch during accept)")
                    elif gitops.commit_of(main, "HEAD") != target_before:
                        gate_problem = (
                            f"{target_branch} moved during accept (concurrent update); "
                            "refusing to overwrite it")
                    elif not gitops.working_tree_status(
                            main, cfg.git_excludes).is_clean:
                        gate_problem = (
                            f"the main worktree {main} became dirty during accept")
                    elif (gitops.branch_tip(main, source_branch) != source_tip
                          or (source_worktree is not None and (
                              gitops.current_branch(source_worktree) != source_branch
                              or not gitops.working_tree_status(
                                  source_worktree, cfg.git_excludes).is_clean))):
                        gate_problem = "the source changed during acceptance"
                    else:
                        for dependency, dependency_tip in dependency_tips:
                            current_tip, problem = _dependency_tip(
                                main, dependency, cfg.git_excludes)
                            if problem is not None or current_tip != dependency_tip:
                                detail = problem or (
                                    f"tip changed from {dependency_tip} to "
                                    f"{current_tip}")
                                gate_problem = (
                                    f"prerequisite {dependency} changed during "
                                    f"acceptance ({detail})")
                                break
                    if gate_problem is None:
                        try:
                            final_digest = verification.verifier_digest(cfg)
                        except AssentError as e:
                            gate_problem = str(e)
                        if gate_problem is None and final_digest != current_digest:
                            gate_problem = (
                                "the verification script changed during acceptance")
                        if gate_problem is None:
                            gitops.fast_forward(main, integration_commit)
                            published = True
            body_completed = True
    except AssentError as cleanup_error:
        if not body_completed:
            notes = getattr(cleanup_error, "__notes__", ())
            if notes and integration is not None and integration_branch is not None:
                _cleanup_warning(
                    integration, integration_branch,
                    AssentError("; ".join(str(note) for note in notes)))
            raise
        assert integration is not None and integration_branch is not None
        _cleanup_warning(integration, integration_branch, cleanup_error)
        if not published and gate_problem is None and not conflict_paths:
            return 1

    if conflict_paths:
        print(f"accept {folder}: refused, merging {source_branch} into "
              f"{target_branch} conflicts. accept never auto-resolves; "
              "accept did not change the target. Conflicting file(s):")
        for path in conflict_paths:
            print(f"  - {path}")
        print(f"Run `assent verify {folder}` to refresh the FAILED receipt after "
              "resolving the source/target conflict")
        return 1
    if gate_problem is not None:
        print(_refresh_message(folder, gate_problem))
        print("accept did not publish; target refs/worktree remain as currently "
              "observed, and the source branch/worktree was kept")
        return 1
    if already_merged:
        print(f"accept {folder}: already accepted; current source {source_branch} "
              f"({source_tip[:12]}) is contained in {target_branch}, and the "
              "reconstructed tree exactly matches the PASSED receipt. Nothing to do.")
        return 0

    target_after = gitops.commit_of(main, "HEAD")
    print(f"accept {folder}: done, PASSED receipt tree published without running verification.")
    print(f"  folder:          {folder}")
    print(f"  source branch:   {source_branch}")
    print(f"  source tip:      {source_tip}")
    print(f"  verified tree:   {receipt.integration_tree}")
    print(f"  verifier digest: {receipt.verify_script_sha256}")
    print(f"  target branch:   {target_branch}")
    print(f"  target before:   {target_before}")
    print(f"  target after:    {target_after}")
    print(f"  audit merge:     {integration_commit}")
    print("  source branch/worktree kept; retain it while a dependent may still need "
          f"its source evidence. `assent clean {folder}` makes the final safety decision.")
    print("  The integration lock coordinates Assent commands only; do not run "
          "concurrent external Git writes during acceptance.")
    return 0
